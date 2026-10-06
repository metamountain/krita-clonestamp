# Project guide — Krita Clone Stamp

Orientation document for development sessions (human or AI-assisted) on
this repository. Historical narratives live in the files listed under
[History](#history-and-deeper-references); this file states what is true
*now* and the rules that keep the project consistent.

> **Status 2026-10-06: the project is C++ only.** `main` contains the native tool
> (`Tool-plugin/`), its release packaging and tests. The Python plugin was removed from
> `main` and lives frozen on branch `krita-5` (release v1.0.2-krita5, not maintained).
> Rules below that mention `python-plugin/`, `VERSION` or `clonestamp.zip` apply to that
> branch only. Release checklist for the C++ tool: build (`Tool-plugin/windows/build-krita.bat`),
> check imports against the installed Krita (llvm-readobj), build the plugin zip with
> `Tool-plugin/pykrita-loader/make_zip.py`, verify it with `tools/check_plugin_zip.py`, run
> `tools/krita_mcp/tests` (real mouse -- only with the user's OK), then tag + GitHub release.

## What this project is

A Photoshop-style Clone Stamp tool for Krita, in two implementations:

| Path | What it is | Status |
| --- | --- | --- |
| `Tool-plugin/` | Native C++ `KisTool` + `KoToolFactoryBase` — the **flagship**. Real toolbox icon; distributable via the prebuilt `release/` DLL for Krita 6.0.4. | Live, automated-tested (via `tools/krita_mcp/`); ahead of the Python port. |
| `python-plugin/clonestamp/` | Pure-Python plugin on Krita's `libkis` scripting API. The Krita 5.x path — users install `python-plugin/clonestamp.zip`. | Live, hands-on tested, self-updating. Krita 5.x only (PyQt5). |

The C++ tool is the reference implementation and is **ahead** of the Python
one. Ctrl+click samples a source point and freezes a copy-on-write source
snapshot. The C++ tool paints **live per frame** while dragging (a 1 byte/px
stroke buffer is composited at most ~60×/s over the united area, opacity
applied via `KisPainter`, one undo step per stroke); the Python plugin still
composites **once** at release (single undo step, no live painting). The two
are **no longer in lockstep** — the C++ tool additionally supports all bit
depths/color spaces, brush tips, presets, pressure, and airbrush.

## Machine layout (development happens on Windows)

| Location | Contents |
| --- | --- |
| This repository | C++ tool source, Python plugin source + zip, `release/` prebuilt DLL, docs. |
| `D:\_Code\Krita\krita-src\` | Full Krita source checkout (tag `v6.0.4.1`). The C++ tool builds there as `plugins\tools\tool_clonestamp\` — copy changed files from `Tool-plugin/` into it, then `cmake --build D:\_Code\Krita\dev\build68 --target kritatoolclonestamp`. |
| `D:\_Code\Krita\dev\` | Build environment: `env68/` (fetched deps + `base-env.bat`), `cmake-3.31.8`, `ninja`, `build68/` (build tree), `install68/` (install tree). |
| `C:\Program Files\Krita (x64)\` | The official Krita 6.0.4 installation the flagship DLL is deployed into (via `deploy-tool.cmd` / `install.cmd`). |
| `%APPDATA%\krita\pykrita\clonestamp\` | The *installed* Python plugin — a separate copy, not this repo. Krita loads Python plugins only at startup. |

## Hard rules

1. **`main` is the single line of development.** The in-plugin updater
   downloads `clonestamp_core.py` / `clonestamp_docker.py` / `__init__.py`
   from `main` via raw.githubusercontent.com and compares `VERSION` in
   `clonestamp_core.py`. Anything merged to `main` is immediately
   user-visible. Parallel work in separate sessions must converge on
   `main` before anyone tests "the latest version".
2. **Bump `VERSION`** (in `clonestamp_core.py`) on every behavior change —
   it drives the updater *and* is the only reliable way to confirm which
   build a running Krita actually loaded (shown in the docker footer).
3. **Rebuild `python-plugin/clonestamp.zip` before every push**: delete
   it, then from `python-plugin/` zip the **`clonestamp/` folder itself**
   (`zip -r clonestamp.zip clonestamp -x "clonestamp/__pycache__/*"`).
   The folder structure is load-bearing: Krita's plugin importer looks
   for `clonestamp/__init__.py` *inside* the archive and reports "No
   plugins found in archive" for a flat zip — this exact regression
   shipped once (an earlier version of this rule said to zip the folder's
   *contents*) and was only caught when a real zip import was attempted.
   The zip is tracked in git on purpose — it is the download users
   install. After changing it, verify with `unzip -l` that every entry
   starts with `clonestamp/`.
4. **Testing.** The C++ tool has an automated suite in `tools/krita_mcp/`:
   an MCP bridge (the `kritamcp` Python plugin listens on 127.0.0.1:50017;
   `krita_mcp.py` is the MCP server; `kcall.py` is the CLI) plus
   `tests/clone_test.py` (8/16-bit/float, left and right image half) and
   `tests/brush_tests.py` (Opacity cap, Flow buildup, Square vs. Round,
   Airbrush, Spacing). **The tests drive the REAL mouse** — synthetic Qt
   events do not paint in Krita — so do not touch the mouse while they run.
   Invoke with `python kcall.py exec "DEPTH='U16'"` then
   `python kcall.py exec -f tests/clone_test.py`. The Python plugin is still
   tested by hand: every change ships with a hands-on test recipe in
   `docs/change-report-2026-07-18.md` (append to it, same format); to test a
   Python change locally, copy the folder over the installed copy, delete
   its `__pycache__`, restart Krita, and verify the docker shows the new
   version.
5. **Deploying to the official install is deliberate and minimal.** The
   official Krita 6.0.4 at `C:\Program Files\Krita (x64)` is the target of
   the flagship DLL — and the *only* file ever placed there is
   `kritatoolclonestamp.dll`, via `Tool-plugin/windows/deploy-tool.cmd`
   (developer) or `release/.../install.cmd` (end user). A running Krita
   locks a loaded DLL against overwrite but allows rename, so the scripts
   rename the old DLL to `.old` and copy the new one in (takes effect at the
   next start; needs admin for `Program Files`). Nothing else in the
   official install is touched. Don't ship debug logging enabled — it does
   file I/O per stroke tick and is gated behind a `%TEMP%` sentinel file
   for that reason.

## Architecture quick reference

The two Python modules carry thorough docstrings — read those first:

- `clonestamp_core.py` — pure pixel logic, no widgets: coordinate mapping,
  source snapshot, dab accumulator, `finalize_stroke` (the only
  `setPixelData` call), preview helpers. Module docstring has the feature
  map.
- `clonestamp_docker.py` — all UI/eventing: global event filter (only
  Press/Release/Move exist; drags are polled at 30 ms), `_StrokeOverlay`
  live preview, ring-cursor pixmap with change-signature caching,
  Shift+drag resize (blank cursor + pointer warp + MouseMove swallowing +
  overlay ring — see `_onResizeTick` for why exactly this combination),
  document-switch watcher (polls active document id; disables the brush
  and clears the source on change), self-update UI.
- `KisToolCloneStamp.cpp/.h` — the C++ flagship. No longer a 1:1 port:
  source snapshot is a copy-on-write `KisPaintDevice`; dabs go into a
  1-byte coverage buffer and are composited live (at most ~60x/s) with
  `KisPainter` through a per-stroke selection mask in the layer's own
  pixel format; brush tips (Round/Square/any Krita tip), presets, flow,
  airbrush and pressure; outline via `updateCanvasToolOutlineDoc` +
  `paintToolOutline`. See `docs/krita-pitfalls.md` for the Krita API
  lessons behind these choices.

Hard-won platform knowledge (do not relearn these the hard way): canvas
widget resolution must go through the QMdiArea's `activeSubWindow()`;
document identity comes from the root node's `uniqueId()` (not sip wrapper
identity); overlay widgets over the GL canvas need
`Qt.WA_AlwaysStackOnTop`; teardown paths need `RuntimeError` guards
because Qt/sip objects can already be deleted when callbacks fire.

## Deferred / roadmap

- **C++ tool** (done as of 2026-10-06): ~~blend **Flow**~~ and ~~a live
  stroke during the drag~~ are now implemented — Flow builds up per dab,
  and the stroke paints live per frame (not just the ghost preview).
  Remaining parity: blend **Mode** and **Sample: Current & Below** (needs
  partial layer-stack compositing).
- **Python**: accumulator is whole-document sized (capped ~800 MB);
  dirty-bounds sizing is the known future optimization if the cap bites.
- **Distribution**: the C++ tool ships as the prebuilt `release/` DLL for
  the one matching Krita version (6.0.4.x today); every new Krita version
  needs a rebuilt DLL. The Python plugin's canonical download is the
  tracked zip on `main` (what the README links). GitHub releases are not
  maintained per version; if one exists, it must match `main` or be
  deleted. Version numbering was restarted at **1.0** on 2026-07-19 — the
  1.x.y prototype history up to 1.7.1 predates the restart (see the change
  report).
- **Installer-zip idea (Acly model) — the next step** (investigated
  2026-07-19, studying `Acly/krita-ai-tools`'s distribution model): that
  project ships a real native `KisTool` as a per-platform, per-Krita-version
  release zip, installed through the ordinary **Import Python Plugin from
  File** dialog — a tiny Python "installer" plugin copies the native `.dll`
  into Krita's `lib/kritaplugins/` on first load, and the next restart's
  native-plugin scan registers the real toolbox icon. We already ship the
  DLL + `install.cmd` directly; this idea would wrap that into a single
  importable zip so no separate install step is needed. Would need: a
  packaging script wrapping the `D:\_Code\Krita\krita-src` build output
  into such an installer zip, a copy-into-place mechanism with
  permission/failure handling, and a maintained per-Krita-version
  compatibility list (mirroring how Acly's README tells users which release
  matches their installed Krita version).

## History and deeper references

- `docs/change-report-2026-07-18.md` — per-change rationale + manual test
  recipes for the 2026-07 debug/optimize/parity passes (v1.5.x–v1.7.1).
- `docs/toolchain-paths.md` — full Windows build environment recipe for
  the Krita source tree (Phases A–C), including resolved gotchas.
- `docs/phase-a-runbook.md` — step-by-step log of the original build
  bring-up.
- Git history — roughly a third of all commits are documented bugfixes;
  commit messages carry the reasoning.
