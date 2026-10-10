#!/usr/bin/env bash
# Regenerate all icons from logo.svg and rebuild Frayme.app (macOS only).
#
#   launcher/build-app.sh
#
# Outputs, in the project root:
#   Frayme.app                                       macOS launcher with app icon
#   favicon.png, favicon.ico, apple-touch-icon.png   browser icons for the web UI
#
# Rebuilding changes the app's code signature, so macOS will ask for
# Downloads/volume access again the next time it's launched.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
APP="$ROOT/Frayme.app"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── Icons ──
osascript -l JavaScript "$HERE/make-icon.js" "$ROOT/logo.svg" "$TMP/mac.png" mac
osascript -l JavaScript "$HERE/make-icon.js" "$ROOT/logo.svg" "$TMP/web.png" web
osascript -l JavaScript "$HERE/make-icon.js" "$ROOT/logo.svg" "$TMP/dialog.png" dialog

ICONSET="$TMP/Frayme.iconset"
mkdir "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z "$s" "$s" "$TMP/mac.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$TMP/mac.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$TMP/Frayme.icns"

DIALOGSET="$TMP/dialog.iconset"
mkdir "$DIALOGSET"
for s in 16 32 128 256 512; do
  sips -z "$s" "$s" "$TMP/dialog.png" --out "$DIALOGSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$TMP/dialog.png" --out "$DIALOGSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$DIALOGSET" -o "$TMP/dialog.icns"

sips -z 32 32 "$TMP/web.png" --out "$ROOT/favicon.png" >/dev/null
sips -z 180 180 "$TMP/web.png" --out "$ROOT/apple-touch-icon.png" >/dev/null
sips -s format ico "$ROOT/favicon.png" --out "$ROOT/favicon.ico" >/dev/null

# ── App bundle ──
rm -rf "$APP"
osacompile -o "$APP" "$HERE/Frayme.applescript"

cp "$TMP/Frayme.icns" "$APP/Contents/Resources/applet.icns"
cp "$TMP/dialog.icns" "$APP/Contents/Resources/dialog.icns"
# osacompile also ships a default icon in Assets.car, which macOS prefers over
# applet.icns via CFBundleIconName; drop it so the Frayme logo is used
rm -f "$APP/Contents/Resources/Assets.car"
plutil -remove CFBundleIconName "$APP/Contents/Info.plist"
plutil -replace CFBundleIdentifier -string local.frayme.launcher "$APP/Contents/Info.plist"
plutil -replace CFBundleName -string Frayme "$APP/Contents/Info.plist"

# Editing the bundle invalidates osacompile's signature; re-sign ad hoc
codesign --force --deep --sign - "$APP" 2>/dev/null
touch "$APP"

# The launchers are only ad hoc signed (no notarization), so Gatekeeper blocks
# them if they carry the quarantine flag. Building on this Mac makes them local,
# but cp and the icon copies above inherit the flag when the project folder was
# itself downloaded/AirDropped, so clear it from what was just generated.
xattr -dr com.apple.quarantine "$APP" "$ROOT/Frayme.command" 2>/dev/null || true
echo "Built $APP and browser icons from logo.svg"
