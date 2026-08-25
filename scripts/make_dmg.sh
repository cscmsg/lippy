#!/bin/bash
# Build a distributable disk image. Assumes make_app.sh has already produced
# (and signed) .dist/Lippy.app.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="$(grep -m1 'static let version' app/Sources/Lippy/AppDelegate.swift | sed 's/.*"\(.*\)".*/\1/')"
APP=".dist/Lippy.app"
STAGE=".dist/dmg"
DMG=".dist/Lippy-${VERSION}.dmg"

[ -d "$APP" ] || { echo "no $APP -- run scripts/make_app.sh first" >&2; exit 1; }

rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Lippy.app"
# The usual drag-to-install affordance.
ln -s /Applications "$STAGE/Applications"

hdiutil create \
  -volname "Lippy ${VERSION}" \
  -srcfolder "$STAGE" \
  -ov -format UDZO \
  "$DMG"

rm -rf "$STAGE"
echo "Built $DMG ($(du -h "$DMG" | cut -f1))"
