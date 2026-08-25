"""Tests for the deterministic pass.

The negative cases matter more than the positive ones here: the whole design
bet is that this pass never eats a real word.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))

import pytest
from rules import RuleConfig, clean


@pytest.mark.parametrize("raw,expected", [
    ("um so I think uh we should ship it", "So I think we should ship it"),
    ("hmm let me think", "Let me think"),
    ("er the the report is done", "The report is done"),
])
def test_removes_fillers_and_stutters(raw, expected):
    assert clean(raw) == expected


@pytest.mark.parametrize("raw", [
    "I had had enough of it",
    "He said that that was wrong",
    "No no, leave it alone",
    "It was very very close",
])
def test_leaves_legitimate_doubles_alone(raw):
    # These read as stutters to a naive collapser and are not.
    assert clean(raw) == raw


def test_does_not_eat_meaningful_words_by_default():
    # "like", "actually" and "I mean" are content until told otherwise.
    raw = "I actually like the way you mean it"
    assert clean(raw) == raw


def test_aggressive_fillers_are_opt_in():
    cfg = RuleConfig(aggressive_fillers=True)
    assert clean("it was like basically fine", cfg) == "It was fine"


def test_false_start_fragments_dropped():
    assert clean("the rep- the report is late") == "The report is late"


def test_new_paragraph_command():
    out = clean("first thought new paragraph second thought")
    assert out == "First thought\n\nSecond thought"


def test_scratch_that_deletes_previous_sentence():
    out = clean("The meeting is Tuesday. Scratch that. The meeting is Wednesday.")
    assert "Tuesday" not in out
    assert "Wednesday" in out


def test_dictionary_fixes_proper_nouns():
    cfg = RuleConfig(dictionary={"lex cloak": "Lex Cloak", "nice f": "NYSCEF"})
    out = clean("I uploaded it to lex cloak from nice f", cfg)
    assert out == "I uploaded it to Lex Cloak from NYSCEF"


def test_dictionary_respects_word_boundaries():
    cfg = RuleConfig(dictionary={"ai": "AI"})
    # Must not turn "said" into "sAId" or "maintain" into "maintAIn".
    assert clean("I said we maintain it", cfg) == "I said we maintain it"


def test_standalone_i_is_capitalised():
    assert clean("i think i agree") == "I think I agree"


def test_empty_input_is_empty_output():
    assert clean("") == ""
    assert clean("   ") == ""


def test_clean_text_is_unchanged():
    # Idempotence: running the pass on its own output must be a no-op.
    text = "The quarterly report is finished and the numbers hold up."
    assert clean(text) == text
    assert clean(clean(text)) == clean(text)


def test_comma_delimited_fillers_do_not_strand_commas():
    # "and, um, the" must become "and the", not "and, the".
    assert clean("the bug is fixed and, um, the release passed") == \
        "The bug is fixed and the release passed"


def test_sentences_are_recapitalised_after_filler_removal():
    # Removing a leading filler exposes a new sentence start.
    out = clean("We ship Tuesday. uh, because the bug is fixed")
    assert out == "We ship Tuesday. Because the bug is fixed"


def test_lines_are_capitalised_after_new_paragraph():
    out = clean("first thought new paragraph second thought")
    assert out == "First thought\n\nSecond thought"
