# Krita plugin development – verified pitfalls

Each entry has evidence (file:line, crash log or reproduced test). Ranked first by krita-rag.
Krita source reference: v6.0.4.1 (`D:\_Code\Krita\krita-src`).

### Tool gestures: Ctrl+click and Shift+drag never reach beginPrimaryAction
KisInputManager routes Ctrl+Left click to `beginAlternateAction(event, SampleFgImage)` and
Shift+Left drag to `beginAlternateAction(event, ChangeSize)`. Checking `event->modifiers()` in
`beginPrimaryAction` never fires. Override `begin/continue/endAlternateAction`.
Evidence: `libs/ui/tool/kis_tool.h` AlternateAction enum comments; `libs/ui/input/kis_change_primary_setting_action.cpp`;
krita-clonestamp docs/toolchain-paths.md "Phase C follow-up".

### Tool outline: use updateCanvasToolOutlineDoc, not canvas()->updateCanvas
`KisCanvas2::updateCanvas(rect)` marks projection AND overlay dirty and goes through the update
compressor (delayed) – the cursor trails the mouse. `updateCanvasToolOutlineDoc(docRect)` updates
only the overlay and repaints immediately (`slotDoCanvasUpdate`). Also enlarge the rect along the
move vector ("update-ahead") or the outline tears (KDE bug 476300).
Evidence: `libs/ui/canvas/kis_canvas2.cpp` updateCanvasWidgetImpl vs updateCanvasToolOutlineWdg;
`libs/ui/tool/kis_tool_paint.cc` requestUpdateOutline (~line 660-705).

### Draw outlines with KisTool::paintToolOutline (GPU path)
`paintToolOutline(&gc, pixelToView(KisOptimizedBrushOutline(path)))` renders via
KisOpenGLCanvas2 natively, same look as the brush outline. QPainter decorations are slower.
Evidence: `libs/ui/tool/kis_tool.cc` KisTool::paintToolOutline.

### Do not assume 8-bit RGBA paint devices
Documents can be U8/U16/F16/F32, RGBA/CMYK/Lab… Reading with readBytes into QImage::Format_ARGB32
only works for RGBA U8 (a 16-bit V-Ray render could not be sampled). Use KisPainter::bitBlt
(converts color spaces), KisPainter::copyAreaOptimized, KisSelection masks; convert to QImage
only for display (`KisPaintDevice::convertToQImage(nullptr, rect)`).
Evidence: clonestamp tool 2026-10-06, tests/clone_test.py PASS for U8/U16/F32 after rework.

### Cheap snapshots: KisPaintDevice copy constructor is copy-on-write
`new KisPaintDevice(*device)` copies tile references only; tiles duplicate when the original
changes. Use it for "original pixels" / frozen-source snapshots instead of reading whole images.
Evidence: `libs/image/kis_paint_device.h` copy ctor (KritaUtils::CopySnapshot).

### User feedback from a tool
`qobject_cast<KisCanvas2*>(canvas())->viewManager()->showFloatingMessage(text, QIcon())`.
Silent early returns look like "the tool is broken".
Evidence: `libs/ui/KisViewManager.h:177`.

### Binary compatibility with the official Windows build
A tool DLL loads into the official Krita only if built with the same Qt and deps. Krita 6.0.4/6.0.4.1
use deps branch `transition.now/qt6.8.0` (Qt 6.8.0); `transition.now/qt6` is Qt 6.11 and its DLLs
will not load. Toolchain llvm-mingw 20251118 (clang 21), CMake 3.31.x (not 4.x),
`-DBUILD_WITH_QT6=ON -DALLOW_UNSTABLE=QT6`. Verify with llvm-readobj imports vs installed exports.
Evidence: krita-src `build-tools/ci-scripts/windows.yml`; commit 5abff03 "Switch Qt6 branch back to
transition.now/qt6.8.0"; `D:\_Code\Krita\dev\build-krita.bat`.

### Replacing a loaded plugin DLL
Windows locks loaded DLLs against overwrite but allows rename: rename to `.old`, copy new, restart
Krita. Program Files needs elevation. Evidence: `D:\_Code\Krita\dev\deploy-tool.cmd`.

### Tool icons from a plugin
`setIconName(koIconNameCStr("name"))` resolves `:/pics/dark_name.svg` / `:/pics/light_name.svg`
from Qt resources (`dark_` = dark glyph #373737 for light themes, `light_` = #d2d2d2).
Embed via `qt_add_resources(<target>_SOURCES file.qrc)`.
Evidence: `libs/widgetutils/kis_icon_utils.cpp:37-54`.

### Python (pykrita) on Krita 6: PyQt6 only
Importing PyQt5 raises "This version of Krita is not compatible with PyQt5!". PyQt6 needs scoped
enums (`Qt.MouseButton.LeftButton`), `QOpenGLWidget` is in `PyQt6.QtOpenGLWidgets`.
Evidence: tested via kritamcp bridge 2026-10-06.

### GUI tests: synthetic Qt mouse events do not paint
`QApplication.sendEvent(canvas, QMouseEvent)` reaches the widget but KisInputManager starts no
stroke (also not for the normal brush). Use real OS input (SetCursorPos/mouse_event) and read view
transforms fresh (a new view settles zoom after some event rounds). Bring Krita up with
`SetWindowPos(HWND_TOPMOST)` + one click on the status bar (activation click), not with an Alt tap:
the Alt trick left a "running" shortcut and the next `addView` crashed in
`KisChangePrimarySettingAction::end -> KoToolProxy::canvas` (null). Do not click the title bar
(hit the window icon, minimized Krita). Tests fail if the user moves the mouse meanwhile.
Evidence: `D:\_Code\Krita	ools\krita_mcp	ests
Evidence: `D:/_Code/Krita/tools/krita_mcp/tests/realinput.py`, brush_tests.py 6/6 (2026-10-06);
kritacrash.log 2026-10-06 12:05 and 12:27.
