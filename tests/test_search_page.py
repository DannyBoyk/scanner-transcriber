"""/search page, its Load more fragment, the header box, and API search filters."""
from test_feed import add


def test_empty_search_shows_help_and_header_box(env):
    _, client, _ = env
    html = client.get("/search").text
    assert "How search works" in html and 'action="/search"' in html
    assert 'class="top-search"' in client.get("/").text                 # header box everywhere


def test_search_highlights_and_counts(env):
    conn, client, d = env
    add(conn, d, 0, transcript="121 traffic stop at Quarry Road")
    add(conn, d, 60, transcript="Medic 3 en route")
    html = client.get("/search?q=quar").text
    assert "1 match" in html and "<mark>Quar</mark>ry" in html
    assert "Medic 3 en route" not in html
    assert 'id="results"' in html and 'class="xhead"' in html           # same block rows


def test_transcript_html_is_escaped(env):
    conn, client, d = env
    add(conn, d, 0, transcript="<img src=x onerror=alert(1)> stop")
    html = client.get("/search?q=stop").text
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_no_results_message(env):
    conn, client, d = env
    add(conn, d, 0, transcript="clear")
    html = client.get("/search?q=zzzqqq").text
    assert "No transmissions match" in html and "remove a filter" in html


def test_filters_only_browse_and_chips(env):
    conn, client, d = env
    add(conn, d, 0, tgid="1101", transcript="sheriff one")
    add(conn, d, 60, tgid="1201", transcript="town one")
    html = client.get("/search?tg=1201").text
    assert "town one" in html and "sheriff one" not in html
    assert 'aria-current="true"' in html and "All talkgroups" in html
    html = client.get("/search?q=one&from=2026-09-30T12:00&to=2026-09-30T12:01").text
    assert "sheriff <mark>one</mark>" in html and "town" not in html  # 12:00 ≤ t < 12:01


def test_bad_date_is_explained(env):
    _, client, _ = env
    html = client.get("/search?q=x&from=yesterday-ish").text
    assert "Couldn" in html and "yesterday-ish" in html


def test_load_more_fragment(env):
    conn, client, d = env
    for i in range(14):
        add(conn, d, i * 60, transcript=f"status check {i}")
    html = client.get("/search?q=status").text                          # page_size 10 in fixture
    assert "14 matches" in html and "Load more" in html
    link = html.split('hx-get="')[1].split('"')[0].replace("&amp;", "&")
    frag = client.get(link).text
    assert frag.count('id="tx-') == 4 and "End of results" in frag and "<mark>" in frag


def test_odd_queries_never_500(env):
    conn, client, d = env
    add(conn, d, 0, transcript="clear")
    for q in ['"', "*", "NEAR(a b)", "a OR", "'; DROP TABLE x; --", "(((", "🚓", "-"]:
        assert client.get("/search", params={"q": q}).status_code == 200


def test_api_search(env):
    conn, client, d = env
    add(conn, d, 0, transcript="Medic 1403 en route")
    add(conn, d, 60, transcript="clear")
    items = client.get("/api/transmissions?q=1403").json()["items"]
    assert [i["transcript"] for i in items] == ["Medic 1403 en route"]
    assert client.get("/api/transmissions?q=x&from=garbage").status_code == 400
