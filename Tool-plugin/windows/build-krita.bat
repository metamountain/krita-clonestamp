@echo off
rem Build the clonestamp tool DLL for the official Krita 6.0.4 (Qt 6.8) Windows release.
rem Toolchain/flags mirror krita-src/build-tools/ci-scripts/windows.yml (windows-release-qt6);
rem deps come from the krita-deps-management branch transition.now/qt6.8.0 (Qt 6.8.0,
rem the Qt that ships with 6.0.4 -- the newer transition.now/qt6 deps are Qt 6.11 and
rem would produce a DLL that cannot load into 6.0.4).
rem
rem   build-krita.bat            configure (first time) + build the tool (+ needed Krita libs)
rem   build-krita.bat full       build + install the complete Krita into dev\install
rem   build-krita.bat deploy     copy the tool DLL into the installed Krita (needs admin)
setlocal
if "%KRITA_DEV%"=="" (set DEV=D:\_Code\Krita\dev) else (set DEV=%KRITA_DEV%)
if "%KRITA_SRC%"=="" set KRITA_SRC=D:\_Code\Krita\krita-src
call %DEV%\env68\base-env.bat || exit /b 1
set PATH=%DEV%\cmake-3.31.8-windows-x86_64\bin;%DEV%\ninja;%PATH%
set BUILD=%DEV%\build68
set INSTALL=%DEV%\install68
set KRITA_INSTALLED=C:\Program Files\Krita (x64)

if not exist %BUILD%\build.ninja (
  cmake -S %KRITA_SRC% -B %BUILD% -G Ninja ^
    -DCMAKE_BUILD_TYPE=RelWithDebInfo ^
    -DCMAKE_PREFIX_PATH=%DEV%\deps68 ^
    -DCMAKE_INSTALL_PREFIX=%INSTALL% ^
    -DBUILD_WITH_QT6=ON -DALLOW_UNSTABLE=QT6 ^
    -DHIDE_SAFE_ASSERTS=ON -DBUILD_TESTING=OFF ^
    -DKRITA_ENABLE_PCH=OFF || exit /b 1
)

if "%1"=="full" (
  cmake --build %BUILD% --target install || exit /b 1
) else if "%1"=="deploy" (
  copy /y %BUILD%\bin\kritatoolclonestamp.dll "%KRITA_INSTALLED%\lib\kritaplugins\" || exit /b 1
) else (
  cmake --build %BUILD% --target kritatoolclonestamp || exit /b 1
)
echo BUILD OK
