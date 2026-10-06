# Native C++ Clone Stamp tool — flagship product

These files are the source of a native `KisTool`/`KoToolFactoryBase` Krita
toolbox plugin (`plugins/tools/tool_clonestamp/`). They are **not** a
standalone build — they only compile as part of Krita's own CMake build,
added as a subdirectory of Krita's own source tree.

The tool is **installable**: a prebuilt `kritatoolclonestamp.dll` for the
official Krita 6.0.4 Windows x64 build ships in
`../release/krita-6.0.4-windows-x64/` and is installed with `install.cmd`
(see the README). It appears in the toolbox next to **Smart Patch**. The DLL
is ABI-locked to that exact Krita build (Qt 6.8), so it fits **only** Krita
6.0.4.x on Windows x64 and every new Krita version needs a rebuild. This is
also the **upstream-submission candidate** (a real toolbox icon).

## Building the DLL for Krita 6.0.4 (Windows)

This replaces the old `C:\dev` / Qt5 / `master` recipe. The build must use
exactly the toolchain of the official 6.0.4 release build, or the DLL will
not load:

- **llvm-mingw 20251118** (clang 21.1.6, UCRT)
- **CMake 3.31.8** (not 4.x)
- **Ninja 1.13.2**
- **Python 3.13** venv
- **Krita deps**: `krita-deps-management` branch **`transition.now/qt6.8.0`**
  (Qt 6.8.0 — the Qt that ships with 6.0.4). Fetch with
  `setup-env.py --full-krita-env --branch transition.now/qt6.8.0`. The newer
  `transition.now/qt6` branch has Qt 6.11 and produces DLLs that do **not**
  load into 6.0.4.
- **Krita source**: tag **`v6.0.4.1`**.
- **CMake flags** (as the CI job `windows-release-qt6`):
  ```
  -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBUILD_WITH_QT6=ON -DALLOW_UNSTABLE=QT6 -DHIDE_SAFE_ASSERTS=ON -DBUILD_TESTING=OFF
  ```
- Build only the target **`kritatoolclonestamp`** (~1600 steps the first
  time, minutes afterwards).

Scripts: `windows/build-krita.bat` (env vars `KRITA_DEV`, `KRITA_SRC`) and
`windows/deploy-tool.cmd`.

To add the plugin to a Krita source checkout: put these files in
`krita-src/plugins/tools/tool_clonestamp/` and add one line to
`plugins/tools/CMakeLists.txt`:
```cmake
add_subdirectory( tool_clonestamp )
```
CMake links `kritaui kritalibbrush kritaresources kritaresourcewidgets
kritawidgetutils`; icons are embedded via `tool_clonestamp.qrc`.

**Binary compatibility check:** after building, confirm the DLL links only
against what the installed Krita exports — `llvm-readobj --coff-imports` of
the DLL against `--coff-exports` of the installed Krita DLLs (every import
must exist).

## Historical note

This patch was originally developed against Krita commit
`2b927d92183e4722ac1561b25bc83b65438dffd7` (2026-07-13) on `master` with a
Qt5 toolchain. That recipe is superseded by the 6.0.4/Qt6.8 recipe above;
the commit is recorded here only as provenance. See `../docs/toolchain-paths.md`
for the original Windows build bring-up.
