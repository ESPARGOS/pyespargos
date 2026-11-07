#!/bin/sh
set -eu
cd "$(dirname "$0")"
shader_baker="${QSB:-qsb}"
if ! command -v "$shader_baker" >/dev/null 2>&1; then shader_baker=/usr/lib/qt6/bin/qsb; fi
"$shader_baker" --glsl '100 es,120,150' --hlsl 50 --msl 12 -o spatialspectrum_vert.qsb spatialspectrum.vert
"$shader_baker" --glsl '100 es,120,150' --hlsl 50 --msl 12 -o spatialspectrum.qsb spatialspectrum.frag
