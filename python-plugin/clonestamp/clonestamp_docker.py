# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
"""UI/interaction layer for the Clone Stamp docker -- the part of the plugin
that talks to Krita's Qt widgets and turns mouse input into calls into
clonestamp_core.py (the pixel logic, kept separate and Qt-widget-free).

Feature map, for anyone new to this file:

- **Enable/disable** (`onEnableToggled` -> `_arm`/`_disarm`): finds Krita's
  canvas widget and installs a `QApplication`-global event filter on it.
  This is the plugin's whole reason for existing as a docker instead of a
  real toolbox tool -- Krita's Python scripting API (`libkis`) has no way to
  register a new `KisTool`, so this intercepts mouse events on the existing
  canvas widget from the outside instead.

- **Sample + paint** (`_onCanvasPress`/`_onCanvasRelease`/`_onTimerTick`):
  Ctrl+click samples a source point (`core.sample_source_point`); a plain
  click+drag paints (`core.begin_stroke`/`continue_stroke`/`finalize_stroke`
  in core.py). Only mouse Press/Release are real Qt events here -- a drag's
  continuous motion is read by polling `QCursor.pos()` on a 30ms `_timer`,
  because continuous mouse-move isn't exposed to Krita's scripting API at
  all (see `eventFilter`).

- **Live drag preview** (`_StrokeOverlay`, `_stampOverlayDab`): the real
  pixel write only happens once, at mouse-release, for a clean single-step
  undo (see core.finalize_stroke's docstring for why). This transparent
  overlay widget mirrors that in-progress write visually during the drag by
  stamping each dab into an offscreen pixmap -- no Krita API calls, so no
  extra lag or undo steps, and nothing is ever committed to the document
  from here.

- **Brush cursor** (`_updateBrushCursor`): the ring/crosshair/ghost-preview
  cursor shown while hovering or dragging, built as a custom `QCursor` from
  a hand-painted `QPixmap` (Krita's own native brush-outline cursor is
  toggled off for as long as this plugin is armed -- see
  `_toggleNativeBrushOutline` -- since it doesn't apply while a different
  tool, the one this plugin is layered on top of, is actually active).

- **Shift-drag resize** (`_onResizeTick`): drag horizontally for brush size,
  vertically for hardness, Photoshop-style. The ring cursor is switched off
  entirely for the duration (no live ring shown while resizing) and the
  real OS cursor is blanked and warped back to the drag's start point every
  tick so it neither moves nor flickers -- see `_onResizeTick`'s own
  comment for the full reasoning, including two earlier approaches that
  didn't work and why.

- **Canvas-change tracking** (`canvasChanged`): re-resolves the canvas
  widget and live-preview overlay whenever Krita's active canvas changes
  (new document, switched tabs, etc.) -- without this, the plugin keeps
  matching events against whatever canvas was active when it was enabled,
  and silently stops responding once that's no longer the visible one.

- **Self-update** (`_onCheckForUpdates`, delegates to clonestamp_update.py):
  checks the `main` branch on GitHub for a newer `core.VERSION` and, if
  found, downloads the current plugin files straight into this install.
"""

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
    # TEMPORARY diagnostic (forced, ignores the debug.enable gate) --
    # investigating a still-unresolved "doesn't work after opening a new
    # document" report. This logs every candidate QOpenGLWidget under the
    # central widget so we can tell whether Krita keeps one shared canvas
    # widget per window (in which case findChild's "first match" is
    # irrelevant) or one per open document/view (in which case findChild
    # always returning the *first-created* one, regardless of which is
    # actually active, would be the real bug). Remove once that's settled.
    candidates = central.findChildren(QOpenGLWidget)
    _debug("_find_canvas_widget: %d candidate(s): %s" % (
        len(candidates),
        [(id(w), w.isVisible()) for w in candidates]), force=True)
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
    """The plugin's whole UI and event-handling surface: a settings panel
    (Size/Hardness/Opacity/Aligned/Sample, registered with Krita as a
    dockable panel via __init__.py) plus, while "Enable Clone Brush" is
    checked, an event filter that turns canvas mouse events into clone-stamp
    strokes. See the module docstring above for the full feature map --
    this class is the one place all of those features are wired together.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Clonestamp Tool with Preview")

        self._canvas_widget = None
        self._overlay = None
        self._stroke_active = False
        self._resize_active = False
        self._resize_start_global = None
        self._resize_start_size = None
        # Also set on every Shift+drag start in _onCanvasPress; initialized
        # here too so no code path can ever see them as missing attributes.
        self._resize_start_hardness_pct = None
        self._resize_accum_dx = 0
        self._resize_accum_dy = 0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._onTimerTick)
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(200)
        self._hover_timer.timeout.connect(self._onHoverTick)
        # Coalesces bursts of Size/Hardness changes from the spin boxes and
        # slider (scroll-wheel and click-and-hold arrow-repeat can both fire
        # valueChanged much faster than a human drag ever would) into at
        # most one _updateBrushCursor() rebuild every 50ms, instead of doing
        # a full QPixmap rebuild + native setCursor() call -- expensive, and
        # visibly flickery on Windows -- for every single tick of the spin
        # box. See _onSizeChanged/_onHardnessChanged for where this is used;
        # _onHoverTick/_onTimerTick don't need it since those already run on
        # their own fixed-rate timers (200ms/30ms) rather than firing
        # unboundedly like a scrubbed UI control can.
        self._cursor_refresh_timer = QTimer(self)
        self._cursor_refresh_timer.setSingleShot(True)
        self._cursor_refresh_timer.setInterval(50)
        self._cursor_refresh_timer.timeout.connect(self._updateBrushCursor)
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
        # new document, switching tabs, closing a document, etc.
        #
        # CONFIRMED via kritacrash.log (2026-07-18 15:56:12): Krita can call
        # this *synchronously from inside a KisView's C++ destructor*. Full
        # stack: sipDockWidget::canvasChanged -> DockWidget::unsetCanvas ->
        # KoCanvasControllerWidget::unsetCanvas/setCanvas -> KisView::~KisView
        # -> ... -> an Access Violation surfaced inside python313.dll's own
        # call dispatch. Doing any widget-touching work here synchronously --
        # unsetCursor() on a canvas widget that may be mid-teardown,
        # setParent(None)/deleteLater() on overlays parented to it, even just
        # walking the widget tree in _find_canvas_widget() -- risks hitting
        # that same reentrancy hazard again: Python callbacks invoked from
        # arbitrary nested C++ destructor call stacks are a known-fragile
        # pattern in PyQt/sip. Deferred via QTimer.singleShot(0, ...) so the
        # actual logic below runs on a clean event-loop tick, after whatever
        # Qt/C++ teardown triggered this callback has actually finished,
        # instead of nested inside it.
        QTimer.singleShot(0, self._onCanvasChangedDeferred)

    def _onCanvasChangedDeferred(self):
        # Our own _canvas_widget was only ever resolved once, in _arm(), so
        # without handling canvasChanged the event filter kept matching
        # mouse events against whichever canvas widget was active back when
        # the brush was enabled -- once a second document existed, that
        # widget was no longer the visible/active one, so the tool silently
        # stopped responding on the new document.
        #
        # A first attempt re-resolved the canvas widget and reparented the
        # overlays onto it in place, to keep the brush armed across the
        # switch -- that still didn't reliably work in practice (and, per
        # the crash above, was doing exactly the kind of widget surgery most
        # likely to crash). Simpler and more robust, per explicit user
        # request: just turn the brush off whenever the active canvas
        # changes out from under it, rather than trying to follow it.
        # Re-enable manually on the new document.
        #
        # TEMPORARY diagnostic lines (force=True) below -- kept until the
        # "doesn't work after opening a new document" report is confirmed
        # fixed; see _find_canvas_widget for the matching log.
        _debug("canvasChanged(deferred): fired enabled=%s tracked_id=%s" % (
            self.enableCheck.isChecked(),
            id(self._canvas_widget) if self._canvas_widget is not None else None),
            force=True)
        if not self.enableCheck.isChecked() or self._canvas_widget is None:
            return
        widget = _find_canvas_widget()
        _debug("canvasChanged(deferred): resolved_id=%s match=%s" % (
            id(widget) if widget is not None else None,
            widget is self._canvas_widget), force=True)
        if widget is None or widget is self._canvas_widget:
            return
        self.enableCheck.setChecked(False)  # triggers onEnableToggled -> _disarm()
        core.STATE.clear_source()
        self.liveSourceLabel.setText("Source: none")
        self.brushStatusLabel.setText(
            "Clone Brush disabled: active document changed.")

    def closeEvent(self, event):
        self._disarm()
        super().closeEvent(event)

    # --- brush settings ----------------------------------------------------

    def _onSizeChanged(self, value):
        core.STATE.brush_size = value
        # Throttled, not immediate -- see _cursor_refresh_timer in __init__
        # for why a direct _updateBrushCursor() call here flickers when this
        # fires rapidly (scroll-wheel/arrow-repeat on the spin box).
        self._cursor_refresh_timer.start()

    def _onHardnessChanged(self, value):
        core.STATE.brush_hardness = value / 100.0
        self.hardnessSlider.blockSignals(True)
        self.hardnessSlider.setValue(value)
        self.hardnessSlider.blockSignals(False)
        self.hardnessSpin.blockSignals(True)
        self.hardnessSpin.setValue(value)
        self.hardnessSpin.blockSignals(False)
        self._cursor_refresh_timer.start()

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
        # TEMPORARY diagnostic (force=True) -- logs every MouseButtonPress
        # app-wide, before the widget-match check below, specifically to
        # see whether a press on a newly-opened document's canvas even
        # matches self._canvas_widget at all. Remove once the "doesn't work
        # after opening a new document" report is root-caused; see the
        # matching logs in _find_canvas_widget/canvasChanged.
        if event.type() == QEvent.MouseButtonPress:
            _debug("eventFilter: PRESS obj=%s id=%s tracked_id=%s match=%s" % (
                obj.__class__.__name__, id(obj),
                id(self._canvas_widget) if self._canvas_widget is not None else None,
                obj is self._canvas_widget), force=True)

        if obj is not self._canvas_widget:
            return False

        et = event.type()
        try:
            if et == QEvent.MouseButtonPress:
                return self._onCanvasPress(event)
            elif et == QEvent.MouseButtonRelease:
                return self._onCanvasRelease(event)
            elif et == QEvent.MouseMove:
                # Swallowed for the entire time the plugin is armed, not
                # just during a resize drag -- previously only the resize
                # case was covered, but the same mechanism applies whenever
                # the mouse is simply hovering or painting: Krita's own
                # underlying tool (whatever's selected in the toolbox
                # underneath ours) still saw every real MouseMove and kept
                # re-asserting its own cursor -- e.g. the selected brush
                # preset's size circle -- over ours, which is what a
                # "second circle" report turned out to be. None of our own
                # tracking (hover/drag/resize) needs these events either;
                # it's all timer-driven, polling QCursor.pos() directly.
                return True
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
            # Blanked (not just unset) and warped back to this exact spot
            # every tick for the whole drag -- see _onResizeTick for why
            # that combination finally holds without flicker this time.
            self._canvas_widget.setCursor(Qt.BlankCursor)
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
        # The OS cursor is blanked (see _onCanvasPress) *and* warped back to
        # the anchor every tick below -- while previous attempts treated
        # those as alternatives (pick one), the actual fix is both together:
        # blanked means the warp itself is invisible (nothing is rendered at
        # the cursor position to see, so repositioning it 33x/sec causes no
        # visible flicker), and the warp is still needed even though it's
        # invisible -- without it a long drag would run the real OS cursor
        # into a screen edge and clamp, silently capping how far you can
        # drag. eventFilter also swallows MouseMove for the whole drag now,
        # which is what actually made the earlier blank-cursor attempt fail:
        # without that, Krita's own underlying tool still saw every move and
        # kept re-asserting its own visible cursor on top of the blanked
        # one. Deltas are accumulated across ticks (each tick's delta is
        # measured from the anchor we just warped back to) rather than read
        # as one absolute offset, matching the warp.
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
        # 0.2s = the hover timer's own 200ms interval: that 5Hz cadence is
        # the refresh cost this project has already run at without lag, so
        # the cache reuses it as the refresh budget rather than inventing a
        # new number.
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
