#!/bin/bash

DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

/usr/lib/qt6/bin/qsb --qt6 -o $DIR/spatialspectrum.qsb $DIR/spatialspectrum.frag
/usr/lib/qt6/bin/qsb --qt6 -o $DIR/spatialspectrum_vert.qsb $DIR/spatialspectrum.vert
