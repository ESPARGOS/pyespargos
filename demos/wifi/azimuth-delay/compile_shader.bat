@echo off
setlocal

set "DIR=%~dp0"
set "QSB=C:\Qt\6.10.2\mingw_64\bin\qsb.exe"

if not exist "%QSB%" (
    echo qsb.exe not found. Ensure Qt6 is installed and qsb.exe is in PATH or PyQt6 is installed.
    exit /b 1
)

"%QSB%" --glsl "300 es,120,150" --hlsl 50 --msl 12 -o "%DIR%fragment_shader.qsb" "%DIR%fragment_shader.frag"
"%QSB%" --glsl "300 es,120,150" --hlsl 50 --msl 12 -o "%DIR%vertex_shader.qsb" "%DIR%vertex_shader.vert"

endlocal
