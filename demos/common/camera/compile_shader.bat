@echo off
cd /d "%~dp0"
if not defined QSB set QSB=qsb
"%QSB%" --glsl "100 es,120,150" --hlsl 50 --msl 12 -o spatialspectrum_vert.qsb spatialspectrum.vert
if errorlevel 1 exit /b 1
"%QSB%" --glsl "100 es,120,150" --hlsl 50 --msl 12 -o spatialspectrum.qsb spatialspectrum.frag
