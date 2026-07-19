# Project guide — Krita Clone Stamp

Orientation document for development sessions (human or AI-assisted) on
this repository. Historical narratives live in the files listed under
[History](#history-and-deeper-references); this file states what is true
*now* and the rules that keep the project consistent.

## What this project is

A Photoshop-style Clone Stamp tool for Krita, in two deliberately parallel
implementations:

| Path | What it is | Status |
| --- | --- | --- |
| `python-plugin/clonestamp/` | Pure-Python plugin on Krita's `libkis` scripting API. The distributable — users install `python-plugin/clonestamp.zip`. | Live, hands-on tested, self-updating. |
| `Tool-plugin/` | Native C++ `KisTool` + `KoToolFactoryBase`, targeting eventual upstream submission (real toolbox icon). | Complete port of the Python algorithm; compiles only inside a Krita source tree. |

Both share one algorithm: Ctrl+click samples a source point and freezes a
source snapshot; drags record soft dabs into an in-memory alpha
accumulator; the stroke composites onto the layer **once** at release
(single undo step). Keep them in sync — every algorithm change lands in
both, and each side's code comments cross-reference the other.

## Machine layout (development happens on Windows)

| Location | Contents |
| --- | --- |
| This repository | Python plugin source + zip, C++ tool source (mirror), docs. |
| `C:\dev\krita-src\` | Full Krita source checkout. The C++ tool builds there as `plugins\tools\tool_clonestamp\` — copy changed files from `Tool-plugin/` into it, then `cmake --build . --target kritatoolclonestamp` and install to `krita-install\lib\kritaplugins\`. |
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
   it, then zip the *contents* of `python-plugin/clonestamp/` (flat, no
   `__pycache__`) into it. The zip is tracked in git on purpose — it is
   the download users install.
4. **Testing is manual.** There is no automated test suite; every change
   ships with a hands-on test recipe in
   `docs/change-report-2026-07-18.md` (append to it, same format). To
   test a Python change locally: copy the folder over the installed copy,
   delete its `__pycache__`, restart Krita, verify the docker shows the
   new version.
5. **Don't touch** the official install at `C:\Program Files\Krita (x64)`
   (no dev headers anyway), and don't ship debug logging enabled — it does
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
- `KisToolCloneStamp.cpp/.h` — the C++ port; mirrors core.py's snapshot/
  accumulator/finalize design and names the divergences in comments (e.g.
  oversized canvases fall back to per-dab compositing instead of refusing,
  because a `KisTool` has no error dialog channel).

Hard-won platform knowledge (do not relearn these the hard way): canvas
widget resolution must go through the QMdiArea's `activeSubWindow()`;
document identity comes from the root node's `uniqueId()` (not sip wrapper
identity); overlay widgets over the GL canvas need
`Qt.WA_AlwaysStackOnTop`; teardown paths need `RuntimeError` guards
because Qt/sip objects can already be deleted when callbacks fire.

## Deferred / roadmap

- **C++ tool**: Photoshop-CS6 options parity — blend **Mode** and **Flow**
  (explicitly deferred by the author), **Sample: Current & Below** (needs
  partial layer-stack compositing). A live stroke overlay during the drag
  (Python has one; C++ shows the ghost preview only) would need a canvas
  decoration.
- **Python**: accumulator is whole-document sized (capped ~800 MB);
  dirty-bounds sizing is the known future optimization if the cap bites.
- **Distribution**: the GitHub release (v1.0.0) is stale; the README
  intentionally links to the tracked zip on `main` instead. Cutting fresh
  releases per version would be nicer but requires keeping them updated.

## History and deeper references

- `docs/change-report-2026-07-18.md` — per-change rationale + manual test
  recipes for the 2026-07 debug/optimize/parity passes (v1.5.x–v1.7.1).
- `docs/toolchain-paths.md` — full Windows build environment recipe for
  the Krita source tree (Phases A–C), including resolved gotchas.
- `docs/phase-a-runbook.md` — step-by-step log of the original build
  bring-up.
- Git history — roughly a third of all commits are documented bugfixes;
  commit messages carry the reasoning.
