# Matrix Grid Tool

A standalone, browser-based generator for grid reference images, useful as
a base reference (e.g. a composition guide or ControlNet-style conditioning
image) when generating AI images. It draws **animated, measurement-based
camera perspective** — real horizontal planes and vertical walls in metres,
projected through a simple pinhole camera (eye height + focal length) — as
well as flat square grids. It opens on a moving "game floor" grid and lets
you stack multiple auto-coloured planes. It has nothing to do with the
Clone Stamp tool elsewhere in this repository; it just happens to live in
the same repo.

## Usage

Open `index.html` directly in any modern browser (desktop or mobile —
works fine over `file://`, no server or install required).

1. **Auflösung**: pick a resolution preset (default 5:4 portrait,
   1536×1920) or type a custom width/height. The swap button flips
   portrait/landscape.
   On load you get the default scene: a **black background with a white
   receding floor grid that animates toward you** — the classic moving
   "game floor".
2. **Hintergrund**: pick the background fill color (swatches or a custom
   color picker); the default is black. JPEG has no transparency, so the
   background is always opaque.
3. **Kamera (Perspektive)**: a simple horizontal pinhole camera that
   drives the perspective layer types. Set the **Standpunkthöhe** (eye
   height above the ground, in metres) and the **Brennweite** (focal
   length in mm, on a fixed full-frame 36 mm sensor — shorter = wider
   angle). The view is level, so the horizon sits on eye level at the
   image centre (toggle *Horizont anzeigen* to show it).
4. **Animation**: the grids move by default. **Play/Pause** stops or
   starts the motion and **Tempo** scales the global speed; each layer has
   its own **Bewegung** (speed/direction). Pause before exporting a still.
5. **Ebenen**: the default layer is a *Horizontale Ebene (Perspektive)* —
   the animated floor. Click **+ Ebene hinzufügen** to add more; new
   planes are placed higher and **auto-coloured sensibly** (the base grid
   is white; further/higher planes take a soft hue and fade to be more
   subtle). **🎲 Zufall** rolls a whole new random constellation (camera,
   layers, colours, motion, accents) — keep clicking to try out different
   looks. Each layer has line width, color, opacity, motion, an accent
   (below), and can be shown/hidden; list order is the stacking order (top
   card drawn last). Click a layer card to make it the *active* layer.
   Each layer's **Typ**:
   - *Horizontale Ebene (Perspektive)*: a real horizontal plane at a given
     **Höhe** (0 = floor) with a metric **Zellgröße**, projected through
     the camera so it recedes correctly into depth toward the horizon.
     Stack several at different heights.
   - *Vertikale Wand (Perspektive)*: a real fronto-parallel wall at a
     given **Entfernung** (distance), with metric height, width and cell
     size. Nearer walls appear larger.
   - *Parallel – horizontal/vertikal*: flat image-space grid lines
     (offset draggable on the canvas).

   **Betonung** (accent): set it to N ≥ 2 to emphasise every N-th grid
   line (*Linie*) or every N-th cell (*Fläche*) in a separate colour.
   Overlapping planes with different accents build up interesting
   interference/moiré patterns.
6. **Drag to move**: flat *Parallel* layers can be dragged directly on the
   canvas preview to reposition them; perspective layers are driven by
   their metric fields instead.
7. **Export**: renders the composed image at the full target resolution
   and downloads it as a JPEG (pause the animation first for a clean
   still).

Everything runs client-side; no data leaves the browser.
