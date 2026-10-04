import pytest

from scanner_transcriber import guards

PROMPT = ("Ashford, Brookfield, Carver, Dunmore, Easton. Medic 3, Ambulance 1403. "
          "Alpha, Bravo, Charlie, Delta, Echo, Foxtrot, Golf.")


@pytest.mark.parametrize("text,expected", [
    ("Thank you.", True),
    ("Thank you. Thank you.", True),
    ("thanks for watching!", True),
    ("Hmm.", True),
    ("Thank you, Lakeside Highway 721.", False),  # real content after the filler
    ("Medic 3 en route.", False),
    ("Okay.", False),                                 # real short replies are not filler
    ("Copy.", False),
    ("", False),
])
def test_is_hallucination(text, expected):
    assert guards.is_hallucination(text) is expected


@pytest.mark.parametrize("text,expected", [("", True), ("  ", True), ("... ... ...", True),
                                           ("Go ahead.", False)])
def test_is_blank(text, expected):
    assert guards.is_blank(text) is expected


def test_collapse_repeats():
    text, changed = guards.collapse_repeats("one oh six, one oh six, one oh six, go ahead")
    assert changed and text == "one oh six, go ahead"
    text, changed = guards.collapse_repeats("go go go go")
    assert changed and text == "go"
    # two copies is normal radio speech ("clear, clear") — left alone
    text, changed = guards.collapse_repeats("One oh six, you're coming in low, one oh six.")
    assert not changed


@pytest.mark.parametrize("text,expected", [
    ("Uniform Alpha Bravo Charlie Delta", True),       # alphabet run copied from prompt
    ("Brookfield, Carver, Dunmore, Easton, clear", True),
    ("Sierra Zulu Tango 7250", False),                 # plate: not in prompt order
    ("Medic 3 en route to Ashford", False),
    ("Charlie 5802", False),
])
def test_prompt_leak(text, expected):
    assert guards.prompt_leak(text, PROMPT) is expected


def test_substitute_whole_words_case_insensitive():
    subs = {"Charley": "Charlie", "fox 3": "Northfield"}
    assert guards.substitute("charley 58022480", subs) == "Charlie 58022480"
    assert guards.substitute("Fox 3 to 135, radio check", subs) == "Northfield to 135, radio check"
    assert guards.substitute("Charleyville", subs) == "Charleyville"


def test_substitution_replacement_is_literal_text():
    from scanner_transcriber.guards import substitute
    assert substitute("unit ten four", {"ten four": r"10\4"}) == r"unit 10\4"
    assert substitute("go to Main", {"main": r"\g<0> St"}) == r"go to \g<0> St"
