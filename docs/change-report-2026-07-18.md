# Change report — 2026-07-18 (branch `claude/debug-optimize-code-z2rmfa`)

This pass was done in a remote session that **cannot run Krita**, so every
change below was verified only by review against the working Python
implementation (plus `python3 -m py_compile` for the Python files). This file
is addressed to the local Claude Code session / hands-on tester: each section
says what changed, why, and exactly how to verify it in a running Krita.

Big picture: the C++ Tool-plugin was missing three correctness fixes the
Python plugin already had (it was ported before those fixes landed), and both
implementations shared two inefficiencies. The C++ tool now mirrors the
Python architecture (source snapshot → per-dab accumulator → single
composite at stroke end), and both sides got brush-size-scaled dab spacing
and a cached soft-circle mask.

Build/run notes:
- **Python plugin**: no build step; copy `python-plugin/clonestamp/` into the
  Krita `pykrita` folder (or re-zip) and restart Krita. `VERSION` was bumped
  to **1.5.0** — remember the self-updater reads `VERSION` off the `main`
  branch, so don't merge to `main` until this has been hands-on tested.
- **C++ Tool-plugin**: only builds inside a full Krita source checkout (see
  `Tool-plugin/NOTE.md`). **A local compile is required** — this session
  could not compile it, so treat "it builds" as the first test.

---

## C++ fix 1 — frozen source snapshot (`KisToolCloneStamp.cpp`)

**Bug:** the tool read the source from the *live* paint device while each dab
wrote back to that same device mid-stroke. When the source region and the
painted region overlap within one stroke, later dabs re-clone pixels already
modified by earlier dabs — a smearing/feedback loop. The Python plugin fixed
this long ago (`_snapshot_source` in `clonestamp_core.py`); the C++ port
predated that fix.

**Change:** `takeSourceSnapshot()` freezes the whole canvas into a `QImage`
at Ctrl+click time (capped at 200M px ≈ 800MB, mirroring the Python caps;
above that it falls back to live reads). All source reads
(`readSourceImage`) go through the snapshot when it exists. Changing the
Sample scope combo after sampling deliberately does *not* retake the
snapshot — same semantics as Python.

**Test:** on a busy photo layer, Ctrl+click a point, then drag a stroke that
passes *through/near* the sampled point with a small offset (e.g. sample,
then start painting ~100px away and drag across the source). Before the fix
this produces repeated/smeared copies; after it, the clone is a clean copy
of the original pixels. Compare with the Python plugin doing the same
gesture — results should match.

## C++ fix 2 — soft falloff no longer distorts at canvas edges

**Bug:** the radial mask was built at the *clipped* dab size, so when a dab
ran off a canvas edge the gradient re-centered on the clipped rectangle —
visibly asymmetric feathering near edges. Python already built the full-size
circle and drew only the clipped sub-rect.

**Change:** everywhere a dab or preview is clipped (`recordDabToAccumulator`,
`stampDabImmediate`, `buildPreviewPatch`), the circle is built at full brush
size and only the matching sub-rect is drawn/masked.

**Test:** with a big soft brush (250px, hardness ~30%), clone strokes that
hang half off each canvas edge. The visible part of the feathered edge must
look like a circle cut off by the canvas, not a smaller ellipse squeezed
into the remaining space. Also hover the cursor so the source preview circle
overlaps a canvas edge — the ghost preview should stay round.

## C++ fix 3 — accumulate-then-blend-once (opacity ceiling now honest)

**Bug:** each dab was composited straight into the layer with `SourceOver`.
At opacity < 100%, overlapping dabs in one stroke (any slow drag) stacked up
past the chosen opacity — the exact problem the Python plugin's accumulator
was built to fix.

**Change:** the C++ tool now mirrors the Python architecture: `beginStroke`
allocates a canvas-sized alpha accumulator; each dab just draws the cached
soft circle into it (no device I/O at all during the drag — also a large
performance win); `finalizeStroke()` (called from `endPrimaryAction` and
from `deactivate` if the tool is switched mid-drag) masks the source by the
accumulated alpha and composites onto the layer **once**, inside the same
undo transaction as before.

One deliberate divergence from Python: on canvases above the 200M px cap,
Python refuses the stroke with an error message; a `KisTool` has no good
error channel, so the C++ tool instead falls back to the old per-dab
immediate compositing (`stampDabImmediate`) — degraded but working.

**Behavior note for the tester:** committed pixels now appear at stroke
*end*, not during the drag (the on-canvas ghost preview still tracks the
cursor). This matches the Python plugin and is expected.

**Test:**
1. Opacity 40%, soft 250px brush, drag one slow overlapping scribble over a
   contrasty area. The cloned paint must nowhere exceed ~40% coverage —
   check with the color picker against a single-dab click. Before the fix,
   overlap zones went much darker/denser.
2. Undo after a stroke — the whole stroke must undo as one step (unchanged
   behavior, but the transaction path was touched).
3. Switch tools mid-drag — the partial stroke should commit, not vanish.
4. Multi-stroke build-up still works: two *separate* 40% strokes over the
   same area should still darken (accumulator resets per stroke).

## C++ optimization — preview patch cache

`paint()` rebuilt the ghost preview (device read + mask) on every repaint.
It now refreshes at most every 200ms and reuses the last frame in between
(`cachedPreviewPatch`) — the same 5Hz budget the Python docker's
`_refreshPreviewCache` already validated. Parameter changes (size/hardness/
opacity) refresh immediately.

**Test:** hover with a source armed; the ghost content may lag the cursor by
up to ~0.2s (same as the Python plugin) but the ring/crosshair must stay
glued to the cursor. Resize with Shift+drag — the preview must track the new
size without a stale frame.

## Both — dab spacing scales with brush size

Fixed 2px spacing meant a 250px brush stamped a dab every 2px of mouse
travel. Spacing is now `max(2.0, brush_size * 0.15)` in both
`continuePrimaryAction` (C++) and `continue_stroke` (Python core; the docker
call site uses the default). 15% of diameter is well under Photoshop's 25%
default brush spacing, so the union of overlapping soft circles stays
smooth.

**Test:** default 250px brush, fast and slow strokes, straight and curly.
Look at the stroke *edges* for scalloping (there should be none) and confirm
drags feel snappier, especially in the C++ tool. Also test at brush size
5–10px (spacing floor of 2px still applies).

## Both — cached soft circle / mask

The radial-gradient circle was rebuilt from scratch for every dab. Both
implementations now cache it (C++: single entry keyed on
size/hardness/opacity; Python: small dict, because the dab circle and the
cursor-preview mask with different keys are alive simultaneously during a
drag). Cached images are treated as read-only by all callers (verified: all
uses are `drawImage` sources).

**Test:** covered by the stroke tests above — mainly confirm hardness and
opacity spinbox changes mid-session still take effect on the *next* stroke
(cache keys on all three parameters, so a stale circle would indicate a
bug).

## Python — deliberate non-change: whole-document accumulator

`_ensure_accumulator` still sizes the stroke buffer to the whole document
(≤800MB cap). Lazy dirty-bounds sizing would save memory but is the
riskiest arithmetic in the codebase (per README history); it stays as a
documented future optimization. A docstring now records this trade-off.

## Python/docker — small hardening + comments (no behavior change)

- `_resize_start_hardness_pct` / `_resize_accum_dx` / `_resize_accum_dy` are
  now initialized in `__init__` instead of springing into existence on the
  first Shift+drag (removes a latent `AttributeError` landmine).
- New "why" comments: `map_widget_to_document` derivation, the Windows-only
  `TEMP` assumption in the debug logger, `_ensure_accumulator` docstring,
  the 0.2s preview-cache interval rationale, and (C++) the gradient-stop
  math and `DestinationIn` masking trick.

## Python — new "Brush cursor outline" toggle (v1.6.0)

Added after hands-on testing reported the ring/ghost-preview cursor still
misbehaving on the tester's machine. The docker now has a **Brush cursor
outline** checkbox (default on, below "Aligned"). Unchecking it skips all
custom-cursor pixmap work in `_updateBrushCursor` and shows a plain
crosshair instead — painting, sampling, and resize all keep working. This
is an escape hatch, not a fix: if the outline glitch is reproducible,
please note the symptoms (wrong position? flicker? stuck? truncated at
large source offsets? — the pixmap is clamped to 512px, which cuts off the
source ring when the sample point is far from the cursor) so the root
cause can be chased properly.

**Test:** arm the brush, toggle the checkbox both ways while hovering,
during a drag, and during a Shift+drag resize. Off = plain crosshair, on =
ring returns immediately; no stuck or invisible cursor in either state.

---

# Follow-up pass — 2026-07-19 (v1.7.0, merged with main's v1.5.8 → v1.7.1)

> **Merge note:** the local session independently pushed v1.4.8–v1.5.8 to
> `main` (MDI-aware canvas finder, document-switch watcher + `clear_source`,
> blank-cursor resize fix, MouseMove swallowing, cursor-refresh coalescing,
> zip artifact). This branch now contains BOTH lines. Where they solved the
> same problem differently, the hands-on-validated `main` fix won: resize
> uses `Qt.BlankCursor` + warp (not my 10ms leash, which was dropped); my
> overlay resize ring and cursor signature cache are layered on top.
> Combined version: **1.7.1**.

Driven by hands-on feedback: "Python plugin flickers a bit during
mouse-drag brush resize, otherwise all good." Local Krita source build
lives in `C:\dev` (relevant for compiling the Tool-plugin).

## Python — cursor flicker fixes (two separate sources)

**1. Paint-drag flicker / wasted work:** `_updateBrushCursor` rebuilt the
cursor pixmap and called `setCursor()` 33x/s during a drag, even though
with a fixed Aligned offset the pixmap only actually changes at the ~5Hz
preview refresh. Every needless OS-cursor swap is a flicker opportunity.
Now a change signature (diameter, source offset, hardness, preview-cache
timestamp) gates the rebuild — identical frames skip `setCursor()`
entirely. This is also the biggest remaining CPU win during drags.

**2. Shift+drag resize:** the pointer-warp trick lets the visible cursor
wander for a full 30ms tick before snapping back — that excursion is the
jitter. The warp leash is now 10ms during resize (restored to 30ms at
release), and a live brush ring (solid + dashed hardness ring) is drawn on
the stroke overlay at the anchor position, so you finally get on-canvas
size/hardness feedback during resize — same as the C++ tool always had.
The overlay is a plain widget, so it's immune to the cursor re-assert
problem that sank the earlier blank-cursor attempt (see v1.3.0 history).

**Test:** (a) sample a source, paint slow and fast drags — the ring cursor
should no longer flicker; ghost content still refreshes ~5Hz. (b)
Shift+drag: a ring at the anchor grows/shrinks live (horizontal = size,
vertical = hardness via dashes), jitter of the OS cursor is much reduced;
release restores the normal ring cursor — including when you land on the
exact starting values (regression guard: signature cache is invalidated
around unsetCursor). (c) Switch documents mid-session; ring cursor must
still appear on the new canvas.

## Feature/look parity audit — Tool-plugin vs. python-plugin

Aligned in this pass:
- **Dashed hardness ring** added to the C++ on-canvas outline.
- **Red source crosshair** in C++ (was white; Python uses red).
- **Hardness slider + spinbox pair** in the C++ option widget (was
  spinbox-only), kept in sync with Shift+drag.
- **Dab placement rounding**: Python now uses round() like C++'s qRound(),
  so both stamp identical pixels for identical input.

Already identical: defaults (250px / 50% / 100% / Aligned / Current
Layer), falloff math, dab spacing (15% of diameter, floor 2px), source
snapshot + accumulate-then-blend architecture, memory caps.

Remaining known differences (deliberate, low priority):
- **Live stroke preview while dragging**: Python stamps dabs onto its
  overlay; the C++ tool shows only the ghost patch under the cursor and
  the real pixels appear at release. Porting the overlay needs a canvas
  decoration in KisTool — noted as future work.
- **Ghost preview strength**: C++ draws at 0.6 opacity; Python masks at
  70/255. Visually close; not worth unifying blind — judge by eye.
- **Python-only UI**: Enable checkbox, cursor-outline toggle, update
  button — all artifacts of the docker/event-filter architecture; the C++
  tool doesn't need them.
- **Python cursor pixmap is clamped to 512px**, so its source ring is cut
  off at large sample offsets; the C++ paint()-based outline has no clamp.
- **Oversized canvas (>200M px)**: Python refuses the stroke with an
  error; C++ falls back to per-dab compositing (no error channel).

**Test:** run both side by side on the same document: same defaults, same
stroke result for the same gesture, same ring/crosshair/dash look; C++
hardness slider tracks Shift+drag.

---

# Follow-up — 2026-07-19 (v1.0.2): remaining paint-drag flicker

User report: still slight flicker while painting. Root cause: during a
stroke the only thing changing in the cursor pixmap is the ghost preview
content, refreshed at ~5Hz — so the signature cache still allowed five
native `setCursor()` swaps per second, each a chance for the OS cursor to
blink.

Fix 1: **the cursor pixmap is frozen for the whole stroke** (zero
setCursor calls mid-drag; the OS moves the frozen pixmap natively). The
ghost is redundant while painting anyway — the stroke overlay draws the
real result at that exact spot. Zoom/brush-size changes still rebuild.

Fix 2 (user's suggestion, applied to hovering): **adaptive refresh rate
by brush size** — ghost refresh drops from 5Hz to 2.5Hz when the
on-screen brush diameter exceeds 256px, since patch scaling, pixmap
rebuild, and cursor swap all scale with brush size.

**Test:** paint slow/fast strokes with a source armed — the ring cursor
must not blink at all during a drag; the ghost under the cursor stays
static mid-stroke (expected — the overlay shows the live result) and
resumes updating on release. Hover with a huge brush (500px+ at 100%
zoom): ghost updates are slightly slower; ring stays glued to the
pointer.

---

## Suggested test order for the local session

1. Python plugin first (drop-in, no build): smoke-test sample/paint/undo,
   then the spacing feel test and the 40%-opacity overlap test (should
   behave exactly as v1.4.1 — these were behavior-preserving there).
2. C++ tool: compile inside the Krita tree (first gate), then run fixes 1–3
   tests above; each has a clear before/after visual.
3. If anything looks off in the C++ finalize path, the reference
   implementation is `finalize_stroke` in `clonestamp_core.py` — the C++
   `finalizeStroke()` is a line-by-line port (same dual-clip shrink, same
   accumulator-origin slicing) and any divergence from it is suspect.
