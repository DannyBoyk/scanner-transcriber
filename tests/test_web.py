"""Web app: health endpoints, status dot and /status page (FastAPI TestClient, synthetic DB)."""
from datetime import UTC, datetime, timedelta

from scanner_transcriber import statestore


def healthy(conn):
    statestore.publish(conn, "capture", "streaming",
                       {"source": "live", "connected_since": datetime.now(UTC).isoformat()})
    statestore.publish(conn, "worker", "gpu", {"model": "small.en", "processed": 3})


def test_healthz_error_when_nothing_running(env):
    _, client, _ = env
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json()["level"] == "error"


def test_healthz_ok_when_healthy(env):
    conn, client, _ = env
    healthy(conn)
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"level": "ok", "conditions": []}


def test_healthz_warn_is_still_200(env):
    conn, client, _ = env
    healthy(conn)
    statestore.publish(conn, "worker", "degraded", {"degraded_reason": "GPU gone"})
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["level"] == "warn"


def test_status_dot_colours_and_tooltip(env):
    conn, client, _ = env
    assert "level-error" in client.get("/fragments/status-dot").text
    healthy(conn)
    html = client.get("/fragments/status-dot").text
    assert "level-ok" in html and 'title="All systems normal"' in html
    statestore.publish(conn, "capture", "reconnecting",
                       {"disconnected_at": (datetime.now(UTC) - timedelta(seconds=20)).isoformat(),
                        "last_error": "no audio for 5.1s"})
    html = client.get("/fragments/status-dot").text
    assert "level-warn" in html and "Scanner unreachable for 20s" in html


def test_every_page_has_the_dot(env):
    _, client, _ = env
    for path in ("/", "/status"):
        html = client.get(path).text
        assert 'class="status-dot level-' in html and "/static/htmx.min.js" in html
        assert "/static/app.js" in html          # keeps the dot live (SSE)


def test_status_page_shows_conditions_details_and_events(env):
    conn, client, _ = env
    healthy(conn)
    statestore.publish(conn, "worker", "degraded", {"model": "parakeet", "degraded_reason": "GPU gone"})
    statestore.event(conn, "capture", "warn", "disconnected", "Scanner connection lost: timeout")
    statestore.event(conn, "capture", "info", "reconnected", "Scanner reconnected after 2m 05s")
    html = client.get("/status").text
    assert "Transcribing in degraded mode" in html and "GPU gone" in html
    assert "Scanner reconnected after 2m 05s" in html and "Scanner connection lost" in html
    assert "Disk free" in html and "200.0 GB" in html


def test_status_event_filter_and_age(env):
    conn, client, _ = env
    statestore.event(conn, "capture", "info", "connected", "Scanner connected")
    statestore.event(conn, "worker", "info", "started", "Transcription worker started")
    conn.execute("UPDATE system_events SET at = ? WHERE kind = 'connected'",
                 ((datetime.now(UTC) - timedelta(days=40)).isoformat(),))
    html = client.get("/status").text
    assert "Transcription worker started" in html and "Scanner connected" not in html  # >30 days
    html = client.get("/status?component=capture").text
    assert "Transcription worker started" not in html


def test_times_shown_in_configured_timezone(env):
    conn, client, _ = env
    statestore.event(conn, "capture", "info", "connected", "Scanner connected")
    conn.execute("UPDATE system_events SET at = '2026-09-30T18:04:12+00:00'")
    html = client.get("/status").text
    assert "Sep 30 14:04:12" in html  # EDT = UTC-4


def test_api_status_json(env):
    conn, client, _ = env
    healthy(conn)
    j = client.get("/api/status").json()
    assert j["health"]["level"] == "ok" and "transcription" in j and "events" in j


def test_static_files_served(env):
    _, client, _ = env
    assert client.get("/static/htmx.min.js").status_code == 200
    assert "--ok" in client.get("/static/app.css").text


def test_bind_star_means_all_interfaces():
    from scanner_transcriber.web.server import bind_host
    assert bind_host("*") == "0.0.0.0"
    assert bind_host("127.0.0.1") == "127.0.0.1"
    assert bind_host(" 192.168.10.200 ") == "192.168.10.200"


def test_assets_use_relative_urls_so_https_proxies_work(env):
    """Behind `tailscale serve` (HTTPS → HTTP) absolute http:// asset URLs are blocked as
    mixed content, which left the page unstyled and without JavaScript."""
    _, client, _ = env
    html = client.get("/", headers={"Host": "nuc.example.ts.net"}).text
    assert 'href="/static/app.css?v=' in html and 'src="/static/app.js?v=' in html
    assert "http://" not in html.split("<body")[0]


def test_icons_and_manifest_are_linked_and_served(env):
    _, client, _ = env
    head = client.get("/").text.split("<body")[0]
    for href in ("/static/icon.svg", "/static/favicon-32.png", "/static/apple-touch-icon.png",
                 "/static/manifest.webmanifest"):
        assert f'href="{href}"' in head
        assert client.get(href).status_code == 200
    m = client.get("/static/manifest.webmanifest").json()
    assert m["short_name"] == "Scanner" and m["start_url"] == "/"
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200



def test_theme_toggle_and_versioned_assets(env):
    """Assets carry their file's mtime (?v=...) so browsers can't serve stale copies, and every
    page has the theme button plus the script that applies a saved choice before paint."""
    _, client, _ = env
    html = client.get("/status").text
    for name in ("app.css", "app.js", "player.js", "htmx.min.js"):
        assert f"/static/{name}?v=" in html
    assert 'id="theme-toggle"' in html and 'localStorage.getItem("theme")' in html
    css = client.get("/static/app.css").text
    assert ':root[data-theme="dark"]' in css and ':root:not([data-theme="light"])' in css
