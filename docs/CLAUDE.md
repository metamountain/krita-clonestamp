# Krita Clone Stamp — Native C++ Toolbox Tool

## Status: Phases A-C complete and live-tested; Phase D (CS6 parity) planned

- **Phase A** (build environment + vanilla Krita build): complete. See
  `toolchain-paths.md` for the full resolved recipe/gotchas.
- **Phase B** (toolbox registration stub): complete.
- **Phase C** (real clone-stamp algorithm + options widget): complete and
  confirmed working in a real hands-on test pass (2026-07-15), after fixing
  three bugs found during that pass (toolbox icon/priority collisions, and
  Ctrl+click/Shift+drag being swallowed by Krita's input manager instead of
  reaching the tool — see `toolchain-paths.md` for the full root-cause
  writeup). Also added: a source crosshair + live read-only content preview
  at the destination outline, matching what real clone tools show.
- **Phase D (in progress)**: bring the tool's options up to Photoshop CS6
  Clone Stamp parity, per user request and confirmed against Adobe's own
  documentation (see "Phase D plan" below) — user explicitly scoped this to
  the options-bar feature set (Mode/Opacity/Flow/Aligned/Sample), not full
  custom brush-engine integration (bristle textures, spacing/jitter dynamics,
  etc. are explicitly out of scope: "just pick a standard custom brush /
  sharp soft pattern whatever"). **Narrowed 2026-07-15: implemented Sample
  (Current Layer / All Layers) only — Mode and Flow explicitly deferred
  ("omit blend mode and flow for now").** Sample reads from
  `image()->projection()` for All Layers (via a new
  `sourceDeviceForSampling()` helper used by both `stampDabAt()` and
  `buildPreviewPatch()`), or `m_sourceNode`'s own device for Current Layer
  (existing behavior). A `QComboBox` ("Current Layer" / "All Layers") was
  added to `createOptionWidget()` and wired to `m_sampleScope` — the
  backend logic existed but had no UI control until this pass. Rebuilt
  (`cmake --build . --target kritatoolclonestamp`), reinstalled to
  `krita-install\lib\kritaplugins\`, launched the dev `krita.exe` —
  stays running (confirmed via `tasklist`). Current & Below still
  deferred. **Not yet manually verified**: toggling the combo against a
  real multi-layer document and confirming sampled pixels differ as
  expected — needs a hands-on pass same as Phase C's.
- **Shift+drag follow-up (2026-07-15, user-reported)**: the Size spinbox
  didn't update while dragging (only the internal `m_brushSize` did), and
  Shift+drag only controlled size. Fixed: `continueAlternateAction`'s
  `ChangeSize` handler now also uses the vertical drag delta to adjust
  hardness (drag up = harder, down = softer, Photoshop-style) and pushes
  both new values into the `Size`/`Hardness` spinboxes live via
  `QSignalBlocker`. See `toolchain-paths.md` for the implementation
  writeup. Rebuilt, reinstalled, launched — stays running. Not yet
  manually verified.
- **Repo rename + brush/preview pass (2026-07-18)**: this repo's native
  half moved from `native-plugin-patch/` to `Tool-plugin/` (naming cleanup,
  no content change — finished a rename an earlier session had left half
  done). Both this variant and the Python plugin now share the same "clone
  brush + preview" spec instead of drifting: default brush size 250px
  (`m_brushSize`/`m_resizeStartSize` in `KisToolCloneStamp.h`, mirrored in
  `krita-src`'s buildable copy), and the target/source outlines in
  `KisToolCloneStamp::paint()` are now both a circle with a center
  crosshair — previously only the source had a crosshair. Source is drawn
  translucent (`QColor(255,255,255,120)`) instead of opaque white so it
  stays visually distinguishable from the fully-opaque target. Rebuilt
  (`cmake --build . --target kritatoolclonestamp`), 7/8 steps, no errors.
  Also dropped `docs/context.md` (an unimplemented, superseded
  `paintOverlay()`/native-paintop design note that never touched
  `krita-src` and had drifted from this repo's actual direction) and the
  empty, untracked `brush/` scaffold dir. Not yet manually verified in a
  running Krita — see Verification plan below.
- **Python plugin: live drag preview + bugfixes + self-update (2026-07-18,
  user hands-on tested this pass, v1.2.0 → v1.4.1)**: previously a paint
  drag wrote nothing to canvas until mouse-release (`finalize_stroke` was
  the only real pixel write, by design, for single-undo-step correctness —
  confirmed via research that Krita's scripting API has no undo-macro/
  grouping call to do better). Added `_StrokeOverlay`, a transparent
  screen-space widget stamped with each dab's masked source patch as the
  drag progresses, purely visual (no Krita API calls, no extra undo steps)
  — `finalize_stroke` is unchanged and still does the one real commit at
  release. **Required `Qt.WA_AlwaysStackOnTop`** on the overlay to actually
  render on top of the canvas's `QOpenGLWidget` — without it Qt does not
  guarantee child-widget stacking order against a GL surface, which was the
  root cause of an initial "still no preview" report (the overlay was
  painting correctly, just invisible). Also found the installed plugin at
  `%APPDATA%\krita\pykrita\clonestamp\` is a separate copy from this repo's
  `python-plugin/clonestamp/` source — every iteration in this pass had to
  be explicitly copied over (`cp` + clear `__pycache__`) and Krita
  restarted, since Python plugins only load at startup; VERSION is bumped
  on every install specifically so the docker's UI label confirms which
  build is actually loaded.
  Two more user-reported bugs fixed in the same pass: (1) opening a second
  document broke the tool silently — `canvasChanged()` was a no-op, so
  `_canvas_widget`/the overlay were never re-resolved when the active
  canvas changed, leaving the event filter matching mouse events against a
  no-longer-active widget; now re-resolves and reparents overlays on every
  canvas change. (2) Shift-drag resize's ring cursor flickered/jittered —
  root cause: the existing warp-every-tick "scratch pad" trick
  (`QCursor.setPos` back to the anchor every ~30ms) combined with a fresh
  `QCursor`/`QPixmap` on every tick. First fix attempt (blank the real
  cursor + draw a fixed-position `_RingOverlay` instead of warping) made it
  *worse* — likely because raw `MouseMove` events are never intercepted by
  this plugin (only Press/Release are), so Krita's own underlying tool kept
  re-asserting its own cursor on top of the blanked one. Reverted that
  attempt entirely. Landed instead on simply switching the ring cursor off
  (`unsetCursor()`) for the whole resize drag and back on at release —
  size/hardness still update live in the status label/spin boxes, just
  with no on-canvas ring during the drag, which removes the flicker source
  outright without needing to fight the underlying tool at all.
  Also replaced the "Check for Updates" button's behavior (previously just
  opened the GitHub releases page) with a real check-and-install: new
  `clonestamp_update.py` reads `VERSION` out of `clonestamp_core.py` on the
   `main` branch via `raw.githubusercontent.com` (no GitHub API/auth/git
   needed), and if newer, downloads `clonestamp_core.py`/`clonestamp_docker.py`/
   `__init__.py` into the running install (files fetched to memory first, only
   written once all succeed, so a dropped connection can't half-update it).
- **Rule: rebuild clonestamp.zip before every push** (`python-plugin/clonestamp.zip`):
   The zip is the distribution artifact for one-click plugin installation (users
   download it from GitHub and install via Krita's "Install from File"). It must
   be rebuilt from the working tree (excluding `__pycache__`) before every push,
   so the repo always ships a zip that matches the current source. Keep the zip
   *tracked* in git (not in `.gitignore`) so it gets included in every push.
   Rebuild command: delete `python-plugin/clonestamp.zip`, then zip the contents
   of `python-plugin/clonestamp/` (excluding `__pycache__`) into it.
 
## Phase D plan: CS6 options-bar parity

Reference: Photoshop CS6 Clone Stamp options bar (per user screenshot) and
Adobe's official Clone Stamp documentation
(helpx.adobe.com/photoshop/desktop/repair-retouch/heal-clone/retouch-images-with-the-clone-stamp-tool.html),
confirmed via web search 2026-07-15:
- **Mode**: blend mode for how cloned pixels composite onto the destination
  (Normal, Multiply, Screen, Darken, Lighten, etc.)
- **Opacity**: transparency of the cloned pixels (already implemented)
- **Flow**: controls how fast paint builds up if you go over the same area
  repeatedly within one stroke (distinct from Opacity, which caps the max)
- **Aligned**: sample point moves with each new stroke vs. always resampling
  from the original point (already implemented)
- **Sample**: which layers are read from — Current Layer / Current & Below /
  All Layers
- (Not in scope: the separate Clone Source panel's 5 saved-source slots,
  per-source rotation/scale, and Show Overlay toggle — our tool already has
  an always-on preview overlay, and the screenshot the user confirmed as
  "enough" only showed the options-bar controls above, not that panel.)

### Concrete implementation plan for `KisToolCloneStamp`

1. **Sample scope** (`m_sampleScope`: CurrentLayer / AllLayers to start —
   Current & Below deferred, see below) — **done 2026-07-15**, see status
   note above for details. **Current & Below** needs walking the node
   stack and compositing only nodes at/below the current one — real but
   more involved; revisit once Current Layer + All Layers are manually
   verified working.
2. **Mode** (`m_blendMode`, default Normal): add a `QComboBox` listing the
   common modes, mapped directly to Qt's `QPainter::CompositionMode` enum
   (`CompositionMode_Multiply`, `_Screen`, `_Darken`, `_Lighten`,
   `_Difference`, `_Exclusion`, `_Overlay`, `_HardLight`, `_SoftLight`,
   `_ColorDodge`, `_ColorBurn` all exist natively in Qt — no need to hand-roll
   blend math). Used in `stampDabAt()`'s
   `painter.setCompositionMode(...)` call instead of the hardcoded
   `CompositionMode_SourceOver`. Known simplification: this composites at
   the `QImage` level via Qt, not through Krita's own `KoColorSpace`
   composite-op machinery — visually equivalent for these common modes on
   8-bit RGBA, but not colorimetrically identical to Krita's native
   compositing (e.g. under non-sRGB profiles). Acceptable first pass; can
   switch to real `KoCompositeOp` later if fidelity issues show up.
3. **Flow** (`m_flow`, default 100%): add a `QSpinBox` (0-100%) next to
   Opacity. Applied as an *additional* per-dab alpha multiplier alongside
   Opacity (`finalAlpha = opacity% * flow%`), not full Photoshop-accurate
   per-stroke paint accumulation (which would need tracking an
   already-painted-alpha ceiling per pixel across an entire stroke — a
   bigger change). Documented simplification, matches "that would be
   enough" scoping.
4. Update `toolchain-paths.md` with a Phase D completion writeup once built
   and tested, same as Phases A-C.

### Verification plan
Manual test pass in the self-built `krita.exe` (same as Phase C): each Mode
value against a busy multi-color source, Flow at 50% vs 100% (repeated
strokes over the same spot), Sample = Current Layer vs All Layers with a
multi-layer document, and confirm none of the new controls break the
existing Ctrl+click/Shift+drag/Aligned/undo-grouping behavior.

The actual source lives in `C:\dev\krita-src\plugins\tools\tool_clonestamp\`
(a separate git repo/checkout, not inside this one — see Phase A below for
why). This repo (`krita-clonestamp-native`) holds only planning/status docs.

## What this project is

A native C++ Krita plugin (a real `KisTool` + `KisToolFactory`) implementing a
Photoshop-style Clone Stamp tool that appears as an actual icon in Krita's
toolbox, next to Pan/Zoom.

## Why native C++ instead of the existing Python plugin

A working pure-Python version already exists and is installed at
`%APPDATA%\krita\pykrita\clonestamp\` (Ctrl+click sample, drag-to-paint round
soft-edged dabs, Shift+drag resize, Aligned/Non-Aligned offset, a floating
live preview window). It works, but Krita's Python scripting API (libkis) has
no way to register a new entry in the native toolbox — that requires a real
`KisTool`/`KisToolFactory` compiled against Krita's own source tree. Confirmed
by inspecting `github.com/Acly/krita-vision-tools`: it's a hybrid C++/Python
plugin whose toolbox registration is done entirely in a compiled `src/`
folder built as part of Krita's own CMake build
(`add_subdirectory(krita-vision-tools)` in `krita/plugins/CMakeLists.txt`).
There is no lighter SDK-only path — the installed Krita at
`C:\Program Files\Krita (x64)` ships zero headers/import libs, confirmed by
searching it for `.h`/`.lib`/`include/`.

## Environment findings (Windows, this machine)

- Krita's official Windows toolchain is **LLVM-MinGW** (clang, UCRT build),
  *not* MSVC — docs.krita.org explicitly warns MSVC codegen is suboptimal.
  Visual Studio 2022 is installed on this machine but is the wrong toolchain
  for this; the pinned LLVM-MinGW package needs to be installed separately.
- CMake (3.31.x specifically, not 4.x) and Ninja are not installed yet.
- Git 2.40 is already installed.
- Python 3.12 is on PATH; Krita's Windows build docs call for Python 3.13 for
  the build scripts/Qt configuration.
- Craft (KDE's build tool) pulls prebuilt binary caches for Qt/KDE Frameworks
  rather than building every dependency from source — Krita's own compile is
  reported at roughly an hour once the dependency cache is fetched. This is
  not a from-scratch Qt build.
- No existing Krita source checkout or Craft setup was found anywhere on this
  machine prior to this project.
- The eventual Krita source checkout should live at `C:\dev\krita-src\`
  (sibling to this repo, not inside it) — cloned from
  `https://invent.kde.org/graphics/krita` (mirrored at `github.com/KDE/krita`),
  ideally at or near the `v5.3.2` tag to match the installed runtime version.

## Found: a real (if empty) scaffold to start Phase B from

`https://invent.kde.org/fonkle/krita-2`, branch `fonkle/clonestamp_tool`
(2 commits, both 2022-11-18: `807c420d` "Initial Clonestamp tool" and
`71be5f1c` "Cloned Freehand Brush tool to CloneStamp tool with icon") is a
real, building toolbox-registration scaffold, confirmed via the GitLab API
(`invent.kde.org/api/v4/projects/fonkle%2Fkrita-2/repository/commits/<sha>/diff`).
It adds `plugins/tools/tool_clonestamp/` with:
- `CMakeLists.txt`, `kis_tool_clonestamp.cc`/`.h` (a `KisToolCloneStamp` class
  that is currently just `KisToolFreehand` renamed — smoothing/stabilizer/
  assistant-snapping only, confirmed **no** source-sampling, offset-tracking,
  or pixel-copy logic exists in it yet)
- `tool_clonestamp.cc`/`.h` (the plugin factory that registers it)
- `kritatoolclonestamp.json` (plugin metadata)
- `KisToolCloneStamp.action` (keyboard shortcut "C")
- light/dark SVG toolbox icons, registered in `tools-svg-16-icons.qrc`
- `plugins/tools/CMakeLists.txt` updated to build the new subdirectory

This branches off Krita's May 2022 codebase, not our installed 5.3.2.1, so
applying it to a current checkout will likely need adjusting for API drift.
But it removes almost all of Phase B's risk (getting a *new* tool to
register/build/appear in the toolbox at all) — plan is to fetch these two
commits into our own checkout once Phase A's vanilla build works, resolve
whatever no longer applies cleanly, and then implement the real algorithm
(Phase C) inside the already-wired `kis_tool_clonestamp.cc`.

## Phased plan (see full detail in the plan doc / commit history)

1. **Phase A** — build environment bring-up: install CMake/Ninja/LLVM-MinGW/
   Python 3.13, clone `krita-ci-utilities` + `krita-deps-management`, clone
   Krita source, get a vanilla unmodified `krita.exe` building and launching.
   This is the checkpoint before writing any of our own code.
2. **Phase B** — cherry-pick/adapt `fonkle/clonestamp_tool`'s two commits
   (see above) as the toolbox-registration scaffold instead of writing one
   from scratch; fix up whatever's drifted since May 2022, confirm the icon
   appears next to Pan/Zoom and the tool receives real mouse events.
3. **Phase C** — port the already-validated interaction design from the
   Python plugin natively: Ctrl+click sample, drag-paint round dabs with
   radial hardness falloff, Shift+drag resize, Aligned/Non-Aligned, using
   `KisPaintDeviceSP`/`KisRandomAccessorSP` for pixels and `KisTransaction`
   for proper single-undo-step strokes (the Python version couldn't group
   undo steps at all — no macro API is exposed to scripting). Live preview
   becomes a real `paint()`-time canvas overlay instead of the Python
   version's floating-window workaround.
4. **Phase D** — polish and adopt: test in the self-built Krita against the
   same manual test matrix used for the Python version.

## What NOT to touch

- `C:\Program Files\Krita (x64)` — the official installed runtime. Not
  modified by this project; ships no dev headers/import libs at all.
- `%APPDATA%\krita\pykrita\clonestamp\` — the working Python plugin (its own
  separate git repo). Stays as-is as a fallback regardless of how this
  native project goes.
