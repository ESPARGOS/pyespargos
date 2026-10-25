@echo off
setlocal

set "DIR=%~dp0"
set "QSB=C:\Qt\6.10.2\mingw_64\bin\qsb.exe"

if not exist "%QSB%" (
    echo qsb.exe not found. Ensure Qt6 is installed and qsb.exe is in PATH or PyQt6 is installed.
    exit /b 1
)

"%QSB%" --qt6 -o "%DIR%spatialspectrum.qsb" "%DIR%spatialspectrum.frag"
"%QSB%" --qt6 -o "%DIR%spatialspectrum_vert.qsb" "%DIR%spatialspectrum.vert"

endlocal
