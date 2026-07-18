# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>

import os
import time
import traceback
from krita import DockWidget, Krita
from PyQt5.QtCore import QEvent, QPointF, QRectF, QTimer, Qt
from PyQt5.QtGui import (
    QColor, QCursor, QIcon, QImage, QPainter, QPen, QPixmap, QRadialGradient,
)
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QOpenGLWidget,
    QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from . import clonestamp_core as core
from . import clonestamp_update as update

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


class _StrokeOverlay(QWidget):
    """Transparent, click-through widget stacked on top of the canvas that
    shows the in-progress stroke *while dragging*. Purely visual -- it never
    touches the document. The real pixels are still only written once, to
    the actual layer, at stroke release (core.finalize_stroke), so this
    costs no extra undo step and no Krita API calls during the drag; it
    just blits into an offscreen QPixmap using the same in-memory source
    snapshot the cursor ghost-preview already reads from.

    Note this is an approximation, not a preview of the exact final pixels:
    each dab is composited here with its own SourceOver blend as it's
    stamped, whereas finalize_stroke unions the whole stroke's alpha mask
    first and blends once. Where dabs overlap (soft/low-opacity brushes,
    slow strokes) the live preview can look slightly more built-up than the
    final result -- acceptable for a live preview that previously showed
    nothing at all."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # Required for a plain child widget to actually composite on top of
        # a QOpenGLWidget -- without this, Qt's docs note the GL surface's
        # own native/texture compositing isn't synchronized with normal
        # widget stacking order, so a raised child can still render *behind*
        # the canvas on screen. This was the actual bug behind "no live
        # preview while dragging" -- the overlay was being painted every
        # tick, just never visible until Krita's own repaint (of the real
        # committed pixels) took over at release.
        self.setAttribute(Qt.WA_AlwaysStackOnTop)
        self._pixmap = None

    def reset(self, size):
        self._pixmap = QPixmap(size)
        self._pixmap.fill(Qt.transparent)
        self.setGeometry(0, 0, size.width(), size.height())

    def stampAt(self, target_rect, image):
        if self._pixmap is None:
            return
        painter = QPainter(self._pixmap)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.drawImage(target_rect, image)
        painter.end()
        self.update(target_rect.toAlignedRect())

    def paintEvent(self, event):
        if self._pixmap is None:
            return
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)


class ClonestampDocker(DockWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Clonestamp Tool with Preview")

        self._canvas_widget = None
        self._overlay = None
        self._stroke_active = False
        self._resize_active = False
        self._resize_start_global = None
        self._resize_start_size = None
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._onTimerTick)
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(200)
        self._hover_timer.timeout.connect(self._onHoverTick)
        self._last_cursor_zoom = -1.0
        self._last_cursor_size = -1
        self._tick_counter = 0

        # Cache for the live content-preview patch (see _updateBrushCursor).
        # Rebuilding it (image scale + soft-mask paint) on every 30ms drag
        # tick was the likely cause of drag lag -- this project's own history
        # only validated that cost at the 200ms/5Hz hover cadence. The cache
        # lets ring/crosshair redraw stay at full tick rate while the
        # expensive preview content itself refreshes at that same
        # already-proven-safe ~5Hz, reusing the last frame in between.
        self._preview_cache_image = None
        self._preview_cache_time = 0.0
        self._preview_cache_diameter = -1
        self._preview_cache_hardness = -1.0

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
        # Krita calls this whenever the active canvas changes -- opening a
        # new document, switching tabs, closing a document, etc. Our own
        # _canvas_widget was only ever resolved once, in _arm(), so without
        # this the event filter kept matching mouse events against
        # whichever canvas widget was active back when the brush was
        # enabled -- once a second document existed, that widget was no
        # longer the visible/active one, so the tool silently stopped
        # responding on the new document. Re-resolving and reparenting the
        # overlays here is what actually fixes that.
        if not self.enableCheck.isChecked() or self._canvas_widget is None:
            return
        widget = _find_canvas_widget()
        if widget is None or widget is self._canvas_widget:
            return
        self._teardownOverlays()
        self._canvas_widget = widget
        self._setupOverlays()
        current = self._currentCanvas()
        if current is not None:
            self._last_cursor_zoom = current.zoomLevel()

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
        # Synchronous on purpose -- this is a manual, infrequent button
        # click, not something running continuously, so a few seconds of
        # blocking UI (with the status label updated first so the user
        # sees *why*) is a reasonable trade against the real complexity of
        # a proper background thread for something this small.
        self.brushStatusLabel.setText("Checking GitHub for updates...")
        QApplication.processEvents()
        try:
            remote = update.fetch_remote_version()
        except update.UpdateError as e:
            self.brushStatusLabel.setText(str(e))
            return

        if not update.remote_version_is_newer(remote, core.VERSION):
            self.brushStatusLabel.setText(
                "Up to date (v{0}).".format(core.VERSION))
            return

        self.brushStatusLabel.setText("Downloading v{0}...".format(remote))
        QApplication.processEvents()
        try:
            update.download_update(os.path.dirname(os.path.abspath(__file__)))
        except update.UpdateError as e:
            self.brushStatusLabel.setText(str(e))
            return

        self.brushStatusLabel.setText(
            "Updated to v{0} -- restart Krita to load it.".format(remote))

    def _describeLiveSource(self):
        p = core.STATE.source_point
        self.liveSourceLabel.setText(
            "Source: layer '{0}' @ ({1:.0f}, {2:.0f})".format(
                core.STATE.source_node.name(), p.x(), p.y()))

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
        self._setupOverlays()
        QApplication.instance().installEventFilter(self)
        self._toggleNativeBrushOutline()
        self._updateBrushCursor()
        canvas = self._currentCanvas()
        if canvas:
            self._last_cursor_zoom = canvas.zoomLevel()
        self._last_cursor_size = core.STATE.brush_size
        if core.STATE.has_point_source:
            self._hover_timer.start()
            self._onHoverTick()
        self.brushStatusLabel.setText("Clone Brush enabled.")

    def _disarm(self):
        self._hover_timer.stop()
        if self._canvas_widget is not None:
            QApplication.instance().removeEventFilter(self)
            self._canvas_widget.unsetCursor()
            self._toggleNativeBrushOutline()
        self._timer.stop()
        self._stroke_active = False
        self._resize_active = False
        self._canvas_widget = None
        self._teardownOverlays()
        self.brushStatusLabel.setText("")

    def _setupOverlays(self):
        self._overlay = _StrokeOverlay(self._canvas_widget)

    def _teardownOverlays(self):
        if self._overlay is not None:
            self._overlay.setParent(None)
            self._overlay.deleteLater()
            self._overlay = None

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
            # Our ring cursor is switched off for the whole resize drag --
            # see _onResizeTick for why -- and back on at release.
            self._canvas_widget.unsetCursor()
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
                self._hover_timer.start()
                self._updateBrushCursor()
            else:
                core.begin_stroke(core.STATE, doc, doc_point)
                self._stroke_active = True
                self._tick_counter = 0
                self._hover_timer.stop()
                self._timer.start()
                if self._overlay is not None:
                    self._overlay.reset(self._canvas_widget.size())
                    self._overlay.show()
                    self._overlay.raise_()
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
            doc = Krita.instance().activeDocument()
            if doc is not None:
                result = core.finalize_stroke(doc, core.STATE)
                if result is not None:
                    self.brushStatusLabel.setText(
                        "Stroke painted at ({0}, {1})".format(result.x(), result.y()))
            core.end_stroke(core.STATE)
            if self._overlay is not None:
                self._overlay.setVisible(False)
            self._stroke_active = False
            self._hover_timer.start()
            self._onHoverTick()
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
            self._hover_timer.start()
            self._onHoverTick()
            return

        self._tick_counter += 1
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
            if result is not None:
                self._stampOverlayDab(doc_point, canvas, zoom)
            elif self.brushStatusLabel.text() == "":
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

    def _onHoverTick(self):
        # Refresh the cursor pixmap's ghost-preview content from the live
        # hover position (5Hz -- cheap Qt-only image ops, no Krita API
        # calls, same budget this project's history has shown to be safe).
        # The ring/crosshair positions need no separate repositioning here
        # -- they're baked into the OS cursor pixmap, which already tracks
        # the pointer natively.
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
        # "scratch pad" feedback. Movement is therefore accumulated across
        # ticks (each tick's delta is measured from the anchor we just
        # warped back to) rather than read as one absolute offset from the
        # start.
        #
        # The ring cursor itself is switched off for the whole drag (see
        # _onCanvasPress) instead of being redrawn every tick -- redrawing
        # a fresh QPixmap/QCursor on top of a cursor that's also being
        # warped every ~30ms was the actual source of the flicker; simply
        # not drawing anything there removes it outright. Size/hardness
        # still update live in the status label and spin boxes below; only
        # the on-canvas ring is gone until release, where _onCanvasRelease
        # switches it back on.
        #
        # (A blank-cursor + fixed on-screen ring overlay was tried instead
        # of just switching the ring off, to still show *something* live --
        # it made things worse, likely because raw MouseMove events aren't
        # intercepted by this plugin at all, so Krita's own underlying tool
        # kept re-asserting its own cursor on top of the blanked one during
        # the drag. See git history around v1.3.0 if revisiting that.)
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

    def _stampOverlayDab(self, doc_point, canvas, zoom):
        """Draws one masked source dab onto the live stroke overlay at
        doc_point, in screen space -- called once per new dab from
        _onTimerTick so a drag looks like it's painting continuously
        instead of only appearing once the mouse is released. No Krita API
        calls here, no full-image rescale -- just one in-memory patch read
        (already cached from the source snapshot), one alpha-mask
        composite, and one scaled QPainter blit, all Qt/CPU-only."""
        if self._overlay is None:
            return
        size = max(1, core.STATE.brush_size)
        patch = core.preview_patch(core.STATE, doc_point, size)
        if patch is None:
            return
        mask = core.build_alpha_mask(size, core.STATE.brush_hardness, core.STATE.brush_opacity)
        painter = QPainter(patch)
        painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, mask)
        painter.end()

        center = core.map_document_to_widget(canvas, self._canvas_widget, doc_point)
        if center is None:
            return
        screen_size = max(1.0, size * zoom)
        target = QRectF(center.x() - screen_size / 2.0, center.y() - screen_size / 2.0,
                         screen_size, screen_size)
        self._overlay.stampAt(target, patch)

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
            # function runs every 30ms during a drag (33Hz) to keep the
            # cheap ring/crosshair responsive, but this project's own
            # history only validated this image-scaling cost at the 200ms
            # hover cadence; doing it at 33Hz was the likely cause of drag
            # lag. The cached frame is reused between recomputes.
            if show_src:
                self._refreshPreviewCache(cursor_doc_pos, diameter)
                if self._preview_cache_image is not None:
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

            # Center mark for the destination ring, matching the native
            # tool's crossRadius=6.0 convention.
            dcs = 6
            painter.setPen(QPen(QColor(0, 0, 0, 200), 3))
            painter.drawLine(half - dcs, half, half + dcs, half)
            painter.drawLine(half, half - dcs, half, half + dcs)
            painter.setPen(QPen(QColor(255, 255, 255, 200), 1))
            painter.drawLine(half - dcs, half, half + dcs, half)
            painter.drawLine(half, half - dcs, half, half + dcs)

            hardness_diameter = max(3, int(diameter * core.STATE.brush_hardness))
            inset = (diameter - hardness_diameter) // 2
            painter.setPen(QPen(QColor(0, 0, 0, 160), 1, Qt.DashLine))
            painter.drawEllipse(dl + inset + 1, dt + inset + 1, hardness_diameter, hardness_diameter)
            painter.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.DashLine))
            painter.drawEllipse(dl + inset, dt + inset, hardness_diameter, hardness_diameter)

            # Source ring + crosshair at the sample offset (follows cursor,
            # shows what's cloned). Same circle-with-center shape as the
            # destination, but fainter so the two stay distinguishable.
            if show_src:
                sx = int(half + offset[0])
                sy = int(half + offset[1])
                sl = sx - diameter // 2
                st = sy - diameter // 2
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor(0, 0, 0, 100), 1))
                painter.drawEllipse(sl + 1, st + 1, diameter, diameter)
                painter.setPen(QPen(QColor(255, 255, 255, 100), 1))
                painter.drawEllipse(sl, st, diameter, diameter)

                cs = 6
                painter.setPen(QPen(QColor(255, 0, 0, 110), 2))
                painter.drawLine(sx - cs, sy, sx + cs, sy)
                painter.drawLine(sx, sy - cs, sx, sy + cs)
                painter.setPen(QPen(QColor(255, 255, 255, 90), 1))
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
