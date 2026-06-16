#!/bin/bash
# Wrapper invoked by launchd. Loads the API key from ~/.anthropic_api_key
# (a plain file with just the key on one line) and runs the scout.
#
# If the key file is missing, the scout still runs but in --no-ai mode, so
# at minimum the calendar of available dates stays current.

set -u
cd "$(dirname "$0")"

KEY_FILE="$HOME/.anthropic_api_key"
if [[ -f "$KEY_FILE" ]]; then
    export ANTHROPIC_API_KEY="$(tr -d '[:space:]' < "$KEY_FILE")"
fi

mkdir -p logs
TS="$(date '+%Y-%m-%d %H:%M:%S')"
{
    echo ""
    echo "===== $TS ====="
    ./.venv/bin/python scout.py
} >> logs/scout.log 2>&1
