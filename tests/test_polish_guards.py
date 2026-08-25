"""Guardrail tests. No model involved -- these are pure functions.

Each case is a way a small instruct model has actually been observed to ruin a
dictated message. The guard has to reject them without a human in the loop,
because by the time the text is pasted it is already in the message box.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))

from polish import _strip_wrapping, validate


def test_accepts_a_faithful_cleanup():
    src = "so um I think we should ship the update on tuesday"
    ok, why = validate(src, "So I think we should ship the update on Tuesday.")
    assert ok, why


def test_rejects_answering_the_question():
    # The single most damaging failure: you dictate a question, it pastes the answer.
    ok, why = validate("what is the capital of france", "Paris.")
    assert not ok, why


def test_rejects_a_same_length_answer():
    # The near-miss: same word count, every content word retained, still an
    # answer rather than a cleanup. Only the interrogative guard catches this.
    ok, why = validate("what is the capital of france",
                       "The capital of France is Paris.")
    assert not ok, "an answer disguised as a cleanup got through"
    assert "question" in why


def test_accepts_a_cleaned_question():
    ok, why = validate("uh whats the capital of france",
                       "What's the capital of France?")
    assert ok, why


def test_statements_are_not_forced_into_questions():
    # "Should" opens a question, but "Should we ship it" cleaned to a statement
    # is only rejected when it really was interrogative -- verify the common
    # declarative case still passes.
    ok, why = validate("we should ship it on tuesday",
                       "We should ship it on Tuesday.")
    assert ok, why


def test_rejects_summarising():
    src = ("the quarterly numbers came in below plan because the enterprise "
           "renewals slipped into next quarter and two deals went to competitors")
    ok, why = validate(src, "Quarterly numbers missed plan.")
    assert not ok


def test_rejects_added_content():
    src = "tell chris the model needs redoing"
    ok, why = validate(
        src,
        "Tell Chris the model needs redoing. I have attached the latest "
        "version for his review and suggested we meet on Thursday to discuss.")
    assert not ok


def test_rejects_meta_commentary():
    ok, why = validate("ship it tuesday", "Here is the corrected text: Ship it Tuesday.")
    assert not ok


def test_rejects_empty_output():
    ok, why = validate("ship it tuesday", "")
    assert not ok


def test_allows_expected_filler_shrinkage():
    # Heavy disfluency legitimately shrinks a lot; the guard must not fire.
    src = "um so uh I think that um we should uh ship it"
    ok, why = validate(src, "So I think we should ship it.")
    assert ok, why


def test_strips_model_preamble_and_fences():
    assert _strip_wrapping("Here's the cleaned text: Ship it.") == "Ship it."
    assert _strip_wrapping("```\nShip it.\n```") == "Ship it."
    assert _strip_wrapping('"Ship it."') == "Ship it."


def test_preserves_legitimate_internal_quotes():
    text = 'He said "no" and left.'
    assert _strip_wrapping(text) == text


def test_contraction_fixes_do_not_count_as_lost_words():
    # ASR drops apostrophes; restoring them is the job, not a content change.
    ok, why = validate("i dont think that wont work and we cant ship it",
                       "I don't think that won't work and we can't ship it.")
    assert ok, why
