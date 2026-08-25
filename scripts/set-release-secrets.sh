#!/bin/bash
# Set the Apple signing secrets on a GitHub repository.
#
# Values go straight from your keychain export / 1Password into `gh`. Nothing is
# echoed, and the two genuinely secret values are piped rather than passed as
# arguments, so they never reach your shell history or the process list.
#
#   ./scripts/set-release-secrets.sh cscmsg/lippy
set -euo pipefail

REPO="${1:-}"
if [ -z "$REPO" ]; then
  echo "usage: $0 <owner/repo>" >&2
  exit 1
fi

command -v gh >/dev/null || { echo "gh is not installed" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "run: gh auth login" >&2; exit 1; }

echo "Setting release secrets on $REPO"
echo

echo "Codesigning identities in your keychain:"
security find-identity -v -p codesigning | sed 's/^/    /'
echo
echo "Export the one you want from Keychain Access first:"
echo "  right-click the identity, Export, save as .p12, and set a password."
echo

read -r -p "Full identity name (e.g. Developer ID Application: Acme (TEAMID)): " IDENTITY_NAME
gh secret set APPLE_DEVELOPER_ID_NAME --repo "$REPO" --body "$IDENTITY_NAME"

read -r -p "Path to the exported .p12: " P12_PATH
[ -f "$P12_PATH" ] || { echo "no such file: $P12_PATH" >&2; exit 1; }
base64 -i "$P12_PATH" | gh secret set APPLE_DEVELOPER_CERT_P12 --repo "$REPO"

# -s so the password is not echoed; piped, not passed as an argument.
read -r -s -p "Password you set when exporting the .p12: " P12_PASSWORD
echo
printf '%s' "$P12_PASSWORD" | gh secret set APPLE_DEVELOPER_CERT_PASSWORD --repo "$REPO"
unset P12_PASSWORD

echo
echo "App Store Connect API key (notarisation). The .p8 is download-once from"
echo "App Store Connect, so it should already be in your password manager."
read -r -p "Key ID: " KEY_ID
gh secret set APPLE_API_KEY_ID --repo "$REPO" --body "$KEY_ID"

read -r -p "Issuer ID: " ISSUER_ID
gh secret set APPLE_API_ISSUER_ID --repo "$REPO" --body "$ISSUER_ID"

read -r -p "Path to AuthKey_*.p8: " P8_PATH
[ -f "$P8_PATH" ] || { echo "no such file: $P8_PATH" >&2; exit 1; }
base64 -i "$P8_PATH" | gh secret set APPLE_API_KEY_P8 --repo "$REPO"

echo
echo "Done. Secrets now set on $REPO:"
gh secret list --repo "$REPO"
echo
echo "Delete the exported .p12 when you are finished with it:"
echo "  rm \"$P12_PATH\""
