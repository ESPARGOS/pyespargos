#!/bin/bash

DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

/usr/lib/qt6/bin/qsb --glsl "300 es,120,150" --hlsl 50 --msl 12 -o $DIR/fragment_shader.qsb $DIR/fragment_shader.frag
/usr/lib/qt6/bin/qsb --glsl "300 es,120,150" --hlsl 50 --msl 12 -o $DIR/vertex_shader.qsb $DIR/vertex_shader.vert
