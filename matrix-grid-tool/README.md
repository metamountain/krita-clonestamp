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

The UI is built like a **TR-808-style jam surface** — a row of performance
dials you scrub for instant, chaotic results:

- **Geschwindigkeit** — global animation speed.
- **Kraftfeld** — the plane density/spacing: turn it up and the planes
  spread apart, down and they spring/settle together ("einfedern"). A
  constant spring stiffness makes them bounce into the new spacing.
- **H-Ebenen / V-Ebenen** — spawn or remove horizontal planes / vertical
  walls *live* as you turn the dial.
- **Farbe** — rotates the hue of the whole palette.
- **Zusatzfelder** — the coloured field pattern for every layer: Aus (off),
  2×2 / 3×3 / 4×4 lattices, Checker, Checker −1 (inverse), Diagonal,
  Reihen (rows), Spalten (columns), Streu (scatter), or Zufall (random
  per layer). Each layer can also override its own pattern in the editor.
- **▶/⏸**, **💥 Anstoßen** (kick the stack), **🎲** (re-roll a whole batch).

Below the jam surface: a **Physik (fein)** panel (Dichte, Gravitation,
Dämpfung) and the **Ebenen** list/editor. The rarely-changed base settings
(Auflösung with presets incl. **4:5** and **Custom**, Kamera, Hintergrund,
and an optional **Rasterlinien-Limit** — cap the number of grid lines per
plane, e.g. horizontal 10 / vertical 0 for horizontal bands only)
live behind the **⚙️ gear** (top-right) so the preview keeps maximum area —
handy on a phone. The tool opens on a **10-plane stack** at **4:5**; **🗑**
clears back to one.

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
5. **Physik (Stapel)**: the horizontal planes behave like a physical
   stack. **Gravitation** pulls them down, **Abstoßung** pushes them apart,
   **Dichte** sets the rest gap between them (higher = tighter), and
   **Dämpfung** controls how bouncy/settled it is. **💥 Anstoßen** kicks
   the stack. It only runs while the animation plays; turn **Physik an**
   off to set plane heights manually with the Höhe slider instead.
6. **Ebenen**: the default scene is a *stack of horizontal planes*
   (*Horizontale Ebene (Perspektive)*) that settles under the physics. Click **+ Ebene hinzufügen** to add more; new
   planes are placed higher and **auto-coloured sensibly** (the base grid
   is white; further/higher planes take a soft hue and fade to be more
   subtle). **🎲 Zufall** rolls a whole new random constellation (camera,
   layers, colours, motion, accents) — keep clicking to try out different
   looks. The layer list is **compact** — one line per layer (colour chip,
   name, 👁 visibility toggle) so it scales to many layers. **Tap a row**
   to make it the *active* layer; its full controls (type, sliders, colour,
   opacity, motion, accent, reorder ▲▼, delete ✕) appear in the **Aktive
   Ebene** panel below. List order is the stacking order (top row drawn
   last). Each layer's **Typ**:
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
7. **Drag to move**: flat *Parallel* layers can be dragged directly on the
   canvas preview to reposition them; perspective layers are driven by
   their metric fields instead.
8. **Export**: renders the composed image at the full target resolution
   and downloads it as a JPEG (pause the animation first for a clean
   still).

Everything runs client-side; no data leaves the browser.
