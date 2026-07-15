# Phase A Runbook — Vanilla Krita 5.3.2.1 Build (Windows, PowerShell)

Goal: `C:\dev\krita-install\bin\krita.exe --version` prints `5.3.2.1`. Don't
touch any plugin code until this works.

## 1. Python 3.13 (side-by-side with your existing 3.12)

```powershell
winget install Python.Python.3.13
py -3.13 --version   # confirm it's on the machine, doesn't need to replace 3.12
```

## 2. LLVM-MinGW toolchain (clang-21, UCRT)

⚠ Confirm the exact filename/URL on `docs.krita.org/en/untranslatable_pages/building_krita.html`
first — this session found it moved to `llvm-mingw-20251118-ucrt` as of
2026-04-21, but pinned-toolchain builds get updated and I couldn't re-check
the live page from this sandbox (403'd through the proxy here).

```powershell
# Download the exact build docs.krita.org links (from github.com/mstorsjo/llvm-mingw releases),
# then unzip to a path with NO SPACES:
Expand-Archive llvm-mingw-20251118-ucrt-x86_64.zip -DestinationPath C:\llvm-mingw
C:\llvm-mingw\bin\clang.exe --version   # expect clang version 21.x
```

## 3. CMake + Ninja

```powershell
cmake --version   # must be 3.31.x -- Krita's build scripts reportedly break on CMake 4.x
ninja --version   # install via `winget install Ninja-build.Ninja` if missing
```

## 4. Clone the three repos (sibling to this project, not inside it)

```powershell
mkdir C:\dev
git clone https://github.com/KDE/krita-deps-management C:\dev\krita-deps-management
git clone https://github.com/KDE/krita-ci-utilities C:\dev\krita-ci-utilities
git clone https://invent.kde.org/graphics/krita C:\dev\krita-src
cd C:\dev\krita-src
git checkout v6.0.2.1
```
There is no `v5.3.2.1` tag — `v6.0.2.1` is the shared Qt5/Qt6 source tree;
which version you get is decided by a CMake flag in step 6, not the git tag.
(Verified this session against krita.org's own release notes and the GitHub
tag list.)

## 5. Python venv + dependency-management requirements

```powershell
py -3.13 -m venv C:\dev\venv313
C:\dev\venv313\Scripts\Activate.ps1
pip install -r C:\dev\krita-deps-management\requirements.txt
```

## 6. Run setup-env.py

⚠ Re-verify exact flags live — this is the step most likely to have drifted
since the research was gathered.

```powershell
python C:\dev\krita-deps-management\tools\setup-env.py `
  --full-krita-env `
  --llvm-mingw-path C:\llvm-mingw `
  --ninja-path <path-to-ninja.exe>
```
This should produce a generated environment/toolchain file and a prebuilt
dependency prefix. Note wherever it tells you the generated env file lives —
you'll source/activate it before the CMake step below if it doesn't do so
automatically.

## 7. CMake configure

```powershell
cd C:\dev\krita-src
mkdir build
cd build
cmake .. -G Ninja `
  -DCMAKE_INSTALL_PREFIX=C:\dev\krita-install `
  -DBUILD_TESTING=OFF
  # deliberately NOT passing -DBUILD_WITH_QT6=ON -- default OFF gives the
  # Qt5 / "5.3.2.1"-reporting build
```

## 8. Build + install

```powershell
ninja -j8 install
```
Expect roughly an hour once the dependency cache from step 6 is in place —
this is not a from-scratch Qt build.

## 9. Verify

```powershell
C:\dev\krita-install\bin\krita.exe --version
# expect: 5.3.2.1
```

If this passes, Phase A is done — do not modify `C:\Program Files\Krita (x64)`
(the separately-installed runtime) or `%APPDATA%\krita\pykrita\clonestamp\`
(the Python plugin fallback); this build is entirely separate from both.

Once `krita.exe` launches and reports the right version, Phase B starts:
creating `plugins/tools/tool_clonestamp/` from scratch against
`plugins/tools/tool_smart_patch/` as the template (see CLAUDE.md — this repo
already has the exact current file paths confirmed via a read-only source
check this session).
