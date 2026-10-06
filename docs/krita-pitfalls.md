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
Windows locks a loaded DLL against overwrite but allows rename/move. In the plugin's own
pykrita folder (`clonestamp_tool\lib\`) renaming the old file aside is fine -- the loader only
loads the exact file name. Never do that inside `lib\kritaplugins` (see next entry). Restart
Krita afterwards. Evidence: 2026-10-06 deploys into `%APPDATA%\krita\pykrita\clonestamp_tool\lib`.

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

### Krita loads EVERY file in lib/kritaplugins, whatever its extension
A DLL renamed to `kritatoolclonestamp.dll.disabled` (or `.old123`) inside `kritaplugins` is
still loaded and registers its tool -- an old copy then wins over a new one (registration is
first-come). To replace a locked DLL, MOVE it out of the folder (e.g. to %TEMP%, same volume),
never rename it in place. Disable a plugin by moving it out, not by renaming.
Evidence: 2026-10-06, `(Get-Process krita).Modules` listed
`...\kritaplugins\kritatoolclonestamp.dll.disabled` as loaded; Alt-click option missing in UI.

### Plain Alt+left click reaches the tool
Krita's default canvas input profile binds Ctrl+click, Shift+drag, Ctrl+Alt+click, but not plain
Alt+left click, so it arrives in `beginPrimaryAction` with `Qt::AltModifier` set -- usable for
tool-specific gestures (Clone Stamp: Alt+click sets the source).
Evidence: `krita/data/input/kritadefault.profile`; clone_test.py ALT=True PASS (U8, U16).

### Undo for pixel changes: one KisTransaction per stroke
Wrap direct pixel writes (`writeBytes`, `KisPainter`, `copyAreaOptimized`) in a `KisTransaction`
created on the layer's paint device at stroke start; `commit(image()->undoAdapter())` at stroke
end gives exactly one undo step, however many dabs were written in between.
Evidence: `KisToolCloneStamp::beginStroke` / `endPrimaryAction` (krita-clonestamp v2.x).

### Copy pixels through a mask in any bit depth
`KisPainter gc(dst, selection); gc.setCompositeOpId(COMPOSITE_OVER); gc.setOpacityF(o);
gc.bitBlt(dstPt, srcDevice, srcRect);` -- the selection (alpha mask, `KisSelection` /
`pixelSelection()->writeBytes`) masks the copy, KisPainter converts color spaces and works for
8/16-bit integer and float. Restore an area first with `KisPainter::copyAreaOptimized`.
Evidence: `KisToolCloneStamp::compositeLive`; clone_test.py PASS for U8/U16/F32.

### Tools registered after startup get no toolbox button
When the tool DLL is loaded via the ctypes loader (Python plugin `clonestamp_tool`), the tool
action is registered but the toolbox has **no button** (measured: 0 buttons with objectName
"KritaShape/KisToolCloneStamp"). Cause: `KoToolManager` builds its `toolActionList` once from
`KoToolRegistry` before Python plugins run; `KoToolBox` (`libs/ui/toolbox/KoToolBox.cpp:105`)
creates its buttons from that list. Fix: `load_clonestamp_plugin()` calls `injectToolboxAction()`,
which grabs `KoToolManager::instance()->priv()` and appends a `new KoToolAction(factory)` to
`Private::toolActionList` -- but only if no action with id "KritaShape/KisToolCloneStamp" is present
yet (duplicate guard). Same approach as Acly's krita-vision-tools (`injectTools`). Needs
`KoToolManager_p.h` (Krita source tree, not installed); `priv()`, `KoToolAction(KoToolFactoryBase*)`
and `KoToolAction::id()` are exported from `libkritaflake.dll` of Krita 6.0.4 (checked with
llvm-readobj). First found by Qwen (fix.md).
Evidence: `ClonestampToolPlugin.cpp` injectToolboxAction; `KoToolBox.cpp:105`; measured 0 buttons 2026-10-06.

### Plugin DLLs in pykrita folders: load a copy, not the original
Re-importing/updating the plugin zip while Krita runs failed: Krita's importer
(`plugin_importer.py`, `extract_module`) does `shutil.rmtree` on the old plugin folder, but the
already-loaded DLL in `clonestamp_tool/lib` is locked by Windows -> `PermissionError [WinError 5]`
(reproduced). Fix: the loader never loads the file inside the plugin folder but a copy at
`%LOCALAPPDATA%\clonestamp_tool\kritatoolclonestamp-<sha1-12>.dll` (`_shadow_copy` in
`Tool-plugin/pykrita-loader/clonestamp_tool/__init__.py`); stale copies are deleted once no longer
locked. The update takes effect after a Krita restart.
Evidence: `plugin_importer.py` extract_module rmtree; PermissionError reproduced 2026-10-06; loader `_shadow_copy`.

### Plugin zip for Import Python Plugin needs directory entries
Krita's importer (`plugins/python/plugin_importer/plugin_importer.py`, `get_source_module`) finds
the module only via an explicit `"<name>/"` directory entry plus `"<name>/__init__.py"` in the zip.
Python's `zipfile` writes no directory entries by itself, so such a zip unpacks fine but Krita reports
"No plugins found in archive". Write the entries (`z.writestr("<name>/", "")`) and verify every
release zip with Krita's own importer code (`tools/check_plugin_zip.py`).
Evidence: v2.0.0/v2.1.0 zips failed on the user's server; check_plugin_zip.py FAIL→OK 2026-10-06.

### PluginImporter leaves the zip file open
`PluginImporter(...).import_all()` keeps `self.archive` (a ZipFile) open; deleting the zip right
after (e.g. a TemporaryDirectory) fails with WinError 32. Call `importer.archive.close()` yourself.
Evidence: `updater.py install()`, PermissionError reproduced 2026-10-06.

### Updating over an install that locks its DLL cannot be fixed from the new zip
The importer `rmtree`s the old plugin folder before any code of the new zip runs, so if the
installed version loaded its DLL straight from that folder (clonestamp v2.1.1 and older), the import
fails while Krita runs. Only manual steps help: close Krita, delete the plugin folder (or disable the
plugin and restart), then import. Design loaders to load a copy from the start.
Evidence: user's server, 2026-10-06 (`C:/Users/blasa/.../clonestamp_tool/lib/kritatoolclonestamp.dll`).
