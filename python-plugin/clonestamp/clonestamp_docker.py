# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>

import time
import traceback
from krita import DockWidget, Krita
from PyQt5.QtCore import QEvent, QPointF, QTimer, Qt, QUrl
from PyQt5.QtGui import (
    QColor, QCursor, QDesktopServices, QIcon, QImage, QPainter, QPen, QPixmap, QRadialGradient,
)
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QOpenGLWidget,
    QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from . import clonestamp_core as core

# Single shared debug writer lives in core (gated off by default there --
# see core._DEBUG_ENABLED for how to switch it on).
_debug = core._debug


def _find_canvas_widget():
    window = Krita.instance().activeWindow()
    if window is None:
        return None
    qwin = window.qwindow()
    if qwin is None:
        return None
    central = qwin.centralWidget()
    if central is None:
        return None
    return central.findChild(QOpenGLWidget)


class SourceCrosshair(QWidget):
    """Tiny transparent widget showing a green crosshair at the source point
    on the canvas. Qt-only, no Krita API calls in paint path."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.resize(40, 40)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        c = self.rect().center()
        r = 8
        painter.setPen(QPen(QColor(255, 255, 255, 160), 3))
        painter.drawLine(c.x() - r, c.y(), c.x() + r, c.y())
        painter.drawLine(c.x(), c.y() - r, c.x(), c.y() + r)
        painter.setPen(QPen(QColor(0, 220, 0, 230), 2))
        painter.drawLine(c.x() - r, c.y(), c.x() + r, c.y())
        painter.drawLine(c.x(), c.y() - r, c.x(), c.y() + r)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 255, 0, 200))
        painter.drawEllipse(c.x() - 2, c.y() - 2, 5, 5)
        painter.end()


class ClonestampDocker(DockWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Clonestamp Tool with Preview")

        self._canvas_widget = None
        self._stroke_active = False
        self._resize_active = False
        self._resize_start_global = None
        self._resize_start_size = None
        self._timer = QTimer(self)
        # PreciseTimer: the default coarse timer type has ~15ms granularity
        # on Windows, which would silently defeat a 16ms interval.
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._onTimerTick)
        self._ch_timer = QTimer(self)
        self._ch_timer.setInterval(200)
        self._ch_timer.timeout.connect(self._onCrosshairTick)
        self._last_cursor_zoom = -1.0
        self._last_cursor_size = -1
        self._tick_counter = 0
        # First/last _onTimerTick timestamps for the active stroke, used to
        # log a single actual-cadence summary on release (see
        # _onCanvasRelease) -- lets the interval/PreciseTimer setting above
        # be empirically checked instead of just assumed.
        self._tick_first_t = 0.0
        self._tick_last_t = 0.0
        self._crosshair = SourceCrosshair()

        # Cache for the live content-preview patch (see _updateBrushCursor).
        # Rebuilding it (image scale + soft-mask paint) on every drag tick
        # was the likely cause of drag lag -- this project's own history
        # only validated that cost at the 200ms/5Hz hover cadence. The cache
        # lets ring/crosshair redraw stay at full tick rate while the
        # expensive preview content itself refreshes at that same
        # already-proven-safe ~5Hz, reusing the last frame in between.
        self._preview_cache_image = None
        self._preview_cache_time = 0.0
        self._preview_cache_diameter = -1
        self._preview_cache_hardness = -1.0
        # Bumped each time _refreshPreviewCache actually recomputes, so
        # _updateBrushCursor's rebuild memo (see _last_cursor_key) can tell
        # "the cached preview content changed" apart from "still the same
        # frame as last tick".
        self._preview_cache_generation = 0

        # Memoizes the last set of inputs _updateBrushCursor rendered from,
        # so a tick where nothing visually changed (the common case while
        # dragging in Aligned mode, where the source offset is fixed for the
        # whole stroke) can skip rebuilding the QPixmap -- see
        # _updateBrushCursor. setCursor() itself is still reissued every
        # call even on a memo hit (with the cached pixmap): the underlying
        # native Krita tool is still selected (we only intercept mouse
        # events, never change the active KisTool) and receives the real
        # MouseMove events we don't consume, so it can reassert its own
        # cursor on plain hover movement. Skipping setCursor() entirely
        # let that win intermittently, making our ring flicker/disappear
        # while moving over the canvas.
        self._last_cursor_key = None
        self._last_cursor_pixmap = None
        self._last_cursor_half = 0

        widget = QWidget()
        layout = QVBoxLayout()

        self.enableCheck = QCheckBox("Enable Clone Brush")
        self.enableCheck.toggled.connect(self.onEnableToggled)
        layout.addWidget(self.enableCheck)

        hint = QLabel(
            "Ctrl+Click on canvas = sample source. Click+drag = paint. "
            "Shift+drag = resize brush. Clicks are consumed while enabled, "
            "so the active Krita tool won't also paint on the same drag.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.liveSourceLabel = QLabel("Source: none")
        self.liveSourceLabel.setWordWrap(True)
        layout.addWidget(self.liveSourceLabel)

        sizeRow = QHBoxLayout()
        sizeRow.addWidget(QLabel("Size:"))
        self.sizeSpin = QSpinBox()
        self.sizeSpin.setRange(1, 2000)
        self.sizeSpin.setValue(core.STATE.brush_size)
        self.sizeSpin.setSuffix(" px")
        self.sizeSpin.valueChanged.connect(self._onSizeChanged)
        sizeRow.addWidget(self.sizeSpin)
        layout.addLayout(sizeRow)

        hardnessRow = QHBoxLayout()
        hardnessRow.addWidget(QLabel("Hardness:"))
        self.hardnessSlider = QSlider(Qt.Horizontal)
        self.hardnessSlider.setRange(0, 100)
        self.hardnessSlider.setValue(int(core.STATE.brush_hardness * 100))
        self.hardnessSlider.valueChanged.connect(self._onHardnessChanged)
        hardnessRow.addWidget(self.hardnessSlider)
        self.hardnessSpin = QSpinBox()
        self.hardnessSpin.setRange(0, 100)
        self.hardnessSpin.setValue(int(core.STATE.brush_hardness * 100))
        self.hardnessSpin.setSuffix(" %")
        self.hardnessSpin.valueChanged.connect(self._onHardnessChanged)
        hardnessRow.addWidget(self.hardnessSpin)
        layout.addLayout(hardnessRow)

        brushOpacityRow = QHBoxLayout()
        brushOpacityRow.addWidget(QLabel("Opacity:"))
        self.brushOpacitySpin = QSpinBox()
        self.brushOpacitySpin.setRange(0, 100)
        self.brushOpacitySpin.setValue(core.STATE.brush_opacity)
        self.brushOpacitySpin.setSuffix(" %")
        self.brushOpacitySpin.valueChanged.connect(self._onBrushOpacityChanged)
        brushOpacityRow.addWidget(self.brushOpacitySpin)
        layout.addLayout(brushOpacityRow)

        self.alignedCheck = QCheckBox("Aligned")
        self.alignedCheck.setChecked(core.STATE.aligned)
        self.alignedCheck.toggled.connect(self._onAlignedToggled)
        layout.addWidget(self.alignedCheck)

        sampleRow = QHBoxLayout()
        sampleRow.addWidget(QLabel("Sample:"))
        self.sampleCombo = QComboBox()
        self.sampleCombo.addItem("Current Layer")
        self.sampleCombo.addItem("All Layers")
        self.sampleCombo.setCurrentIndex(1 if core.STATE.sample_scope == "all" else 0)
        self.sampleCombo.currentIndexChanged.connect(self._onSampleScopeChanged)
        sampleRow.addWidget(self.sampleCombo)
        layout.addLayout(sampleRow)

        self.brushStatusLabel = QLabel("")
        self.brushStatusLabel.setWordWrap(True)
        layout.addWidget(self.brushStatusLabel)

        aboutRow = QHBoxLayout()
        versionLabel = QLabel("v{0}".format(core.VERSION))
        aboutRow.addWidget(versionLabel)
        githubLabel = QLabel('<a href="{0}">GitHub</a>'.format(core.GITHUB_URL))
        githubLabel.setOpenExternalLinks(True)
        aboutRow.addWidget(githubLabel)
        aboutRow.addStretch()
        updateButton = QPushButton("Check for Updates")
        updateButton.clicked.connect(self._onCheckForUpdates)
        aboutRow.addWidget(updateButton)
        layout.addLayout(aboutRow)

        layout.addStretch()
        widget.setLayout(layout)
        self.setWidget(widget)

        if core.STATE.has_point_source:
            self._describeLiveSource()

    # --- lifecycle -------------------------------------------------------

    def canvasChanged(self, canvas):
        pass

    def closeEvent(self, event):
        self._disarm()
        super().closeEvent(event)

    # --- brush settings ----------------------------------------------------

    def _onSizeChanged(self, value):
        core.STATE.brush_size = value
        self._updateBrushCursor()

    def _onHardnessChanged(self, value):
        core.STATE.brush_hardness = value / 100.0
        self.hardnessSlider.blockSignals(True)
        self.hardnessSlider.setValue(value)
        self.hardnessSlider.blockSignals(False)
        self.hardnessSpin.blockSignals(True)
        self.hardnessSpin.setValue(value)
        self.hardnessSpin.blockSignals(False)
        self._updateBrushCursor()

    def _onBrushOpacityChanged(self, value):
        core.STATE.brush_opacity = value

    def _onAlignedToggled(self, checked):
        core.STATE.aligned = checked

    def _onSampleScopeChanged(self, index):
        core.STATE.sample_scope = "all" if index == 1 else "current"

    def _onCheckForUpdates(self):
        QDesktopServices.openUrl(QUrl(core.GITHUB_URL + "/releases/latest"))

    def _describeLiveSource(self):
        p = core.STATE.source_point
        self.liveSourceLabel.setText(
            "Source: layer '{0}' @ ({1:.0f}, {2:.0f})".format(
                core.STATE.source_node.name(), p.x(), p.y()))

    # --- crosshair widget positioning --------------------------------------

    def _positionCrosshair(self):
        """Move the crosshair widget to the screen position of source_point.
        Lightweight: calls zoomLevel/preferredCenter but no pixel I/O."""
        if not core.STATE.has_point_source or self._canvas_widget is None:
            self._crosshair.hide()
            return
        canvas = self._currentCanvas()
        if canvas is None or not core.coordinate_mapping_reliable(canvas):
            self._crosshair.hide()
            return
        zoom = canvas.zoomLevel()
        pref = canvas.preferredCenter()
        cw = self._canvas_widget.width()
        ch = self._canvas_widget.height()
        sx = (core.STATE.source_point.x() - pref.x()) * zoom + cw / 2.0
        sy = (core.STATE.source_point.y() - pref.y()) * zoom + ch / 2.0
        global_pos = self._canvas_widget.mapToGlobal(QPointF(sx, sy).toPoint())
        self._crosshair.move(global_pos.x() - 20, global_pos.y() - 20)
        if not self._crosshair.isVisible():
            self._crosshair.show()

    # --- Enable/disable ---------------------------------------------------

    def onEnableToggled(self, checked):
        if checked:
            self._arm()
        else:
            self._disarm()

    def _arm(self):
        widget = _find_canvas_widget()
        if widget is None:
            self.brushStatusLabel.setText(
                "Could not find the canvas widget; Clone Brush can't be enabled.")
            self.enableCheck.setChecked(False)
            return
        self._canvas_widget = widget
        QApplication.instance().installEventFilter(self)
        self._toggleNativeBrushOutline()
        self._updateBrushCursor()
        canvas = self._currentCanvas()
        if canvas:
            self._last_cursor_zoom = canvas.zoomLevel()
        self._last_cursor_size = core.STATE.brush_size
        if core.STATE.has_point_source:
            self._ch_timer.start()
            self._positionCrosshair()
        self.brushStatusLabel.setText("Clone Brush enabled.")

    def _disarm(self):
        self._ch_timer.stop()
        self._crosshair.hide()
        if self._canvas_widget is not None:
            QApplication.instance().removeEventFilter(self)
            self._canvas_widget.unsetCursor()
            self._toggleNativeBrushOutline()
        # unsetCursor() above bypasses the _updateBrushCursor rebuild memo,
        # so without this a rearm whose key happens to match the pre-disarm
        # state would skip setCursor() and leave the default OS cursor
        # showing instead of the brush ring.
        self._last_cursor_key = None
        self._last_cursor_pixmap = None
        self._timer.stop()
        self._stroke_active = False
        self._resize_active = False
        self._canvas_widget = None
        self.brushStatusLabel.setText("")

    def _toggleNativeBrushOutline(self):
        """Flips Krita's own brush-outline circle (the round cursor overlay
        the underlying native tool -- e.g. Freehand Brush -- still draws,
        since we only intercept mouse events and never actually change the
        active KisTool). It's a canvas-level paint overlay, not a QCursor,
        so our own setCursor()/unsetCursor() calls have no effect on it and
        it would otherwise show doubled up with our own ring. Krita exposes
        exactly this as the "toggle_brush_outline" action (bound to a
        keyboard shortcut for users to peek under their brush while
        painting) -- it flips between OUTLINE_NONE and whatever style was
        last in use, so calling it once on arm and once on disarm hides it
        while Clone Brush is enabled and symmetrically restores it after."""
        action = Krita.instance().action("toggle_brush_outline")
        if action is not None:
            action.trigger()

    # --- event filter: only press/release are used; drag is timer-driven --

    def eventFilter(self, obj, event):
        if obj is not self._canvas_widget:
            return False

        et = event.type()
        try:
            if et == QEvent.MouseButtonPress:
                return self._onCanvasPress(event)
            elif et == QEvent.MouseButtonRelease:
                return self._onCanvasRelease(event)
        except Exception as e:
            # Exceptions are rare and always worth a trace, even with the
            # debug gate off -- hence force=True.
            _debug("eventFilter error: %s\n%s" % (e, traceback.format_exc()),
                   force=True)
            self.brushStatusLabel.setText(str(e)[:80])
        return False

    def _currentCanvas(self):
        window = Krita.instance().activeWindow()
        view = window.activeView() if window else None
        return view.canvas() if view else None

    def _mapEventPos(self, event):
        canvas = self._currentCanvas()
        if canvas is None or self._canvas_widget is None:
            return None
        if not core.coordinate_mapping_reliable(canvas):
            self.brushStatusLabel.setText(
                "Canvas is rotated or mirrored; Clone Brush coordinates would be wrong. "
                "Reset canvas rotation/mirror to use it.")
            return None
        return core.map_widget_to_document(canvas, self._canvas_widget, event.pos())

    def _onCanvasPress(self, event):
        _debug("_onCanvasPress: button=%d mods=%s" % (event.button(), event.modifiers()))
        if event.button() != Qt.LeftButton:
            return False

        canvas = self._currentCanvas()
        if canvas:
            self._last_cursor_zoom = canvas.zoomLevel()
        self._last_cursor_size = core.STATE.brush_size

        mods = event.modifiers()

        if mods & Qt.ShiftModifier:
            self._resize_active = True
            self._resize_start_global = QCursor.pos()
            self._resize_start_size = core.STATE.brush_size
            self._resize_start_hardness_pct = int(round(core.STATE.brush_hardness * 100))
            self._resize_accum_dx = 0
            self._resize_accum_dy = 0
            self._timer.start()
            return True

        doc_point = self._mapEventPos(event)
        if doc_point is None:
            return True
        doc = Krita.instance().activeDocument()

        try:
            if mods & Qt.ControlModifier:
                core.sample_source_point(doc, core.STATE, doc_point)
                self._describeLiveSource()
                self.brushStatusLabel.setText("Source sampled.")
                self._ch_timer.start()
                self._positionCrosshair()
                self._updateBrushCursor()
            else:
                core.begin_stroke(core.STATE, doc, doc_point)
                self._stroke_active = True
                self._tick_counter = 0
                self._crosshair.hide()
                self._ch_timer.stop()
                self._timer.start()
                self.brushStatusLabel.setText("Drag to paint...")
        except core.ClonestampError as e:
            self.brushStatusLabel.setText(str(e))
            self._warn(str(e))
        return True

    def _onCanvasRelease(self, event):
        _debug("_onCanvasRelease: button=%d stroke_active=%s" % (event.button(), self._stroke_active))
        if event.button() != Qt.LeftButton:
            return False
        if self._stroke_active:
            # One-line actual-tick-rate summary per stroke (force=True: this
            # is the empirical check for whether setInterval(16) +
            # PreciseTimer is actually being honored on this machine, so it
            # logs even with the debug gate off).
            if self._tick_counter > 1:
                span_ms = (self._tick_last_t - self._tick_first_t) * 1000.0
                avg_ms = span_ms / (self._tick_counter - 1)
                _debug("stroke ticks: n=%d avg_dt=%.1fms span=%.0fms"
                       % (self._tick_counter, avg_ms, span_ms), force=True)
            doc = Krita.instance().activeDocument()
            if doc is not None:
                result = core.finalize_stroke(doc, core.STATE)
                if result is not None:
                    self.brushStatusLabel.setText(
                        "Stroke painted at ({0}, {1})".format(result.x(), result.y()))
            core.end_stroke(core.STATE)
            self._stroke_active = False
            self._crosshair.show()
            self._ch_timer.start()
            self._positionCrosshair()
        if self._resize_active:
            self._resize_active = False
            cursor_brush_status = (
                "Size: {0} px  Hardness: {1}%  Opacity: {2}%"
                .format(core.STATE.brush_size,
                        int(round(core.STATE.brush_hardness * 100)),
                        core.STATE.brush_opacity))
            self.brushStatusLabel.setText(cursor_brush_status)
            doc_point = self._mapEventPos(event)
            self._updateBrushCursor(doc_point)
        return True

    def _onTimerTick(self):
        _debug("_onTimerTick: stroke_active=%s resize_active=%s" % (
            self._stroke_active, self._resize_active))
        if self._canvas_widget is None:
            self._timer.stop()
            return
        if self._resize_active:
            self._onResizeTick()
            return
        if not self._stroke_active:
            self._timer.stop()
            self._crosshair.show()
            self._ch_timer.start()
            self._positionCrosshair()
            return

        self._tick_counter += 1
        now = time.perf_counter()
        if self._tick_counter == 1:
            self._tick_first_t = now
        self._tick_last_t = now
        local_pos = self._canvas_widget.mapFromGlobal(QCursor.pos())
        canvas = self._currentCanvas()
        if canvas is None or not core.coordinate_mapping_reliable(canvas):
            return
        doc_point = core.map_widget_to_document(canvas, self._canvas_widget, QPointF(local_pos))
        if doc_point is None:
            return
        zoom = canvas.zoomLevel()

        doc = Krita.instance().activeDocument()
        try:
            result = core.continue_stroke(doc, core.STATE, doc_point)
            if result is None and self.brushStatusLabel.text() == "":
                self.brushStatusLabel.setText("Painting...")
        except core.ClonestampError as e:
            self.brushStatusLabel.setText(str(e))
            self._stroke_active = False
            return

        # Cursor refresh -- every tick while a source is armed, so the red
        # source-offset crosshair tracks the actual cursor during a drag
        # instead of visibly lagging behind it.
        zoom_changed = abs(zoom - self._last_cursor_zoom) > 0.01
        size_changed = core.STATE.brush_size != self._last_cursor_size
        if zoom_changed or size_changed:
            self._last_cursor_zoom = zoom
            self._last_cursor_size = core.STATE.brush_size
        if core.STATE.has_point_source:
            self._updateBrushCursor(doc_point)
        elif zoom_changed or size_changed:
            self._updateBrushCursor()

    def _onCrosshairTick(self):
        self._positionCrosshair()
        # Also refresh the cursor's content preview from the live hover
        # position (5Hz -- cheap Qt-only image ops, no Krita API calls, same
        # budget this project's history has shown to be safe).
        canvas = self._currentCanvas()
        if canvas is None or not core.coordinate_mapping_reliable(canvas):
            return
        zoom = canvas.zoomLevel()
        if abs(zoom - self._last_cursor_zoom) > 0.01:
            self._last_cursor_zoom = zoom
            self._last_cursor_size = core.STATE.brush_size
        local_pos = self._canvas_widget.mapFromGlobal(QCursor.pos())
        doc_point = core.map_widget_to_document(canvas, self._canvas_widget, QPointF(local_pos))
        if doc_point is not None:
            self._updateBrushCursor(doc_point)

    def _onResizeTick(self):
        # The OS cursor is warped back to the drag's start position every
        # tick (below) so it stays visually anchored in place instead of
        # travelling across the screen while resizing -- Photoshop-style
        # "scratch pad" feedback where only the brush ring changes, not the
        # pointer position. Movement is therefore accumulated across ticks
        # (each tick's delta is measured from the anchor we just warped
        # back to) rather than read as one absolute offset from the start.
        current = QCursor.pos()
        dx = current.x() - self._resize_start_global.x()
        dy = current.y() - self._resize_start_global.y()
        self._resize_accum_dx += dx
        self._resize_accum_dy += dy
        if dx or dy:
            QCursor.setPos(self._resize_start_global)

        new_size = max(1, int(self._resize_start_size + self._resize_accum_dx))
        size_changed = new_size != core.STATE.brush_size
        if size_changed:
            core.STATE.brush_size = new_size
            self.sizeSpin.blockSignals(True)
            self.sizeSpin.setValue(new_size)
            self.sizeSpin.blockSignals(False)

        new_hardness_pct = max(0, min(100, self._resize_start_hardness_pct - self._resize_accum_dy))
        new_hardness = new_hardness_pct / 100.0
        hardness_changed = new_hardness != core.STATE.brush_hardness
        if hardness_changed:
            core.STATE.brush_hardness = new_hardness
            self.hardnessSlider.blockSignals(True)
            self.hardnessSlider.setValue(new_hardness_pct)
            self.hardnessSlider.blockSignals(False)
            self.hardnessSpin.blockSignals(True)
            self.hardnessSpin.setValue(new_hardness_pct)
            self.hardnessSpin.blockSignals(False)

        if size_changed or hardness_changed:
            local_pos = self._canvas_widget.mapFromGlobal(current)
            canvas = self._currentCanvas()
            if canvas and core.coordinate_mapping_reliable(canvas):
                doc_point = core.map_widget_to_document(canvas, self._canvas_widget, QPointF(local_pos))
                self._updateBrushCursor(doc_point)
            else:
                self._updateBrushCursor()

    def _updateBrushCursor(self, cursor_doc_pos=None):
        """Sets the canvas cursor to a ring matching the current brush size
        (in screen pixels, accounting for zoom) -- Krita's own brush-outline
        cursor doesn't apply while another tool is active underneath ours.
        Draws a semi-transparent fill showing the softness gradient and a
        solid inner ring marking the fully-opaque zone.
        When cursor_doc_pos is given and a source is sampled, also draws a
        crosshair + source outline ring at the relative offset."""
        if self._canvas_widget is None:
            return
        canvas = self._currentCanvas()
        zoom = canvas.zoomLevel() if canvas else 1.0
        diameter = max(4, int(core.STATE.brush_size * zoom))

        offset = core.source_screen_offset(core.STATE, cursor_doc_pos, zoom)
        show_src = offset is not None

        # Refresh (or reuse) the preview-content cache before computing the
        # memo key below, so a due refresh always bumps
        # _preview_cache_generation *before* it's read -- otherwise a stale
        # generation value would get baked into the key and the cache would
        # never be seen as due again. This runs before the pixmap/painter
        # below exist, so it needs its own try/except: an exception here
        # would otherwise propagate straight out of the QTimer slot with no
        # safety net (the try/finally below only guards the paint routine
        # itself), reintroducing the crash risk that guard exists for.
        if show_src:
            try:
                self._refreshPreviewCache(cursor_doc_pos, diameter)
            except Exception as e:
                _debug("_refreshPreviewCache error: %s\n%s" % (e, traceback.format_exc()),
                       force=True)
                self._preview_cache_image = None

        # Skip the rebuild when nothing that determines the rendered pixels
        # has changed since the last call. setCursor() only sets the cursor
        # *shape* -- the windowing system repositions that shape at the live
        # pointer location on its own every frame, so skipping an unchanged
        # rebuild cannot make the ring visually lag. In Aligned mode the
        # source offset is fixed for the whole stroke, so this collapses the
        # per-tick cost from every ~16ms tick down to whenever the preview
        # cache actually refreshes (~5Hz) or size/hardness/zoom change; in
        # Non-Aligned mode the offset itself tracks the live cursor, so this
        # self-adjusts back to rebuilding every tick.
        cursor_key = (
            diameter,
            core.STATE.brush_hardness,
            (int(round(offset[0])), int(round(offset[1])),
             self._preview_cache_generation) if show_src else None,
        )
        if cursor_key == self._last_cursor_key and self._last_cursor_pixmap is not None:
            # Reissue the unchanged pixmap rather than skipping outright --
            # see the comment on _last_cursor_key in __init__ for why this
            # call still has to happen every tick even though the rebuild
            # below doesn't.
            self._canvas_widget.setCursor(
                QCursor(self._last_cursor_pixmap, self._last_cursor_half, self._last_cursor_half))
            return
        self._last_cursor_key = cursor_key

        half_ring = diameter // 2 + 2
        if show_src:
            ox, oy = offset
            ox, oy = int(ox), int(oy)
            need_w = max(half_ring * 2, abs(ox) * 2 + half_ring * 2)
            need_h = max(half_ring * 2, abs(oy) * 2 + half_ring * 2)
            pixmap_size = int(min(max(need_w, need_h), 512))
        else:
            pixmap_size = half_ring * 2
        half = pixmap_size // 2

        pixmap = QPixmap(pixmap_size, pixmap_size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(Qt.NoBrush)

            # Destination ring (at pixmap center).
            dl = half - diameter // 2
            dt = half - diameter // 2

            # Live content preview: an actual (ghosted) copy of the source
            # pixels that would be cloned right now, masked to the same
            # soft-round shape as the brush. Drawn at the destination
            # (dl, dt) -- i.e. under the brush ring, where painting will
            # actually land -- not at the source point, which is separately
            # marked by the crosshair below. Drawn first so the
            # ring/dashed-hardness outlines below paint on top of it and
            # stay crisp.
            #
            # The scale+mask work below is only recomputed at ~5Hz (see
            # _preview_cache_*), not on every call to this function -- this
            # function runs on every drag tick to keep the cheap
            # ring/crosshair responsive, but this project's own history only
            # validated this image-scaling cost at the 200ms hover cadence;
            # doing it at full tick rate was the likely cause of drag lag.
            # The cached frame is reused between recomputes.
            if show_src and self._preview_cache_image is not None:
                painter.drawImage(dl, dt, self._preview_cache_image)

            if core.STATE.brush_hardness < 1.0:
                dcenter = half + 0.5
                grad = QRadialGradient(dcenter, dcenter, half - 1)
                grad.setColorAt(0.0, QColor(255, 255, 255, 12))
                grad.setColorAt(float(core.STATE.brush_hardness),
                                QColor(255, 255, 255, 12))
                grad.setColorAt(1.0, QColor(255, 255, 255, 0))
                painter.setBrush(grad)
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(dl, dt, diameter, diameter)

            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0, 200), 1))
            painter.drawEllipse(dl + 1, dt + 1, diameter, diameter)
            painter.setPen(QPen(QColor(255, 255, 255, 200), 1))
            painter.drawEllipse(dl, dt, diameter, diameter)

            hardness_diameter = max(3, int(diameter * core.STATE.brush_hardness))
            inset = (diameter - hardness_diameter) // 2
            painter.setPen(QPen(QColor(0, 0, 0, 160), 1, Qt.DashLine))
            painter.drawEllipse(dl + inset + 1, dt + inset + 1, hardness_diameter, hardness_diameter)
            painter.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.DashLine))
            painter.drawEllipse(dl + inset, dt + inset, hardness_diameter, hardness_diameter)

            # Red crosshair at source offset (follows cursor, shows what's cloned).
            if show_src:
                sx = int(half + offset[0])
                sy = int(half + offset[1])
                cs = 6
                painter.setPen(QPen(QColor(255, 0, 0, 220), 2))
                painter.drawLine(sx - cs, sy, sx + cs, sy)
                painter.drawLine(sx, sy - cs, sx, sy + cs)
                painter.setPen(QPen(QColor(255, 255, 255, 180), 1))
                painter.drawLine(sx - cs, sy - 1, sx + cs, sy - 1)
                painter.drawLine(sx - cs, sy + 1, sx + cs, sy + 1)
                painter.drawLine(sx - 1, sy - cs, sx - 1, sy + cs)
                painter.drawLine(sx + 1, sy - cs, sx + 1, sy + cs)
        except Exception as e:
            # A mid-paint exception here (e.g. from the preview patch/mask
            # work) would otherwise leave the painter still active on
            # `pixmap` when we hand it to QCursor below -- Qt does not allow
            # a QPixmap to be used elsewhere while a QPainter is still open
            # on it, which is a real crash risk, not just a cosmetic one.
            # Falling back to the plain ring (no preview) for this one frame
            # keeps the brush cursor usable instead of losing it or crashing.
            _debug("_updateBrushCursor error: %s\n%s" % (e, traceback.format_exc()),
                   force=True)
        finally:
            painter.end()
        self._last_cursor_pixmap = pixmap
        self._last_cursor_half = half
        self._canvas_widget.setCursor(QCursor(pixmap, half, half))

    def _refreshPreviewCache(self, cursor_doc_pos, diameter):
        """Recomputes self._preview_cache_image (the scaled+masked ghost of
        the source content) at most ~5Hz -- see the cache fields set in
        __init__ for why. Cheap no-op if called again before that interval
        elapses or brush size/hardness haven't changed; the previous frame
        is simply reused by the caller."""
        now = time.monotonic()
        stale = (
            self._preview_cache_image is None
            or (now - self._preview_cache_time) >= 0.2
            or self._preview_cache_diameter != diameter
            or self._preview_cache_hardness != core.STATE.brush_hardness
        )
        if not stale:
            return
        self._preview_cache_time = now
        self._preview_cache_diameter = diameter
        self._preview_cache_hardness = core.STATE.brush_hardness
        self._preview_cache_generation += 1
        patch = core.preview_patch(core.STATE, cursor_doc_pos, max(1, core.STATE.brush_size))
        if patch is None:
            self._preview_cache_image = None
            return
        preview = patch.scaled(diameter, diameter, Qt.IgnoreAspectRatio,
                                Qt.SmoothTransformation)
        preview = preview.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        mask = core.build_alpha_mask(diameter, core.STATE.brush_hardness, 70)
        mask_painter = QPainter(preview)
        mask_painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        mask_painter.drawImage(0, 0, mask)
        mask_painter.end()
        self._preview_cache_image = preview

    def _warn(self, message):
        window = Krita.instance().activeWindow()
        if window is not None and window.activeView() is not None:
            window.activeView().showFloatingMessage(message, QIcon(), 3000, 1)
