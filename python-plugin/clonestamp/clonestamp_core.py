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

# Krita's default 8-bit RGBA layers store pixels as straight (non-premultiplied)
# BGRA bytes, which matches QImage.Format_ARGB32 byte-for-byte on little-endian.
PIXEL_FORMAT = QImage.Format_ARGB32
BLEND_FORMAT = QImage.Format_ARGB32_Premultiplied


class ClonestampError(Exception):
    """Raised for any expected/user-facing failure; callers show it and stop."""


class ClonestampState:
    """Shared state for the Ctrl+click sample / drag-to-paint brush flow."""

    def __init__(self):
        self.source_node = None
        self.source_point = None  # QPointF, document coordinates
        self.aligned = True
        self.stroke_offset = None  # QPointF, recomputed per-stroke unless aligned
        self.last_dab_point = None  # QPointF, for drag spacing/throttling
        self.brush_size = 50
        self.brush_hardness = 0.5
        self.brush_opacity = 100
        # 'current' reads source_node's own device (default); 'all' reads
        # the document's merged/composited projection via the root node,
        # regardless of which single layer was Ctrl+clicked to set the point.
        self.sample_scope = "current"

    @property
    def has_point_source(self):
        return self.source_node is not None and self.source_point is not None


# Module-level singleton so the docker's UI and the pure-logic functions
# below always agree on the current brush/source state.
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


def _scale_alpha(image, factor):
    if factor >= 1.0:
        return
    overlay = QImage(image.size(), PIXEL_FORMAT)
    overlay.fill(QColor(0, 0, 0, max(0, min(255, int(255 * factor)))))
    painter = QPainter(image)
    painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, overlay)
    painter.end()


def _image_bytes(image):
    image = image.convertToFormat(PIXEL_FORMAT)
    ptr = image.constBits()
    try:
        nbytes = image.sizeInBytes()
    except AttributeError:
        nbytes = image.byteCount()
    ptr.setsize(nbytes)
    return QByteArray(bytes(ptr))


# ---------------------------------------------------------------------------
# Alt+click-to-sample / drag-to-paint brush flow.
#
# Confirmed working technique (Krita Artists forum "eventFiltter on the
# canvas"): a QApplication-global event filter reliably sees MouseButtonPress/
# Release for the canvas widget; continuous MouseMove during a drag is *not*
# exposed to Python (a separate forum thread confirms Krita's internal
# KisCanvasController.documentMousePositionChanged signal isn't hooked up to
# scripting), so continuous painting is driven by a UI-side QTimer polling
# QCursor.pos() instead of by move events. The functions below are the pure
# logic; the docker owns the QApplication filter, the QTimer, and translating
# QCursor.pos() into calls here.
# ---------------------------------------------------------------------------

def map_widget_to_document(canvas, widget, local_pos):
    """Best-effort widget-local pixel -> document pixel coordinate.

    libkis's Canvas has no widgetToDocument() call, so this is reconstructed
    from zoomLevel()/preferredCenter() and the widget's own size. Only exact
    when the canvas isn't rotated or mirrored (check separately).
    """
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
    """False when canvas rotation/mirroring would break the linear mapping above."""
    return canvas.rotation() == 0 and not canvas.mirror()


def sample_source_point(doc, state, doc_point):
    """Alt+click handler: remember (layer, point) as the clone source."""
    if doc is None:
        raise ClonestampError("No active document.")
    node = doc.activeNode()
    _ensure_paint_layer(node, "read from")
    _ensure_color_space(node, "read from")
    if node.locked():
        raise ClonestampError("Layer '{0}' is locked.".format(node.name()))

    state.source_node = node
    state.source_point = QPointF(doc_point)
    state.stroke_offset = None  # force recompute at the next stroke
    state.last_dab_point = None
    return node, state.source_point


def begin_stroke(state, doc_point):
    """Plain-click/press handler (once a source point exists): fixes the
    source-to-destination offset for this stroke, per the Aligned/Non-Aligned
    semantics GIMP/Photoshop use."""
    if not state.has_point_source:
        raise ClonestampError("Alt+click on the canvas first to set a clone source.")
    if not (state.aligned and state.stroke_offset is not None):
        state.stroke_offset = QPointF(state.source_point.x() - doc_point.x(),
                                       state.source_point.y() - doc_point.y())
    state.last_dab_point = None


def continue_stroke(doc, state, doc_point, min_spacing=2.0):
    """Timer-tick handler while the button is held: stamps a round dab if the
    cursor has moved far enough (in document pixels) since the last one."""
    if state.stroke_offset is None:
        return None

    if state.last_dab_point is not None:
        dx = doc_point.x() - state.last_dab_point.x()
        dy = doc_point.y() - state.last_dab_point.y()
        if (dx * dx + dy * dy) < (min_spacing * min_spacing):
            return None

    dst_node = doc.activeNode()
    src_center = QPointF(doc_point.x() + state.stroke_offset.x(),
                          doc_point.y() + state.stroke_offset.y())
    result = stamp_dab(doc, state.source_node, src_center, dst_node, doc_point,
                        state.brush_size, state.brush_hardness, state.brush_opacity,
                        state.sample_scope)
    state.last_dab_point = QPointF(doc_point)
    return result


def end_stroke(state):
    state.last_dab_point = None


def _build_radial_mask(w, h, hardness):
    """A round mask: opaque out to `hardness` fraction of the radius, fading
    to fully transparent at the edge -- the standard soft-brush falloff."""
    mask = QImage(w, h, PIXEL_FORMAT)
    mask.fill(0)
    painter = QPainter(mask)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)

    radius = min(w, h) / 2.0
    hardness = max(0.0, min(1.0, hardness))
    grad = QRadialGradient(w / 2.0, h / 2.0, max(radius, 0.5))
    grad.setColorAt(0.0, QColor(255, 255, 255, 255))
    grad.setColorAt(min(hardness, 0.999), QColor(255, 255, 255, 255))
    grad.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.setBrush(grad)
    painter.drawEllipse(QRectF(0, 0, w, h))
    painter.end()
    return mask


def _apply_radial_mask(image, hardness):
    mask = _build_radial_mask(image.width(), image.height(), hardness)
    painter = QPainter(image)
    painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, mask)
    painter.end()


def _resolve_source(doc, src_node, sample_scope):
    """Returns (read_fn, bounds) for the configured sample scope. read_fn(x,
    y, w, h) -> QByteArray, matching Node.pixelData's signature.

    'all' reads the document's merged/composited projection via the root
    node's projectionPixelData() -- confirmed in Krita's own libkis source
    (Node.cpp) to read node->projection(), the same merged view the native
    C++ tool's image()->projection() uses; a plain pixelData() call on a
    group/root node reads its paintDevice(), which is null for groups, so
    projectionPixelData() is required here, not pixelData(). 'current'
    reads src_node's own device, unchanged prior behavior."""
    if sample_scope == "all":
        root = doc.rootNode()
        return root.projectionPixelData, root.bounds()
    return src_node.pixelData, src_node.bounds()


def read_preview_patch(doc, src_node, center, size, hardness, sample_scope="current"):
    """Read-only crop+mask (same shape as stamp_dab's source side) for a live
    ghost preview -- does not touch the document, no undo step involved."""
    if doc is None or src_node is None or center is None:
        return None
    read_fn, bounds = _resolve_source(doc, src_node, sample_scope)
    size = max(1, int(size))
    half = size / 2.0
    rect = QRect(int(round(center.x() - half)), int(round(center.y() - half)), size, size)
    clip = rect.intersected(bounds)
    if clip.isEmpty():
        return None
    raw = read_fn(clip.x(), clip.y(), clip.width(), clip.height())
    image = QImage(raw, clip.width(), clip.height(), PIXEL_FORMAT).convertToFormat(BLEND_FORMAT)
    _apply_radial_mask(image, hardness)
    return image


def stamp_dab(doc, src_node, src_center, dst_node, dst_center, size, hardness, opacity, sample_scope="current"):
    """Stamp one round, soft-edged dab: read a `size`x`size` square centered
    at `src_center`, mask+scale it, blend over the destination centered at
    `dst_center`, write back in a single setPixelData call."""
    _ensure_paint_layer(dst_node, "written to")
    if dst_node.locked():
        raise ClonestampError("Layer '{0}' is locked.".format(dst_node.name()))
    _ensure_color_space(dst_node, "written to")

    if sample_scope == "all":
        # The merged projection is always in the document's own base color
        # space, which the destination-layer check above already confirmed
        # is a supported space -- no separate source check needed.
        pass
    else:
        _ensure_paint_layer(src_node, "read from")
        _ensure_color_space(src_node, "read from")
        if dst_node.colorModel() != src_node.colorModel() or dst_node.colorDepth() != src_node.colorDepth():
            raise ClonestampError("Source and destination layers must have the same color model/depth.")

    read_fn, src_bounds = _resolve_source(doc, src_node, sample_scope)

    size = max(1, int(size))
    half = size / 2.0
    src_rect = QRect(int(round(src_center.x() - half)), int(round(src_center.y() - half)), size, size)
    dst_rect = QRect(int(round(dst_center.x() - half)), int(round(dst_center.y() - half)), size, size)

    src_clip = src_rect.intersected(src_bounds)
    dst_clip = dst_rect.intersected(dst_node.bounds())

    # Shrink both rects by whichever side needs it more, so they stay the
    # same size and pixel-aligned even when one side runs off its layer.
    left = max(src_clip.left() - src_rect.left(), dst_clip.left() - dst_rect.left())
    top = max(src_clip.top() - src_rect.top(), dst_clip.top() - dst_rect.top())
    right = max(src_rect.right() - src_clip.right(), dst_rect.right() - dst_clip.right())
    bottom = max(src_rect.bottom() - src_clip.bottom(), dst_rect.bottom() - dst_clip.bottom())

    w = size - left - right
    h = size - top - bottom
    if w <= 0 or h <= 0:
        return None  # entirely off one of the two layers; skip this dab

    src_x, src_y = src_rect.x() + left, src_rect.y() + top
    dst_x, dst_y = dst_rect.x() + left, dst_rect.y() + top

    src_bytes = read_fn(src_x, src_y, w, h)
    src_image = QImage(src_bytes, w, h, PIXEL_FORMAT).convertToFormat(BLEND_FORMAT)

    dst_bytes = dst_node.pixelData(dst_x, dst_y, w, h)
    dst_image = QImage(dst_bytes, w, h, PIXEL_FORMAT).convertToFormat(BLEND_FORMAT)

    _apply_radial_mask(src_image, hardness)
    _scale_alpha(src_image, opacity / 100.0)

    painter = QPainter(dst_image)
    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
    painter.drawImage(0, 0, src_image)
    painter.end()

    result_bytes = _image_bytes(dst_image)
    ok = dst_node.setPixelData(result_bytes, dst_x, dst_y, w, h)
    if not ok:
        raise ClonestampError("Failed to write pixel data to '{0}'.".format(dst_node.name()))

    doc.refreshProjection()
    return QRect(dst_x, dst_y, w, h)
