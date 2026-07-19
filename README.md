# Clonestamp Tool with Preview

A Photoshop-style Clone Stamp for [Krita](https://krita.org): Ctrl+click to
sample a source point, then drag to paint a soft-edged copy of it anywhere
else — with a live preview while you paint and a single undo step per
stroke. Krita has no native equivalent of this tool today; this project
adds one.

![Demo](https://raw.githubusercontent.com/metamountain/krita-clonestamp/main/docs/preview.gif)

## Features

- **Ctrl+click sampling** with a frozen source snapshot — strokes that
  cross their own source clone the *original* pixels, never freshly
  painted ones (matching Photoshop's behavior).
- **Live preview**: a ghost of the source content rides under the brush
  cursor, and the stroke itself is previewed on-canvas while you drag.
- **Soft round brush** with adjustable size (1–2000 px), hardness, and
  opacity; overlapping dabs within one stroke never exceed the chosen
  opacity.
- **On-canvas resize**: Shift+drag horizontally for size, vertically for
  hardness, with a live ring readout at the gesture's anchor point.
- **Aligned / Non-Aligned** source offset modes and **Sample: Current
  Layer / All Layers**.
- **One undo step per stroke.**
- **Built-in updater**: the docker's *Check for Updates* button installs
  the latest version from this repository in place.

## Installation

1. Download **[clonestamp.zip](https://github.com/metamountain/krita-clonestamp/raw/main/python-plugin/clonestamp.zip)**
   (the ready-to-import plugin package, rebuilt from source on every
   change — not the whole repository).
2. In Krita: **Tools › Scripts › Import Python Plugin from File...** and
   select the downloaded zip. (**Import Python Plugin from Web...** with
   the URL above pasted in works too.)
3. Restart Krita, then enable the docker under **Settings › Dockers ›
   Clonestamp Tool with Preview**.

Works with any reasonably recent Krita installation — no build step. The
installed version is shown at the bottom of the docker; *Check for
Updates* keeps it current from then on (a restart is required after an
update, since Krita loads Python plugins only at startup).

## Usage

| Action | Gesture |
| --- | --- |
| Enable the tool | Check **Enable Clone Brush** in the docker |
| Sample a source point | **Ctrl+click** on the canvas |
| Paint | **Click and drag** |
| Resize brush / adjust hardness | **Shift+drag** (horizontal / vertical) |
| Toggle the ring cursor | **Brush cursor outline** checkbox in the docker |

Requirements: a **paint layer** in **RGBA/8-bit** color (the tool checks
and reports anything else), and an unrotated, unmirrored canvas — the
plugin detects rotation/mirroring and asks you to reset it rather than
painting at wrong coordinates. Switching to another document disables the
brush automatically; re-enable and resample there.

## Repository layout

| Path | Contents |
| --- | --- |
| `python-plugin/` | The installable plugin (pure Python on Krita's `libkis` scripting API) and the packaged `clonestamp.zip`. |
| `Tool-plugin/` | A native C++ `KisTool` implementation of the same tool, intended for eventual upstream submission to Krita. Not installable on its own — it only builds inside a full Krita source checkout (see `Tool-plugin/NOTE.md`). |
| `docs/` | Development history, build/toolchain notes, and per-change test documentation. |

## Why two implementations?

Krita's Python scripting API cannot register a new entry in the native
toolbox — that requires a compiled `KisTool`/`KoToolFactoryBase` built as
part of Krita itself. The Python plugin works around this with a docker
and an application-level mouse event filter, and is what you can install
today. The C++ tool is the long-term path to a real toolbox icon, but a
compiled Krita plugin is ABI-locked to the exact Krita build it was
compiled against, so it cannot be distributed as a drop-in for existing
installations. Both implementations share the same algorithm and are kept
deliberately in sync.

## Technical notes and known limitations

- **Strokes commit at mouse release.** A drag accumulates dabs into an
  in-memory alpha mask and writes the composited result to the layer once
  — the only way to get a single undo step through the scripting API,
  which exposes no undo grouping. The on-canvas preview during the drag is
  a close approximation; the committed pixels are always computed the
  exact way. On large canvases this buffer is sizeable (capped at ~800 MB;
  larger canvases are refused with a message).
- **Mouse input is polled.** Continuous mouse-move is not exposed to
  Krita's scripting API, so drags are tracked by polling the cursor at
  30 ms intervals via a global event filter (a technique adapted from the
  Krita Artists forum).
- **The native brush outline is swapped out.** While the brush is enabled,
  Krita's own brush-outline cursor is toggled off and the plugin draws its
  own ring cursor instead; if Krita exits abnormally mid-session, that
  toggle can be left inverted (toggle it back via the `toggle_brush_outline`
  shortcut or by enabling/disabling the plugin once).
- **Diagnostics are off by default** (they cost real per-stroke I/O). To
  enable: create an empty file named `clonestamp_debug.enable` in your
  `%TEMP%` folder and restart Krita; logs go to `clonestamp_debug.txt`
  next to it. Delete the file and restart to disable.

## Project status and contributing

This project was built by **metamountain** — not a professional
programmer — in AI-assisted pair programming with Claude Code, and has
been validated by hands-on testing rather than an automated test suite.
It works, and it is honest about what it is: a well-documented prototype
with rough edges likely remaining.

**If you are an experienced Krita, Qt, or KDE developer**, your review,
maintenance, or help shepherding the C++ tool through Krita's contribution
process could turn this from a personal prototype into something upstream.
The `docs/` directory preserves the full development trail — including
dead ends and their resolutions — precisely to make that handover
feasible.

Bug reports are welcome and genuinely useful: please
[open an issue](https://github.com/metamountain/krita-clonestamp/issues)
describing what you did and what happened.

## Credits

- **[Krita](https://krita.org)** and the **KDE community** — the
  application and APIs this project builds on.
- **Krita Artists forum** — origin of the global-event-filter technique
  for canvas mouse capture from Python.
- **[Acly/krita-ai-tools](https://github.com/Acly/krita-ai-tools)** —
  studied as the closest precedent for distributing a Krita plugin with a
  compiled component; its pure-Python packaging approach informed this
  project's distribution model.
- **`fonkle/clonestamp_tool`** (2022 branch on `invent.kde.org`) —
  examined as prior art for native toolbox registration.
- Built with AI pair-programming assistance from **Claude Code**
  (Anthropic).

## License

- Repository overall and `Tool-plugin/`: **GPL-2.0-or-later** (see
  `LICENSE`) — the C++ tool derives from and links against Krita's GPL
  codebase.
- `python-plugin/`: **CC0-1.0** (public-domain dedication, per the SPDX
  headers in its files) — deliberately unencumbered.
