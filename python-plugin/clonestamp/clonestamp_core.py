# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
"""Pure logic for the Clone Stamp plugin: capture, feather/opacity blend, write-back.

No Qt-widget code lives here so it can be exercised/reasoned about independently
of the docker and extension UI (see clonestamp_docker.py for that half, and
its own module docstring for the full feature map of how the two fit
together).

Feature map for this file specifically:

- **Coordinate mapping** (`map_widget_to_document`/`map_document_to_widget`):
  screen-pixel <-> document-pixel conversion, accounting for zoom and pan
  (`canvas.preferredCenter()`). `coordinate_mapping_reliable` refuses this
  when the canvas is rotated or mirrored, since the plugin has no way to
  account for that and would otherwise paint in the wrong place silently.

- **Sampling** (`sample_source_point`, `_snapshot_source`): Ctrl+click
  records the source point *and* freezes a copy of the source pixels at
  that instant (`ClonestampState._source_snapshot`). Painting always reads
  from that frozen copy, not the live layer -- otherwise, once a stroke's
  path crosses ground it already painted over earlier in the same session,
  it would clone its own just-painted pixels back onto itself instead of
  the original content, which is not how a real clone stamp tool behaves.

- **Stroke lifecycle** (`begin_stroke`/`continue_stroke`/`end_stroke`/
  `finalize_stroke`): a drag accumulates dabs into an in-memory alpha mask
  (`ClonestampState._acc_image`) rather than touching the document on every
  dab. `finalize_stroke` is the *only* place that calls
  `Node.setPixelData()` -- once, at mouse-release, compositing the whole
  stroke's accumulated mask over the destination in one pass. This is what
  gives the plugin a single undo step per stroke; Krita's scripting API has
  no undo-grouping/macro call that would let multiple smaller writes be
  merged into one after the fact, so accumulate-then-write-once is the only
  way to get that at all. (The docker's `_StrokeOverlay` fakes a live
  on-canvas look during the drag without needing any of this to change --
  see its docstring.)

- **Preview helpers** (`preview_offset`/`preview_patch`/`build_alpha_mask`):
  shared by the docker's brush-cursor ghost-preview and its live drag
  overlay, so both draw from the exact same source-offset and soft-mask
  math the real paint will eventually use, rather than duplicating it.
"""

import os
from PyQt5.QtCore import QRect, QRectF, QByteArray, Qt, QPointF
from PyQt5.QtGui import QImage, QPainter, QColor, QRadialGradient

# Windows-only by design: TEMP (and the backslash separator) only resolve to
# a real location there. On Linux/macOS TEMP is normally unset, the path
# degenerates to a relative "\\clonestamp_debug.txt", and _debug()'s
# try/except turns every log call into a silent no-op -- acceptable, since
# this logging exists for debugging the primary (Windows) install.
_DEBUG_LOG = os.environ.get("TEMP", "") + "\\clonestamp_debug.txt"

# Diagnostic logging is off by default: _debug() writes to a real file on disk,
# and doing that on every 30ms stroke tick makes drags visibly laggy. To turn
# it on, create an empty file named "clonestamp_debug.enable" next to the log
# (i.e. in your TEMP folder) and restart Krita; delete it and restart to turn
# it back off. Checked once at plugin load, not per call.
_DEBUG_ENABLED = os.path.exists(
    os.environ.get("TEMP", "") + "\\clonestamp_debug.enable")


def _debug(msg, force=False):
    if not (force or _DEBUG_ENABLED):
        return
    try:
        with open(_DEBUG_LOG, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass

SUPPORTED_COLOR_MODEL = "RGBA"
SUPPORTED_COLOR_DEPTH = "U8"

VERSION = "1.7.1"
GITHUB_URL = "https://github.com/metamountain/krita-clonestamp"

# Krita's default 8-bit RGBA layers store pixels as straight (non-premultiplied)
# BGRA bytes, which matches QImage.Format_ARGB32 byte-for-byte on little-endian.
PIXEL_FORMAT = QImage.Format_ARGB32
BLEND_FORMAT = QImage.Format_ARGB32_Premultiplied

# Ceiling on the stroke accumulator (pixel count) to bound worst-case memory
# use -- a Format_ARGB32_Premultiplied buffer this size is ~4 bytes/px, so
# this caps it around 800MB. Above this, the stroke is refused outright
# (see begin_stroke) rather than silently doing nothing.
MAX_ACCUMULATOR_PIXELS = 200_000_000  # ~14000x14000

# Same cap applied to the source snapshot taken at sample time (see
# sample_source_point) -- above this, finalize_stroke falls back to reading
# the source live, which re-opens the smear-when-overlapping issue but at
# least keeps working on very large documents.
MAX_SOURCE_SNAPSHOT_PIXELS = MAX_ACCUMULATOR_PIXELS


class ClonestampError(Exception):
    """Raised for any expected/user-facing failure; callers show it and stop."""


class ClonestampState:
    """Shared state for the Ctrl+click sample / drag-to-paint brush flow."""

    def __init__(self):
        self.source_node = None
        self.source_point = None
        self.aligned = True
        self.stroke_offset = None
        self.last_dab_point = None
        self.brush_size = 250
        self.brush_hardness = 0.5
        self.brush_opacity = 100
        self.sample_scope = "current"

        # Frozen copy of the source pixels, taken once when the source point
        # is sampled (see sample_source_point) -- read from this instead of
        # the live layer so that painting over pixels the source point has
        # already passed over doesn't clone already-modified content back
        # onto itself (real Photoshop's Clone Stamp always samples the
        # original, un-painted-over pixels, not a live/current read).
        self._source_snapshot = None
        self._source_snapshot_left = 0
        self._source_snapshot_top = 0

        # In-memory alpha-accumulator for the current stroke.
        # Painted dabs record a soft white circle here instead of hitting
        # the Krita node each time.  At stroke end the accumulated mask is
        # composited onto the destination in a single pass.
        self._acc_image = None
        self._acc_left = 0
        self._acc_top = 0
        self._acc_bounds = None

    @property
    def has_point_source(self):
        return self.source_node is not None and self.source_point is not None

    def clear_accumulator(self):
        self._acc_image = None
        self._acc_bounds = None

    def clear_source(self):
        """Drops the sampled source point/node and its frozen pixel
        snapshot. STATE is a module-level singleton shared across every
        open document in this Krita session -- without calling this when
        the active document changes, a source sampled on one document
        would silently keep being read from on another, which is wrong on
        every level (wrong node reference, wrong pixel content) even if
        Krita's API doesn't raise an error for it. Deliberately *not*
        called from every disarm -- only where the active document is
        known to have actually changed -- so a plain manual toggle-off/
        toggle-on of the brush on the same document doesn't lose the
        user's sample for no reason."""
        self.source_node = None
        self.source_point = None
        self.stroke_offset = None
        self.last_dab_point = None
        self._source_snapshot = None
        self._source_snapshot_left = 0
        self._source_snapshot_top = 0


STATE = ClonestampState()


def _ensure_paint_layer(node, action):
    if node is None:
        raise ClonestampError("No active layer.")
    if node.type() != "paintlayer":
        raise ClonestampError(
            "Clone Stamp only works on paint layers; '{0}' is a {1} and can't be {2}."
            .format(node.name(), node.type(), action))


def _ensure_color_space(node, action):
    if node.colorModel() != SUPPORTED_COLOR_MODEL or node.colorDepth() != SUPPORTED_COLOR_DEPTH:
        raise ClonestampError(
            "Clone Stamp only supports {0}/{1} layers; '{2}' is {3}/{4} and can't be {5}."
            .format(SUPPORTED_COLOR_MODEL, SUPPORTED_COLOR_DEPTH,
                    node.name(), node.colorModel(), node.colorDepth(), action))


# ---------------------------------------------------------------------------
# Coordinate mapping
# ---------------------------------------------------------------------------

def map_widget_to_document(canvas, widget, local_pos):
    """Widget-local position -> document pixel position, assuming no canvas
    rotation/mirroring (see coordinate_mapping_reliable). Derivation: Krita
    keeps canvas.preferredCenter() -- a document-space point -- rendered at
    the center of the canvas widget, and everything scales around that
    center by the zoom factor. So a widget point's offset from the widget
    center, divided by zoom to convert screen pixels back to document
    pixels, is its offset from preferredCenter in document space."""
    zoom = canvas.zoomLevel()
    if not zoom:
        return None
    center_x = widget.width() / 2.0
    center_y = widget.height() / 2.0
    pref = canvas.preferredCenter()
    doc_x = pref.x() + (local_pos.x() - center_x) / zoom
    doc_y = pref.y() + (local_pos.y() - center_y) / zoom
    return QPointF(doc_x, doc_y)


def coordinate_mapping_reliable(canvas):
    return canvas.rotation() == 0 and not canvas.mirror()


def map_document_to_widget(canvas, widget, doc_point):
    """Inverse of map_widget_to_document -- used by the live stroke overlay
    to place each dab's screen-space rect."""
    zoom = canvas.zoomLevel()
    if not zoom:
        return None
    center_x = widget.width() / 2.0
    center_y = widget.height() / 2.0
    pref = canvas.preferredCenter()
    wx = center_x + (doc_point.x() - pref.x()) * zoom
    wy = center_y + (doc_point.y() - pref.y()) * zoom
    return QPointF(wx, wy)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def sample_source_point(doc, state, doc_point):
    if doc is None:
        raise ClonestampError("No active document.")
    node = doc.activeNode()
    _ensure_paint_layer(node, "read from")
    _ensure_color_space(node, "read from")
    if node.locked():
        raise ClonestampError("Layer '{0}' is locked.".format(node.name()))

    state.source_node = node
    state.source_point = QPointF(doc_point)
    state.stroke_offset = None
    state.last_dab_point = None
    _snapshot_source(doc, state, node)
    return node, state.source_point


def _snapshot_source(doc, state, node):
    """Freezes a copy of the source pixels at the moment they're sampled --
    see ClonestampState._source_snapshot for why. Falls back to a live read
    (state._source_snapshot left as None) if the source is too big to
    reasonably hold a whole extra copy of in memory."""
    read_fn, bounds = _resolve_source(doc, node, state.sample_scope)
    w, h = bounds.width(), bounds.height()
    if w <= 0 or h <= 0 or w * h > MAX_SOURCE_SNAPSHOT_PIXELS:
        _debug("_snapshot_source: source %dx%d too large or empty, "
               "falling back to live read" % (w, h))
        state._source_snapshot = None
        return
    snap_bytes = read_fn(bounds.x(), bounds.y(), w, h)
    state._source_snapshot = QImage(snap_bytes, w, h, PIXEL_FORMAT).copy()
    state._source_snapshot_left = bounds.x()
    state._source_snapshot_top = bounds.y()


# ---------------------------------------------------------------------------
# Accumulator helpers
# ---------------------------------------------------------------------------

def _ensure_accumulator(state, doc_bounds):
    """Allocates the stroke accumulator (see ClonestampState._acc_image) if
    one isn't already active; returns False when the document is too large
    to allocate one at all (caller turns that into a user-facing error).

    Deliberately sized to the WHOLE document rather than grown lazily to the
    stroke's dirty bounds: a stroke can wander anywhere, and resizing a
    QImage mid-stroke while preserving painted content is exactly the kind
    of offset-arithmetic this project has already had bugs in (see the
    _acc_left/_acc_bounds distinction in finalize_stroke). Whole-doc sizing
    keeps every dab's accumulator coordinates trivially derivable from its
    document coordinates. The cost is memory (4 bytes/px, capped by
    MAX_ACCUMULATOR_PIXELS at ~800MB); dirty-bounds sizing is the known
    future optimization if that cap ever bites in practice."""
    if state._acc_image is not None:
        return True
    w = doc_bounds.width()
    h = doc_bounds.height()
    if w <= 0 or h <= 0:
        return False
    if w * h > MAX_ACCUMULATOR_PIXELS:
        return False
    state._acc_image = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    state._acc_image.fill(Qt.transparent)
    state._acc_left = doc_bounds.x()
    state._acc_top = doc_bounds.y()
    state._acc_bounds = None
    return True


# Cache for _build_soft_circle: a stroke stamps the same circle for every
# dab (size/hardness/opacity almost never change mid-stroke), and repainting
# the radial gradient dozens of times per drag was pure waste. A small dict
# rather than a single entry because two different circles are alive at once
# during a drag -- the dab circle (document size, brush opacity) and the
# cursor preview's mask (screen diameter, fixed 70) -- and a single slot
# would thrash between them. Cleared when it grows past a handful of entries
# (resize drags churn through sizes); each entry is at most a few MB.
_soft_circle_cache = {}
_SOFT_CIRCLE_CACHE_MAX = 8


def _build_soft_circle(size, hardness, opacity_pct):
    """White circle with the hardness falloff and opacity baked into its
    alpha: solid out to `hardness` (fraction of the radius), then fading to
    transparent at the rim. Returns a cached instance when the parameters
    are unchanged -- callers must treat the result as read-only."""
    key = (size, hardness, opacity_pct)
    cached = _soft_circle_cache.get(key)
    if cached is not None:
        return cached

    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)

    radius = size / 2.0
    alpha_val = max(0, min(255, int(255 * opacity_pct / 100.0)))
    grad = QRadialGradient(size / 2.0, size / 2.0, max(radius, 0.5))
    grad.setColorAt(0.0, QColor(255, 255, 255, alpha_val))
    grad.setColorAt(min(hardness, 0.999), QColor(255, 255, 255, alpha_val))
    grad.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.setBrush(grad)
    painter.drawEllipse(QRectF(0, 0, size, size))
    painter.end()
    if len(_soft_circle_cache) >= _SOFT_CIRCLE_CACHE_MAX:
        _soft_circle_cache.clear()
    _soft_circle_cache[key] = img
    return img


def _paint_dab_to_accumulator(state, dst_center, size, hardness, opacity_pct):
    if state._acc_image is None:
        return False

    half = size / 2.0
    # round(), not int()-truncation, so dab placement matches the C++
    # tool's qRound() and the two implementations stamp identically.
    left = int(round(dst_center.x() - half))
    top = int(round(dst_center.y() - half))
    dab_rect = QRect(left, top, size, size)

    if state._acc_bounds is None:
        state._acc_bounds = QRect(dab_rect)
    else:
        state._acc_bounds = state._acc_bounds.united(dab_rect)

    local_x = left - state._acc_left
    local_y = top - state._acc_top
    acc_w = state._acc_image.width()
    acc_h = state._acc_image.height()

    if local_x + size <= 0 or local_y + size <= 0:
        return True
    if local_x >= acc_w or local_y >= acc_h:
        return True

    clip = QRect(local_x, local_y, size, size).intersected(
        QRect(0, 0, acc_w, acc_h))
    if clip.isEmpty():
        return True

    circle = _build_soft_circle(size, hardness, opacity_pct)
    src_clip = QRect(clip.x() - local_x, clip.y() - local_y,
                     clip.width(), clip.height())

    painter = QPainter(state._acc_image)
    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
    painter.drawImage(clip.topLeft(), circle, src_clip)
    painter.end()
    return True


# ---------------------------------------------------------------------------
# Stroke lifecycle
# ---------------------------------------------------------------------------

def begin_stroke(state, doc, doc_point):
    if not state.has_point_source:
        raise ClonestampError(
            "Ctrl+click on the canvas first to set a clone source.")
    if doc is None:
        raise ClonestampError("No active document.")
    if not (state.aligned and state.stroke_offset is not None):
        state.stroke_offset = QPointF(
            state.source_point.x() - doc_point.x(),
            state.source_point.y() - doc_point.y())
    state.last_dab_point = None
    state.clear_accumulator()
    bounds = doc.bounds()
    _debug("begin_stroke: doc bounds=%s" % (bounds,))
    ok = _ensure_accumulator(state, bounds)
    _debug("begin_stroke: acc=%s bounds=%s left=%d top=%d size=%s" % (
        "OK" if ok else "FAIL",
        state._acc_bounds, state._acc_left, state._acc_top,
        (state._acc_image.width(), state._acc_image.height())
        if state._acc_image else "NONE"))
    if not ok:
        raise ClonestampError(
            "Canvas is {0}x{1} ({2:,} px), too large for Clone Stamp's "
            "stroke buffer (limit {3:,} px).".format(
                bounds.width(), bounds.height(),
                bounds.width() * bounds.height(), MAX_ACCUMULATOR_PIXELS))


def continue_stroke(doc, state, doc_point, min_spacing=None):
    if state.stroke_offset is None:
        return None

    if min_spacing is None:
        # Dab spacing scales with brush size: overlapping soft circles union
        # to a smooth mask, so at 15% of the diameter (well under Photoshop's
        # 25% default brush spacing) no scalloping is visible, while the
        # default 250px brush stamps ~19x fewer dabs than the old fixed
        # 2px spacing did.
        min_spacing = max(2.0, state.brush_size * 0.15)

    if state.last_dab_point is not None:
        dx = doc_point.x() - state.last_dab_point.x()
        dy = doc_point.y() - state.last_dab_point.y()
        if (dx * dx + dy * dy) < (min_spacing * min_spacing):
            return None

    painted = _paint_dab_to_accumulator(state, doc_point, state.brush_size,
                                         state.brush_hardness, state.brush_opacity)
    _debug("continue_stroke: dab@(%d,%d) painted=%s acc_bounds=%s" % (
        int(doc_point.x()), int(doc_point.y()), painted, state._acc_bounds))
    state.last_dab_point = QPointF(doc_point)

    src_center = QPointF(
        doc_point.x() + state.stroke_offset.x(),
        doc_point.y() + state.stroke_offset.y())
    return src_center


def end_stroke(state):
    state.last_dab_point = None


# ---------------------------------------------------------------------------
# Final composite -- one pass using the accumulated alpha mask
# ---------------------------------------------------------------------------

def _resolve_source(doc, src_node, sample_scope):
    if sample_scope == "all":
        root = doc.rootNode()
        return root.projectionPixelData, root.bounds()
    return src_node.pixelData, src_node.bounds()


def preview_offset(state, cursor_doc_pos):
    """Doc-space (dx, dy) QPointF from cursor_doc_pos to wherever the source
    would currently be read from -- None if no source is sampled. Once
    Aligned mode has a fixed per-stroke offset, uses that (matching what
    painting will actually do); otherwise (Non-Aligned, or before the first
    stroke) it's relative to the live cursor, since that's what the next
    click will set as the offset."""
    if not state.has_point_source or cursor_doc_pos is None:
        return None
    if state.aligned and state.stroke_offset is not None:
        return state.stroke_offset
    return QPointF(state.source_point.x() - cursor_doc_pos.x(),
                    state.source_point.y() - cursor_doc_pos.y())


def source_screen_offset(state, cursor_doc_pos, zoom):
    """Returns (dx, dy) screen-pixel offset from cursor to source point,
    or None if no source is sampled."""
    off = preview_offset(state, cursor_doc_pos)
    if off is None:
        return None
    return (off.x() * zoom, off.y() * zoom)


def build_alpha_mask(size, hardness, opacity_pct):
    """Public entry point for the soft round hardness/opacity mask, so the
    docker can reuse the exact same falloff for the on-cursor content
    preview instead of duplicating the gradient math."""
    return _build_soft_circle(size, hardness, opacity_pct)


def preview_patch(state, cursor_doc_pos, doc_size):
    """Returns a doc_size x doc_size QImage of the source content that would
    currently be cloned at cursor_doc_pos (out-of-bounds areas left
    transparent), or None if there's no snapshot to read from (no source
    armed, or the source was too large to snapshot -- see
    _snapshot_source)."""
    if state._source_snapshot is None:
        return None
    off = preview_offset(state, cursor_doc_pos)
    if off is None:
        return None

    half = doc_size / 2.0
    left = int(cursor_doc_pos.x() + off.x() - half)
    top = int(cursor_doc_pos.y() + off.y() - half)

    snap_w = state._source_snapshot.width()
    snap_h = state._source_snapshot.height()
    local_x = left - state._source_snapshot_left
    local_y = top - state._source_snapshot_top
    clip = QRect(local_x, local_y, doc_size, doc_size).intersected(
        QRect(0, 0, snap_w, snap_h))
    if clip.isEmpty():
        return None

    patch = QImage(doc_size, doc_size, QImage.Format_ARGB32_Premultiplied)
    patch.fill(Qt.transparent)
    sub = state._source_snapshot.copy(clip)
    painter = QPainter(patch)
    painter.drawImage(clip.x() - local_x, clip.y() - local_y, sub)
    painter.end()
    return patch


def finalize_stroke(doc, state):
    _debug("finalize_stroke: acc=%s bounds=%s" % (
        "EXISTS" if state._acc_image else "NONE",
        state._acc_bounds))
    if state._acc_image is None or state._acc_bounds is None:
        _debug("finalize_stroke: nothing to paint (no accumulator)")
        return None

    dst_node = doc.activeNode()
    if dst_node is None or dst_node.locked():
        _debug("finalize_stroke: dst_node=%s locked=%s" % (
            dst_node.name() if dst_node else "NONE",
            dst_node.locked() if dst_node else "N/A"))
        state.clear_accumulator()
        return None

    src_node = state.source_node
    if src_node is None:
        _debug("finalize_stroke: src_node is None")
        state.clear_accumulator()
        return None

    read_fn, src_bounds = _resolve_source(doc, src_node, state.sample_scope)
    if state.sample_scope != "all":
        _ensure_paint_layer(src_node, "read from")
        _ensure_color_space(src_node, "read from")

    doc_bounds = doc.bounds()
    _debug("finalize_stroke: doc_bounds=%s src_bounds=%s" % (doc_bounds, src_bounds))
    mask_rect = state._acc_bounds.intersected(doc_bounds)
    _debug("finalize_stroke: mask_rect=%s" % (mask_rect,))
    if mask_rect.isEmpty():
        state.clear_accumulator()
        return None

    # Source position = destination + stroke offset.
    src_x = mask_rect.x() + int(round(state.stroke_offset.x()))
    src_y = mask_rect.y() + int(round(state.stroke_offset.y()))
    src_full = QRect(src_x, src_y, mask_rect.width(), mask_rect.height())
    dst_full = QRect(mask_rect.x(), mask_rect.y(),
                     mask_rect.width(), mask_rect.height())

    src_clip = src_full.intersected(src_bounds)
    # Clip against the canvas, not dst_node.bounds() -- for a paint layer that
    # returns the tight bounding box of its current non-transparent content,
    # which is empty on a fresh/blank layer, so every stroke would abort here
    # before ever painting a single pixel.
    dst_clip = dst_full.intersected(doc_bounds)

    if src_clip.isEmpty() or dst_clip.isEmpty():
        _debug("finalize_stroke: src_clip=%s dst_clip=%s EMPTY -- abort" % (src_clip, dst_clip))
        state.clear_accumulator()
        return None

    # Shrink both rects by whichever side needs it more, so they stay the
    # same size and pixel-aligned even when one side runs off the canvas.
    left = max(src_clip.x() - src_full.x(), dst_clip.x() - dst_full.x())
    top = max(src_clip.y() - src_full.y(), dst_clip.y() - dst_full.y())
    right = max(src_full.right() - src_clip.right(),
                dst_full.right() - dst_clip.right())
    bottom = max(src_full.bottom() - src_clip.bottom(),
                 dst_full.bottom() - dst_clip.bottom())

    final_w = mask_rect.width() - left - right
    final_h = mask_rect.height() - top - bottom
    if final_w <= 0 or final_h <= 0:
        _debug("finalize_stroke: final rect is empty (%dx%d)" % (final_w, final_h))
        state.clear_accumulator()
        return None

    src_rect = QRect(src_full.x() + left, src_full.y() + top, final_w, final_h)
    dst_rect = QRect(dst_full.x() + left, dst_full.y() + top, final_w, final_h)
    _debug("finalize_stroke: src_rect=%s dst_rect=%s" % (src_rect, dst_rect))

    # Slice the accumulator: _acc_left/_acc_top is the accumulator origin
    # (= document origin), not _acc_bounds.
    mask_sx = dst_rect.x() - state._acc_left
    mask_sy = dst_rect.y() - state._acc_top

    # Read source -- from the frozen snapshot taken at sample time when
    # available, so that once the source path crosses ground already painted
    # in this session, it clones the original pixels there, not the
    # already-modified ones. Falls back to a live read if the source was too
    # large to snapshot.
    if state._source_snapshot is not None:
        snap_x = src_rect.x() - state._source_snapshot_left
        snap_y = src_rect.y() - state._source_snapshot_top
        src_image = state._source_snapshot.copy(snap_x, snap_y, final_w, final_h)
    else:
        src_bytes = read_fn(src_rect.x(), src_rect.y(), final_w, final_h)
        src_image = QImage(src_bytes, final_w, final_h, PIXEL_FORMAT)
    src_image = src_image.convertToFormat(BLEND_FORMAT)

    # Read destination.
    dst_bytes = dst_node.pixelData(dst_rect.x(), dst_rect.y(), final_w, final_h)
    dst_image = QImage(dst_bytes, final_w, final_h, PIXEL_FORMAT)
    dst_image = dst_image.convertToFormat(BLEND_FORMAT)

    # Read mask slice.
    mask_image = state._acc_image.copy(mask_sx, mask_sy, final_w, final_h)
    _debug("finalize_stroke: mask slice size=%dx%d null=%s" % (
        mask_image.width(), mask_image.height(), mask_image.isNull()))

    # Step 1: multiply source by mask alpha (DestinationIn).
    painter = QPainter(src_image)
    painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, mask_image)
    painter.end()

    # Step 2: composite masked source over destination (SourceOver).
    painter = QPainter(dst_image)
    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
    painter.drawImage(0, 0, src_image)
    painter.end()

    # Write back.
    result_bytes = _image_bytes(dst_image)
    _debug("finalize_stroke: write %d bytes @ (%d,%d) %dx%d" % (
        len(result_bytes), dst_rect.x(), dst_rect.y(), final_w, final_h))
    ok = dst_node.setPixelData(result_bytes,
                                dst_rect.x(), dst_rect.y(), final_w, final_h)
    doc.refreshProjection()
    _debug("finalize_stroke: setPixelData ok=%s" % ok)
    state.clear_accumulator()
    if not ok:
        raise ClonestampError(
            "Failed to write pixel data to '{0}'.".format(dst_node.name()))
    return dst_rect


def _image_bytes(image):
    image = image.convertToFormat(PIXEL_FORMAT)
    ptr = image.constBits()
    try:
        nbytes = image.sizeInBytes()
    except AttributeError:
        nbytes = image.byteCount()
    ptr.setsize(nbytes)
    return QByteArray(bytes(ptr))
