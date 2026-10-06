@echo off
rem Clone Stamp tool for Krita 6.0.4 / 6.0.4.1 (official Windows x64 build).
rem Copies kritatoolclonestamp.dll into Krita's plugin folder. Run it, confirm the
rem admin prompt, restart Krita. The tool appears in the toolbox next to Smart Patch.
rem
rem Optional argument: Krita install folder (default C:\Program Files\Krita (x64)).
setlocal
set "KRITA=%~1"
if "%KRITA%"=="" set "KRITA=C:\Program Files\Krita (x64)"
set "HERE=%~dp0"
set "PLUG=%KRITA%\lib\kritaplugins"

if not exist "%KRITA%\bin\krita.exe" (
  echo Krita not found in "%KRITA%".
  echo Usage: install.cmd "D:\Path\To\Krita"
  pause & exit /b 1
)

rem This DLL only loads into Krita 6.0.4.x (Qt 6.8) -- check before copying.
for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "(Get-Item '%KRITA%\bin\krita.exe').VersionInfo.ProductVersion"`) do set "KVER=%%v"
echo Found Krita %KVER%
echo %KVER% | findstr /b /c:"6.0.4" >nul || (
  echo This build is for Krita 6.0.4.x only. Your Krita is %KVER%.
  echo See https://github.com/metamountain/krita-clonestamp for other versions.
  pause & exit /b 1
)

rem Program Files needs admin rights: re-run elevated if needed.
net session >nul 2>&1 || (
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0' -ArgumentList '\"%KRITA%\"'"
  exit /b
)

rem A running Krita locks the DLL against overwriting, but it may be renamed.
del /f /q "%PLUG%\kritatoolclonestamp.dll.old*" 2>nul
if exist "%PLUG%\kritatoolclonestamp.dll" ren "%PLUG%\kritatoolclonestamp.dll" "kritatoolclonestamp.dll.old%RANDOM%"
copy /y "%HERE%kritatoolclonestamp.dll" "%PLUG%\kritatoolclonestamp.dll" >nul || (
  echo Copy failed. & pause & exit /b 1
)
echo.
echo Installed. Restart Krita -- the Clone Stamp is in the toolbox next to Smart Patch.
echo To uninstall, delete "%PLUG%\kritatoolclonestamp.dll".
pause
