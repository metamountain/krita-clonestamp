# Project guide — Krita Clone Stamp

Orientation for development sessions (human or AI-assisted). This file states
what is true *now* and the rules that keep the project consistent; history is
in the files listed at the end.

## What this project is

A Photoshop-style Clone Stamp for Krita as a **native C++ tool** (`KisTool` +
`KoToolFactoryBase`). **C++ only** — the old Python plugin is frozen on branch
`krita-5` (release v1.0.2-krita5, not maintained) and must not be revived on
`main`.

Users get exactly **one zip per supported Krita version**
(`clonestamp_tool-krita-<x.y.z>-windows-x64.zip`, currently 6.0.4), installed via
*Tools › Scripts › Import Python Plugin from File*. The zip holds a small
Python loader (`clonestamp_tool`) plus the DLL. No other install route
(no install.cmd, no copying into the Krita install).

| Path | Contents |
| --- | --- |
| `Tool-plugin/` | The tool: `KisToolCloneStamp.cpp/.h`, `ClonestampToolPlugin.cpp`, icons + `.qrc`, `CMakeLists.txt`, `NOTE.md` (build recipe) |
| `Tool-plugin/pykrita-loader/` | Loader plugin (`clonestamp_tool/__init__.py`, `updater.py`, `.desktop`) and `make_zip.py` |
| `Tool-plugin/windows/build-krita.bat` | Configure + build the tool DLL against Krita 6.0.4 |
| `release/krita-6.0.4-windows-x64/` | The released zip |
| `tools/check_plugin_zip.py` | Runs Krita's own importer code on a zip |
| `tools/krita_mcp/` | MCP bridge into a running Krita + GUI tests |
| `docs/krita-pitfalls.md` | Verified Krita API pitfalls with evidence — read before changing tool or loader |

## Machine layout (Windows)

| Location | Contents |
| --- | --- |
| `D:\_Code\Krita\krita-src\` | Krita source, tag `v6.0.4.1`; the tool builds as `plugins\tools\tool_clonestamp\` (copy changed files there from `Tool-plugin/`) |
| `D:\_Code\Krita\dev\` | Build env: `env68\` (deps branch `transition.now/qt6.8.0` = Qt 6.8), `cmake-3.31.8` (not 4.x), `ninja`, llvm-mingw 20251118, `build68\` (build tree) |
| `%APPDATA%\krita\pykrita\clonestamp_tool\` | Installed tool (loader + `lib\kritatoolclonestamp.dll`) |
| `%LOCALAPPDATA%\clonestamp_tool\` | Cached DLL copy the loader actually loads (`kritatoolclonestamp-<sha1>.dll`) |
| `C:\Program Files\Krita (x64)\` | Official Krita 6.0.4 — never put clonestamp files into `lib\kritaplugins\` (Krita loads every file there, any extension) |
| `D:\_Code\Krita\krita-rag\` | Krita RAG (MCP `krita-rag`: `search_krita`, `get_krita_symbol`, …) over Krita headers, plugin sources and the pitfalls |

## Hard rules

1. **`main` is the single line of development**; `krita-6.0.4` mirrors it.
   Commits may also come from the GitHub web UI — fetch/rebase before pushing,
   never force-push `main`.
2. **Bump `VERSION`** in `Tool-plugin/pykrita-loader/clonestamp_tool/__init__.py`
   for every release; it must equal the release tag (`v2.1.3` ↔ `"2.1.3"`).
   The update menu compares it with GitHub's latest release.
3. **One zip per Krita version.** Build it with `make_zip.py` (it writes the
   directory entries Krita's importer needs — without them: "No plugins found
   in archive") and check it with `tools/check_plugin_zip.py` before
   releasing. When a new release replaces an old one for the same Krita
   version, delete the old GitHub release (keep the tag).
4. **A DLL is binary-locked to one Krita build.** Build against the exact deps
   and tag of the official release; then compare the DLL's imports with the
   installed Krita's exports (`llvm-readobj --coff-imports` / `--coff-exports`,
   nothing may be missing). The loader refuses other Krita versions.
5. **GUI tests drive the real mouse** (synthetic Qt mouse events don't paint in
   Krita): run them only with the user's explicit OK, Krita visible in the
   foreground, user away from the mouse. The harness refuses to click when
   Krita is minimized. The first 1–2 runs after a Krita start are flaky.
6. **Never restart or kill Krita without checking for unsaved documents**
   (`[(d.fileName(), d.modified()) for d in Krita.instance().documents()]`
   via the bridge). If anything is modified, ask the user.
7. Don't ship debug logging enabled.

## Release checklist

1. Copy changed files into `krita-src\plugins\tools\tool_clonestamp\`, run
   `Tool-plugin\windows\build-krita.bat` → `BUILD OK`.
2. Import check against the installed Krita (rule 4).
3. Bump `VERSION` (rule 2); `python Tool-plugin/pykrita-loader/make_zip.py <dll> <zip>`;
   `python -I tools/check_plugin_zip.py <zip>` → `OK`.
4. Install the zip into the running Krita via the bridge (Krita's
   `PluginImporter`), restart Krita (rule 6), check: toolbox button present
   (last in the Fill section), `get_errors` empty; GUI tests with the user's OK.
5. Copy the zip to `release/…/`, update README links, commit
   (`Co-Authored-By` line), tag `vX.Y.Z`, push `main` + tag, mirror
   `krita-6.0.4`.
6. `gh release create` with the zip (gh: `C:\Program Files\GitHub CLI\gh.exe`,
   token via `git credential fill` → `GH_TOKEN`), mark latest, delete the
   superseded release for the same Krita version, download the asset once and
   re-run `check_plugin_zip.py` on it.

## Architecture quick reference

- `KisToolCloneStamp.cpp/.h` — the tool. Source = copy-on-write
  `KisPaintDevice` snapshot taken at Ctrl+click (or Alt+click). Dabs go into a
  1-byte coverage image; `compositeLive` restores the pre-stroke pixels and
  `KisPainter::bitBlt`s the source through a per-stroke selection mask in the
  layer's own pixel format (any bit depth), at most ~60×/s; one
  `KisTransaction` per stroke = one undo step. Brush tips (Round/Square/any
  Krita tip via `KisBrush::brushTipImage`), presets, flow, airbrush, pressure.
  Outline via `updateCanvasToolOutlineDoc` + update-ahead and `paintToolOutline`.
- `ClonestampToolPlugin.cpp` — registers the factory once; the exported
  `load_clonestamp_plugin()` (called by the loader) also injects the tool's
  `KoToolAction` into `KoToolManager::Private::toolActionList`, otherwise a
  tool registered after startup has no toolbox button (Acly's approach,
  needs `KoToolManager_p.h`). Toolbox position: Fill section, priority 100.
- `pykrita-loader/clonestamp_tool/__init__.py` — Krita version check, loads a
  hashed **copy** of the DLL from `%LOCALAPPDATA%` (so the plugin folder is
  never locked and re-importing a zip works while Krita runs), adds the menu
  action *Tools › Scripts › Clone Stamp: Check for Updates*.
- `updater.py` — GitHub latest release → zip for the running Krita version →
  Krita's `PluginImporter` (closes the archive itself; the importer leaves it
  open). Statuses: up to date / update / no build for this Krita / error.

Why the code looks the way it does: `docs/krita-pitfalls.md` (also in the RAG).

## Roadmap

- **Krita 5.3.4 build** (deferred): same source tree built with Qt5
  (`BUILD_WITH_QT6` off reports 5.3.4); verify the Qt5 deps match the official
  5.3.4 first; loader needs a PyQt5 fallback; ship as a second zip in the same
  release (the updater already picks the zip by Krita version).
- Tool parity with Photoshop: blend **Mode**, **Sample: Current & Below**.
- New Krita versions: rebuild per version (rule 4).

## History and deeper references

- `docs/change-report-2026-07-18.md` — per-change rationale and test recipes,
  including the 2026-10-06 Krita 6.0.4 port and v2.1.x fixes.
- `Tool-plugin/NOTE.md` — the current build recipe for Krita 6.0.4.
- `docs/toolchain-paths.md`, `docs/phase-a-runbook.md` — historical Qt5 /
  `C:\dev` build bring-up (superseded by `NOTE.md`, kept for the gotchas).
- Branch `krita-5` — the frozen Python plugin and its own rules.
- Git history — commit messages carry the reasoning.
