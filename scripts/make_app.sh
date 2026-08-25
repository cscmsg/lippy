#!/bin/bash
# Build LocalFlow.app from the SwiftPM release binary.
#
# Signing matters more here than in a normal app: the Accessibility and
# Microphone grants are bound to the code signature, so an ad-hoc signature
# means macOS treats every rebuild as a new app and you re-grant both
# permissions each time. A stable Developer ID makes the grants stick.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="$(grep -m1 'static let version' app/Sources/LocalFlow/AppDelegate.swift | sed 's/.*"\(.*\)".*/\1/')"
BUNDLE_ID="com.cscmsg.localflow"
APP=".dist/LocalFlow.app"

swift build -c release --package-path app

rm -rf "$APP"
mkdir -p .dist "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Belt and braces alongside the dot-directory: Spotlight skips dot-prefixed
# paths, and this marker makes the exclusion explicit for anything that does not.
touch .dist/.metadata_never_index
cp app/.build/release/LocalFlow "$APP/Contents/MacOS/LocalFlow"
[ -f Assets/AppIcon.icns ] && cp Assets/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

# Ship the daemon inside the bundle so a DMG install needs no repository
# checkout. These are plain .py files -- no compiled code -- so they do not
# complicate signing.
mkdir -p "$APP/Contents/Resources/daemon"
cp daemon/*.py daemon/requirements.txt "$APP/Contents/Resources/daemon/"
cp scripts/setup.sh "$APP/Contents/Resources/setup.sh"
chmod +x "$APP/Contents/Resources/setup.sh"
cp LICENSE NOTICE "$APP/Contents/Resources/"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleExecutable</key>
	<string>LocalFlow</string>
	<key>CFBundleIdentifier</key>
	<string>${BUNDLE_ID}</string>
	<key>CFBundleName</key>
	<string>LocalFlow</string>
	<key>CFBundleDisplayName</key>
	<string>LocalFlow</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleIconFile</key>
	<string>AppIcon</string>
	<key>CFBundleShortVersionString</key>
	<string>${VERSION}</string>
	<key>CFBundleVersion</key>
	<string>${VERSION}</string>
	<key>LSMinimumSystemVersion</key>
	<string>14.0</string>
	<key>LSUIElement</key>
	<true/>
	<key>LSApplicationCategoryType</key>
	<string>public.app-category.productivity</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>NSMicrophoneUsageDescription</key>
	<string>LocalFlow transcribes your speech on this Mac. Audio is never sent anywhere.</string>
	<key>NSHumanReadableCopyright</key>
	<string>Private tool — Courtney Cook</string>
</dict>
</plist>
PLIST

# Sign by certificate hash, not name: this keychain holds two identically-named
# "Developer ID Application: Courtney Cook" certs, and signing by name fails as
# ambiguous.
IDENTITY="${SIGN_IDENTITY:-$(security find-identity -v -p codesigning 2>/dev/null \
  | grep -E '"(Developer ID Application|Apple Development)' | head -1 | awk '{print $2}')}"

# --entitlements is not optional here: with --options runtime and no
# entitlements file, the hardened runtime silently blocks the microphone.
if [ -n "${IDENTITY:-}" ]; then
  codesign --force --options runtime \
    --entitlements app/LocalFlow.entitlements --sign "$IDENTITY" "$APP"
  echo "Signed with: $(codesign -dvv "$APP" 2>&1 | grep '^Authority' | head -1 | cut -d= -f2)"
else
  codesign --force --entitlements app/LocalFlow.entitlements --sign - "$APP"
  echo "WARNING: ad-hoc signed - macOS will require re-granting Accessibility and"
  echo "         Microphone access after every rebuild."
fi

# Guard: a build without this entitlement looks fine and cannot use the mic.
if ! codesign -d --entitlements - "$APP" 2>&1 | grep -q "audio-input"; then
  echo "ERROR: microphone entitlement missing from the signed bundle." >&2
  exit 1
fi

echo "Built $APP (v${VERSION})"
