# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>

import time
import traceback
from krita import DockWidget, Krita
from PyQt5.QtCore import QEvent, QPoint, QPointF, QRect, QTimer, Qt, QUrl
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
    """Finds the QOpenGLWidget for the currently active canvas. Krita's
    central widget can hold more than one of these at once (one per open
    document tab/subwindow, with only the active one actually visible), so
    picking the first match via findChild() is unreliable -- it can grab a
    background tab's (hidden) canvas instead of the active one, leaving the
    real on-screen canvas never getting our cursor/event-filter wiring.
    Prefer whichever candidate is actually visible; fall back to the first
    match if visibility can't disambiguate (e.g. only one candidate)."""
    window = Krita.instance().activeWindow()
    if window is None:
        return None
    qwin = window.qwindow()
    if qwin is None:
        return None
    central = qwin.centralWidget()
    if central is None:
        return None
    candidates = central.findChildren(QOpenGLWidget)
    for widget in candidates:
        if widget.isVisible():
            return widget
    # Only fall back to a guess when there's nothing to disambiguate between
    # (a single candidate, e.g. transiently not-yet-shown or the window is
    # minimized) -- with multiple candidates and none visible, guessing
    # candidates[0] would reintroduce the exact "wrong hidden canvas gets
    # wired up" bug this visibility check exists to avoid. Callers already
    # handle None reasonably (canvasChanged/self-heal just skip the rebind
    # for now; _arm() reports it and leaves the tool disabled).
    return candidates[0] if len(candidates) == 1 else None


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


class StrokePreviewOverlay(QWidget):
    """Transparent overlay showing the in-progress stroke result live during
    a drag. Krita's Python scripting API has no undo-macro grouping (see
    docs/CLAUDE.md), so continue_stroke() only paints into an in-memory
    accumulator while dragging and the real layer isn't touched until
    release (one setPixelData() call in finalize_stroke = one undo step).
    That means the canvas itself doesn't visibly change mid-drag -- this
    widget renders what finalize_stroke *would* currently write, purely as a
    screen-space visual on top of the canvas, so nothing here ever touches
    document pixels or the undo stack."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self._image = None
        self._target_rect = None

    def setPreview(self, image, target_rect):
        """target_rect is in this widget's own local coordinates (not
        screen/global) -- the caller keeps the widget's own OS-level window
        geometry fixed (covering the whole canvas) and only moves where
        *within* it the image is drawn. Resizing/moving the actual top-level
        window on every drag tick was the previous approach, and caused
        visible flicker on Windows: a translucent frameless window being
        natively resized shows a blank frame from the OS compositor before
        Qt's own repaint lands."""
        self._image = image
        self._target_rect = target_rect
        self.update()

    def paintEvent(self, event):
        if self._image is None or self._target_rect is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(self._target_rect, self._image)
        painter.end()


class ClonestampDocker(DockWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Clonestamp Tool with Preview")

        self._canvas_widget = None
        self._stroke_active = False
        # The document and canvas a stroke is painting into, pinned at press
        # time and reused for the whole stroke (continue_stroke ticks +
        # release) instead of re-fetching Krita.instance().activeDocument()/
        # _currentCanvas() at each step -- otherwise switching the active
        # document mid-drag (e.g. Ctrl+Tab while still holding the mouse
        # button) would silently finish compositing/writing into a different
        # document than the one the accumulator's rect math was built
        # against, using that other document's zoom/pan for the coordinate
        # mapping to boot. canvasChanged() defers rebinding self._canvas_widget
        # itself while a stroke is active for the same reason (see there).
        self._stroke_doc = None
        self._stroke_canvas = None
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

        # Live on-canvas preview of the in-progress stroke (see
        # StrokePreviewOverlay) -- recomputed at this throttled cadence
        # rather than every ~16ms tick, since it involves a real
        # dst_node.pixelData() read over the (growing) accumulator bounds.
        # 100ms/10Hz mirrors the same "expensive work throttled, cheap
        # position work every tick" split already validated for the cursor
        # content preview (_preview_cache_*) at 200ms/5Hz, just a bit faster
        # since a painted result reads as laggier than a hover-only preview.
        self._stroke_preview = StrokePreviewOverlay()
        self._stroke_preview_time = 0.0
        self._STROKE_PREVIEW_INTERVAL = 0.1

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

        # Krita's own "newOutlineStyle" setting value saved across an
        # arm/disarm cycle -- see _setNativeBrushOutlineHidden.
        self._native_outline_saved = None

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
        """Krita calls this whenever the active canvas changes -- switching
        document tabs, opening a new document, switching windows. Without
        this, self._canvas_widget (captured once in _arm()) would keep
        pointing at whatever canvas was active at arm time: our event filter
        checks `obj is not self._canvas_widget`, so clicks on a newly active
        canvas would silently fall through to Krita's native tool instead of
        Clone Stamp, needing a manual disable/re-enable of the checkbox to
        "reset" and re-resolve it. Rebinding here keeps it live instead.

        This notification isn't the only path that rebinds, though -- see
        eventFilter()'s self-heal branch for why a brand new document (as
        opposed to switching between already-open ones) can't fully rely on
        this firing at the right time."""
        if not self.enableCheck.isChecked():
            return
        new_widget = _find_canvas_widget()
        if new_widget is None or new_widget is self._canvas_widget:
            return
        self._rebindCanvasWidget(new_widget)

    def _rebindCanvasWidget(self, new_widget):
        """Switches self._canvas_widget to new_widget and refreshes the
        cursor/crosshair state to match -- shared by canvasChanged() (Krita's
        own notification) and eventFilter()'s self-heal path. Returns
        whether it actually rebound (callers that need the rebind to have
        happened before proceeding, e.g. eventFilter's self-heal, must check
        this rather than assume success)."""
        if self._stroke_active or self._resize_active:
            # Defer: an in-progress stroke/resize pins self._stroke_doc /
            # self._stroke_canvas (see __init__) and keeps polling
            # self._canvas_widget every tick via those pinned references.
            # Rebinding mid-operation would desync self._canvas_widget's
            # screen geometry from the still-pinned canvas/document, feeding
            # continue_stroke doc-space coordinates mapped through one
            # document's canvas while writing into another. The next
            # canvasChanged (or the next click, via self-heal) picks this up
            # once the operation naturally ends at release.
            return False
        self._canvas_widget = new_widget
        self._last_cursor_key = None
        self._last_cursor_pixmap = None
        self._updateBrushCursor()
        c = self._currentCanvas()
        if c:
            self._last_cursor_zoom = c.zoomLevel()
        self._last_cursor_size = core.STATE.brush_size
        if core.STATE.has_point_source:
            self._ch_timer.start()
            self._positionCrosshair()
        else:
            self._crosshair.hide()
        return True

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
        if QApplication.instance().applicationState() != Qt.ApplicationActive:
            # The crosshair is an always-on-top OS-level overlay
            # (WindowStaysOnTopHint) positioned in absolute screen
            # coordinates, not clipped to Krita's own window -- without this
            # check it keeps floating visibly on top of whatever other
            # application the user switched to instead of hiding along with
            # Krita.
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
        self._setNativeBrushOutlineHidden(True)
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
        self._stroke_preview.hide()
        if self._canvas_widget is not None:
            # Best-effort: the canvas widget may already be a dead Qt object
            # here (e.g. its document/view was closed while Clone Brush was
            # still armed). Without the try/except, an exception from either
            # call below would skip _setNativeBrushOutlineHidden(False) next
            # -- and since that's a persisted kritarc setting, not in-memory
            # Krita state, skipping it leaves Krita's own brush-outline
            # circle hidden even after quitting and relaunching Krita.
            try:
                QApplication.instance().removeEventFilter(self)
                self._canvas_widget.unsetCursor()
            except Exception as e:
                _debug("_disarm: canvas widget cleanup failed: %s" % e, force=True)
        self._setNativeBrushOutlineHidden(False)
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

    def _setNativeBrushOutlineHidden(self, hidden):
        """Forces (or restores) Krita's own brush-outline circle -- the
        round cursor overlay the underlying native tool (e.g. Freehand
        Brush) still draws, since we only intercept mouse events and never
        actually change the active KisTool. It's a canvas-level paint
        overlay, not a QCursor, so our own setCursor()/unsetCursor() calls
        have no effect on it, and it would otherwise show doubled up with
        our own ring/dashed-hardness circles.

        This reads/writes the "newOutlineStyle" kritarc setting directly
        (0 = OUTLINE_NONE; see enum OutlineStyle in
        libs/global/kis_global.h in the Krita source) via
        Krita.readSetting/writeSetting, rather than triggering Krita's own
        "toggle_brush_outline" action. That action *flips* relative to
        whatever state the setting is already in, which assumes it starts
        visible -- on a machine where the user already had it off by
        default (or had toggled it off themselves before ever enabling
        Clone Brush), that flip turned the native circle ON instead of
        hiding it, which is how it ended up showing a third circle
        alongside our own two. Saving the exact previous value and
        restoring it verbatim on disarm sidesteps that regardless of
        starting state. Krita reads this config fresh on each paint/cursor
        update (KisConfig wraps the same shared KSharedConfig instance
        Krita.writeSetting() writes into), so this takes effect immediately
        with no canvas refresh or restart needed."""
        app = Krita.instance()
        if hidden:
            self._native_outline_saved = app.readSetting("", "newOutlineStyle", "2")
            app.writeSetting("", "newOutlineStyle", "0")
        elif self._native_outline_saved is not None:
            app.writeSetting("", "newOutlineStyle", self._native_outline_saved)
            self._native_outline_saved = None

    # --- event filter: only press/release are used; drag is timer-driven --

    def eventFilter(self, obj, event):
        et = event.type()
        if obj is not self._canvas_widget:
            # Self-heal against canvasChanged() lagging or being skipped for
            # some transitions -- observed: File > New can leave Clone Brush
            # enabled but inert, still bound to the previous document's now-
            # inactive canvas widget, because Krita's canvasChanged
            # notification for a brand-new canvas isn't as reliably timed as
            # for switching between already-open documents (see
            # canvasChanged()/_rebindCanvasWidget for the normal path). If
            # this press/release actually lands on what _find_canvas_widget()
            # currently resolves as the active canvas, rebind here instead of
            # silently dropping the event -- cheap, since it only runs on the
            # rare event that arrives for a widget we're not already bound to.
            if not (self.enableCheck.isChecked()
                    and et in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease)
                    and isinstance(obj, QOpenGLWidget)
                    and _find_canvas_widget() is obj
                    and self._rebindCanvasWidget(obj)):
                # Either this isn't a plausible rebind candidate, or
                # _rebindCanvasWidget deferred because a stroke/resize is
                # still active on the widget we were already bound to (see
                # there) -- don't fall through and process this event
                # against a widget we didn't actually rebind to.
                return False

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
                # Pinned for the whole stroke (see the field comments in
                # __init__) instead of re-fetching activeDocument()/
                # _currentCanvas() on every tick/at release.
                self._stroke_doc = doc
                self._stroke_canvas = canvas
                self._tick_counter = 0
                self._crosshair.hide()
                self._ch_timer.stop()
                # Reset the throttle so the first tick shows a preview right
                # away instead of waiting out a stale interval left over
                # from the previous stroke.
                self._stroke_preview_time = 0.0
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
            # The document pinned at press time (see self._stroke_doc), not
            # Krita.instance().activeDocument() -- same reasoning as
            # _onTimerTick.
            doc = self._stroke_doc
            try:
                if doc is not None:
                    result = core.finalize_stroke(doc, core.STATE)
                    if result is not None:
                        self.brushStatusLabel.setText(
                            "Stroke painted at ({0}, {1})".format(result.x(), result.y()))
            except core.ClonestampError as e:
                # finalize_stroke can still raise (e.g. the source layer's
                # type/color space changed mid-drag) -- without this, every
                # line below would be skipped: end_stroke never runs, the
                # timer/stroke_active flags are left stuck (so hover
                # movement alone starts getting treated as an active
                # stroke), and the preview overlay is left visibly pinned on
                # screen showing paint that was never actually written.
                self.brushStatusLabel.setText(str(e))
                self._warn(str(e))
            finally:
                core.end_stroke(core.STATE)
                core.STATE.clear_accumulator()
                self._stroke_active = False
                self._stroke_doc = None
                self._stroke_canvas = None
                # The real layer now shows the finalized result
                # (finalize_stroke just wrote it), so the visual-only
                # preview overlay would either double-render it or show a
                # stale frame -- hide it.
                self._stroke_preview.hide()
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
        # Pinned at press time (see self._stroke_canvas), not re-fetched --
        # see the comment there for why.
        canvas = self._stroke_canvas
        if canvas is None or not core.coordinate_mapping_reliable(canvas):
            return
        doc_point = core.map_widget_to_document(canvas, self._canvas_widget, QPointF(local_pos))
        if doc_point is None:
            return
        zoom = canvas.zoomLevel()

        # The document the stroke began on (see self._stroke_doc), not
        # Krita.instance().activeDocument() -- if the active document
        # changed mid-drag (e.g. Ctrl+Tab while still holding the mouse
        # button), re-fetching here would feed this stroke's accumulator
        # doc-space coordinates computed against a different document's
        # canvas/zoom, and finalize_stroke would go on to composite/write
        # into the wrong document entirely.
        doc = self._stroke_doc
        try:
            result = core.continue_stroke(doc, core.STATE, doc_point)
            if result is None and self.brushStatusLabel.text() == "":
                self.brushStatusLabel.setText("Painting...")
        except core.ClonestampError as e:
            self.brushStatusLabel.setText(str(e))
            self._stroke_active = False
            return

        self._updateStrokePreview(doc, canvas, now)

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

    def _updateStrokePreview(self, doc, canvas, now):
        """Refreshes the live stroke-preview overlay (see
        StrokePreviewOverlay) at a throttled cadence -- see
        self._STROKE_PREVIEW_INTERVAL for why. A no-op if called again
        before that interval elapses."""
        if now - self._stroke_preview_time < self._STROKE_PREVIEW_INTERVAL:
            return
        self._stroke_preview_time = now
        if doc is None or canvas is None or not core.coordinate_mapping_reliable(canvas):
            return
        if QApplication.instance().applicationState() != Qt.ApplicationActive:
            # Same always-on-top-overlay concern as _positionCrosshair --
            # without this, dragging and then alt-tabbing away mid-stroke
            # would leave the painted preview floating over whatever other
            # application now has focus.
            self._stroke_preview.hide()
            return
        try:
            result = core.preview_stroke_composite(doc, core.STATE)
        except core.ClonestampError:
            # Read-only despite the name (see preview_stroke_composite's
            # docstring), but it still runs the same validation as
            # finalize_stroke (paint-layer/color-space/locked checks) --
            # unlike continue_stroke's identical exception a few lines
            # above, this one isn't fatal to the stroke (the accumulator
            # itself is unaffected), so just skip this frame's preview
            # rather than aborting the drag.
            self._stroke_preview.hide()
            return
        if result is None:
            self._stroke_preview.hide()
            return
        dst_rect, image = result

        # Keep the overlay's own OS-level window pinned to the canvas
        # widget's screen rect and only touch it (one native move+resize)
        # when that rect actually changed -- e.g. the Krita window itself
        # moved/resized mid-drag, which is rare. Under normal painting this
        # reduces to a no-op every tick, which is what actually eliminates
        # the flicker (see StrokePreviewOverlay.setPreview): the drawn
        # *content* still moves every tick via a plain repaint, cheap and
        # flicker-free since it never touches the native window geometry.
        canvas_origin = self._canvas_widget.mapToGlobal(QPoint(0, 0))
        canvas_geo = QRect(canvas_origin, self._canvas_widget.size())
        if self._stroke_preview.geometry() != canvas_geo:
            self._stroke_preview.setGeometry(canvas_geo)

        zoom = canvas.zoomLevel()
        pref = canvas.preferredCenter()
        cw = self._canvas_widget.width()
        ch = self._canvas_widget.height()
        lx = (dst_rect.x() - pref.x()) * zoom + cw / 2.0
        ly = (dst_rect.y() - pref.y()) * zoom + ch / 2.0
        lw = max(1, int(round(dst_rect.width() * zoom)))
        lh = max(1, int(round(dst_rect.height() * zoom)))
        target_rect = QRect(int(round(lx)), int(round(ly)), lw, lh)

        self._stroke_preview.setPreview(image, target_rect)
        if not self._stroke_preview.isVisible():
            self._stroke_preview.show()

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
