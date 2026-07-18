# Clonestamp Tool with Preview

A Photoshop-style Clone Stamp for [Krita](https://krita.org): Ctrl+click to
sample a source point, drag to paint a soft-edged copy of it elsewhere, with
a live preview and proper single-step undo. Krita doesn't have this as a
native tool today — this project adds it.

## Status and an honest disclosure

This was built by **metamountain**, who is **not a programmer**, working
with AI pair-programming assistance (Claude Code). The working prototype
here is real and has been hands-on tested, but the author cannot personally
review, maintain, or extend this code at the level a project like this
eventually needs — especially the native C++ half, which would need to go
through Krita's own upstream code review to ever ship inside Krita itself.

**If you're an experienced Krita, Qt, or KDE developer and this looks
interesting: your help would genuinely make the difference between this
staying a personal prototype and it becoming something real.** That could
mean reviewing/cleaning up the code, taking over maintenance, or helping
shepherd a submission through Krita's own contribution process. See
`docs/` for the full build/debugging history — it's a detailed trail of
exactly how this was put together, including the mistakes and dead ends.

## What's in this repo

- **`python-plugin/`** — the tool you can actually install today. Pure
  Python, no build step, works with your existing Krita installation.
- **`Tool-plugin/`** — a native C++ `KisTool` implementation of the
  same tool, meant to eventually be proposed for upstream inclusion in
  Krita itself (so it would get a real toolbox icon next to Pan/Zoom,
  instead of a floating preview window). **Not installable on its own** —
  see `Tool-plugin/NOTE.md` for why and what building it requires.
- **`docs/`** — the build/debugging log from developing both versions:
  environment setup, bugs hit and fixed, design decisions and why.

## Installing the tool (normal users, no build required)

1. Download **[clonestamp.zip](https://github.com/metamountain/krita-clonestamp/releases/latest/download/clonestamp.zip)**
   from the latest release (a ready-to-import zip of just the plugin —
   not the whole repository).
2. In Krita: **Tools › Scripts › Import Python Plugin from File...**, and
   select the zip you downloaded. (Krita also has **Import Python Plugin
   from Web...**, which accepts that same direct zip URL pasted in.)
3. Restart Krita. Open the **Clonestamp Tool with Preview** docker
   (Settings › Dockers), check **Enable Clone Brush**.
4. Ctrl+click on the canvas to sample a source point, then click and drag
   to paint. Shift+drag resizes the brush (horizontal) and adjusts
   hardness (vertical, drag up for harder edges, down for softer).

This works against any reasonably recent existing Krita install — no
custom build needed. (The `python-plugin/clonestamp/` folder in this repo
is the source the release zip is built from, structured to match exactly
what Krita's own plugin importer expects — a folder named after the
plugin, containing `__init__.py` and the `.desktop` file.)

**Debug logging** (off by default — it writes to disk on every stroke tick
and makes drags laggy): create an empty file named `clonestamp_debug.enable`
in your TEMP folder (paste `%TEMP%` into the Explorer address bar to get
there) and restart Krita. The plugin then logs to `clonestamp_debug.txt` in
that same folder. Delete the `.enable` file and restart to turn it off.

## Why two implementations?

Krita's Python scripting API (`libkis`) has no way to register a new entry
in Krita's native toolbox — that requires a real `KisTool`/`KoToolFactoryBase`
compiled as part of Krita's own build. The pure-Python version above works
around that with a floating preview window and a Krita docker instead of a
toolbox icon; the native C++ version gets a real toolbox icon, at the cost
of only being installable by building the exact matching Krita source tree
yourself (a compiled Krita plugin's ABI is locked to the exact compiler/Qt/
KDE-Frameworks/commit combination it was built against — there's no
supported way to drop it into someone else's existing Krita installation).

## Credits and prior art

- **[Krita](https://krita.org)** and the **KDE community** — the
  application and toolbox/scripting APIs this project builds on and
  extends. The native half is licensed GPL-2.0-or-later to match.
- **Krita Artists community forum** — the technique used to capture canvas
  mouse events from a pure-Python plugin (a `QApplication`-global event
  filter, since continuous mouse-move isn't exposed to Krita's Python
  scripting API) was adapted from a forum discussion there.
- **[Acly/krita-ai-tools](https://github.com/Acly/krita-ai-tools)**
  (formerly krita-vision-tools) — studied as the closest real-world
  precedent for how a Krita plugin with a compiled component gets
  distributed to end users. Its actual approach (pure-Python packaging,
  zip-imported, no compiled binaries in the release) directly informed the
  decision to make the Python plugin above the primary distributable here.
- **`fonkle/clonestamp_tool`** (a 2022 branch on `invent.kde.org`) — an
  early native toolbox-registration scaffold investigated as a possible
  starting point for the native C++ version. Ultimately not used (it
  turned out to be a renamed Freehand tool with no actual clone-stamp
  logic), but worth crediting as prior art that was looked at.
- Built with AI pair-programming assistance from **Claude Code**
  (Anthropic).

## License

GPL-2.0-or-later (see `LICENSE`) for this repository overall and the
`Tool-plugin/` code specifically, since it's derived from and
links against Krita's own GPL codebase.

`python-plugin/` is separately licensed **CC0-1.0** (public domain
dedication) per the `SPDX-License-Identifier` header in each of its files —
it has no such linking constraint, and CC0 was a deliberate choice to keep
that half of the project as unencumbered as possible.
