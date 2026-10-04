#!/bin/bash
# Run every browser check against throwaway scratch web instances on 127.0.0.1:8099.
# Your real data and services are untouched. Needs snap Firefox + geckodriver, and a venv
# with selenium + numpy (created on first run at ~/.cache/scanner-ui-venv).
#
#   tests/ui/run_all.sh            all checks
#   tests/ui/run_all.sh player     only the named ones (live_updates reconnect row_cap
#                                  channels admin player)
set -u
R=$(cd "$(dirname "$0")/../.." && pwd)
V=$HOME/.cache/scanner-ui-venv
if [ ! -x "$V/bin/python" ]; then
  uv venv -q --python 3.12 "$V" && VIRTUAL_ENV="$V" uv pip install -q selenium numpy || exit 1
fi
PY=$V/bin/python
export TMPDIR=$HOME/snap/firefox/common          # snap Firefox only sees files under here
D=$(mktemp -d -p "$TMPDIR" st-ui.XXXX)
CFG=$D/sse.toml
cd "$R"
WEB=
cleanup() {
  [ -n "$WEB" ] && kill "$WEB" 2>/dev/null
  # stray browsers from a failed check (only ones started with -marionette, i.e. by selenium)
  pkill -u "$USER" -f '^/snap/firefox/.*-marionette' 2>/dev/null
  pkill -u "$USER" -x geckodriver 2>/dev/null
  rm -rf "$D"
}
trap cleanup EXIT
cfg() {  # $1 = extra [web] lines
  rm -rf "$D/data"; mkdir -p "$D/data"
  printf '[storage]\ndata_dir = "%s/data"\n[web]\nbind = "127.0.0.1"\nport = 8099\n%b' "$D" "$1" > "$CFG"
}
password() {
  .venv/bin/python -c "from scanner_transcriber import db, auth; c=db.connect('$D/data/scanner.db'); auth.set_password(c, 'scratch-pass-123'); c.commit()"
}
web_up() { .venv/bin/python -m scanner_transcriber --config "$CFG" web > "$D/web.log" 2>&1 & WEB=$!; sleep 4; }
web_down() { kill "$WEB" 2>/dev/null; wait "$WEB" 2>/dev/null; WEB=; }
FAILED=0
run() {
  local name=$1; shift
  echo "=== $name"
  local out rc
  out=$(timeout 600 "$@" 2>&1); rc=$?
  echo "$out" | tail -30
  # every check exits 1 on a failure (common.finish); a FAIL line is also caught as a safety net
  if [ "$rc" != 0 ] || echo "$out" | grep -qE '^FAIL |SOME FAILED'; then FAILED=1; rc=${rc/#0/1}; fi
  echo "exit $rc"
}
SEL=("$@")
sel() { [ ${#SEL[@]} -eq 0 ] && return 0; for s in "${SEL[@]}"; do [ "$s" = "$1" ] && return 0; done; return 1; }

if sel live_updates; then cfg ""; web_up; run live_updates $PY tests/ui/live_updates_check.py "$D/data/scanner.db"; web_down; fi
if sel reconnect; then cfg ""; run reconnect $PY tests/ui/reconnect_check.py "$D/data/scanner.db" "$CFG" "$R"; fi
if sel row_cap; then cfg 'live_max_rows = 8\npage_size = 5\n'; web_up; run row_cap $PY tests/ui/row_cap_check.py "$D/data/scanner.db" 8; web_down; fi
if sel channels; then cfg ""; password; web_up; run channels $PY tests/ui/channels_check.py "$D/data/scanner.db" scratch-pass-123; web_down; fi
if sel admin; then cfg ""; password; web_up; run admin $PY tests/ui/admin_check.py "$D/data/scanner.db" scratch-pass-123; web_down; fi
if sel player; then cfg 'page_size = 4\n'; web_up; run player $PY tests/ui/player_check.py "$D/data/scanner.db" "$D/data"; web_down; fi
[ $FAILED = 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit $FAILED
