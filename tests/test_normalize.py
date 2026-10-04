"""Scoring normalisation: formatting differences aren't errors; each number is one token."""
import pytest

from scanner_transcriber.bench import normalize


@pytest.mark.parametrize("a,b", [
    ("135, radio check.", "one three five radio check"),
    ("One thirty-five, radio check", "135 radio check"),
    ("one oh six, I'm in the area", "106, I'm in the area."),
    ("One hundred six", "106"),
    ("one hundred and six", "106"),
    ("twenty-one, it's okay", "21 it's okay"),
    ("Medic 1403 en route", "medic one four oh three en route"),
    ("Clear, 19-15.", "clear 1915"),
    ("are you picking up one oh six's radio traffic", "are you picking up 106 radio traffic"),
    ("10-4", "Ten four."),
    ("Go ahead.", "go ahead"),
])
def test_same_content_scores_the_same(a, b):
    assert normalize(a) == normalize(b)


@pytest.mark.parametrize("a,b", [
    ("106", "116"),                                   # a different unit ID is different
    ("Traffic stop 29, Valley Inn 2020, 21", "Traffic stop 29, Valley Inn 2021"),
    ("Medic 3", "Medic 13"),
])
def test_different_numbers_differ(a, b):
    assert normalize(a) != normalize(b)


def test_a_wrong_number_is_one_token():
    assert normalize("one oh six, clear") == "106 clear"
    assert len(normalize("Medic 1403 is clear").split()) == 4
    assert normalize("2020, 21") == "2020 21"        # punctuation keeps separate numbers apart
