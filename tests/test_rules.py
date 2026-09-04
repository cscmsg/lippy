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


def test_cleanup_dial_levels_do_progressively_more():
    """Each step should do strictly more than the one below it."""
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))
    from config import Config

    cfg = Config(dictionary={"lex cloak": "Lex Cloak"})
    spoken = "um so the the report on lex cloak is uh done"

    fillers = clean(spoken, cfg.rule_config("fillers"))
    full = clean(spoken, cfg.rule_config("clean"))

    # "fillers" drops um/uh but leaves the stutter and the proper noun alone.
    assert "um" not in fillers.lower().split()
    assert "the the" in fillers.lower()
    assert "Lex Cloak" not in fillers

    # "clean" additionally collapses the stutter and applies the dictionary.
    assert "the the" not in full.lower()
    assert "Lex Cloak" in full


def test_config_migrates_a_renamed_setting_instead_of_refusing_to_start():
    """An existing config from an older version must still load."""
    import sys, pathlib, json, tempfile
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))
    from config import Config

    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "config.json"
        path.write_text(json.dumps({"polish_enabled": False, "strip_fillers": True}))
        cfg = Config.load(path)
        assert cfg.cleanup_level == "clean"

        path.write_text(json.dumps({"polish_enabled": True}))
        assert Config.load(path).cleanup_level == "polish"


def test_config_ignores_an_unknown_key_rather_than_failing():
    import sys, pathlib, json, tempfile
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))
    from config import Config

    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "config.json"
        path.write_text(json.dumps({"strip_fillers": False, "nonsense_key": 1}))
        assert Config.load(path).strip_fillers is False


# --------------------------------------------------------------------------
# Replacements are authored text, so prose conventions do not get to edit them.
# --------------------------------------------------------------------------

def test_a_lowercase_replacement_stays_lowercase_at_the_start():
    # Sentence capitalisation used to turn this into "Session end", which
    # matches no skill. The slash form escaped only because "/" is not a letter.
    cfg = RuleConfig(dictionary={"session end": "session end"})
    assert clean("Session end.", cfg) == "session end."


def test_a_lowercase_username_replacement_keeps_its_case():
    cfg = RuleConfig(dictionary={"S. Golati": "sgulhati"})
    assert clean("S. Golati", cfg) == "sgulhati"
    assert clean("email S. Golati today", cfg) == "Email sgulhati today"


def test_an_authored_capital_is_still_honoured():
    cfg = RuleConfig(dictionary={"nice f": "NYSCEF"})
    assert clean("nice f filing today", cfg) == "NYSCEF filing today"


def test_case_restoration_does_not_reach_into_a_longer_word():
    cfg = RuleConfig(dictionary={"session end": "session end"})
    assert clean("the session ending was abrupt", cfg) == "The session ending was abrupt"


def test_a_command_line_takes_no_full_stop():
    cfg = RuleConfig(dictionary={"start session": "/session start"})
    assert clean("Start session.", cfg) == "/session start"


def test_a_command_line_keeps_its_argument_intact():
    cfg = RuleConfig(dictionary={"start session": "/session start"},
                     spoken_numbers=True)
    assert clean("Start session ten fifty.", cfg) == "/session start 1050"


def test_prose_that_merely_begins_with_a_slash_keeps_its_punctuation():
    cfg = RuleConfig(dictionary={"start session": "/session start"})
    out = clean("Start session. Then check the logs.", cfg)
    assert out.endswith("logs.")


def test_an_ordinary_sentence_still_gets_its_full_stop_left_alone():
    assert clean("this is a sentence.") == "This is a sentence."


def test_a_replacement_is_not_re_matched_by_a_later_key():
    # Two keys mapping to the same value used to cascade: the longer key
    # produced "/session start", and the shorter one then matched inside that
    # output, because a leading slash satisfies the word boundary.
    cfg = RuleConfig(dictionary={"Sessions start": "/session start",
                                 "Session start": "/session start"})
    assert clean("Sessions start", cfg) == "/session start"
    assert clean("Session start", cfg) == "/session start"


def test_a_replacement_containing_a_key_is_left_alone():
    cfg = RuleConfig(dictionary={"kick off": "session start", "start": "BEGIN"})
    # "session start" contains "start", which a second pass would have eaten.
    assert clean("kick off now", cfg) == "session start now"


def test_the_longest_key_still_wins():
    cfg = RuleConfig(dictionary={"lex cloak": "Lex Cloak",
                                 "lex cloak app": "the Lex Cloak app"})
    assert clean("open lex cloak app now", cfg) == "Open the Lex Cloak app now"
    assert clean("open lex cloak now", cfg) == "Open Lex Cloak now"
