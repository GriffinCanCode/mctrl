#!/usr/bin/env bash
# Compile Contents/MacOS/MindControl, the bundle executable.
#
# A Mach-O, not a script that exec's python3: NSBundle.mainBundle() is derived
# from the running image, and macOS 26 will not host a status item with no id.
#
# Called by build_app.sh and by `make refresh`, so that editing launcher.c is
# enough. A stale launcher in a refreshed bundle is invisible until something
# re-execs sys.executable -- the overlay, which spawns -- and then fails.
#
#   packaging/launcher.sh build/MindControl.app
set -euo pipefail

APP="${1:?usage: launcher.sh <path to MindControl.app>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RES="$APP/Contents/Resources"
OUT="$APP/Contents/MacOS/MindControl"

clang -Os -o "$OUT" "$ROOT/packaging/launcher.c" \
    -I "$RES/python/include/python3.12" \
    "$RES/python/lib/libpython3.12.dylib" \
    -Wl,-rpath,@executable_path/../Resources/python/lib \
    -framework CoreFoundation
# clang may still record the dylib's pre-rewrite id if the tree was reused; force
# the load path onto @rpath.
linked="$(otool -L "$OUT" | awk '/libpython/{print $1; exit}')"
if [ -n "$linked" ] && [ "$linked" != "@rpath/libpython3.12.dylib" ]; then
    install_name_tool -change "$linked" @rpath/libpython3.12.dylib "$OUT"
fi
