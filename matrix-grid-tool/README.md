# Matrix Grid Tool

A standalone, browser-based generator for black/white or gray/black grid
reference images, useful as a base reference (e.g. a composition guide or
ControlNet-style conditioning image) when generating AI images. It draws
flat square grids plus **measurement-based camera perspective** — real
horizontal planes and vertical walls in metres, projected through a simple
pinhole camera (eye height + focal length). It has nothing to do with the
Clone Stamp tool elsewhere in this repository; it just happens to live in
the same repo.

## Usage

Open `index.html` directly in any modern browser (desktop or mobile —
works fine over `file://`, no server or install required).

1. **Auflösung**: pick a resolution preset (default 5:4 portrait,
   1536×1920) or type a custom width/height. The swap button flips
   portrait/landscape.
2. **Hintergrund**: pick the background fill color (swatches or a custom
   color picker). JPEG has no transparency, so the background is always
   opaque.
3. **Kamera (Perspektive)**: a simple horizontal pinhole camera that
   drives the perspective layer types. Set the **Standpunkthöhe** (eye
   height above the ground, in metres) and the **Brennweite** (focal
   length in mm, on a fixed full-frame 36 mm sensor — shorter = wider
   angle). The view is level, so the horizon sits on eye level at the
   image centre (toggle *Horizont anzeigen* to show it).
4. **Ebenen**: by default there's one horizontal and one vertical
   *Parallel* layer with the same cell size, which together form a flat
   square base grid. Each layer has line width, color, opacity, an accent
   (below) and can be shown/hidden independently; list order is the
   stacking order (top card drawn last). Click a layer card to make it the
   *active* layer. Each layer's **Typ**:
   - *Parallel – horizontal/vertikal*: flat image-space grid lines
     (offset draggable on the canvas).
   - *Horizontale Ebene (Perspektive)*: a real horizontal plane at a given
     **Höhe** (0 = floor) with a metric **Zellgröße**, projected through
     the camera so it recedes correctly into depth toward the horizon.
     Stack several at different heights.
   - *Vertikale Wand (Perspektive)*: a real fronto-parallel wall at a
     given **Entfernung** (distance), with metric height, width and cell
     size. Nearer walls appear larger.

   **Betonung** (accent): set it to N ≥ 2 to emphasise every N-th grid
   line (*Linie*) or every N-th cell (*Fläche*) in a separate colour.
   Overlapping planes with different accents build up interesting
   interference/moiré patterns.
5. **Drag to move**: flat *Parallel* layers can be dragged directly on the
   canvas preview to reposition them; perspective layers are driven by
   their metric fields instead.
6. **Export**: renders the composed image at the full target resolution
   and downloads it as a JPEG.

Everything runs client-side; no data leaves the browser.
