"""Spoken numbers, written the way you would have typed them.

The speech model already does some of this. Ask it for "session start ten fifty
one" and it returns "Session start 1051", correctly, because the words around
the number told it what the number was for. The failure is that the inference is
fragile: mishear one word of that context and the digits collapse back into
words. Observed from one voice in one session, changing only "start" to "end":

    "session start ten fifty one"  ->  "Session start 1051"
    "session end ten fifty one"    ->  "Session and ten fifty one"

So this pass does not try to be cleverer than the model. It applies a policy the
model cannot know, stated once and applied the same way every time.

The policy has three parts, because a run of number words means three different
things depending on what surrounds it.

**An identifier.** Digits that happen to be adjacent, not a quantity: a session
number, a code, a room. Concatenated with nothing between them. This is the
reading after a configured trigger phrase, for an utterance that is nothing but
a number, and for any run of three or more single digit words, which is a shape
a quantity almost never takes.

**A time.** Only when something says so: a preposition in front, or a meridiem
after. "at nine thirty" is 9:30 and a bare "nine thirty" is 930, because the
session number said fifty times a day should not have to fight the clock for it.

**A quantity.** Everything else, where the only question is whether to spell it.
Numbers up to `word_max` stay words and larger ones become digits, which is the
ordinary convention and, usefully, keeps the carve-out over exactly the words
that are most dangerous to touch: "one", "two", and the rest of the small ones
that appear constantly in prose meaning something other than a count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
}

# Spoken only when reading digits aloud, never a quantity. "oh" in prose is an
# interjection, so it counts as zero in an identifier and nowhere else.
ORAL_ZEROS = {"oh", "o"}

TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}

TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

SCALES = {"hundred": 100, "thousand": 1000}

# A run this long is being read out digit by digit, which a quantity is not.
BARE_DIGIT_RUN = 3

# Shorter than this and an identifier is indistinguishable from a small count.
MIN_IDENTIFIER_DIGITS = 3

# What makes a number a clock reading rather than an identifier.
TIME_CUES = {
    "at", "by", "around", "before", "after", "until", "till", "from",
    "since", "about", "past",
}
MERIDIEMS = {"am", "pm", "oclock", "o'clock"}

# Internal dots are kept so "a.m." survives as one token, but a sentence ending
# full stop is not part of the word before it.
_TOKEN_RE = re.compile(r"[A-Za-z]+(?:\.[A-Za-z]+)*|\d+")

# Number words may only be joined by space or hyphen. A comma or a full stop
# means the speaker separated them and they are not one number.
_JOINABLE_GAP_RE = re.compile(r"^[\s-]*$")


@dataclass
class NumberConfig:
    enabled: bool = False
    word_max: int = 12
    triggers: list[str] = field(default_factory=list)


def _is_number_word(word: str) -> bool:
    lowered = word.lower()
    return (lowered in UNITS or lowered in ORAL_ZEROS or lowered in TEENS
            or lowered in TENS or lowered in SCALES)


# --------------------------------------------------------------------------
# The two readings of a run.
# --------------------------------------------------------------------------

def read_as_identifier(words: list[str]) -> str | None:
    """Digits side by side. "ten fifty one" is 1051, not 61."""
    digits: list[str] = []
    index = 0
    while index < len(words):
        word = words[index].lower()
        if word in TENS:
            value = TENS[word]
            index += 1
            # "fifty one" is one pair. "fifty oh" is not, so oral zeros do not
            # attach here.
            if index < len(words) and words[index].lower() in UNITS \
                    and UNITS[words[index].lower()] != 0:
                value += UNITS[words[index].lower()]
                index += 1
            digits.append(f"{value:02d}")
        elif word in TEENS:
            digits.append(f"{TEENS[word]:02d}")
            index += 1
        elif word in UNITS:
            digits.append(str(UNITS[word]))
            index += 1
        elif word in ORAL_ZEROS:
            digits.append("0")
            index += 1
        else:
            return None  # a scale word is a quantity, not an identifier
    return "".join(digits) or None


def read_as_quantity(words: list[str], start: int) -> tuple[int | None, int]:
    """The longest valid English number starting at `start`.

    Stops where the sequence stops being one number, which is what keeps
    "ten fifty one" from being read as 61. After "ten", "fifty" cannot follow.
    """
    total, current, index, seen = 0, 0, start, False

    while index < len(words):
        word = words[index].lower()
        if word in TENS:
            if current % 100 != 0:
                break
            current += TENS[word]
            index += 1
            seen = True
            if index < len(words) and words[index].lower() in UNITS \
                    and UNITS[words[index].lower()] != 0:
                current += UNITS[words[index].lower()]
                index += 1
            continue
        if word in TEENS:
            if current % 100 != 0:
                break
            current += TEENS[word]
            index += 1
            seen = True
            continue
        if word in UNITS:
            if current % 100 != 0 or UNITS[word] == 0:
                break
            current += UNITS[word]
            index += 1
            seen = True
            continue
        if word == "hundred" and seen and current:
            current *= 100
            index += 1
            continue
        if word == "thousand" and seen and current:
            total += current * 1000
            current = 0
            index += 1
            continue
        break

    if not seen:
        return None, start
    return total + current, index


def as_clock(digits: str) -> str | None:
    """"930" is 9:30. Minutes are the last two digits, the hour is the rest."""
    if not 3 <= len(digits) <= 4:
        return None
    hour, minute = int(digits[:-2]), int(digits[-2:])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour}:{minute:02d}"


# --------------------------------------------------------------------------
# Choosing a reading, which is entirely a question of context.
# --------------------------------------------------------------------------

def _runs(text: str) -> list[list[re.Match[str]]]:
    """Maximal stretches of number words joined by nothing but space or hyphen."""
    tokens = [m for m in _TOKEN_RE.finditer(text)]
    runs: list[list[re.Match[str]]] = []
    current: list[re.Match[str]] = []

    for index, token in enumerate(tokens):
        if not _is_number_word(token.group(0)):
            if current:
                runs.append(current)
                current = []
            continue
        if current:
            gap = text[tokens[index - 1].end():token.start()]
            if not _JOINABLE_GAP_RE.match(gap):
                runs.append(current)
                current = []
        current.append(token)

    if current:
        runs.append(current)
    return runs


def _normalise_phrase(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _trigger_before(text: str, start: int, triggers: list[str]) -> bool:
    if not triggers:
        return False
    before = _normalise_phrase(text[:start].rstrip().rstrip(":,-"))
    return any(before.endswith(_normalise_phrase(t)) for t in triggers if t.strip())


def _word_before(text: str, start: int) -> str:
    matches = list(_TOKEN_RE.finditer(text[:start]))
    return matches[-1].group(0).lower() if matches else ""


def _word_after(text: str, end: int) -> str:
    match = _TOKEN_RE.search(text[end:])
    return match.group(0).lower().replace(".", "") if match else ""


def _is_whole_utterance(text: str, run: list[re.Match[str]]) -> bool:
    """Nothing in the utterance but this number and punctuation."""
    outside = text[:run[0].start()] + text[run[-1].end():]
    return not re.search(r"[A-Za-z0-9]", outside)


def _all_single_digits(words: list[str]) -> bool:
    return all(w.lower() in UNITS or w.lower() in ORAL_ZEROS for w in words)


def _quantity_segments(words: list[str]) -> int:
    """How many separate numbers this run is, read as prose.

    One is a quantity: "twenty three people". More than one means the words do
    not form a single number, which is what a number read out in pieces looks
    like. "ten fifty one" is two, and no English sentence counts that way.
    """
    count, index = 0, 0
    while index < len(words):
        _, end = read_as_quantity(words, index)
        if end == index:
            index += 1
            continue
        count += 1
        index = end
    return count


def _as_quantity_text(text: str, run: list[re.Match[str]], word_max: int) -> str:
    """Spell the small ones, digitise the rest. The default reading."""
    words = [token.group(0) for token in run]
    pieces: list[str] = []
    index = 0

    while index < len(words):
        value, end = read_as_quantity(words, index)
        if value is None:
            pieces.append(text[run[index].start():run[index].end()])
            index += 1
            continue
        source = text[run[index].start():run[end - 1].end()]
        pieces.append(str(value) if value > word_max else source)
        if end < len(words):
            pieces.append(text[run[end - 1].end():run[end].start()])
        index = end

    return "".join(pieces)


def convert(text: str, cfg: NumberConfig) -> str:
    """Apply the policy in the module docstring to every run of number words."""
    if not cfg.enabled or not text:
        return text

    out: list[str] = []
    cursor = 0

    for run in _runs(text):
        start, end = run[0].start(), run[-1].end()
        words = [token.group(0) for token in run]
        digits = read_as_identifier(words)

        replacement: str | None = None

        if digits and _trigger_before(text, start, cfg.triggers):
            # A trigger is the speaker saying what the number is for, so it
            # outranks every other reading including the clock.
            replacement = digits
        else:
            if digits and (_word_before(text, start) in TIME_CUES
                           or _word_after(text, end) in MERIDIEMS):
                # A cue asks for a clock. It does not get to force one: "at
                # twenty three sixty" has a cue and no valid hour, so the
                # reading falls through rather than inventing 23:60.
                replacement = as_clock(digits)
            if replacement is None and digits \
                    and len(digits) >= MIN_IDENTIFIER_DIGITS and (
                        _is_whole_utterance(text, run)
                        or (len(words) >= BARE_DIGIT_RUN
                            and _all_single_digits(words))
                        or _quantity_segments(words) > 1):
                replacement = digits

        out.append(text[cursor:start])
        out.append(replacement if replacement is not None
                   else _as_quantity_text(text, run, cfg.word_max))
        cursor = end

    out.append(text[cursor:])
    return "".join(out)
