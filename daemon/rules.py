"""Deterministic transcript cleanup.

Everything here is rule-based and reversible in your head: no model, no
randomness, no network. It runs before the LLM polish pass and does the work
that does not need judgement -- and on `--raw` it is the *only* pass, so it has
to leave text you would be happy to send.

Design rule: when a rule could plausibly damage meaning, it is off by default.
A filler word left in is a blemish; a word silently deleted is a bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import digits as digits_mod
import terms as terms_mod

# Non-lexical fillers only. These are never meaningful words in English, so
# deleting them cannot change meaning. Deliberately NOT here: "like", "you
# know", "I mean", "actually", "basically" -- all of them carry meaning often
# enough that removal is a content edit, not a cleanup. They live in
# config.aggressive_fillers for anyone who wants them.
FILLERS = {
    "um", "uh", "umm", "uhh", "uhm", "erm", "eh", "ah", "ahh",
    "mm", "mmm", "hmm", "hm", "er",
}

AGGRESSIVE_FILLERS = {
    "like", "you know", "i mean", "sort of", "kind of", "basically", "actually",
}

# Word pairs where an immediate repeat is real English, so the stutter collapser
# must leave them alone. "I had had lunch", "that that he said", "no no no".
LEGITIMATE_DOUBLES = {
    "had", "that", "no", "very", "really", "so", "well", "now", "long",
    "far", "much", "many", "again", "ha", "bye", "yes", "never",
}

# Spoken commands. Punctuation commands are absent on purpose: Parakeet already
# emits punctuation, so a "period" rule mostly fires on "the period of time".
# These three are unambiguous enough to be safe.
SPOKEN_COMMANDS = [
    (r"\bnew paragraph\b", "\n\n"),
    (r"\bnew line\b", "\n"),
]

SCRATCH_THAT = re.compile(r"\bscratch that\b", re.IGNORECASE)


@dataclass
class RuleConfig:
    strip_fillers: bool = True
    aggressive_fillers: bool = False
    collapse_stutters: bool = True
    spoken_commands: bool = True
    spoken_urls: bool = True
    spoken_emails: bool = False
    dictionary: dict[str, str] = field(default_factory=dict)
    protected_terms: list[str] = field(default_factory=list)
    fuzzy_threshold: float = terms_mod.DEFAULT_THRESHOLD
    spoken_numbers: bool = False
    number_word_max: int = 12
    digit_triggers: list[str] = field(default_factory=list)


def _strip_fillers(text: str, cfg: RuleConfig) -> str:
    words = FILLERS | (AGGRESSIVE_FILLERS if cfg.aggressive_fillers else set())
    # Longest first so "you know" is tried before "you".
    for phrase in sorted(words, key=len, reverse=True):
        pattern = re.compile(
            # The word-boundary lookbehind must sit immediately before the
            # filler, not before the optional comma -- otherwise ", um," never
            # matches, because the character before the comma is a word char.
            r"[,]?\s*(?<![\w'])" + re.escape(phrase) + r"(?![\w'])\s*[,]?\s*",
            re.IGNORECASE,
        )
        text = pattern.sub(" ", text)
    return text


def _collapse_stutters(text: str) -> str:
    """Collapse "the the cat" -> "the cat", and "th- the cat" -> "the cat"."""
    # Dropped false starts: a word fragment ending in a hyphen.
    text = re.sub(r"\b[a-z]{1,4}-\s+", "", text, flags=re.IGNORECASE)

    def repl(match: re.Match[str]) -> str:
        first, second = match.group(1), match.group(3)
        if first.lower() in LEGITIMATE_DOUBLES:
            return match.group(0)
        return second

    # Run twice so triples ("the the the") fully collapse.
    for _ in range(2):
        text = re.sub(r"\b(\w+)(\s+)(\1)\b", lambda m: repl(m), text, flags=re.IGNORECASE)
    return text


def _apply_spoken_commands(text: str) -> str:
    # "scratch that" deletes the clause before it, which is what people mean.
    while True:
        match = SCRATCH_THAT.search(text)
        if not match:
            break
        before = text[: match.start()].rstrip()
        after = text[match.end():]
        # The retracted sentence may already carry its own full stop; step past
        # it so the search below finds the boundary of the sentence BEFORE it.
        if before and before[-1] in ".?!":
            before = before[:-1]
        boundary = max(before.rfind("."), before.rfind("?"), before.rfind("!"))
        # No earlier boundary means the speaker retracted everything so far.
        before = before[: boundary + 1] if boundary >= 0 else ""
        text = (before + " " + after.lstrip(" ,.")).strip()

    for pattern, replacement in SPOKEN_COMMANDS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text
def _apply_dictionary(text: str, dictionary: dict[str, str]) -> tuple[str, list[str]]:
    """Fix proper nouns the ASR model has never seen.

    Keys are matched case-insensitively on word boundaries, longest first so
    "lex cloak app" beats "lex cloak".

    Every key is tried in ONE pass, which is what stops a replacement being
    re-matched by a later key. Substituting in a loop cascaded: with both
    "Sessions start" and "Session start" mapped to "/session start", the first
    produced "/session start" and the second then matched inside that output
    and produced "//session start". A leading slash is not a word character,
    so the boundary the pattern asks for was satisfied.

    Addresses are held out of the substitution. A key that matches a hostname
    used to rewrite the host into its display form, turning a correctly heard
    "lexcloak.com" into "Lex Cloak.com", for the same reason: a full stop also
    satisfies that boundary. The name was right and the address was ruined.

    Also returns the replacement values that actually fired, so the tidy pass
    can put their authored case back. See `_restore_authored_case`.
    """
    text, urls = terms_mod.protect_urls(text)
    if not dictionary:
        return terms_mod.restore_urls(text, urls), []

    # Longest first: regex alternation is leftmost-first, so ordering the
    # branches by length is what makes the longer key win.
    ordered = sorted(dictionary, key=len, reverse=True)
    lookup = {key.lower(): dictionary[key] for key in ordered}
    # The trailing group lets a key match through a possessive and hand it back,
    # so "Soina's" reaches "Soyna's" without a second entry for every name.
    pattern = re.compile(
        r"(?<![\w'])(" + "|".join(re.escape(key) for key in ordered)
        + r")([’']s|[’'])?(?![\w'])",
        re.IGNORECASE,
    )

    used: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        value = lookup[match.group(1).lower()]
        used.append(value)
        return value + (match.group(2) or "")

    text = pattern.sub(substitute, text)
    return terms_mod.restore_urls(text, urls), used


def _restore_authored_case(text: str, values: list[str]) -> str:
    """Put back the case a replacement was written with.

    A replacement is authored text, not prose. If it was written lowercase the
    author meant it lowercase, and sentence capitalisation has no business
    overruling that. This started mattering the moment replacements began
    carrying commands and usernames: `session end` came back as `Session end`,
    which matches no skill, and `sgulhati` came back as `Sgulhati`.

    Runs after the tidy pass rather than holding the spans out of it, because
    the number pass in between rewrites the text and a span recorded earlier
    would no longer point at the same characters.
    """
    for value in values:
        if not value:
            continue
        text = re.sub(
            r"(?<![\w'])" + re.escape(value) + r"(?![\w'])",
            lambda match, replacement=value: replacement,
            text,
            flags=re.IGNORECASE,
        )
    return text


def _strip_command_period(text: str) -> str:
    """A slash command is a line, not a sentence, so it takes no full stop.

    Parakeet adds one to a short utterance about half the time, and on
    "start session ten fifty" it was 6 voices out of 6. The stop lands inside
    the argument, so `/session start 1050.` hands the skill "1050." to parse.

    Only fires on a single command line. Anything carrying a sentence break is
    prose that happens to begin with a slash, and keeps its punctuation.
    """
    stripped = text.rstrip()
    if (stripped.startswith("/") and stripped.endswith(".")
            and not stripped.endswith("..") and ". " not in stripped):
        return stripped[:-1]
    return text


def _tidy(text: str) -> str:
    # Addresses are held out for the same reason as in the dictionary pass:
    # sentence capitalisation would uppercase a host that begins an utterance,
    # and the standalone "i" rule can reach inside one.
    text, urls = terms_mod.protect_urls(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\bi\b", "I", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = text.strip()
    text = _capitalise_sentences(text)
    return terms_mod.restore_urls(text, urls)


def _capitalise_sentences(text: str) -> str:
    """Uppercase the first letter of every sentence and of every line."""
    def upper_after(match: re.Match[str]) -> str:
        return match.group(1) + match.group(2).upper()

    text = re.sub(r"([.!?]\s+|\n+)([a-z])", upper_after, text)
    if text:
        text = text[0].upper() + text[1:]
    return text


def clean(text: str, cfg: RuleConfig | None = None) -> str:
    """Full deterministic pass. Safe to run on already-clean text."""
    cfg = cfg or RuleConfig()
    if not text or not text.strip():
        return ""
    if cfg.strip_fillers:
        text = _strip_fillers(text, cfg)
    if cfg.collapse_stutters:
        text = _collapse_stutters(text)
    if cfg.spoken_commands:
        text = _apply_spoken_commands(text)
    # Terms run before the dictionary so that spoken addresses have already
    # been joined into real ones, which is what lets the dictionary pass hold
    # them out.
    if cfg.protected_terms or cfg.spoken_urls or cfg.spoken_emails:
        text = terms_mod.apply(text, cfg.protected_terms, cfg.fuzzy_threshold,
                               join_urls=cfg.spoken_urls,
                               join_emails=cfg.spoken_emails)
    used: list[str] = []
    if cfg.dictionary:
        text, used = _apply_dictionary(text, cfg.dictionary)
    # Numbers run last of the substituting passes, because a trigger phrase is
    # often something the dictionary just finished repairing.
    if cfg.spoken_numbers:
        text = digits_mod.convert(text, digits_mod.NumberConfig(
            enabled=True, word_max=cfg.number_word_max,
            triggers=cfg.digit_triggers))
    text = _tidy(text)
    if used:
        text = _restore_authored_case(text, used)
    return _strip_command_period(text)
