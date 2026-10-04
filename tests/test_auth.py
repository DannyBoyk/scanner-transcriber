"""Admin password, sessions, rate limiting, fail-closed, and what's protected."""
import time

from scanner_transcriber import auth
from scanner_transcriber.auth import LoginLimiter

from conftest import login
from test_channels import save_clip, url

HX = {"HX-Request": "true"}


def test_hash_roundtrip_and_wrong_password():
    h = auth.hash_password("s3cret-pass")
    assert h.startswith("scrypt$") and auth.check_password("s3cret-pass", h)
    assert not auth.check_password("nope", h) and not auth.check_password("x", "garbage")


def test_admin_disabled_until_password_set(env):
    conn, client, _ = env
    assert "Admin is turned off" in client.get("/admin").text
    assert "set-password" in client.get("/login").text
    save_clip(conn, _, "1101")
    assert client.patch(url("1101"), json={"alias": "x"}).status_code == 403   # fail closed


def test_login_logout_and_protection(env):
    conn, client, d = env
    save_clip(conn, d, "1101")
    auth.set_password(conn, "correct horse battery")
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/admin"
    assert client.patch(url("1101"), json={"alias": "x"}).status_code == 401
    assert "Log in" in client.get("/channels").text and "swatch-btn" not in client.get("/channels").text
    r = client.post("/login", data={"password": "wrong", "next": "/admin"})
    assert r.status_code == 401 and "Wrong password" in r.text
    time.sleep(1.1)                                                   # first failure: 1 s delay
    r = login(client, conn)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
    assert "Log out" in client.get("/admin").text
    assert "swatch-btn" in client.get("/channels").text               # edit controls when logged in
    assert client.patch(url("1101"), json={"alias": "ok"}).status_code == 200
    client.post("/logout")
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_next_redirect_is_local_only(env):
    conn, client, _ = env
    auth.set_password(conn, "pw-12345678")
    r = client.post("/login", data={"password": "pw-12345678", "next": "//evil.example/x"},
                    follow_redirects=False)
    assert r.headers["location"] == "/admin"
    r = client.post("/login", data={"password": "pw-12345678", "next": "/channels"},
                    follow_redirects=False)
    assert r.headers["location"] == "/channels"


def test_tampered_expired_and_rotated_sessions(env):
    conn, client, _ = env
    auth.set_password(conn, "pw-12345678")
    good, _ = auth.make_session(conn)
    assert auth.valid_session(conn, good)
    exp, sig = good.split(".")
    assert not auth.valid_session(conn, f"{int(exp) + 999}.{sig}")          # tampered expiry
    assert not auth.valid_session(conn, good, now=int(exp) + 1)             # expired
    auth.set_password(conn, "new-password-1")                              # rotation
    assert not auth.valid_session(conn, good)
    auth.set_password(conn, None)                                          # cleared → off
    assert not auth.enabled(conn)


def test_login_rate_limit():
    lim = LoginLimiter()
    assert lim.wait_s("1.2.3.4", now=100) == 0
    for _ in range(3):
        lim.failed("1.2.3.4", now=100)
    assert lim.wait_s("1.2.3.4", now=100) == 4                             # 1, 2, 4 s
    assert lim.wait_s("5.6.7.8", now=100) == 0                             # per client
    lim.ok("1.2.3.4")
    assert lim.wait_s("1.2.3.4", now=100) == 0


def test_rate_limited_login_page(env):
    conn, client, _ = env
    auth.set_password(conn, "pw-12345678")
    client.post("/login", data={"password": "wrong"})
    r = client.post("/login", data={"password": "pw-12345678"})           # still inside the delay
    assert "Too many attempts" in r.text
