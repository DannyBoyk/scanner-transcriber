#!/bin/bash
# Regenerates the README screenshots in docs/screenshots/ from invented demo data
# (tests/ui/demo_data.py) on a throwaway web instance at 127.0.0.1:8099. Your real data and
# services are untouched. Same requirements as run_all.sh.
set -eu
R=$(cd "$(dirname "$0")/../.." && pwd)
V=$HOME/.cache/scanner-ui-venv
if [ ! -x "$V/bin/python" ]; then
  uv venv -q --python 3.12 "$V" && VIRTUAL_ENV="$V" uv pip install -q selenium numpy
fi
export TMPDIR=$HOME/snap/firefox/common
D=$(mktemp -d -p "$TMPDIR" st-shots.XXXX)
WEB=
cleanup() { [ -n "$WEB" ] && kill "$WEB" 2>/dev/null; rm -rf "$D"; }
trap cleanup EXIT
cd "$R"
mkdir -p "$D/data" docs/screenshots
printf '[storage]\ndata_dir = "%s/data"\n[web]\nbind = "127.0.0.1"\nport = 8099\n' "$D" > "$D/cfg.toml"
.venv/bin/python tests/ui/demo_data.py "$D/data"
.venv/bin/python -m scanner_transcriber --config "$D/cfg.toml" web > "$D/web.log" 2>&1 & WEB=$!
sleep 4
"$V/bin/python" tests/ui/screenshots.py "$D/data/scanner.db" docs/screenshots
