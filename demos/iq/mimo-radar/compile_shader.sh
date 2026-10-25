#!/bin/sh
set -eu
cd "$(dirname "$0")"
"${QSB:-/usr/lib/qt6/bin/qsb}" --glsl "300 es,120,150" --hlsl 50 --msl 12 -o heatmap.frag.qsb heatmap.frag
"${QSB:-/usr/lib/qt6/bin/qsb}" --glsl "300 es,120,150" --hlsl 50 --msl 12 -o heatmap.vert.qsb heatmap.vert
