# Matrix Grid Tool

A standalone, browser-based generator for black/white or gray/black
**perspective line overlays** — vanishing-point rays, concentric rings, and
plain parallel lines, stacked in multiple layers so their overlap creates
spatial structure, densification and moiré effects. Useful as a base
reference (e.g. a composition/perspective guide or ControlNet-style
conditioning image) when generating AI images. It has nothing to do with
the Clone Stamp tool elsewhere in this repository; it just happens to live
in the same repo.

## Usage

Open `index.html` directly in any modern browser (desktop or mobile —
works fine over `file://`, no server or install required).

1. **Auflösung**: pick a resolution preset (default 5:4 portrait,
   1536×1920) or type a custom width/height. The swap button flips
   portrait/landscape.
2. **Hintergrund**: pick the background fill color (swatches or a custom
   color picker). JPEG has no transparency, so the background is always
   opaque.
3. **Ebenen** — each layer has a **Typ**:
   - *Radial (Perspektive)*: straight rays spreading out from a
     vanishing/center point — the classic one-point-perspective grid.
     Controlled by ray count and rotation.
   - *Konzentrisch*: concentric rings around a center point (ring
     spacing), for depth/distance cues.
   - *Parallel – horizontal/vertikal*: plain evenly-spaced straight
     lines, for combining with the perspective types.

   Every layer also has line width, color, opacity and a position
   (vanishing point / ring center / offset, depending on type). Stack
   several layers — different positions, densities, rotations — and their
   overlap generates the spatial/moiré effects; layer order in the list is
   the stacking order (top card drawn last, i.e. on top). Click a layer
   card to make it the *active* layer.
4. **Drag to move**: click/tap and drag directly on the canvas preview to
   reposition the active layer's vanishing point / center (its Position
   X/Y updates live).
5. **Export**: renders the composed image at the full target resolution
   and downloads it as a JPEG.

Everything runs client-side; no data leaves the browser.
