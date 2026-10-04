#!/bin/bash
# Update and restart (run as your user from the repo; asks for sudo only to restart):
#   deploy/update.sh
# Pulls (if the repo has a remote), syncs dependencies, runs the tests, and restarts the
# services only if they pass. Restarting capture loses a few seconds of scanner audio.
set -euo pipefail
cd "$(dirname "$0")/.."

if git remote | grep -q .; then
  git pull --ff-only
fi
uv sync --extra worker --extra web --extra bench   # bench: draft/label/bench tools
.venv/bin/python -m pytest -q
sudo systemctl restart scanner-transcriber.service scanner-transcriber-web.service
sleep 5
systemctl --no-pager --lines=5 status scanner-transcriber.service scanner-transcriber-web.service
