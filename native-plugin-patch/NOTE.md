# Native plugin patch — not a standalone build

These six files are the source of a native `KisTool`/`KoToolFactoryBase`
Krita toolbox plugin (`plugins/tools/tool_clonestamp/`). They are **not**
meant to be built on their own — they only compile as part of Krita's own
CMake build, added as a subdirectory of Krita's own source tree.

This is the **upstream-submission candidate**, not something end users
should try to install: a compiled native Krita plugin's ABI is locked to
the exact compiler/Qt/KDE-Frameworks/Krita-commit combination it was built
against. There is no supported way to drop this into an existing Krita
installation. If you want to actually use the Clone Stamp tool today, see
`../python-plugin/` instead — that one installs into any existing Krita via
Tools › Scripts › Import Python Plugin from File, no build required.

## How to try building this yourself

1. Clone Krita: `git clone https://invent.kde.org/graphics/krita`
2. Check out (or start from a branch based on) commit
   `2b927d92183e4722ac1561b25bc83b65438dffd7` (2026-07-13) — the commit this
   patch was developed against. It will very likely still apply cleanly to
   a more recent `master`, but hasn't been re-verified against one.
3. Copy these six files into `plugins/tools/tool_clonestamp/` in that
   checkout.
4. Add one line to `plugins/tools/CMakeLists.txt`:
   ```cmake
   add_subdirectory( tool_clonestamp )
   ```
5. Build Krita following its own
   [Windows](https://docs.krita.org/en/untranslatable_pages/building_krita.html)
   or Linux build docs. The plugin builds as its own CMake target
   (`kritatoolclonestamp`) alongside the rest of Krita.

See `../docs/toolchain-paths.md` for the exact resolved build recipe (CMake/
Ninja/LLVM-MinGW versions, dependency-fetching steps, gotchas hit along the
way) used to get this building on Windows.
