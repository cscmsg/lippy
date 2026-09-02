"""Tests for spoken numbers.

A number written wrongly is worse than a number left as words, because it still
reads as a number and nothing about it looks repaired. So the cases that matter
most are the ones where a run of number words must be left exactly alone.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))

import pytest
import digits as digits_mod
from digits import NumberConfig, convert
from rules import RuleConfig, clean

CFG = NumberConfig(enabled=True, word_max=12,
                   triggers=["/session start", "/session end"])


# --------------------------------------------------------------------------
# Identifiers.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("spoken,expected", [
    ("ten fifty one", "1051"),
    ("one zero five one", "1051"),
    ("eleven oh five", "1105"),
    ("one zero five", "105"),
    ("twenty three sixty", "2360"),
])
def test_a_number_alone_is_an_identifier(spoken, expected):
    assert convert(spoken, CFG) == expected


@pytest.mark.parametrize("spoken", ["ten fifty one", "one zero five one"])
def test_a_trigger_forces_the_identifier_reading(spoken):
    assert convert(f"/session start {spoken}.", CFG) == "/session start 1051."
    assert convert(f"/session end {spoken}.", CFG) == "/session end 1051."


def test_a_run_read_in_pieces_survives_a_misheard_trigger():
    # "session end" is heard as "session and" often enough that the trigger
    # cannot be relied on. Two numbers side by side is itself the signal.
    assert convert("Session and ten fifty one.", CFG) == "Session and 1051."


def test_digits_read_out_mid_sentence_are_joined():
    assert convert("the code is one zero five one okay", CFG) == \
        "the code is 1051 okay"


def test_an_identifier_needs_enough_digits_to_be_one():
    # "six two" is two digits and could be a count, so it stays a quantity.
    assert convert("he is six two in socks", CFG) == "he is six two in socks"


# --------------------------------------------------------------------------
# Times.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("let us meet at nine thirty", "let us meet at 9:30"),
    ("the meeting is at ten fifteen tomorrow", "the meeting is at 10:15 tomorrow"),
    ("it starts at nine thirty a.m.", "it starts at 9:30 a.m."),
    ("nine thirty p.m. works", "9:30 p.m. works"),
    ("wait until ten fifteen", "wait until 10:15"),
])
def test_a_cue_makes_it_a_clock(text, expected):
    assert convert(text, CFG) == expected


def test_a_trigger_outranks_a_clock_reading():
    # 1015 is a valid time and a valid session number. The trigger decides.
    assert convert("/session start ten fifteen", CFG) == "/session start 1015"


def test_without_a_cue_it_is_not_a_time():
    assert convert("ten fifteen", CFG) == "1015"


def test_an_impossible_clock_is_not_forced_into_one():
    # 2360 is not a time, so the cue does not make it one.
    assert convert("at twenty three sixty", CFG) == "at 2360"


# --------------------------------------------------------------------------
# Quantities, and the words that must survive untouched.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("there were twenty three people", "there were 23 people"),
    ("we need fifty of them", "we need 50 of them"),
    ("about one hundred twenty three items", "about 123 items"),
    ("twelve is fine but thirteen is not", "twelve is fine but 13 is not"),
    ("it took forty five minutes", "it took 45 minutes"),
])
def test_numbers_above_the_limit_become_digits(text, expected):
    assert convert(text, CFG) == expected


@pytest.mark.parametrize("text", [
    "I have three ideas",
    "ten of them arrived",
    "I said no to all four of them",
    "one of the two options",
    "give me a second",
    "twelve people came",
    "he is one of nine",
])
def test_small_numbers_stay_as_words(text):
    assert convert(text, CFG) == text


def test_the_limit_is_configurable():
    loose = NumberConfig(enabled=True, word_max=0)
    assert convert("I have three ideas", loose) == "I have 3 ideas"


def test_a_comma_separates_two_numbers():
    # "one, two" is a list and not the number 12.
    assert convert("count one, two, three now", CFG) == "count one, two, three now"


def test_off_by_default():
    assert convert("ten fifty one", NumberConfig()) == "ten fifty one"
    assert clean("ten fifty one") == "Ten fifty one"


# --------------------------------------------------------------------------
# The readings on their own.
# --------------------------------------------------------------------------

def test_a_quantity_stops_where_the_number_stops():
    # "ten fifty one" is not sixty one. The parser must refuse to add them.
    assert digits_mod.read_as_quantity(["ten", "fifty", "one"], 0) == (10, 1)


def test_a_scale_word_is_not_an_identifier():
    assert digits_mod.read_as_identifier(["one", "hundred"]) is None


def test_a_clock_needs_a_real_hour_and_minute():
    assert digits_mod.as_clock("930") == "9:30"
    assert digits_mod.as_clock("1015") == "10:15"
    assert digits_mod.as_clock("2360") is None
    assert digits_mod.as_clock("99") is None


# --------------------------------------------------------------------------
# Through the whole deterministic pass.
# --------------------------------------------------------------------------

def test_runs_after_the_dictionary_so_a_repaired_trigger_counts():
    cfg = RuleConfig(dictionary={"Session and": "/session end"},
                     spoken_numbers=True, digit_triggers=["/session end"])
    assert clean("Session and ten fifty one.", cfg) == "/session end 1051."


def test_survives_filler_removal():
    cfg = RuleConfig(spoken_numbers=True, digit_triggers=["/session start"])
    assert clean("um /session start uh ten fifty one", cfg) == "/session start 1051"


def test_hyphenated_numbers_from_the_asr_are_handled():
    # Parakeet writes "fifty-two" with a hyphen.
    cfg = RuleConfig(spoken_numbers=True, digit_triggers=["/session end"])
    assert clean("/session end ten fifty-two", cfg) == "/session end 1052"
