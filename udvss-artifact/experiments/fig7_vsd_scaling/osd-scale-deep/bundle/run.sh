#!/bin/bash
# Runs a bundled binary with its own glibc/libs (built on Ubuntu 22.04, node94).
D=$(cd "$(dirname "$0")" && pwd)
exec "$D/lib/ld-linux-x86-64.so.2" --library-path "$D/lib" "$D/bin/$1" "${@:2}"
