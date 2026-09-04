"""Tests for fuzzy protected terms and address handling.

Same bet as the rest of the deterministic pass, raised: this matcher rewrites
words it was never given, so the negative cases are the ones that matter. Every
"leaves alone" test below is a word an over-eager matcher would eat.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))

import pytest
import terms
from rules import RuleConfig, clean

TERMS = ["Lex Cloak", "Monty Home", "NYSCEF"]


# --------------------------------------------------------------------------
# The thing it is for.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("heard", [
    "Lexiclook",      # observed: run together and mangled
    "lexi cloak",     # observed: split with an extra syllable
    "lex clock",      # observed: real word substituted
    "lex cloke",
    "legs cloak",
    "leks cloak",
    "lex claok",
])
def test_near_misses_snap_onto_the_written_form(heard):
    assert terms.apply(f"we shipped {heard} last week", TERMS) == \
        "we shipped Lex Cloak last week"


def test_an_exact_term_is_left_exactly_alone():
    text = "Lex Cloak is the product"
    assert terms.apply(text, TERMS) == text


def test_several_terms_coexist():
    out = terms.apply("montey home and nice cef and lexiclook", TERMS)
    assert "Monty Home" in out and "Lex Cloak" in out


# --------------------------------------------------------------------------
# The thing it must never do. These carry the design.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "the cloak was black",
    "the clock is broken",
    "she wore a black cloak",
    "I checked the lexicon",
    "let's look at the numbers",
    "next look at the chart",
    "flex the muscle",
    "he drives a Lexus",
    "close the door",
    "my legs hurt",
    "the home team won",
    "welcome home",
])
def test_leaves_ordinary_english_alone(text):
    assert terms.apply(text, TERMS) == text


def test_short_words_are_rejected_on_length_not_similarity():
    # "cloak" scores well against "lexcloak" and is stopped before it is scored.
    import difflib
    term = terms.prepare(["Lex Cloak"])[0]
    assert terms.score("cloak", term) == 0.0
    # Without the guard it would have cleared a 0.75 bar, which is why the
    # guard exists rather than a tighter threshold.
    bare = difflib.SequenceMatcher(None, "cloak", term.key).ratio()
    assert bare > 0.70


def test_a_window_does_not_cross_a_sentence_boundary():
    # "...lex. Cloak..." is two thoughts, not a mangled name.
    text = "I met Lex. Cloak the message before sending."
    assert terms.apply(text, TERMS) == text


def test_a_window_does_not_cross_a_comma():
    text = "for Lex, cloak the file"
    assert terms.apply(text, TERMS) == text


def test_threshold_is_respected():
    # "lex clock" clears the default bar and not a strict one.
    assert "Lex Cloak" in terms.apply("the lex clock update", TERMS, 0.80)
    assert "Lex Cloak" not in terms.apply("the lex clock update", TERMS, 0.99)


def test_no_terms_configured_changes_nothing():
    text = "we shipped lexiclook last week"
    assert terms.apply(text, []) == text


# --------------------------------------------------------------------------
# Spoken addresses.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("spoken,written", [
    ("go to example dot com now", "go to example.com now"),
    ("go to example dot app now", "go to example.app now"),
    ("go to www dot example dot com now", "go to www.example.com now"),
    ("go to example dot co dot uk now", "go to example.co.uk now"),
])
def test_spoken_addresses_are_joined(spoken, written):
    assert terms.apply(spoken, []) == written


def test_a_two_word_name_becomes_one_host():
    # The suffix rule alone would leave "Lex" behind and host only "Cloak".
    assert terms.apply("Go to Lex Cloak dot app for the download", TERMS) == \
        "Go to lexcloak.app for the download"


def test_a_mis_heard_host_is_snapped_to_the_url_form():
    assert terms.apply("Go to Lexclope dot com for the download.", TERMS) == \
        "Go to lexcloak.com for the download."


@pytest.mark.parametrize("text", [
    "the dot com bubble burst",
    "put a dot in the margin",
    "take the dot product of the vectors",
])
def test_dot_is_not_always_an_address(text):
    assert terms.apply(text, TERMS) == text


def test_an_unknown_suffix_is_not_an_address():
    text = "connect the dot puzzle"
    assert terms.apply(text, TERMS) == text


# --------------------------------------------------------------------------
# Written addresses. The regression this feature was built alongside.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "The site is www.lexcloak.com.",
    "download it from lexcloak.app today",
    "email me at courtney@lexcloak.com",
    "check google.com and github.com too",
])
def test_a_correct_address_is_never_expanded_into_a_display_form(text):
    assert terms.apply(text, TERMS) == text


def test_a_mis_heard_written_host_is_repaired():
    assert terms.apply("go to lexclope.com for the download", TERMS) == \
        "go to lexcloak.com for the download"


def test_a_display_form_written_into_a_host_is_collapsed():
    assert terms.apply("Go to LexCloak.com for the download.", TERMS) == \
        "Go to lexcloak.com for the download."


# --------------------------------------------------------------------------
# The dictionary pass, which had the bug this holds shut.
# --------------------------------------------------------------------------

def test_dictionary_no_longer_rewrites_inside_an_address():
    # A full stop satisfies the word boundary, so "lexcloak" used to match
    # inside the host and produce "www.Lex Cloak.com".
    cfg = RuleConfig(dictionary={"lexcloak": "Lex Cloak"})
    assert clean("The site is www.lexcloak.com.", cfg) == \
        "The site is www.lexcloak.com."


def test_dictionary_still_fixes_the_same_word_in_prose():
    cfg = RuleConfig(dictionary={"lexcloak": "Lex Cloak"})
    assert clean("lexcloak is the product", cfg) == "Lex Cloak is the product"


def test_a_leading_host_is_not_sentence_capitalised():
    assert clean("lexcloak.com is the download page") == \
        "lexcloak.com is the download page"


def test_protected_terms_run_through_the_full_clean_pass():
    cfg = RuleConfig(protected_terms=["Lex Cloak"])
    assert clean("um so lexiclook is uh ready", cfg) == "So Lex Cloak is ready"


def test_terms_are_off_unless_configured():
    assert clean("lexiclook is ready") == "Lexiclook is ready"


def test_fillers_level_does_not_run_terms_or_addresses():
    import config as config_mod
    cfg = config_mod.Config(protected_terms=["Lex Cloak"])
    rc = cfg.rule_config("fillers")
    assert rc.protected_terms == []
    assert rc.spoken_urls is False


# --------------------------------------------------------------------------
# Auditing.
# --------------------------------------------------------------------------

def test_audit_reports_real_word_collisions():
    words = ["paddle", "addle", "peddle", "cathedral", "xylophone"]
    hits = terms.audit("Paddle", words)
    assert "addle" in hits
    assert "cathedral" not in hits
    # The term's own spelling is not a collision with itself.
    assert "paddle" not in hits


def test_audit_of_a_distinctive_term_is_quiet():
    words = ["cathedral", "xylophone", "monument", "hospital"]
    assert terms.audit("NYSCEF", words) == []


def test_a_missing_word_list_is_reported_as_missing():
    assert terms.load_wordlist(paths=("/nonexistent/words",)) is None


def test_host_threshold_is_looser_but_floored():
    assert terms.url_threshold(0.80) == pytest.approx(0.70)
    assert terms.url_threshold(0.60) == pytest.approx(terms.URL_FLOOR)


def test_a_path_segment_is_never_rewritten():
    # Rewriting a path breaks the link rather than tidying a name.
    text = "see https://example.com/lexclope/docs for details"
    assert terms.apply(text, TERMS) == text


def test_the_host_is_repaired_while_its_path_is_left_alone():
    assert terms.apply("see https://lexclope.com/lexclope for details", TERMS) == \
        "see https://lexcloak.com/lexclope for details"


def test_addresses_can_be_turned_off():
    text = "go to example dot com now"
    assert terms.apply(text, [], join_urls=False) == text
    cfg = RuleConfig(spoken_urls=False)
    assert "example.com" not in clean(text, cfg)


# --------------------------------------------------------------------------
# Spoken addresses with a local part. The cue is what makes this safe.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("spoken,expected", [
    ("send it to alice at example.com",
     "send it to alice@example.com"),
    ("email alice at example dot com",
     "email alice@example.com"),
    ("cc alice at example.com please",
     "cc alice@example.com please"),
    ("forward it to alice at example.com",
     "forward it to alice@example.com"),
])
def test_a_cue_makes_it_an_address(spoken, expected):
    assert terms.apply(spoken, [], join_emails=True) == expected


@pytest.mark.parametrize("text", [
    "look at example.com for the download",
    "the docs are at example.com",
    "I saw it at example.com",
    "meet me at the office",
    "go to example.com and send it",          # cue comes after, not before
    "email me the link at some point",        # no host
])
def test_without_a_cue_and_a_host_it_stays_prose(text):
    assert terms.apply(text, [], join_emails=True) == text


@pytest.mark.parametrize("text", [
    "send it to him at example.com",
    "send it to them at example.com",
    "send the file, it is at example.com",
])
def test_a_pronoun_is_never_a_local_part(text):
    assert terms.apply(text, [], join_emails=True) == text


def test_the_local_part_is_lowercased():
    # The speech model capitalises a name; an address does not want that.
    assert terms.apply("email Alice at example.com", [], join_emails=True) == \
        "email alice@example.com"


def test_an_existing_address_is_left_alone():
    text = "send it to alice@example.com now"
    assert terms.apply(text, [], join_emails=True) == text


def test_a_link_is_not_turned_into_an_address():
    text = "send it to https://example.com/docs now"
    assert terms.apply(text, [], join_emails=True) == text


def test_off_by_default():
    text = "send it to alice at example.com"
    assert terms.apply(text, []) == text
    assert clean(text) == "Send it to alice at example.com"


def test_runs_through_the_full_clean_pass():
    cfg = RuleConfig(spoken_emails=True)
    assert clean("send it to alice at example dot com", cfg) == \
        "Send it to alice@example.com"
