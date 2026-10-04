"""Shared fixtures: a web app over a synthetic DB (host metrics stubbed)."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from scanner_transcriber import db
from scanner_transcriber.config import Config, StorageConfig, WebConfig
from scanner_transcriber.web.server import create_app


@pytest.fixture
def conn(tmp_path):
    """A migrated database in tmp_path/scanner.db."""
    c = db.connect(tmp_path / "scanner.db")
    yield c
    c.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("scanner_transcriber.health.host_info",
                        lambda d: {"disk_free_gb": 200.0, "disk_free_pct": 80.0,
                                   "disk_total_gb": 250.0, "cpu_temp_c": 45.0, "cpu_mhz": 2800,
                                   "uptime_s": 3600})
    cfg = replace(Config(), storage=StorageConfig(data_dir=tmp_path),
                  web=WebConfig(timezone="America/New_York", page_size=10))
    conn = db.connect(cfg.storage.db_path)
    yield conn, TestClient(create_app(cfg)), tmp_path
    conn.close()


def login(client, conn, password="correct horse battery"):
    """Set the admin password and log the test client in (session cookie kept by the client)."""
    from scanner_transcriber import auth
    auth.set_password(conn, password)
    r = client.post("/login", data={"password": password, "next": "/admin"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


@pytest.fixture
def admin_env(env):
    conn, client, d = env
    login(client, conn)
    return conn, client, d
