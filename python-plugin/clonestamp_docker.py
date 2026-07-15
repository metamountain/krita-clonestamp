# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>

from krita import DockWidget, Krita
from PyQt5.QtCore import QEvent, QPoint, QPointF, QTimer, Qt
from PyQt5.QtGui import QColor, QCursor, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QOpenGLWidget,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from . import clonestamp_core as core


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


class PreviewOverlay(QWidget):
    """A small always-on-top, click-through, translucent window that shows
    the current source patch near the cursor. Independent of Krita's own
    canvas widget/rendering -- it never touches or overlays on top of the
    OpenGL canvas surface itself, which is what made a true in-canvas ghost
    preview infeasible from Python. Just an ordinary floating Qt window."""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(0.75)
        self._image = None

    def setImage(self, image):
        self._image = image
        self.update()

    def paintEvent(self, event):
        if self._image is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(self.rect(), self._image)


class ClonestampDocker(DockWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Clonestamp Tool with Preview")

        self._canvas_widget = None
        self._stroke_active = False
        self._resize_active = False
        self._resize_start_global = None
        self._resize_start_size = None
        self._preview = PreviewOverlay()
        self._timer = QTimer(self)
        self._timer.setInterval(40)  # ~25Hz polling of QCursor.pos()
        self._timer.timeout.connect(self._onTimerTick)

        widget = QWidget()
        layout = QVBoxLayout()

        # --- Primary flow: Ctrl+click to sample, drag on canvas to paint ---
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

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        layout.addWidget(divider)

        # --- Fallback: manual selection-based capture/stamp (precision use) ---
        layout.addWidget(QLabel("Manual (selection-based, for precision placement):"))

        self.captureButton = QPushButton("Capture Source")
        self.captureButton.clicked.connect(self.onCaptureSource)
        layout.addWidget(self.captureButton)

        self.sourceLabel = QLabel("Source: none")
        self.sourceLabel.setWordWrap(True)
        layout.addWidget(self.sourceLabel)

        self.useSelectionCheck = QCheckBox("Use current selection as destination")
        self.useSelectionCheck.setChecked(True)
        self.useSelectionCheck.toggled.connect(self.onUseSelectionToggled)
        layout.addWidget(self.useSelectionCheck)

        destRow = QHBoxLayout()
        destRow.addWidget(QLabel("X:"))
        self.destXSpin = QSpinBox()
        self.destXSpin.setRange(0, 100000)
        self.destXSpin.setEnabled(False)
        destRow.addWidget(self.destXSpin)
        destRow.addWidget(QLabel("Y:"))
        self.destYSpin = QSpinBox()
        self.destYSpin.setRange(0, 100000)
        self.destYSpin.setEnabled(False)
        destRow.addWidget(self.destYSpin)
        layout.addLayout(destRow)

        featherRow = QHBoxLayout()
        featherRow.addWidget(QLabel("Feather:"))
        self.featherSpin = QSpinBox()
        self.featherSpin.setRange(0, 64)
        self.featherSpin.setValue(core.STATE.last_feather)
        self.featherSpin.setSuffix(" px")
        featherRow.addWidget(self.featherSpin)
        layout.addLayout(featherRow)

        opacityRow = QHBoxLayout()
        opacityRow.addWidget(QLabel("Opacity:"))
        self.opacitySpin = QSpinBox()
        self.opacitySpin.setRange(0, 100)
        self.opacitySpin.setValue(core.STATE.last_opacity)
        self.opacitySpin.setSuffix(" %")
        opacityRow.addWidget(self.opacitySpin)
        layout.addLayout(opacityRow)

        self.stampButton = QPushButton("Stamp")
        self.stampButton.setEnabled(core.STATE.has_source)
        self.stampButton.clicked.connect(self.onStamp)
        layout.addWidget(self.stampButton)

        self.statusLabel = QLabel("")
        self.statusLabel.setWordWrap(True)
        layout.addWidget(self.statusLabel)

        layout.addStretch()
        widget.setLayout(layout)
        self.setWidget(widget)

        if core.STATE.has_source:
            self._describeSource(core.STATE.src_node, core.STATE.src_rect)
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

    def _describeLiveSource(self):
        p = core.STATE.source_point
        self.liveSourceLabel.setText(
            "Source: layer '{0}' @ ({1:.0f}, {2:.0f})".format(
                core.STATE.source_node.name(), p.x(), p.y()))

    # --- Enable/disable the global click/drag filter ----------------------

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
        if core.STATE.has_point_source:
            self._timer.start()
        self.brushStatusLabel.setText("Clone Brush enabled.")

    def _disarm(self):
        if self._canvas_widget is not None:
            QApplication.instance().removeEventFilter(self)
            self._canvas_widget.unsetCursor()
        self._timer.stop()
        self._stroke_active = False
        self._resize_active = False
        self._canvas_widget = None
        self._preview.hide()

    # --- event filter: only press/release are used; drag is timer-driven --

    def eventFilter(self, obj, event):
        if obj is not self._canvas_widget:
            return False

        et = event.type()
        if et == QEvent.MouseButtonPress:
            return self._onCanvasPress(event)
        elif et == QEvent.MouseButtonRelease:
            return self._onCanvasRelease(event)
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
        if event.button() != Qt.LeftButton:
            return False  # only left-click is ours; right/middle pass through untouched

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
            return True  # still consume it; we just can't act on it right now
        doc = Krita.instance().activeDocument()

        try:
            if mods & Qt.ControlModifier:
                core.sample_source_point(doc, core.STATE, doc_point)
                self._describeLiveSource()
                self.brushStatusLabel.setText("Source sampled.")
                self._timer.start()  # begin the continuous hover-preview
            else:
                core.begin_stroke(core.STATE, doc_point)
                self._stroke_active = True
                self._timer.start()
        except core.ClonestampError as e:
            self.brushStatusLabel.setText(str(e))
            self._warn(str(e))
        return True

    def _onCanvasRelease(self, event):
        if event.button() != Qt.LeftButton:
            return False
        if self._stroke_active:
            core.end_stroke(core.STATE)
            self._stroke_active = False
            # timer keeps running: hover-preview continues after the stroke
        if self._resize_active:
            self._resize_active = False
            self._updateBrushCursor()
        return True

    def _onTimerTick(self):
        if self._canvas_widget is None:
            self._timer.stop()
            return
        if self._resize_active:
            self._onResizeTick()
            return
        if not (self._stroke_active or core.STATE.has_point_source):
            self._timer.stop()
            self._preview.hide()
            return

        local_pos = self._canvas_widget.mapFromGlobal(QCursor.pos())
        canvas = self._currentCanvas()
        if canvas is None or not core.coordinate_mapping_reliable(canvas):
            return
        doc_point = core.map_widget_to_document(canvas, self._canvas_widget, QPointF(local_pos))
        if doc_point is None:
            return

        zoom = canvas.zoomLevel()
        diameter = max(4, int(core.STATE.brush_size * zoom))
        doc = Krita.instance().activeDocument()

        if self._stroke_active:
            try:
                core.continue_stroke(doc, core.STATE, doc_point)
            except core.ClonestampError as e:
                self.brushStatusLabel.setText(str(e))
                self._stroke_active = False
                return
            offset = core.STATE.stroke_offset
            src_center = QPointF(doc_point.x() + offset.x(), doc_point.y() + offset.y())
            self._updatePreview(doc, core.STATE.source_node, src_center, diameter)
        else:
            # Hovering with a source already set: preview what the *first*
            # dab of a new stroke starting right here would look like --
            # i.e. the original sampled patch, carried to the cursor.
            self._updatePreview(doc, core.STATE.source_node, core.STATE.source_point, diameter)

    def _updatePreview(self, doc, src_node, src_center, diameter):
        image = core.read_preview_patch(doc, src_node, src_center, core.STATE.brush_size,
                                         core.STATE.brush_hardness, core.STATE.sample_scope)
        if image is None:
            self._preview.hide()
            return
        radius = diameter // 2
        pos = QCursor.pos() - QPoint(radius, radius)
        self._preview.setImage(image)
        self._preview.setGeometry(pos.x(), pos.y(), diameter, diameter)
        if not self._preview.isVisible():
            self._preview.show()

    def _onResizeTick(self):
        current = QCursor.pos()
        dx = current.x() - self._resize_start_global.x()
        # Screen y grows downward, so negate: dragging up increases
        # hardness, down softens -- matches the native tool's convention
        # (and Photoshop's on-canvas brush resize gesture).
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
            self._updateBrushCursor()

    def _updateBrushCursor(self):
        """Sets the canvas cursor to a ring matching the current brush size
        (in screen pixels, accounting for zoom) -- Krita's own brush-outline
        cursor doesn't apply while another tool is active underneath ours."""
        if self._canvas_widget is None:
            return
        canvas = self._currentCanvas()
        zoom = canvas.zoomLevel() if canvas else 1.0
        diameter = max(4, int(core.STATE.brush_size * zoom))
        pixmap = QPixmap(diameter + 2, diameter + 2)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 200), 1))
        painter.drawEllipse(1, 1, diameter, diameter)
        painter.setPen(QPen(QColor(255, 255, 255, 200), 1))
        painter.drawEllipse(0, 0, diameter, diameter)
        painter.end()
        center = (diameter + 2) // 2
        self._canvas_widget.setCursor(QCursor(pixmap, center, center))

    # --- manual (selection-based) fallback --------------------------------

    def onUseSelectionToggled(self, checked):
        self.destXSpin.setEnabled(not checked)
        self.destYSpin.setEnabled(not checked)

    def _describeSource(self, node, rect):
        self.sourceLabel.setText(
            "Source: layer '{0}', {1}x{2} @ ({3},{4})".format(
                node.name(), rect.width(), rect.height(), rect.x(), rect.y()))

    def onCaptureSource(self):
        doc = Krita.instance().activeDocument()
        try:
            rect = core.capture_source(doc, core.STATE)
            self._describeSource(core.STATE.src_node, rect)
            self.stampButton.setEnabled(True)
            self.statusLabel.setText("")
        except core.ClonestampError as e:
            self.statusLabel.setText(str(e))
            self._warn(str(e))

    def onStamp(self):
        doc = Krita.instance().activeDocument()
        try:
            core.stamp(
                doc, core.STATE,
                self.useSelectionCheck.isChecked(),
                self.destXSpin.value(), self.destYSpin.value(),
                self.featherSpin.value(), self.opacitySpin.value())
            self.statusLabel.setText("Stamped.")
        except core.ClonestampError as e:
            self.statusLabel.setText(str(e))
            self._warn(str(e))
        except Exception as e:
            self.statusLabel.setText("Unexpected error: {0}".format(e))
            self._warn(str(e))

    def _warn(self, message):
        window = Krita.instance().activeWindow()
        if window is not None and window.activeView() is not None:
            window.activeView().showFloatingMessage(message, QIcon(), 3000, 1)
