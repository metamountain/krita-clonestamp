@echo off
rem Install the freshly built clonestamp tool into the official Krita, also while Krita runs:
rem a loaded DLL can be renamed but not overwritten, so the old one is moved aside first.
rem Takes effect at the next Krita start. Needs admin (Program Files).
rem   deploy-tool.cmd [path\to\kritatoolclonestamp.dll]   (default: %KRITA_DEV%\build68\bin\...)
set PLUG=C:\Program Files\Krita (x64)\lib\kritaplugins
if "%KRITA_DEV%"=="" set KRITA_DEV=D:\_Code\Krita\dev
if "%~1"=="" (set NEW=%KRITA_DEV%\build68\bin\kritatoolclonestamp.dll) else (set NEW=%~1)
rem A running Krita locks the DLL against overwriting, but it can be moved.
rem Move it OUT of kritaplugins: Krita loads every file in that folder, whatever
rem its extension, so a renamed copy left there would still register.
del /f /q "%PLUG%\kritatoolclonestamp.dll.*" 2>nul
if exist "%PLUG%\kritatoolclonestamp.dll" move /y "%PLUG%\kritatoolclonestamp.dll" "%TEMP%\kritatoolclonestamp-old-%RANDOM%.dll" >nul
copy /y "%NEW%" "%PLUG%\kritatoolclonestamp.dll" || (echo COPY FAILED & exit /b 1)
echo DEPLOY OK
