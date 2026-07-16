# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>

import traceback
from krita import DockWidget, Krita
from PyQt5.QtCore import QEvent, QPointF, QTimer, Qt, QUrl
from PyQt5.QtGui import QColor, QCursor, QDesktopServices, QIcon, QPainter, QPen, QPixmap, QRadialGradient
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QOpenGLWidget,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
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
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._onTimerTick)
        self._ch_timer = QTimer(self)
        self._ch_timer.setInterval(200)
        self._ch_timer.timeout.connect(self._onCrosshairTick)
        self._last_cursor_zoom = -1.0
        self._last_cursor_size = -1
        self._tick_counter = 0
        self._crosshair = SourceCrosshair()

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
        self._timer.stop()
        self._stroke_active = False
        self._resize_active = False
        self._canvas_widget = None
        self.brushStatusLabel.setText("")

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
        # Also update cursor ring when zoom changes during hover.
        canvas = self._currentCanvas()
        if canvas:
            zoom = canvas.zoomLevel()
            if abs(zoom - self._last_cursor_zoom) > 0.01:
                self._last_cursor_zoom = zoom
                self._last_cursor_size = core.STATE.brush_size
                self._updateBrushCursor()

    def _onResizeTick(self):
        current = QCursor.pos()
        dx = current.x() - self._resize_start_global.x()
        dy = current.y() - self._resize_start_global.y()

        new_size = max(1, int(self._resize_start_size + dx))
        size_changed = new_size != core.STATE.brush_size
        if size_changed:
            core.STATE.brush_size = new_size
            self.sizeSpin.blockSignals(True)
            self.sizeSpin.setValue(new_size)
            self.sizeSpin.blockSignals(False)

        new_hardness_pct = max(0, min(100, self._resize_start_hardness_pct - dy))
        new_hardness = new_hardness_pct / 100.0
        hardness_changed = new_hardness != core.STATE.brush_hardness
        if hardness_changed:
            core.STATE.brush_hardness = new_hardness
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
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(Qt.NoBrush)

        # Destination ring (at pixmap center).
        dl = half - diameter // 2
        dt = half - diameter // 2

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

        painter.end()
        self._canvas_widget.setCursor(QCursor(pixmap, half, half))

    def _warn(self, message):
        window = Krita.instance().activeWindow()
        if window is not None and window.activeView() is not None:
            window.activeView().showFloatingMessage(message, QIcon(), 3000, 1)
