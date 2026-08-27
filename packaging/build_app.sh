#!/usr/bin/env bash
# Build MindControl.app: a self-contained menu-bar app you can keep in the Dock.
#
# The bundle carries its own relocatable CPython, so once it is installed it does
# not depend on the checkout it was built from.
#
#   packaging/build_app.sh              # build into build/MindControl.app
#   ICON=lock packaging/build_app.sh    # a different icon concept
#   SIGN_IDENTITY="..." packaging/…     # sign with a certificate, not ad-hoc
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
APP="$BUILD/MindControl.app"
CONTENTS="$APP/Contents"
RES="$CONTENTS/Resources"
ICON="${ICON:-gaze}"
PY_VERSION="${PY_VERSION:-3.12.9}"
BUNDLE_ID="com.griffinstrier.mindcontrol"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"

say() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }

# ------------------------------------------------------------------------ skeleton

say "assembling the bundle"
rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$RES"
cp "$ROOT/config.toml" "$RES/config.toml"

cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>MindControl</string>
    <key>CFBundleDisplayName</key><string>MindControl</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleExecutable</key><string>MindControl</string>
    <key>CFBundleIconFile</key><string>MindControl</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <!-- A status-bar app: no Dock tile while it runs, no menu bar of its own. -->
    <key>LSUIElement</key><true/>
    <key>NSCameraUsageDescription</key>
    <string>MindControl watches your hands and eyes through the camera so they can move the cursor.</string>
</dict>
</plist>
PLIST

# ----------------------------------------------------------------------- payload

say "copying a relocatable CPython $PY_VERSION"
uv python install "$PY_VERSION" >/dev/null
PY_SRC="$(uv python dir)/cpython-$PY_VERSION-macos-aarch64-none"
[ -x "$PY_SRC/bin/python3" ] || { echo "no standalone python at $PY_SRC" >&2; exit 1; }
mkdir -p "$RES/python"
ditto "$PY_SRC" "$RES/python"
PY="$RES/python/bin/python3"
# uv marks its managed interpreters PEP 668 externally-managed so nobody installs
# into the shared copy. This one is private to the bundle, which is the whole point.
rm -f "$RES/python/lib/python3.12/EXTERNALLY-MANAGED"
# clang records the dylib's LC_ID; point that at @rpath so the launcher does not
# bake in the path of the uv store copy this was duplicated from.
install_name_tool -id @rpath/libpython3.12.dylib "$RES/python/lib/libpython3.12.dylib"

say "installing pinned dependencies"
uv export --no-dev --no-emit-project --format requirements-txt -o "$BUILD/requirements.txt" >/dev/null
"$PY" -m pip install --quiet --no-input --disable-pip-version-check \
    --requirement "$BUILD/requirements.txt"
"$PY" -m pip install --quiet --no-input --disable-pip-version-check --no-deps "$ROOT"
# Nothing in the bundle should be rebuilding itself once it lives under
# /Applications, so bytecode is compiled here instead.
"$PY" -m compileall -q "$RES/python/lib/python3.12/site-packages/mindcontrol" >/dev/null 2>&1 || true

# --------------------------------------------------------------------------- icon

say "rendering the $ICON icon"
# Drawn with the bundle's own interpreter: it already has pyobjc, so building the
# app never depends on the development virtualenv.
PYTHON="$PY" ICON="$ICON" "$ROOT/packaging/icns.sh" "$RES/MindControl.icns"

# -------------------------------------------------------------------------- helper

if [ -f "$ROOT/native/Package.swift" ]; then
    say "building the native cursor helper"
    if swift build -c release --package-path "$ROOT/native" >/dev/null 2>&1; then
        cp "$ROOT/native/.build/release/mindcontrol-bridge" "$CONTENTS/MacOS/mindcontrol-bridge"
    else
        echo "    helper did not build; the app will fall back to the raw pointer" >&2
    fi
fi

# ------------------------------------------------------------------------ launcher

say "compiling the bundle executable"
"$ROOT/packaging/launcher.sh" "$APP"

# --------------------------------------------------------------------------- sign

# A certificate keeps the Camera, Accessibility and Menu Bar grants across builds
# and ad-hoc does not; identity.sh explains why and picks one off the keychain.
SIGN="$("$ROOT/packaging/identity.sh")"
say "signing as $([ "$SIGN" = - ] && echo ad-hoc || echo "$SIGN")"
codesign --force --deep --sign "$SIGN" "$APP" >/dev/null 2>&1 \
    || echo "    could not sign with $SIGN; permissions will need re-granting" >&2

say "built $APP ($(du -sh "$APP" | cut -f1))"
