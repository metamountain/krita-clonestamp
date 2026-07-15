# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
"""Pure logic for the Clone Stamp plugin: capture, feather/opacity blend, write-back.

No Qt-widget code lives here so it can be exercised/reasoned about independently
of the docker and extension UI.
"""

from PyQt5.QtCore import QRect, QRectF, QByteArray, Qt, QPointF
from PyQt5.QtGui import QImage, QPainter, QColor, QRadialGradient

SUPPORTED_COLOR_MODEL = "RGBA"
SUPPORTED_COLOR_DEPTH = "U8"

VERSION = "1.2.0"
GITHUB_URL = "https://github.com/metamountain/krita-clonestamp"

# Krita's default 8-bit RGBA layers store pixels as straight (non-premultiplied)
# BGRA bytes, which matches QImage.Format_ARGB32 byte-for-byte on little-endian.
PIXEL_FORMAT = QImage.Format_ARGB32
BLEND_FORMAT = QImage.Format_ARGB32_Premultiplied

# Skip accumulator for canvases larger than this (pixel count) to avoid
# excessive memory use -- falls back to per-dab direct writes.
MAX_ACCUMULATOR_PIXELS = 50_000_000  # ~7000x7000


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
        self.brush_size = 50
        self.brush_hardness = 0.5
        self.brush_opacity = 100
        self.sample_scope = "current"

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
    return node, state.source_point


# ---------------------------------------------------------------------------
# Accumulator helpers
# ---------------------------------------------------------------------------

def _ensure_accumulator(state, doc_bounds):
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


def _build_soft_circle(size, hardness, opacity_pct):
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
    return img


def _paint_dab_to_accumulator(state, dst_center, size, hardness, opacity_pct):
    if state._acc_image is None:
        return False

    half = size / 2.0
    left = int(dst_center.x() - half)
    top = int(dst_center.y() - half)
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
    if not (state.aligned and state.stroke_offset is not None):
        state.stroke_offset = QPointF(
            state.source_point.x() - doc_point.x(),
            state.source_point.y() - doc_point.y())
    state.last_dab_point = None
    state.clear_accumulator()
    _ensure_accumulator(state, doc.bounds())


def continue_stroke(doc, state, doc_point, min_spacing=2.0):
    if state.stroke_offset is None:
        return None

    if state.last_dab_point is not None:
        dx = doc_point.x() - state.last_dab_point.x()
        dy = doc_point.y() - state.last_dab_point.y()
        if (dx * dx + dy * dy) < (min_spacing * min_spacing):
            return None

    _paint_dab_to_accumulator(state, doc_point, state.brush_size,
                               state.brush_hardness, state.brush_opacity)
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


def finalize_stroke(doc, state):
    if state._acc_image is None or state._acc_bounds is None:
        return None

    dst_node = doc.activeNode()
    if dst_node is None or dst_node.locked():
        state.clear_accumulator()
        return None

    src_node = state.source_node
    if src_node is None:
        state.clear_accumulator()
        return None

    read_fn, src_bounds = _resolve_source(doc, src_node, state.sample_scope)
    if state.sample_scope != "all":
        _ensure_paint_layer(src_node, "read from")
        _ensure_color_space(src_node, "read from")

    doc_bounds = doc.bounds()
    mask_rect = state._acc_bounds.intersected(doc_bounds)
    if mask_rect.isEmpty():
        state.clear_accumulator()
        return None

    # Source position = destination + stroke offset.
    src_x = mask_rect.x() + int(round(state.stroke_offset.x()))
    src_y = mask_rect.y() + int(round(state.stroke_offset.y()))
    src_rect = QRect(src_x, src_y,
                     mask_rect.width(), mask_rect.height()).intersected(src_bounds)
    dst_rect = mask_rect.intersected(dst_node.bounds())

    if src_rect.isEmpty() or dst_rect.isEmpty():
        state.clear_accumulator()
        return None

    final_w = min(src_rect.width(), dst_rect.width())
    final_h = min(src_rect.height(), dst_rect.height())
    if final_w <= 0 or final_h <= 0:
        state.clear_accumulator()
        return None

    src_rect.setWidth(final_w)
    src_rect.setHeight(final_h)
    dst_rect.setWidth(final_w)
    dst_rect.setHeight(final_h)

    # Slice the accumulator at the mask_rect origin, offset to dst_rect.
    mask_sx = dst_rect.x() - state._acc_bounds.x()
    mask_sy = dst_rect.y() - state._acc_bounds.y()

    # Read source.
    src_bytes = read_fn(src_rect.x(), src_rect.y(), final_w, final_h)
    src_image = QImage(src_bytes, final_w, final_h, PIXEL_FORMAT)
    src_image = src_image.convertToFormat(BLEND_FORMAT)

    # Read destination.
    dst_bytes = dst_node.pixelData(dst_rect.x(), dst_rect.y(), final_w, final_h)
    dst_image = QImage(dst_bytes, final_w, final_h, PIXEL_FORMAT)
    dst_image = dst_image.convertToFormat(BLEND_FORMAT)

    # Read mask slice.
    mask_image = state._acc_image.copy(mask_sx, mask_sy, final_w, final_h)

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
    ok = dst_node.setPixelData(result_bytes,
                                dst_rect.x(), dst_rect.y(), final_w, final_h)
    doc.refreshProjection()
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
