#!/usr/bin/env bash
# Package build/MindControl.app as a drag-to-install disk image.
#
#   packaging/make_dmg.sh     # writes dist/MindControl-<version>.dmg
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
APP="$BUILD/MindControl.app"
DIST="$ROOT/dist"
VOLUME="MindControl"
MOUNT="/Volumes/$VOLUME"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
DMG="$DIST/MindControl-$VERSION.dmg"

say() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }

[ -d "$APP" ] || { echo "no app at $APP; run packaging/build_app.sh first" >&2; exit 1; }

# ------------------------------------------------------------------------ staging

say "staging the image contents"
STAGE="$BUILD/dmg"
rm -rf "$STAGE"; mkdir -p "$STAGE/.background"
ditto "$APP" "$STAGE/MindControl.app"
ln -s /Applications "$STAGE/Applications"

"$APP/Contents/Resources/python/bin/python3" "$ROOT/packaging/dmg_background.py" >/dev/null
# One TIFF holding both scales, so the window is crisp on a Retina display and
# still sized in points on a non-Retina one.
tiffutil -cathidpicheck "$BUILD/icons/dmg-background.png" "$BUILD/icons/dmg-background@2x.png" \
    -out "$STAGE/.background/background.tiff" >/dev/null

# --------------------------------------------------------------------- read-write

say "creating the image"
mkdir -p "$DIST"
rm -f "$DMG" "$BUILD/rw.dmg"
hdiutil detach "$MOUNT" >/dev/null 2>&1 || true
SIZE=$(( $(du -sm "$STAGE" | cut -f1) + 80 ))
hdiutil create -srcfolder "$STAGE" -volname "$VOLUME" -fs HFS+ \
    -fsargs "-c c=64,a=16,e=16" -format UDRW -size "${SIZE}m" "$BUILD/rw.dmg" >/dev/null
hdiutil attach "$BUILD/rw.dmg" -readwrite -noverify -noautoopen -mountpoint "$MOUNT" >/dev/null

# ---------------------------------------------------------------------- appearance

say "arranging the window"
# Finder is scripted through Apple events, which need Automation permission the
# first time. If that is refused the image is still perfectly installable, just
# plainly laid out, so a refusal must not fail the build.
cat > "$BUILD/layout.applescript" <<APPLESCRIPT
tell application "Finder"
    tell disk "$VOLUME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {180, 140, 840, 560}
        set options to the icon view options of container window
        set arrangement of options to not arranged
        set icon size of options to 128
        set text size of options to 13
        set background picture of options to file ".background:background.tiff"
        set position of item "MindControl.app" of container window to {165, 218}
        set position of item "Applications" of container window to {495, 218}
        update without registering applications
        close
    end tell
end tell
APPLESCRIPT

osascript "$BUILD/layout.applescript" >/dev/null 2>&1 &
layout=$!
( sleep 45; kill "$layout" 2>/dev/null ) >/dev/null 2>&1 &
watchdog=$!
wait "$layout" 2>/dev/null || echo "    Finder layout skipped (Automation permission)" >&2
kill "$watchdog" 2>/dev/null || true

# The volume icon is written to the mounted volume rather than staged into the
# source folder, which hdiutil does not carry across. The custom-icon attribute
# is what makes Finder prefer it over the plain disk image picture.
cp "$APP/Contents/Resources/MindControl.icns" "$MOUNT/.VolumeIcon.icns"
SetFile -a C "$MOUNT" 2>/dev/null || true
chmod -Rf go-w "$MOUNT" 2>/dev/null || true
sync

# ---------------------------------------------------------------------- compress

say "compressing"
for _ in 1 2 3; do
    hdiutil detach "$MOUNT" >/dev/null 2>&1 && break
    sleep 3
done
# LZFSE rather than zlib: a smaller image in a fraction of the time, and every
# system this app supports can read it.
hdiutil convert "$BUILD/rw.dmg" -format ULFO -o "$DMG" >/dev/null
rm -f "$BUILD/rw.dmg"

say "built $DMG ($(du -sh "$DMG" | cut -f1))"
