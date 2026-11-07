#!/bin/sh
set -eu
exec sh "$(dirname "$0")/../../common/camera/compile_shader.sh"
