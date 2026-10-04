#!/bin/bash
# Install the systemd units (run with sudo from the repo):
#
#   sudo deploy/install.sh           install + enable (start on boot); doesn't start anything
#   sudo deploy/install.sh --start   ... and cut over now: stop hand-started `run` / `web`
#                                    processes gracefully (RTSP TEARDOWN), then start the units
#
# Refuses --start while an Admin prompt test is queued or running (add --force to override).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
USER_NAME=${SUDO_USER:-$(stat -c %U "$REPO")}
HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
UNITS=(scanner-transcriber.service scanner-transcriber-web.service)
START=0 FORCE=0
for a in "$@"; do
  case $a in
    --start) START=1 ;;
    --force) FORCE=1 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -x "$REPO/.venv/bin/python" ] || { echo "$REPO/.venv missing (uv sync --extra worker --extra web)" >&2; exit 1; }
as_user() { sudo -u "$USER_NAME" -H "$@"; }

if [ $START = 1 ] && [ $FORCE = 0 ]; then
  busy=$(cd "$REPO" && as_user .venv/bin/python -c "
import sqlite3
try:
    c = sqlite3.connect('file:data/scanner.db?mode=ro', uri=True)
    print(c.execute(\"SELECT COUNT(*) FROM prompt_evals WHERE status IN ('queued','running')\").fetchone()[0])
except sqlite3.Error:
    print(0)")
  if [ "$busy" != 0 ]; then
    echo "A prompt test is queued or running; wait for it (or use --force)." >&2
    exit 1
  fi
fi

echo "installing units for user $USER_NAME, repo $REPO"
for u in "${UNITS[@]}"; do
  sed -e "s|@USER@|$USER_NAME|g" -e "s|@REPO@|$REPO|g" -e "s|@HOME@|$HOME_DIR|g" \
      "$REPO/deploy/$u" > "/etc/systemd/system/$u"
done
as_user mkdir -p "$REPO/data" "$HOME_DIR/.cache"
systemctl daemon-reload
systemctl enable "${UNITS[@]}"

if [ $START = 1 ]; then
  # hand-started processes (e.g. in screen): SIGTERM = clean stop, like systemctl stop
  pids=$(pgrep -u "$USER_NAME" -f '^\S*python\S* -m scanner_transcriber (run|web)$' || true)
  for p in $pids; do
    [ "$(cat /proc/$p/cgroup 2>/dev/null | grep -c 'scanner-transcriber')" = 0 ] || continue
    echo "stopping $p: $(tr '\0' ' ' < /proc/$p/cmdline)"
    kill -TERM "$p"
  done
  for _ in $(seq 60); do
    alive=0
    for p in $pids; do kill -0 "$p" 2>/dev/null && alive=1; done
    [ $alive = 0 ] && break
    sleep 1
  done
  systemctl restart "${UNITS[@]}"
  sleep 5
  systemctl --no-pager --lines=5 status "${UNITS[@]}" || true
fi
echo "done. Logs: journalctl -u scanner-transcriber -f   (and data/logs/*.log)"
