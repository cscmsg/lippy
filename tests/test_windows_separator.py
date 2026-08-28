"""Whether a pasted utterance needs a space in front of it.

The sixteen cases below are `Separator.runSelfTest`'s, carried over as golden
data rather than rewritten. They are the same sixteen the macOS job runs through
the Swift on every pull request, so a divergence between the two implementations
shows up as a disagreement here rather than as a stray space somebody notices in
a document six weeks later.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))

import pytest
import separator


# (preceding, inserting, expected, why) -- Separator.runSelfTest, unchanged.
GOLDEN = [
    (None, "Hello", False, "empty field, or an app that does not publish its text"),
    (".", "How are you", True, "the reported bug: sentence hard against the last full stop"),
    ("d", "How are you", True, "mid-sentence continuation still needs a space"),
    ("?", "Yes", True, "after a question mark"),
    (" ", "Hello", False, "already separated"),
    ("\n", "Hello", False, "start of a line"),
    ("\t", "Hello", False, "after a tab"),
    ("(", "Hello", False, "opening bracket"),
    ('"', "Hello", False, "opening quote"),
    ("-", "Hello", False, "hyphen, likely mid-word"),
    ("/", "Hello", False, "path or URL"),
    ("@", "Hello", False, "handle or address"),
    (".", ", and then", False, "inserted text opens with its own punctuation"),
    (".", " already spaced", False, "inserted text supplies its own space"),
    (".", '"Quoted"', True, "an opening quote is text, not punctuation to hug"),
    (".", "", False, "nothing to insert"),
]


@pytest.mark.parametrize("preceding,inserting,expected,why", GOLDEN)
def test_golden_cases_from_the_swift_self_test(preceding, inserting, expected, why):
    assert separator.needed(preceding, inserting) is expected, why


def test_the_golden_set_is_all_sixteen_cases():
    """A case quietly dropped from this list is a case nobody is checking."""
    assert len(GOLDEN) == 16


# ---- the openers, one at a time ------------------------------------------

@pytest.mark.parametrize("opener", sorted(separator.OPENERS))
def test_no_space_after_any_opener(opener):
    assert separator.needed(opener, "Hello") is False


def test_the_long_dashes_and_curly_quotes_are_openers():
    """Smart quotes are what a word processor has already substituted by the
    time the cursor is sitting after one."""
    assert separator.needed("\u201C", "Hello") is False
    assert separator.needed("\u2018", "Hello") is False
    assert separator.needed("\u2013", "Hello") is False
    assert separator.needed("\u2014", "Hello") is False


def test_a_closing_bracket_is_not_an_opener():
    """The asymmetry is the point. Text after a closing bracket needs its space."""
    assert separator.needed(")", "Hello") is True
    assert separator.needed("]", "Hello") is True


def test_openers_cannot_be_edited_by_a_caller():
    assert isinstance(separator.OPENERS, frozenset)


# ---- what counts as punctuation ------------------------------------------

@pytest.mark.parametrize("opening", [",", ".", "!", "?", ";", ":", "\u2026"])
def test_text_opening_with_its_own_punctuation_supplies_its_own_spacing(opening):
    assert separator.needed("d", opening + " then") is False


def test_text_opening_with_a_bracket_still_needs_a_space():
    """An opening bracket is text arriving, not punctuation hugging what is
    already there, so it takes the space that any other word would."""
    assert separator.needed("d", "(aside)") is True
    assert separator.needed(".", "[note]") is True


def test_a_digit_needs_a_space_like_any_other_character():
    assert separator.needed(".", "2026 was") is True
    assert separator.needed("7", "and then") is True


def test_a_currency_symbol_before_the_cursor_holds_the_space_back():
    """A dollar sign is a symbol rather than punctuation in Unicode, so it is
    only handled because it is listed as an opener."""
    assert separator.needed("$", "40") is False


def test_various_whitespace_before_the_cursor_never_takes_a_space():
    for space in [" ", "\t", "\n", "\r", "\u00A0", "\u2009"]:
        assert separator.needed(space, "Hello") is False


# ---- the fail-closed rule ------------------------------------------------

def test_an_unreadable_field_never_gets_a_space():
    """None is what an application that does not publish its contents returns.
    A missing space is one keystroke to fix. A spurious leading space appears on
    the first dictation into every such application, which is worse."""
    assert separator.needed(None, "Hello") is False
    assert separator.needed(None, "") is False


# ---- prepare -------------------------------------------------------------

def test_prepare_adds_the_space_only_where_needed():
    assert separator.prepare("How are you", ".") == " How are you"
    assert separator.prepare("How are you", " ") == "How are you"
    assert separator.prepare("How are you", None) == "How are you"


def test_prepare_leaves_the_text_itself_alone():
    """No trimming, no capitalising. Those are rules.py's job and doing them
    twice would mean doing them differently."""
    assert separator.prepare("  odd  spacing  ", "x") == "  odd  spacing  "
    assert separator.prepare("", ".") == ""


# ---- malformed input -----------------------------------------------------

def test_a_whole_field_passed_as_the_preceding_character_is_rejected():
    """The Swift signature is Character? and cannot express this mistake.
    Python's can, so it has to be caught rather than silently truncated."""
    with pytest.raises(ValueError):
        separator.needed("Ship it on Tuesday.", "How are you")
    with pytest.raises(ValueError):
        separator.needed("", "How are you")


def test_a_preceding_character_that_is_not_a_string_is_rejected():
    for bad in [46, 0.5, ["."], True]:
        with pytest.raises(TypeError):
            separator.needed(bad, "Hello")


def test_text_that_is_not_a_string_is_rejected():
    for bad in [None, 42, ["Hello"]]:
        with pytest.raises(TypeError):
            separator.needed(".", bad)


def test_a_multi_codepoint_grapheme_is_still_one_character_to_python():
    """An emoji with a modifier is two code points and Python calls that a
    length of two. It reaches here only if a UI Automation read hands back more
    than one, which is a bug on that side, so it is rejected rather than guessed
    at."""
    with pytest.raises(ValueError):
        separator.needed("\U0001F44D\U0001F3FD", "Hello")
