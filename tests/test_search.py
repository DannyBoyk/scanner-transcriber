"""Search semantics: stems, partial words, phrases, exclusions, filters, paging, highlighting."""

from test_feed import T0, add

from scanner_transcriber.web import search
from scanner_transcriber.web.feed import encode_cursor
from scanner_transcriber.web.search import Filters, Term, highlight, parse


def found(conn, q, f=Filters(), **kw):
    rows, _ = search.run(conn, q, f, **kw)
    return [r["transcript"] for r in rows]


def seed(c, d):
    texts = ["Traffic stop Mill Road", "121 traffic stops at Quarry Road",
             "Medic 1403 en route", "Stopped vehicle on Hillcrest Avenue",
             "I'm direct, back at station", "Test transmission please disregard"]
    return [add(c, d, i * 60, transcript=t, tgid=("1101", "1201")[i % 2]) for i, t in enumerate(texts)]


def test_parse():
    assert parse('mill "traffic stop" -test quar') == [
        Term("mill"), Term("traffic stop", phrase=True), Term("test", negate=True), Term("quar")]
    assert parse('"unterminated phrase') == [Term("unterminated phrase", phrase=True)]
    assert parse("") == [] and parse('   ""  ') == []


def test_words_stems_and_partials(conn, tmp_path):
    c, d = conn, tmp_path
    seed(c, d)
    assert set(found(c, "stop")) >= {"Traffic stop Mill Road", "121 traffic stops at Quarry Road"}
    assert "Stopped vehicle on Hillcrest Avenue" in found(c, "stop")          # stem/substring
    assert found(c, "quar") == ["121 traffic stops at Quarry Road"]        # partial word
    assert found(c, "1403") == ["Medic 1403 en route"]                        # number substring
    assert found(c, "140") == ["Medic 1403 en route"]                         # partial number
    assert found(c, "hillcrest avenue") == ["Stopped vehicle on Hillcrest Avenue"]  # AND


def test_phrase_and_exclusion(conn, tmp_path):
    c, d = conn, tmp_path
    seed(c, d)
    # phrases keep word order but still allow stems/substrings ("traffic stops")
    assert set(found(c, '"traffic stop"')) == {"Traffic stop Mill Road",
                                              "121 traffic stops at Quarry Road"}
    assert found(c, '"stop mill"') == ["Traffic stop Mill Road"]
    assert found(c, "traffic -quarry") == ["Traffic stop Mill Road"]
    assert "Test transmission please disregard" not in found(c, "-test")
    assert found(c, "direct") == ["I'm direct, back at station"]


def test_odd_input_never_errors(conn, tmp_path):
    c, d = conn, tmp_path
    seed(c, d)
    for q in ['"', "*", "NEAR(a b)", "a OR b", "col:value", "'; DROP TABLE transmissions; --",
              "{transcript}: x", "^start", "a AND", "(((", "-", "🚓"]:
        search.run(c, q, Filters())       # must not raise
    assert c.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == 6


def test_filters_combine(conn, tmp_path):
    c, d = conn, tmp_path
    ids = seed(c, d)
    c.execute("UPDATE transmissions SET no_speech = 1, transcript = '' WHERE id = ?", (ids[5],))
    assert found(c, "traffic", Filters(tgs=("1201",))) == ["121 traffic stops at Quarry Road"]
    since = T0.replace(minute=2).isoformat()
    until = T0.replace(minute=4).isoformat()
    assert found(c, "", Filters(since=since, until=until)) == \
        ["Stopped vehicle on Hillcrest Avenue", "Medic 1403 en route"]
    assert len(found(c, "")) == 5                                   # no-speech excluded by default
    assert len(found(c, "", Filters(include_no_speech=True))) == 6
    assert search.count(c, "traffic", Filters()) == 2


def test_keyset_paging(conn, tmp_path):
    c, d = conn, tmp_path
    for i in range(12):
        add(c, d, i * 60, transcript=f"status check {i}")
    page1, _ = search.run(c, "status", Filters(), limit=5)
    last = page1[-1]
    page2, _ = search.run(c, "status", Filters(), limit=5,
                          before=encode_cursor(last["started_at"], last["id"]))
    ids1, ids2 = [r["id"] for r in page1], [r["id"] for r in page2]
    assert len(ids1) == len(ids2) == 5 and not set(ids1) & set(ids2) and ids1[-1] > ids2[0]


def test_trigram_index_tracks_updates_and_deletes(conn, tmp_path):
    c, d = conn, tmp_path
    tx = add(c, d, 0, transcript="alpha bravo")
    assert found(c, "brav") == ["alpha bravo"]
    c.execute("UPDATE transmissions SET transcript = 'charlie delta' WHERE id = ?", (tx,))
    assert found(c, "brav") == [] and found(c, "harl") == ["charlie delta"]
    c.execute("DELETE FROM transmissions WHERE id = ?", (tx,))
    assert found(c, "harl") == []


def test_highlight():
    terms = parse('quar stops "mill road" -test')
    h = str(highlight("121 traffic stop at Quarry, Mill  Road", terms))
    assert "<mark>Quar</mark>ry" in h and "<mark>stop</mark>" in h
    assert "<mark>Mill  Road</mark>" in h
    assert str(highlight("<script>alert(1)</script> stop", parse("stop"))) == \
        "&lt;script&gt;alert(1)&lt;/script&gt; <mark>stop</mark>"          # escaped
    assert str(highlight("go to 10 now", parse("10"))) == "go to <mark>10</mark> now"
    assert highlight(None, terms) is None
