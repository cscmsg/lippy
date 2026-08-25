#!/bin/bash
# Create the Python venv and pull down the models. Safe to re-run.
set -euo pipefail

VENV="${LOCALFLOW_VENV:-$HOME/.cache/localflow-venv}"
PYTHON="${LOCALFLOW_PYTHON:-/opt/homebrew/bin/python3.12}"

if [ ! -d "$VENV" ]; then
  echo "creating venv at $VENV"
  "$PYTHON" -m venv "$VENV"
fi

"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$(dirname "$0")/../daemon/requirements.txt"

echo "pre-fetching models (about 4.5 GB on first run)"
"$VENV/bin/python" - <<'PY'
from huggingface_hub import snapshot_download
for repo in ("mlx-community/parakeet-tdt-0.6b-v3",
             "mlx-community/Qwen3-4B-Instruct-2507-4bit"):
    print(f"  {repo}")
    snapshot_download(repo)
PY

echo "done. start the daemon with:  make daemon"
