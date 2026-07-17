# Clone brush live preview — patch plan (not started)

## Goal

Add a live, ghosted bitmap preview of the source pixels under the cursor to
Krita's own **native "Clone" brush** (internally the `duplicate` paintop, at
`plugins/paintops/defaultpaintops/duplicate/` in the Krita source tree) --
patching Krita's real brush in place, not adding a separately-named
competing brush.

## Distribution: upstream, not a custom build

This patch touches Krita's own core files directly (see below), so unlike
`../python-plugin/` there is no drop-in-install path for regular users at
all -- it can only exist as part of a full Krita binary. Self-distributing a
custom Krita build (own installer/zip, hosted per platform) was considered
and ruled out: it would mean owning ongoing maintenance of a Krita fork --
tracking upstream releases, rebuilding, hosting installers -- indefinitely,
which isn't feasible here.

**The explicit target is upstreaming**: submit this as a merge request to
Krita's own project (`invent.kde.org/graphics/krita`) once it works, so it
ships to every Krita user through the normal official release, the same way
`../native-plugin-patch/` is meant to be handed off to a Krita/KDE
contributor per this project's README. That means from the start: keep the
diff minimal and scoped against a clean base commit, match Krita's existing
code style in the touched files, keep the GPL-2.0-or-later licensing
consistent with the surrounding code, and expect it will need a human
Krita/KDE reviewer's help to actually land -- same caveat the README already
gives for the toolbox-tool half of this project.

## Why this instead of `../native-plugin-patch/`

That project's `KisToolCloneStamp` is a dedicated toolbox tool with its own
`paint()` override, so it can already draw arbitrary bitmaps freely -- no
architectural blocker there. This project targets the brush/paintop instead,
which is used generically by the Freehand tool (and any other paint tool) --
a different, shared code path.

## Investigated: is a bitmap preview actually reachable from a paintop?

Initial read was pessimistic: `KisPaintOpSettings::brushOutline()` (see
`libs/image/brushengine/kis_paintop_settings.h:189`) is the only hook a
paintop's settings object gets to describe its cursor, and it returns a
`KisOptimizedBrushOutline` -- a vector path, not a bitmap. `KisDuplicateOpSettings::brushOutline()` (`plugins/paintops/defaultpaintops/duplicate/kis_duplicateop_settings.cpp:134`)
confirms this is *all* the native Clone brush currently draws: a circle
outline at the cursor and a mirrored one at the source point. No pixel
content, ever. This looked like a dead end for a content preview specifically.

**It isn't, though** -- the outline path is only half the picture:

- `KisToolPaint::paint(QPainter &gc, const KoViewConverter &converter)`
  (`libs/ui/tool/kis_tool_paint.cc:262`) is the tool's actual per-frame
  canvas paint override, shared by every brush-based tool (Freehand
  included). It receives a **real `QPainter`**, not just a path consumer:
  ```cpp
  void KisToolPaint::paint(QPainter &gc, const KoViewConverter &converter) {
      KisOptimizedBrushOutline path = tryFixBrushOutline(pixelToView(m_currentOutline));
      paintToolOutline(&gc, path);
      m_colorSamplerHelper.paint(gc, converter);   // <-- existing precedent
  }
  ```
- `m_colorSamplerHelper` is a `KisAsyncColorSamplerHelper`
  (`libs/ui/tool/KisAsyncColorSamplerHelper.h`) that Krita *already* uses to
  draw its own custom on-canvas preview overlay (a swatch showing the color
  being sampled) directly via this same `QPainter`, completely independent
  of `brushOutline()`'s vector path. This is a live, working example of
  exactly the pattern needed: a helper object the tool owns, given the
  `QPainter` every paint() call, drawing whatever it wants on top of the
  canvas.
- The canvas widget being `QOpenGLWidget` is a non-issue: Qt composites
  `QPainter` drawing on top of GL-rendered content automatically, and that's
  how the brush outline / assistants / marching ants are already drawn
  today. No GL-specific code needed -- a plain `gc.drawImage(...)` call
  composites correctly.
- `KisDuplicateOpSettings` already carries the state a preview needs with no
  new plumbing: `m_position`, `m_sourceNode`, `m_offset` (see the `clone()`
  method around `kis_duplicateop_settings.cpp:125`) track the live source
  point, source layer, and cursor-to-source offset already, for the outline
  mirroring.

## Concrete plan

1. Add a small helper class analogous to `KisAsyncColorSamplerHelper` (e.g.
   `KisClonePreviewHelper`), owned by `KisToolPaint` (or more surgically,
   only instantiated/used when `KisToolFreehand`'s active paintop id is
   `"duplicate"` -- avoid paying any cost for every other brush).
2. On each `paint()` call, when active: read a small patch of pixels from
   `m_sourceNode`'s `KisPaintDeviceSP` at `m_position + m_offset` (radius =
   current brush size), soften/mask it to match the brush's hardness falloff
   (mirrors what `../python-plugin/clonestamp/clonestamp_core.py`'s
   `build_alpha_mask`/`preview_patch` already do, just re-derived in C++
   against real `KisPaintDeviceSP` reads instead of a frozen `QImage`
   snapshot -- likely simpler, since the paint device already does
   bounds-safe reads), convert to a `QImage`, and `gc.drawImage(...)` it at
   the destination cursor position, under the existing outline draw so the
   outline stays crisp on top.
3. Gate visibility the same way the existing outline mirroring does
   (`m_isOffsetNotUptodate` / `DUPLICATE_MOVE_SOURCE_POINT` /
   `DUPLICATE_RESET_SOURCE_POINT` -- see `kis_duplicateop_settings.cpp:150`)
   so the preview only shows once a source point actually exists.
4. Build against the same `krita-src` checkout / toolchain already set up
   for `../native-plugin-patch/` (see `../docs/toolchain-paths.md` for the
   resolved recipe). This patch touches two existing Krita files
   (`libs/ui/tool/kis_tool_paint.cc`/`.h`, or possibly
   `kis_tool_freehand.cc` if scoped more narrowly there) plus the
   `duplicate/` paintop files -- unlike `native-plugin-patch/`, which is a
   wholly new `add_subdirectory()`, this is an in-place modification of
   Krita's own source, so keep a clean diff against the exact base commit
   for later upstreaming.

## Status

Plan only -- no code written yet. Next step is picking the exact
integration point (on `KisToolPaint` generically vs. scoped narrower to
avoid touching a class used by every single brush) and prototyping the
pixel-read + mask + draw against a real `krita-src` build.
