"""FastAPI app: server-rendered pages (Jinja2 + htmx) over the SQLite database.

Runs as its own process (`web` command), separate from capture, so it keeps reporting
status when capture is down and restarting it never touches the scanner session. It only
reads the database in M3."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import audio as audio_mod
from .. import (
    auth,
    cleanstart,
    commands,
    db,
    health,
    jobs,
    logsetup,
    prompteval,
    promptstore,
    retention,
    retranscribe,
    statestore,
)
from ..config import Config
from . import admin, changes, feed, search
from . import stream as stream_mod

HERE = Path(__file__).parent
POLL_S = 0.5          # change-feed poll interval
STATUS_EVERY_S = 5    # re-evaluate health for the header dot
KEEPALIVE_S = 15      # SSE comment so proxies/phones don't drop an idle stream
EVENT_COMPONENTS = ("capture", "worker", "queue", "host")


def _tz(cfg: Config) -> tzinfo | None:
    return ZoneInfo(cfg.web.timezone) if cfg.web.timezone else None  # None = system local


def create_app(cfg: Config) -> FastAPI:
    station = stream_mod.Station(cfg.storage.db_path, cfg.storage.data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await station.close()                  # stop the Listen live station on shutdown

    app = FastAPI(title="Scanner Transcriber", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.station = station
    app.state.sse_max_seconds = None
    tz = _tz(cfg)
    _c = db.connect(cfg.storage.db_path)      # apply migrations once at startup
    for w in promptstore.ensure_seeded(_c, cfg):
        logging.getLogger(__name__).warning(w)
    _c.close()

    templates = Jinja2Templates(directory=HERE / "templates")

    def local(ts: str | datetime | None, fmt: str = "%H:%M:%S") -> str:
        if not ts:
            return "—"
        dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        return dt.astimezone(tz).strftime(fmt)

    def ago(ts: str | None) -> str:
        if not ts:
            return "—"
        s = int((datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h {s % 3600 // 60}m ago"
        return f"{s // 86400}d ago"

    def duration(seconds: float | None) -> str:
        if seconds is None:
            return "—"
        s = int(seconds)
        return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s" if s < 3600 else \
            f"{s // 3600}h {s % 3600 // 60:02d}m"

    templates.env.filters.update(local=local, ago=ago, duration=duration)

    def asset(name: str) -> str:
        """/static URL with the file's modification time, so a changed file is a new URL and
        browsers (Safari especially) can't keep serving a stale cached copy."""
        try:
            return f"/static/{name}?v={int((HERE / 'static' / name).stat().st_mtime)}"
        except OSError:
            return f"/static/{name}"

    templates.env.globals["asset"] = asset
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(cfg.storage.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
        finally:
            conn.close()

    limiter = auth.LoginLimiter()

    def is_admin(request: Request, conn: sqlite3.Connection) -> bool:
        return auth.valid_session(conn, request.cookies.get(auth.COOKIE))

    def require_admin(request: Request, conn: sqlite3.Connection) -> None:
        """For API/fragment endpoints: 401 unless logged in (403 if admin is disabled)."""
        if not auth.enabled(conn):
            raise HTTPException(403, "Admin is disabled until a password is set on the NUC")
        if not is_admin(request, conn):
            raise HTTPException(401, "Log in to change settings")

    def page(request: Request, name: str, conn: sqlite3.Connection, **ctx) -> HTMLResponse:
        h = health.evaluate(conn, cfg)
        # `since` lets the page's SSE stream start exactly where this render's data ended
        return templates.TemplateResponse(request, name, {
            "health": h, "since": db.utcnow(), "admin": is_admin(request, conn),
            "admin_enabled": auth.enabled(conn), **ctx})

    def safe_next(nxt: str | None) -> str:
        return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/admin"

    # ---- login (M4.5) ------------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str | None = None, error: str | None = None,
                   conn: sqlite3.Connection = Depends(get_conn)):
        return page(request, "login.html", conn, next=safe_next(next), error=error)

    @app.post("/login")
    async def login(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        form = {k: v[-1] for k, v in parse_qs((await request.body()).decode()).items()}
        nxt = safe_next(form.get("next"))
        client = request.client.host if request.client else "?"
        wait = limiter.wait_s(client)
        if wait > 0:
            return page(request, "login.html", conn, next=nxt,
                        error=f"Too many attempts. Try again in {int(wait) + 1} s.")
        if not auth.enabled(conn):
            return page(request, "login.html", conn, next=nxt, error=None)
        if not auth.verify(conn, form.get("password", "")):
            limiter.failed(client)
            resp = page(request, "login.html", conn, next=nxt, error="Wrong password.")
            resp.status_code = 401
            return resp
        limiter.ok(client)
        value, max_age = auth.make_session(conn)
        conn.commit()
        resp = RedirectResponse(nxt, status_code=303)
        secure = (request.url.scheme == "https"
                  or request.headers.get("x-forwarded-proto") == "https")
        resp.set_cookie(auth.COOKIE, value, max_age=max_age, httponly=True, samesite="strict",
                        secure=secure, path="/")
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        if not auth.enabled(conn):
            return page(request, "admin_disabled.html", conn)
        if not is_admin(request, conn):
            return RedirectResponse("/login?next=/admin", status_code=303)
        return page(request, "admin.html", conn, **admin_context(conn))

    def admin_context(conn) -> dict:
        ctx: dict = {}
        for kind in promptstore.KINDS:
            hist = promptstore.history(conn, kind)
            for newer, older in pairwise(hist):
                if kind == "prompt":
                    newer["diff"] = admin.word_diff(older["value"], newer["value"])
                else:
                    o, n = older["value"], newer["value"]
                    newer["diff"] = ([("ins", f"{k} → {v}") for k, v in n.items() if o.get(k) != v]
                                     + [("del", f"{k} → {v}") for k, v in o.items() if k not in n])
            ctx[f"{kind}_history"] = hist
            ctx[f"{kind}_active"] = next((h for h in hist if h["active"]), None)
        text = ctx["prompt_active"]["value"] if ctx["prompt_active"] else ""
        n, exact = admin.count_tokens(text, cfg.stt.model)
        ctx.update(tokens=n, tokens_exact=exact, token_level=admin.token_level(n),
                   token_limit=admin.PROMPT_TOKEN_LIMIT, leak_hints=admin.leak_hints(text))
        ctx.update(controls_context(conn))
        ctx.update(retranscribe_context(conn))
        ctx.update(log_context("capture", "INFO"))
        ctx.update(evals_context(conn))
        return ctx

    def evals_context(conn) -> dict:
        rows = [dict(r) for r in conn.execute("SELECT * FROM prompt_evals ORDER BY id DESC LIMIT 8")]
        for r in rows:
            r["detail"] = json.loads(r["detail"]) if r["detail"] else {}
        n_clips = len(prompteval.labelled_clips(cfg.storage.data_dir))
        return {"evals": rows, "evals_running": any(r["status"] in ("queued", "running") for r in rows),
                "labelled_clips": n_clips, "eval_minutes": round(n_clips * 4.7 / 60)}

    @app.post("/admin/prompt-test", response_class=HTMLResponse)
    async def prompt_test(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        form = await admin_form(request, conn)
        draft = " ".join(str(form.get("draft", "")).split()) or None
        version = form.get("version")
        if not draft:
            try:
                version = int(version) if version else promptstore.active(conn, "prompt")[0]
            except ValueError:
                raise HTTPException(400, "Bad version") from None
        eid = conn.execute("INSERT INTO prompt_evals (prompt_version, draft_body, created_at, status) "
                           "VALUES (?, ?, ?, 'queued')", (None if draft else version, draft,
                                                          db.utcnow())).lastrowid
        commands.post(conn, "worker", "prompt-test", {"eval_id": eid})
        conn.commit()
        return templates.TemplateResponse(request, "_admin_evals.html", evals_context(conn))

    @app.post("/admin/prompt-test/{eid}/delete", response_class=HTMLResponse)
    async def prompt_test_delete(request: Request, eid: int,
                                 conn: sqlite3.Connection = Depends(get_conn)):
        await admin_form(request, conn)
        conn.execute("DELETE FROM prompt_evals WHERE id = ? AND status NOT IN ('queued', 'running')",
                     (eid,))
        conn.commit()
        return templates.TemplateResponse(request, "_admin_evals.html", evals_context(conn))

    @app.get("/admin/fragments/evals", response_class=HTMLResponse)
    def evals_fragment(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        require_admin(request, conn)
        return templates.TemplateResponse(request, "_admin_evals.html", evals_context(conn))

    def log_context(component: str, level: str) -> dict:
        component = component if component in logsetup.COMPONENTS else "capture"
        level = level if level in ("INFO", "WARNING") else "INFO"
        return {"log_component": component, "log_level": level,
                "log_lines": logsetup.tail(cfg.storage.data_dir, component, 300, level),
                "log_components": logsetup.COMPONENTS}

    @app.get("/admin/fragments/logs", response_class=HTMLResponse)
    def logs_fragment(request: Request, c: str = "capture", level: str = "INFO", auto: int = 0,
                      conn: sqlite3.Connection = Depends(get_conn)):
        require_admin(request, conn)
        return templates.TemplateResponse(request, "_admin_logs.html",
                                          {**log_context(c, level), "log_auto": bool(auto)})

    def retranscribe_context(conn) -> dict:
        bs = retranscribe.batches(conn)
        now = datetime.now(UTC).astimezone(tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return {"batches": bs, "batches_running": any(not b["finished"] for b in bs),
                "rt_talkgroups": feed.known_talkgroups(conn),
                "rt_ranges": [("Last hour", to_input(now - timedelta(hours=1)), ""),
                              ("Today", to_input(today), ""),
                              ("Yesterday", to_input(today - timedelta(days=1)), to_input(today))],
                "max_input": to_input(datetime.now(UTC)), "max_batch": retranscribe.MAX_BATCH}

    def rt_params(form: dict) -> tuple:
        since = until = None
        if form.get("from"):
            since = parse_local(str(form["from"])).isoformat(timespec="milliseconds")
        if form.get("to"):
            until = parse_local(str(form["to"])).isoformat(timespec="milliseconds")
        tg = form.get("tg", [])
        tgids = [t for t in ([tg] if isinstance(tg, str) else tg) if t.isdigit()]
        return since, until, tgids, bool(form.get("include_no_speech"))

    @app.post("/admin/retranscribe/preview", response_class=HTMLResponse)
    async def rt_preview(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        form = await admin_form(request, conn)
        try:
            p = retranscribe.preview(conn, *rt_params(form))
        except ValueError:
            p = {"error": "Couldn't read one of the dates."}
        return templates.TemplateResponse(request, "_admin_rt_preview.html", {"p": p})

    @app.post("/admin/retranscribe", response_class=HTMLResponse)
    async def rt_start(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        form = await admin_form(request, conn)
        try:
            params = rt_params(form)
        except ValueError:
            raise HTTPException(400, "Couldn't read one of the dates") from None
        if not (params[0] or params[1] or params[2]):
            raise HTTPException(400, "Pick a time range or talkgroups first")
        bid, n = retranscribe.start(conn, *params)
        statestore.event(conn, "worker", "info", "retranscribe",
                         f"Re-transcription batch {bid} queued ({n} clips) from the Admin page")
        conn.commit()
        return templates.TemplateResponse(request, "_admin_retranscribe.html", {
            **retranscribe_context(conn), "message": f"Batch {bid}: {n} clips queued behind live traffic."})

    @app.get("/admin/fragments/retranscribe", response_class=HTMLResponse)
    def rt_fragment(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        require_admin(request, conn)
        return templates.TemplateResponse(request, "_admin_retranscribe.html", retranscribe_context(conn))

    @app.get("/admin/batches/{bid}", response_class=HTMLResponse)
    def batch_page(request: Request, bid: int, changed: int = 1,
                   conn: sqlite3.Connection = Depends(get_conn)):
        if not auth.enabled(conn) or not is_admin(request, conn):
            return RedirectResponse(f"/login?next=/admin/batches/{bid}", status_code=303)
        batch = next((b for b in retranscribe.batches(conn, limit=1000) if b["id"] == bid), None)
        if batch is None:
            resp = page(request, "not_found.html", conn, what=f"Batch {bid}")
            resp.status_code = 404
            return resp
        rows = []
        for r in retranscribe.batch_rows(conn, bid, bool(changed)):
            d = dict(r)
            d["started"] = datetime.fromisoformat(r["started_at"]).astimezone(tz)
            d["label"] = r["alias"] or r["channel"] or f"TG {r['tgid']}"
            d["hue"] = feed.hue_for(r["color"], r["tgid"])
            d["diff"] = admin.word_diff(r["prev_transcript"] or "", r["transcript"] or "") \
                if r["status"] == "done" else None
            rows.append(d)
        return page(request, "batch.html", conn, batch=batch, rows=rows, changed=bool(changed))

    @app.get("/admin/fragments/clean", response_class=HTMLResponse)
    def clean_fragment(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        require_admin(request, conn)
        return templates.TemplateResponse(request, "_admin_clean.html",
                                          {"preview": cleanstart.preview(conn, cfg)})

    @app.post("/admin/clean-start", response_class=HTMLResponse)
    async def clean_start(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        form = await admin_form(request, conn)
        if str(form.get("confirm", "")).strip() != "DELETE":
            return templates.TemplateResponse(request, "_admin_clean.html", {
                "message": "Not started: type DELETE (in capitals) to confirm.", "error": True})
        commands.post(conn, "capture", "clean-start")
        statestore.event(conn, "host", "warn", "clean_start_requested",
                         "Clean start requested from the Admin page")
        conn.commit()
        return templates.TemplateResponse(request, "_admin_clean.html", {
            "message": "Clean start queued: capture runs it within about 5 s. Reload the "
                       "Live page afterwards."})

    def controls_context(conn) -> dict:
        recent = commands.recent(conn)
        return {"components": statestore.states(conn), "commands": recent,
                "pending": any(c["done_at"] is None for c in recent),
                "auto_reboot": cfg.scanner.auto_reboot,
                "auto_reboot_min": f"{cfg.scanner.auto_reboot_after_s / 60:g}"}

    @app.get("/admin/fragments/controls", response_class=HTMLResponse)
    def controls_fragment(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        require_admin(request, conn)
        return templates.TemplateResponse(request, "_admin_controls.html", controls_context(conn))

    @app.post("/admin/commands", response_class=HTMLResponse)
    async def post_command(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        form = await admin_form(request, conn)
        try:
            commands.post(conn, str(form.get("target")), str(form.get("action")))
        except ValueError:
            raise HTTPException(400, "Unknown command") from None
        conn.commit()
        return templates.TemplateResponse(request, "_admin_controls.html", controls_context(conn))

    async def admin_form(request: Request, conn) -> dict:
        require_edit_request(request)
        require_admin(request, conn)
        return {k: v[-1] if len(v) == 1 else v
                for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items()}

    def admin_section(request: Request, conn, name: str, **extra):
        return templates.TemplateResponse(request, f"_admin_{name}.html",
                                          {**admin_context(conn), **extra})

    @app.post("/admin/prompt/count", response_class=HTMLResponse)
    async def prompt_count(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        form = await admin_form(request, conn)
        text = str(form.get("prompt", ""))
        n, exact = admin.count_tokens(text, cfg.stt.model)
        return templates.TemplateResponse(request, "_admin_tokens.html", {
            "tokens": n, "tokens_exact": exact, "token_level": admin.token_level(n),
            "token_limit": admin.PROMPT_TOKEN_LIMIT, "leak_hints": admin.leak_hints(text)})

    @app.post("/admin/prompt", response_class=HTMLResponse)
    async def prompt_save(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        form = await admin_form(request, conn)
        text = " ".join(str(form.get("prompt", "")).split())
        if not text:
            return admin_section(request, conn, "prompt", error="The prompt can't be empty.")
        _, current = promptstore.active(conn, "prompt")
        if text == current:
            return admin_section(request, conn, "prompt", message="No changes to save.")
        vid = promptstore.save(conn, "prompt", text, note=str(form.get("note", "")).strip() or None)
        statestore.event(conn, "worker", "info", "prompt_changed",
                         f"Prompt version {vid} activated from the Admin page")
        conn.commit()
        return admin_section(request, conn, "prompt",
                             message=f"Saved as version {vid}. It applies from the next transmission.")

    @app.post("/admin/versions/{vid}/activate", response_class=HTMLResponse)
    async def version_activate(request: Request, vid: int,
                               conn: sqlite3.Connection = Depends(get_conn)):
        await admin_form(request, conn)
        try:
            kind = promptstore.activate(conn, vid)
        except KeyError:
            raise HTTPException(404, "No such version") from None
        statestore.event(conn, "worker", "info", f"{kind}_changed",
                         f"{kind.capitalize()} version {vid} re-activated from the Admin page")
        conn.commit()
        name = "prompt" if kind == "prompt" else "subs"
        return admin_section(request, conn, name, message=f"Version {vid} is active again.")

    @app.post("/admin/substitutions", response_class=HTMLResponse)
    async def subs_save(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        form = await admin_form(request, conn)
        src = form.get("src", [])
        dst = form.get("dst", [])
        src, dst = ([src] if isinstance(src, str) else src), ([dst] if isinstance(dst, str) else dst)
        subs = {s.strip(): d.strip() for s, d in zip(src, dst) if s.strip()}
        _, current = promptstore.active(conn, "substitutions")
        if subs == current:
            return admin_section(request, conn, "subs", message="No changes to save.")
        vid = promptstore.save(conn, "substitutions", subs,
                               note=str(form.get("note", "")).strip() or None)
        statestore.event(conn, "worker", "info", "substitutions_changed",
                         f"Substitutions version {vid} activated from the Admin page")
        conn.commit()
        return admin_section(request, conn, "subs",
                             message=f"Saved as version {vid}. Applies to new transcripts.")

    # ---- health / status ---------------------------------------------------------------

    @app.get("/healthz")
    def healthz(conn: sqlite3.Connection = Depends(get_conn)) -> JSONResponse:
        h = health.evaluate(conn, cfg)
        return JSONResponse({"level": h.level, "conditions": [c.message for c in h.conditions]},
                            status_code=503 if h.level == "error" else 200)

    @app.get("/api/status")
    def api_status(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        h = health.evaluate(conn, cfg)
        return {"health": h.as_dict(), "transcription": jobs.stats(conn),
                "events": statestore.recent_events(conn, limit=20)}

    @app.get("/fragments/status-dot", response_class=HTMLResponse)
    def status_dot(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return templates.TemplateResponse(request, "_status_dot.html",
                                          {"health": health.evaluate(conn, cfg)})

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request, component: str | None = None, days: int = 30,
                    conn: sqlite3.Connection = Depends(get_conn)):
        if component not in EVENT_COMPONENTS:
            component = None
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        events = statestore.recent_events(conn, limit=1000, component=component, since=since)
        return page(request, "status.html", conn, stats=jobs.stats(conn), events=events,
                    stream=station.info(),
                    retention=cfg.retention, retention_last=retention.last_run(conn),
                    db_mb=round(cfg.storage.db_path.stat().st_size / 1e6, 1)
                    if cfg.storage.db_path.exists() else None,
                    component=component, components=EVENT_COMPONENTS,
                    now=datetime.now(UTC))

    # ---- live feed + jump-to (step 3) -----------------------------------------------------

    def parse_tgs(tg: str | None) -> list[str]:
        return [t for t in (tg or "").replace(" ", "").split(",") if t.isdigit()][:50]

    def parse_local(at: str) -> datetime:
        """'YYYY-MM-DDTHH:MM' in local time → aware UTC. Naive datetimes use the OS rules
        (DST-correct via mktime); a configured zone uses zoneinfo."""
        naive = datetime.fromisoformat(at).replace(tzinfo=None, second=0, microsecond=0)
        if tz is None:
            return naive.astimezone(UTC)
        return naive.replace(tzinfo=tz).astimezone(UTC)

    def to_input(dt: datetime) -> str:
        return dt.astimezone(tz).strftime("%Y-%m-%dT%H:%M")

    def shortcuts() -> list[tuple[str, str]]:
        now = datetime.now(UTC).astimezone(tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return [("1 hour ago", to_input(now - timedelta(hours=1))),
                ("This morning", to_input(today.replace(hour=6))),
                ("Last night", to_input((today - timedelta(days=1)).replace(hour=21))),
                ("Yesterday same time", to_input(now - timedelta(days=1)))]

    @app.get("/", response_class=HTMLResponse)
    def live(request: Request, tg: str | None = None, at: str | None = None,
             conn: sqlite3.Connection = Depends(get_conn)):
        tgs = parse_tgs(tg)
        size = cfg.web.page_size
        anchor, error, newer_rows = None, None, []
        if at:
            try:
                anchor = parse_local(at)
            except ValueError:
                error = f"Couldn't read the date/time {at!r}"
        if anchor:
            a, b = feed.around(conn, anchor, tgs=tgs, each_side=size // 2)
            rows = feed.mark_days(feed.present(a + b, tz))
            if len(rows) > len(a):
                rows[len(a)].cont = False  # the marker splits an exchange: repeat its header
            newer_rows = rows[:len(a)]
        else:
            rows = feed.mark_days(feed.present(feed.latest(conn, tgs=tgs, limit=size), tz))
        oldest, newest = feed.time_bounds(conn)
        return page(request, "live.html", conn, rows=rows, anchor=anchor,
                    anchor_input=to_input(anchor) if anchor else "",
                    n_newer=len(newer_rows), error=error, tgs=tgs, tg_param=",".join(tgs),
                    talkgroups=feed.recent_talkgroups(conn), shortcuts=shortcuts(),
                    min_input=to_input(datetime.fromisoformat(oldest)) if oldest else "",
                    max_input=to_input(datetime.now(UTC)),
                    purged=any(not r.has_audio for r in rows), page_size=size,
                    max_rows=cfg.web.live_max_rows)

    # ---- search (M4) ------------------------------------------------------------------------

    def search_filters(tg, department, from_, to, has_audio, include_no_speech):
        f = search.Filters(tgs=tuple(parse_tgs(tg)), department=department or None,
                           has_audio=bool(has_audio), include_no_speech=bool(include_no_speech))
        errors = []
        for name, value in (("since", from_), ("until", to)):
            if value:
                try:
                    setattr(f, name, parse_local(value).isoformat(timespec="milliseconds"))
                except ValueError:
                    errors.append(f"Couldn't read the date/time {value!r}")
        return f, errors

    def search_rows(conn, q, f, before, size):
        rows, terms = search.run(conn, q, f, before=before, limit=size)
        out = feed.present(rows, tz)
        for r, raw in zip(out, rows):
            r.transcript_html = search.highlight(raw["transcript"], terms)
        return out

    def quick_ranges() -> list[tuple[str, str, str]]:
        now = datetime.now(UTC).astimezone(tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return [("Last hour", to_input(now - timedelta(hours=1)), ""),
                ("Today", to_input(today), ""),
                ("Yesterday", to_input(today - timedelta(days=1)), to_input(today)),
                ("Last 7 days", to_input(today - timedelta(days=6)), "")]

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", tg: str | None = None,
                    department: str | None = None, from_: str | None = Query(None, alias="from"),
                    to: str | None = None, has_audio: int = 0, include_no_speech: int = 0,
                    conn: sqlite3.Connection = Depends(get_conn)):
        f, errors = search_filters(tg, department, from_, to, has_audio, include_no_speech)
        asked = bool(q.strip() or f.tgs or f.department or f.since or f.until or f.has_audio)
        size = cfg.web.page_size
        rows = feed.mark_days(search_rows(conn, q, f, None, size)) if asked and not errors else []
        total = search.count(conn, q, f) if asked and not errors else 0
        params = {k: v for k, v in {"q": q, "tg": ",".join(f.tgs), "department": f.department,
                                     "from": from_, "to": to,
                                     "has_audio": 1 if f.has_audio else None,
                                     "include_no_speech": 1 if f.include_no_speech else None
                                     }.items() if v}
        departments = [r[0] for r in conn.execute(
            "SELECT DISTINCT department FROM channels WHERE department IS NOT NULL ORDER BY 1")]

        def href(**changes) -> str:
            p = {**params, **changes}
            return "/search?" + urlencode({k: v for k, v in p.items() if v})

        ranges = [(label, href(**{"from": a, "to": b})) for label, a, b in quick_ranges()]
        chips = []
        for t in feed.known_talkgroups(conn):
            on = t["tgid"] in f.tgs
            nxt = [x for x in f.tgs if x != t["tgid"]] if on else [*f.tgs, t["tgid"]]
            chips.append((t, on, href(tg=",".join(nxt))))
        return page(request, "search.html", conn, q=q, rows=rows, total=total, asked=asked,
                    errors=errors, f=f, from_input=from_ or "", to_input=to or "",
                    tgs=list(f.tgs), chips=chips, all_tg_href=href(tg=None),
                    departments=departments, ranges=ranges, qs=urlencode(params),
                    page_size=size, max_input=to_input(datetime.now(UTC)))

    @app.get("/fragments/search", response_class=HTMLResponse)
    def search_fragment(request: Request, before: str, q: str = "", tg: str | None = None,
                        department: str | None = None,
                        from_: str | None = Query(None, alias="from"), to: str | None = None,
                        has_audio: int = 0, include_no_speech: int = 0,
                        conn: sqlite3.Connection = Depends(get_conn)):
        f, errors = search_filters(tg, department, from_, to, has_audio, include_no_speech)
        size = cfg.web.page_size
        try:
            rows = search_rows(conn, q, f, before, size) if not errors else []
        except ValueError:
            rows = []
        feed.mark_days(rows, feed.cursor_date(before, tz))
        params = {k: v for k, v in request.query_params.items() if k != "before"}
        return templates.TemplateResponse(request, "_search_rows.html", {
            "rows": rows, "qs": urlencode(params), "page_size": size})

    # ---- talkgroup settings (M4) -------------------------------------------------------------

    def channel_rows(conn, key: str | None = None, sort: str = "recent") -> list[dict]:
        now = datetime.now(UTC)
        day_start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        marks = {"h1": (now - timedelta(hours=1)).isoformat(),
                 "today": day_start.astimezone(UTC).isoformat(),
                 "d7": (now - timedelta(days=7)).isoformat()}
        sql = """SELECT c.*,
                   (SELECT COUNT(*) FROM transmissions t WHERE t.channel_key = c.key) AS n_total,
                   (SELECT COUNT(*) FROM transmissions t WHERE t.channel_key = c.key AND t.started_at >= :h1) AS n_h1,
                   (SELECT COUNT(*) FROM transmissions t WHERE t.channel_key = c.key AND t.started_at >= :today) AS n_today,
                   (SELECT COUNT(*) FROM transmissions t WHERE t.channel_key = c.key AND t.started_at >= :d7) AS n_d7,
                   (SELECT COUNT(*) FROM transmissions t WHERE t.channel_key = c.key AND t.status = 'skipped'
                        AND t.error = 'ignored channel' AND t.audio_path IS NOT NULL) AS n_skipped
                 FROM channels c"""
        args: dict = dict(marks)
        if key:
            sql += " WHERE c.key = :key"
            args["key"] = key
        sql += " ORDER BY n_d7 DESC, c.last_heard_at DESC" if sort == "busy" else \
            " ORDER BY c.last_heard_at DESC"
        out = []
        for r in conn.execute(sql, args):
            d = dict(r)
            d["name"] = d["alias"] or d["channel"] or (f"TG {d['tgid']}" if d["tgid"] else d["key"])
            d["hue"] = feed.hue_for(d["color"], d["tgid"])
            out.append(d)
        return out

    def render_channel(request: Request, conn, key: str, saved: str | None = None):
        rows = channel_rows(conn, key)
        if not rows:
            raise HTTPException(404, "No such talkgroup")
        return templates.TemplateResponse(request, "_channel_row.html", {
            "c": rows[0], "palette": feed.PALETTE, "saved": saved, "admin": True})

    def require_edit_request(request: Request) -> None:
        """No login (Dan's choice), but refuse cross-site form posts: edits must come from htmx
        (HX-Request header) or be JSON. Other sites can't send either to the NUC without a
        CORS preflight, which this server never approves."""
        ctype = request.headers.get("content-type", "")
        if request.headers.get("hx-request") != "true" and not ctype.startswith("application/json"):
            raise HTTPException(403, "Edits must be made from the web page")

    @app.get("/channels", response_class=HTMLResponse)
    def channels_page(request: Request, sort: str = "recent",
                      conn: sqlite3.Connection = Depends(get_conn)):
        sort = sort if sort in ("recent", "busy") else "recent"
        return page(request, "channels.html", conn, channels=channel_rows(conn, sort=sort),
                    palette=feed.PALETTE, sort=sort)

    @app.get("/api/channels")
    def api_channels(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {"items": channel_rows(conn)}

    @app.patch("/api/channels/{key:path}")
    async def patch_channel(request: Request, key: str,
                            conn: sqlite3.Connection = Depends(get_conn)):
        require_edit_request(request)
        require_admin(request, conn)
        if request.headers.get("content-type", "").startswith("application/json"):
            data = await request.json()
            if not isinstance(data, dict):
                raise HTTPException(400, "Expected a JSON object")
        else:  # htmx sends application/x-www-form-urlencoded; no multipart needed
            data = {k: v[-1] for k, v in parse_qs((await request.body()).decode()).items()}
        sets, args, what = [], [], []
        if "alias" in data:
            alias = str(data["alias"]).strip()[:60] or None
            sets.append("alias = ?"); args.append(alias); what.append("name")
        if "color" in data:
            try:
                color = int(data["color"]) % len(feed.PALETTE)
            except (TypeError, ValueError):
                raise HTTPException(400, "color must be 0-9") from None
            sets.append("color = ?"); args.append(color); what.append("colour")
        for field, label in (("ignored", "transcription"), ("hidden", "live feed")):
            if field in data:
                val = str(data[field]).lower() in ("1", "true", "on", "yes")
                sets.append(f"{field} = ?"); args.append(int(val)); what.append(label)
        if not sets:
            raise HTTPException(400, "Nothing to change")
        cur = conn.execute(f"UPDATE channels SET {', '.join(sets)} WHERE key = ?", (*args, key))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "No such talkgroup")
        if request.headers.get("hx-request") == "true":
            return render_channel(request, conn, key, saved="Saved")
        return channel_rows(conn, key)[0]

    @app.post("/api/channels/{key:path}/transcribe-skipped")
    def transcribe_skipped(request: Request, key: str,
                           conn: sqlite3.Connection = Depends(get_conn)):
        require_edit_request(request)
        require_admin(request, conn)
        cur = conn.execute(
            """UPDATE transmissions SET status = 'queued', attempts = 0, error = NULL,
                   updated_at = ? WHERE channel_key = ? AND status = 'skipped'
                   AND error = 'ignored channel' AND audio_path IS NOT NULL""",
            (db.utcnow(), key))
        conn.commit()
        n = cur.rowcount
        if request.headers.get("hx-request") == "true":
            return render_channel(request, conn, key,
                                  saved=f"Queued {n} clip{'s' if n != 1 else ''} for transcription")
        return {"queued": n}

    # ---- Listen live (M6): one continuous MP3 stream of everything the scanner hears -------
    @app.get("/stream.mp3")
    async def stream_mp3(t: str | None = None):
        # t: the Listen page's per-connection token; Safari's probe and real request share it
        return StreamingResponse(station.listen((t or "")[:40] or None), headers=stream_mod.HEADERS)

    @app.get("/listen", response_class=HTMLResponse)
    def listen(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return page(request, "listen.html", conn, stream=station.info())

    @app.get("/fragments/listen-info", response_class=HTMLResponse)
    def listen_info(request: Request):
        return templates.TemplateResponse(request, "_listen_info.html", {"stream": station.info()})

    @app.get("/fragments/tg-chips", response_class=HTMLResponse)
    def tg_chips_fragment(request: Request, tg: str | None = None,
                          conn: sqlite3.Connection = Depends(get_conn)):
        """The live page's talkgroup chips (new talkgroups, fresh 24 h counts)."""
        return templates.TemplateResponse(request, "_tg_chips.html", {
            "talkgroups": feed.recent_talkgroups(conn), "tgs": parse_tgs(tg),
            "anchor": None, "anchor_input": ""})

    @app.get("/fragments/feed", response_class=HTMLResponse)
    def feed_fragment(request: Request, before: str | None = None, after: str | None = None,
                      tg: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
        """Older rows (appended below) or newer rows (inserted above) for the feed list."""
        tgs = parse_tgs(tg)
        size = cfg.web.page_size
        try:
            if after:
                rows = feed.present(feed.newer(conn, after, tgs=tgs, limit=size), tz)
                direction = "newer"
            else:
                rows = feed.present(feed.older(conn, before, tgs=tgs, limit=size), tz) \
                    if before else []
                direction = "older"
        except ValueError:
            rows, direction = [], "older"
        feed.mark_days(rows, feed.cursor_date(before, tz) if direction == "older" and before
                       else None)
        return templates.TemplateResponse(request, "_tx_rows.html", {
            "rows": rows, "direction": direction, "tg_param": ",".join(tgs), "page_size": size})

    @app.get("/api/transmissions")
    def api_transmissions(tg: str | None = None, before: str | None = None,
                          after: str | None = None, at: str | None = None, limit: int = 50,
                          hide_no_speech: bool = False, q: str | None = None,
                          department: str | None = None,
                          from_: str | None = Query(None, alias="from"), to: str | None = None,
                          has_audio: bool = False, include_no_speech: bool = True,
                          conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        tgs, limit = parse_tgs(tg), max(1, min(limit, 500))
        kw = {"tgs": tgs, "hide_no_speech": hide_no_speech}
        if q or department or from_ or to or has_audio:
            f, errors = search_filters(tg, department, from_, to, has_audio,
                                       include_no_speech and not hide_no_speech)
            if errors:
                raise HTTPException(400, "; ".join(errors))
            rows, _ = search.run(conn, q or "", f, before=before, limit=limit)
        elif at:
            a, b = feed.around(conn, parse_local(at), each_side=limit // 2, **kw)
            rows = a + b
        elif after:
            rows = feed.newer(conn, after, limit=limit, **kw)
        elif before:
            rows = feed.older(conn, before, limit=limit, **kw)
        else:
            rows = feed.latest(conn, limit=limit, **kw)
        items = [dict(r) | {"cursor": feed.encode_cursor(r["started_at"], r["id"])} for r in rows]
        for it in items:
            it["flags"] = json.loads(it["flags"]) if it["flags"] else []
            it["unit_ids"] = json.loads(it["unit_ids"]) if it["unit_ids"] else []
            it.pop("audio_path", None)
        return {"items": items}

    # ---- live updates: Server-Sent Events (step 4) -------------------------------------------

    @app.get("/events")
    async def events(request: Request, since: str | None = None, last: str | None = None,
                     feed_on: int = Query(0, alias="feed"), tg: str | None = None):
        """One stream per page. `status` events (header dot) on every page; `tx` events
        (created/updated rows, rendered HTML) when feed=1. Resumes from Last-Event-ID (sent
        by the browser on auto-reconnect) or `last` (a fresh EventSource after phone sleep),
        else from `since` (the page render time)."""
        resume = request.headers.get("last-event-id") or last
        # a raw "+00:00" in a hand-typed URL arrives as " 00:00"
        resume = resume.replace(" ", "+") if resume else resume
        since = since.replace(" ", "+") if since else since
        try:
            cursor = (changes.cursor_from_event_id(resume) if resume
                      else datetime.fromisoformat(since).isoformat(timespec="milliseconds")
                      if since else db.utcnow())
        except ValueError:
            cursor = db.utcnow()
        tgs = parse_tgs(tg)
        cf = changes.ChangeFeed(cursor, tgs) if feed_on else None
        item_t = templates.get_template("_tx_item.html")
        head_t = templates.get_template("_tx_head.html")
        dot_t = templates.get_template("_status_dot.html")
        limit_s = app.state.sse_max_seconds  # tests stop the stream; None in production

        def poll(conn: sqlite3.Connection, check_status: bool) -> tuple[list, str | None]:
            out = []
            if cf is not None:
                for r in cf.poll(conn):
                    [row] = feed.present([r], tz)
                    out.append((changes.event_id(r), {
                        "id": row.id, "tgid": row.tgid, "started_at": row.started_at,
                        "ended_at": row.ended_at, "day": row.day,
                        "item": item_t.render(r=row), "head": head_t.render(r=row)}))
            dot = None
            if check_status:
                h = health.evaluate(conn, cfg)
                dot = json.dumps({"level": h.level, "html": dot_t.render(health=h)})
            return out, dot

        async def stream():
            conn = sqlite3.connect(cfg.storage.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            started = last_ka = last_status = asyncio.get_running_loop().time()
            last_dot = None
            yield "retry: 3000\n\n"
            try:
                first = True
                while True:
                    now = asyncio.get_running_loop().time()
                    check = first or now - last_status >= STATUS_EVERY_S
                    txs, dot = await asyncio.to_thread(poll, conn, check)
                    for eid, payload in txs:
                        yield f"event: tx\nid: {eid}\ndata: {json.dumps(payload)}\n\n"
                    if check:
                        last_status = now
                        if dot != last_dot:
                            yield f"event: status\ndata: {dot}\n\n"
                            last_dot = dot
                    if now - last_ka >= KEEPALIVE_S:
                        yield ": keep-alive\n\n"
                        last_ka = now
                    first = False
                    if limit_s is not None and now - started >= limit_s:
                        return
                    if await request.is_disconnected():
                        return
                    await asyncio.sleep(POLL_S)
            finally:
                conn.close()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- transmission detail + audio (step 5) ---------------------------------------------

    def download_name(r: sqlite3.Row) -> str:
        local_start = datetime.fromisoformat(r["started_at"]).astimezone(tz)
        tg = f"_TG{r['tgid']}" if r["tgid"] else ""
        return f"{local_start:%Y-%m-%d_%H%M%S}{tg}_{r['id']}.wav"

    @app.get("/t/{tx_id}", response_class=HTMLResponse)
    def detail(request: Request, tx_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        r = conn.execute("SELECT t.*, c.alias, c.color FROM transmissions t "
                         "LEFT JOIN channels c ON c.key = t.channel_key WHERE t.id = ?",
                         (tx_id,)).fetchone()
        if r is None:
            resp = page(request, "not_found.html", conn, what=f"Transmission {tx_id}")
            resp.status_code = 404
            return resp
        [row] = feed.present([r], tz)
        tx = dict(r)
        for k in ("flags", "unit_ids", "status_changes"):
            tx[k] = json.loads(tx[k]) if tx.get(k) else []
        # the conversation: neighbours on the same talkgroup
        before, after = [], []
        if r["tgid"]:
            cur = feed.encode_cursor(r["started_at"], r["id"])
            before = feed.present(feed.older(conn, cur, tgs=[r["tgid"]], limit=5), tz)
            after = feed.present(feed.newer(conn, cur, tgs=[r["tgid"]], limit=5), tz)
        latency = None
        if r["transcribed_at"]:
            latency = (datetime.fromisoformat(r["transcribed_at"])
                       - datetime.fromisoformat(r["ended_at"])).total_seconds()
        return page(request, "detail.html", conn, r=row, tx=tx, before=before, after=after,
                    started_local=row.started, download=download_name(r),
                    utc_time=datetime.fromisoformat(r["started_at"]).astimezone(UTC)
                    .strftime("%H:%M:%S"), latency_s=latency, flag_text=feed.FLAG_TEXT)

    @app.get("/fragments/tx-text/{tx_id}", response_class=HTMLResponse)
    def tx_text(request: Request, tx_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        """Polled by the detail page while a transcript is pending; HTTP 286 stops polling."""
        r = conn.execute("SELECT t.*, c.alias, c.color FROM transmissions t LEFT JOIN channels c "
                         "ON c.key = t.channel_key WHERE t.id = ?", (tx_id,)).fetchone()
        if r is None:
            raise HTTPException(404)
        [row] = feed.present([r], tz)
        resp = templates.TemplateResponse(request, "_detail_text.html", {"r": row, "tx": dict(r)})
        if not row.pending:
            resp.status_code = 286
        return resp

    @app.get("/api/transmissions/{tx_id}")
    def api_transmission(tx_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        r = conn.execute("SELECT * FROM transmissions WHERE id = ?", (tx_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "No such transmission")
        d = dict(r)
        for k in ("flags", "unit_ids", "status_changes"):
            d[k] = json.loads(d[k]) if d.get(k) else []
        d["has_audio"] = bool(d.pop("audio_path"))
        d["audio_url"] = f"/api/transmissions/{tx_id}/audio" if d["has_audio"] else None
        return d

    @lru_cache(maxsize=64)
    def levelled_wav(path: str, mtime_ns: int) -> bytes:
        """Levelled copy for listening (cached: Safari fetches each clip in several ranges)."""
        pcm, rate = audio_mod.read_wav(path)
        return audio_mod.wav_bytes(audio_mod.level(pcm), rate)

    @app.get("/api/transmissions/{tx_id}/audio")
    def audio(request: Request, tx_id: int, download: bool = False, original: bool = False,
              conn: sqlite3.Connection = Depends(get_conn)):
        """Levelled WAV for listening, with HTTP Range support (Safari won't play <audio>
        without it). `download=1` / `original=1`: the file exactly as recorded."""
        row = conn.execute("SELECT id, started_at, tgid, audio_path FROM transmissions "
                           "WHERE id = ?", (tx_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such transmission")
        if not row["audio_path"]:
            raise HTTPException(410, "Audio was removed by retention")
        path = (cfg.storage.data_dir / row["audio_path"]).resolve()
        if not path.is_file() or cfg.storage.data_dir.resolve() not in path.parents:
            raise HTTPException(410, "Audio file is missing")
        if download or original:
            return FileResponse(path, media_type="audio/wav",
                                filename=download_name(row) if download else None,
                                content_disposition_type="attachment" if download else "inline")
        try:
            data = levelled_wav(str(path), path.stat().st_mtime_ns)
        except (OSError, ValueError, EOFError):
            return FileResponse(path, media_type="audio/wav")   # play it as recorded
        return byte_range_response(request, data, "audio/wav")

    return app


def byte_range_response(request: Request, data: bytes, media_type: str) -> Response:
    """200 with the whole body, or 206 for a single `Range: bytes=a-b` (what media players
    send); 416 for a range that can't be satisfied."""
    size = len(data)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    rng = request.headers.get("range", "")
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng) if rng else None
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return Response(data, media_type=media_type, headers=headers)
    if m.group(1) == "":                       # suffix range: last N bytes
        start, end = max(0, size - int(m.group(2))), size - 1
    else:
        start = int(m.group(1))
        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    if start >= size or start > end:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(data[start:end + 1], status_code=206, media_type=media_type, headers=headers)


def bind_host(bind: str) -> str:
    """[web] bind: an address, or "*" for every interface."""
    return "0.0.0.0" if bind.strip() in ("*", "") else bind.strip()


def serve(cfg: Config) -> None:
    import uvicorn

    # SSE streams never end on their own, so graceful shutdown must not wait for them
    # (otherwise Ctrl-C / systemd stop hangs while any page is open).
    uvicorn.run(create_app(cfg), host=bind_host(cfg.web.bind), port=cfg.web.port, log_level="info",
                proxy_headers=True, timeout_graceful_shutdown=3)
