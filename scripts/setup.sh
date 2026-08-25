#!/bin/bash
# First-run setup, shipped inside LocalFlow.app.
#
# Creates the Python environment and downloads the models, so installing from a
# DMG does not require cloning the repository. Safe to re-run.
#
#   /Applications/LocalFlow.app/Contents/Resources/setup.sh
set -euo pipefail

if [ "$(uname -m)" != "arm64" ]; then
  echo "LocalFlow requires an Apple Silicon Mac (M1 or later)." >&2
  echo "This machine reports: $(uname -m)" >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
# When bundled, the daemon sits beside this script in Resources/. When run from
# a source checkout, it is one level up in daemon/.
if [ -d "$HERE/daemon" ]; then
  DAEMON_DIR="$HERE/daemon"
else
  DAEMON_DIR="$(cd "$HERE/.." && pwd)/daemon"
fi

SUPPORT="$HOME/Library/Application Support/LocalFlow"
VENV="$SUPPORT/venv"
AGENT_LABEL="com.cscmsg.localflow.flowd"
AGENT_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"

# Python 3.12 or newer, wherever it lives. The system python3 is deliberately
# not used: it is not guaranteed to be 3.12+, and pip installs into it fight
# with PEP 668.
PYTHON=""
for candidate in \
  /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
  /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3; do
  [ -x "$candidate" ] && PYTHON="$candidate" && break
done

if [ -z "$PYTHON" ]; then
  cat >&2 <<'MSG'
Python 3.12 or newer was not found.

Install it with either:
    brew install python@3.12
or download it from https://www.python.org/downloads/macos/

Then run this script again.
MSG
  exit 1
fi
echo "Using $PYTHON ($("$PYTHON" -V 2>&1))"

mkdir -p "$SUPPORT"
[ -d "$VENV" ] || "$PYTHON" -m venv "$VENV"

echo "Installing Python dependencies…"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$DAEMON_DIR/requirements.txt"

echo "Downloading models (about 4.5 GB the first time; later runs are instant)…"
"$VENV/bin/python" - <<'PY'
from huggingface_hub import snapshot_download
for repo in ("mlx-community/parakeet-tdt-0.6b-v3",
             "mlx-community/Qwen3-4B-Instruct-2507-4bit"):
    print(f"  {repo}")
    snapshot_download(repo)
PY

# Escape hatch for testing the script without touching the live launchd
# session: the agent label is user-global, so a test run would otherwise
# replace a working install.
if [ -n "${LOCALFLOW_SKIP_AGENT:-}" ]; then
  echo "LOCALFLOW_SKIP_AGENT set - stopping before the background service."
  exit 0
fi

echo "Installing the background service…"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$AGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$AGENT_LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$VENV/bin/python</string>
		<string>$DAEMON_DIR/flowd.py</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<dict>
		<key>SuccessfulExit</key>
		<false/>
	</dict>
	<key>ProcessType</key>
	<string>Interactive</string>
	<key>StandardOutPath</key>
	<string>$SUPPORT/flowd.out.log</string>
	<key>StandardErrorPath</key>
	<string>$SUPPORT/flowd.err.log</string>
</dict>
</plist>
PLIST

# Re-running setup replaces a running service, so the old one is unloaded
# first. If the load then fails the machine is left with nothing -- say so
# loudly and give the exact command to recover, rather than exiting on a bare
# "Input/output error".
launchctl bootout "gui/$(id -u)/$AGENT_LABEL" 2>/dev/null || true
sleep 1
if ! launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST" 2>&1; then
  echo >&2
  echo "Could not start the background service." >&2
  echo "The launch agent is written to:" >&2
  echo "  $AGENT_PLIST" >&2
  echo "Retry it directly with:" >&2
  echo "  launchctl bootstrap gui/$(id -u) \"$AGENT_PLIST\"" >&2
  exit 1
fi

echo "Waiting for the models to load (about 25 seconds)…"
for _ in $(seq 1 45); do
  if "$VENV/bin/python" "$DAEMON_DIR/flowctl.py" status >/dev/null 2>&1; then
    echo
    echo "Ready. Launch LocalFlow, then grant Microphone and Accessibility"
    echo "when macOS asks. Hold Right Option to dictate."
    exit 0
  fi
  sleep 2
done

echo "The service did not come up in time. Check:" >&2
echo "  tail -f \"$SUPPORT/flowd.err.log\"" >&2
exit 1
