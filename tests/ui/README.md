# Browser checks (manual)

**Quick way:** `tests/ui/run_all.sh` runs all six checks against throwaway scratch web
instances on 127.0.0.1:8099 (your data and services are untouched), and prints
`ALL CHECKS PASSED` or `SOME CHECKS FAILED`. `tests/ui/run_all.sh player admin` runs only
those. It creates the Selenium venv at `~/.cache/scanner-ui-venv` on first use. The steps below
are what it does, for running a check by hand.

Real-browser checks of the live feed, run against a **scratch** web instance so your data is
untouched. They're not collected by pytest because they need Firefox + geckodriver (both on
the NUC as snaps) and Selenium.

```bash
S=$(mktemp -d -p ~/snap/firefox/common)          # snap Firefox can only read under ~/snap/firefox
printf '[storage]\ndata_dir = "%s/data"\n[web]\nbind = "127.0.0.1"\nport = 8099\n' $S > $S/cfg.toml
uv venv $S/venv && VIRTUAL_ENV=$S/venv uv pip install selenium
export TMPDIR=$HOME/snap/firefox/common

# 1) live updates: new rows, transcript swap, exchange joining, pill/pause, status dot
.venv/bin/python -m scanner_transcriber --config $S/cfg.toml web &  WEB=$!
sleep 3; $S/venv/bin/python tests/ui/live_updates_check.py $S/data/scanner.db; kill $WEB

# 2) reconnect: server restarted mid-session → rows written meanwhile arrive, no duplicates
rm -f $S/data/scanner.db*
$S/venv/bin/python tests/ui/reconnect_check.py $S/data/scanner.db $S/cfg.toml "$PWD"

# 3) row cap: add `live_max_rows = 8` and `page_size = 5` under [web] in cfg.toml, start the
#    scratch web instance again, then:
$S/venv/bin/python tests/ui/row_cap_check.py $S/data/scanner.db 8
```

```bash
# 4) talkgroup settings: alias, colour, hide from live feed (fresh scratch data dir, with a
#    scratch password set as in 5)
$S/venv/bin/python tests/ui/channels_check.py $S/data/scanner.db scratch-pass-123
```

```bash
# 5) admin: set a scratch password first, e.g.
#    .venv/bin/python -c "from scanner_transcriber import db, auth; c=db.connect('$S/data/scanner.db'); auth.set_password(c, 'scratch-pass-123'); c.commit()"
$S/venv/bin/python tests/ui/admin_check.py $S/data/scanner.db scratch-pass-123 [screenshot.png]
```

The status-dot check expects the dot to leave red; it may show amber if the scratch
directory is on a small filesystem (the disk-space warning).
