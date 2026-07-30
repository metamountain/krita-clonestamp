# Matrix Grid Tool

A standalone, browser-based generator for black/white or gray/black
**square grid overlay images**, useful as a base reference (e.g. a
composition guide or ControlNet-style conditioning image) when generating
AI images. It has nothing to do with the Clone Stamp tool elsewhere in
this repository; it just happens to live in the same repo.

## Usage

Open `index.html` directly in any modern browser (desktop or mobile —
works fine over `file://`, no server or install required).

1. **Auflösung**: pick a resolution preset (default 5:4 portrait,
   1536×1920) or type a custom width/height. The swap button flips
   portrait/landscape.
2. **Hintergrund**: pick the background fill color (swatches or a custom
   color picker). JPEG has no transparency, so the background is always
   opaque.
3. **Ebenen**: by default there's one horizontal and one vertical layer
   with the same cell size, which together form a square grid. Each layer
   has a cell size, line width, color, opacity and an offset (position),
   and can be shown/hidden independently. Add more horizontal/vertical
   layers to stack several grid scales (e.g. a coarse and a fine grid).
   Layer order in the list is the stacking order (top card drawn last,
   i.e. on top). Click a layer card to make it the *active* layer.

   Each layer's **Typ** can also be switched to *Radial (Perspektive)*
   (straight rays spreading out from a vanishing/center point) or
   *Konzentrisch* (concentric rings around a center point) for
   perspective/moiré-style overlays instead of a straight grid line —
   optional, not the default.
4. **Drag to move**: click/tap and drag directly on the canvas preview to
   reposition the active layer (its offset/position updates live).
5. **Export**: renders the composed grid at the full target resolution and
   downloads it as a JPEG.

Everything runs client-side; no data leaves the browser.
