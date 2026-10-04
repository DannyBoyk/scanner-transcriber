"""Admin page: prompt editor (count, save, history/diff, revert) and substitutions editor."""
from scanner_transcriber import promptstore, statestore

HX = {"HX-Request": "true"}


def test_admin_page_shows_prompt_and_counter(admin_env):
    conn, client, _ = admin_env
    html = client.get("/admin").text
    assert 'id="prompt"' in html and 'id="subs"' in html
    assert promptstore.DEFAULT_PROMPT[:40] in html or "Lakeside" in html
    assert "/ 223 tokens" in html


def test_token_count_endpoint(admin_env):
    _, client, _ = admin_env
    r = client.post("/admin/prompt/count", data={"prompt": "Medic 1, Medic 2."}, headers=HX)
    assert r.status_code == 200 and "/ 223 tokens" in r.text and "level-ok" in r.text
    r = client.post("/admin/prompt/count", data={"prompt": "Northfield County dispatch. " * 60}, headers=HX)
    assert "level-error" in r.text and "too long" in r.text
    r = client.post("/admin/prompt/count", data={"prompt": "go ahead, copy that, radio check"}, headers=HX)
    assert "could trigger a re-run" in r.text


def test_save_prompt_creates_active_version_and_event(admin_env):
    conn, client, _ = admin_env
    before, _ = promptstore.active(conn, "prompt")
    r = client.post("/admin/prompt", data={"prompt": "Ashford,  Brookfield.\n Direct.", "note": "trim"},
                    headers=HX)
    assert "applies from the next transmission" in r.text
    vid, text = promptstore.active(conn, "prompt")
    assert vid != before and text == "Ashford, Brookfield. Direct."          # whitespace tidied
    assert "<ins>" in r.text or "<del>" in r.text                             # diff in history
    assert any(e["kind"] == "prompt_changed" for e in statestore.recent_events(conn))
    r = client.post("/admin/prompt", data={"prompt": "Ashford, Brookfield. Direct."}, headers=HX)
    assert "No changes" in r.text
    r = client.post("/admin/prompt", data={"prompt": "   "}, headers=HX)
    assert "can&#39;t be empty" in r.text or "can't be empty" in r.text


def test_revert_to_previous_version(admin_env):
    conn, client, _ = admin_env
    v1, t1 = promptstore.active(conn, "prompt")
    client.post("/admin/prompt", data={"prompt": "new text"}, headers=HX)
    r = client.post(f"/admin/versions/{v1}/activate", headers=HX)
    assert "is active again" in r.text and promptstore.active(conn, "prompt") == (v1, t1)
    assert client.post("/admin/versions/99999/activate", headers=HX).status_code == 404


def test_substitutions_editor(admin_env):
    conn, client, _ = admin_env
    r = client.post("/admin/substitutions",
                    data={"src": ["Charley", " Medicaid ", ""], "dst": ["Charlie", "Medic 8", "x"]},
                    headers=HX)
    assert "Saved as version" in r.text
    assert promptstore.active(conn, "substitutions")[1] == {"Charley": "Charlie", "Medicaid": "Medic 8"}
    r = client.post("/admin/substitutions", data={"src": ["Charley", ""], "dst": ["Charlie", ""]}, headers=HX)
    assert promptstore.active(conn, "substitutions")[1] == {"Charley": "Charlie"}     # row removed
    assert "<del>Medicaid → Medic 8</del>" in r.text


def test_admin_actions_need_login_and_htmx(env, admin_env):
    conn, client, _ = admin_env
    assert client.post("/admin/prompt", data={"prompt": "x"}).status_code == 403    # no HX header
    client.post("/logout")
    assert client.post("/admin/prompt", data={"prompt": "x"}, headers=HX).status_code == 401
    assert client.post("/admin/prompt/count", data={"prompt": "x"}, headers=HX).status_code == 401
