#!/bin/bash
# start_openrouter.sh — Start the ProutGPT OpenRouter proxy server
#
# Prerequisites:
#   1. Export your API key: export OPENROUTER_API_KEY=sk-or-v1-...
#      (or put it in ~/.env and source it)
#   2. Make sure the virtualenv exists: python3 -m venv ~/openrouter-env
#   3. Install deps: ~/openrouter-env/bin/pip install flask flask-cors requests

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HOME/openrouter-env"
LOGFILE="$HOME/openrouter.log"
PIDFILE="$HOME/openrouter.pid"

# Load API key from ~/.env if not already set
if [ -z "$OPENROUTER_API_KEY" ] && [ -f "$HOME/.env" ]; then
    source "$HOME/.env"
fi

if [ -z "$OPENROUTER_API_KEY" ]; then
    echo "ERROR: OPENROUTER_API_KEY is not set."
    echo "  Export it: export OPENROUTER_API_KEY=sk-or-v1-..."
    echo "  Or put it in ~/.env as: export OPENROUTER_API_KEY=sk-or-v1-..."
    exit 1
fi

# Activate virtualenv
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtualenv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# Install/upgrade dependencies silently
pip install -q flask flask-cors requests

echo "Starting ProutGPT backend on port 5000 ..."
echo "Logs: $LOGFILE"

exec python "$SCRIPT_DIR/openrouter_proxy.py" >> "$LOGFILE" 2>&1
