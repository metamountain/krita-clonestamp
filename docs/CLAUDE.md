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
- **Distribution**: the canonical download is the tracked zip on `main`
  (what the README links). GitHub releases are not maintained per version;
  if one exists, it must match `main` or be deleted. Version numbering
  was restarted at **1.0** on 2026-07-19 — the 1.x.y prototype history up
  to 1.7.1 predates the restart (see the change report).
- **Real toolbox icon for `Tool-plugin/` via a Python-installer wrapper**
  (investigated 2026-07-19, studying `Acly/krita-ai-tools`'s distribution
  model — not yet designed or implemented): that project ships a real
  native `KisTool` as a per-platform, per-Krita-version release zip
  (`krita_vision_tools-windows-x64-3.0.0.zip`, "Built for Krita 6.0.2.1
  official release"), installed through the ordinary **Import Python
  Plugin from File** dialog. Best-evidence reconstruction (release asset
  names + README install steps + the mandatory restart after import;
  not byte-verified against the actual zip, which this environment's
  network scope couldn't fetch): the zip bundles a tiny Python
  "installer" plugin (`__init__.py` + `.desktop`) alongside the
  precompiled native plugin files; on first load the installer copies
  the native `.dll`/`.so` + its JSON metadata into Krita's own
  `lib/kritaplugins/` folder, and the *next* restart's normal native-
  plugin scan picks it up and registers the real toolbox icon — no
  ABI magic, just moving the "build once" step from the end user (today's
  `Tool-plugin/NOTE.md` requirement) to whoever packages each release.
  Would need, if pursued: a packaging script wrapping the
  `C:\dev\krita-src` build output into such an installer zip, a copy-into-
  place mechanism with permission/failure handling, and a maintained
  per-Krita-version compatibility list (mirroring how Acly's README tells
  users which release matches their installed Krita version).

## History and deeper references

- `docs/change-report-2026-07-18.md` — per-change rationale + manual test
  recipes for the 2026-07 debug/optimize/parity passes (v1.5.x–v1.7.1).
- `docs/toolchain-paths.md` — full Windows build environment recipe for
  the Krita source tree (Phases A–C), including resolved gotchas.
- `docs/phase-a-runbook.md` — step-by-step log of the original build
  bring-up.
- Git history — roughly a third of all commits are documented bugfixes;
  commit messages carry the reasoning.
