"""Whether inserted text needs a space in front of it.

Ported from `needed(after:inserting:)` in
`app/Sources/Lippy/Separator.swift`, which is pure and ports as written. The
reading of the character before the cursor does not port, because that is an
accessibility call on one platform and a UI Automation call on the other, and
neither belongs in a decision.

Dictation arrives one utterance at a time and is pasted at the cursor, which
knows nothing about what is already there. Two sentences in a row therefore run
together: "Ship it on Tuesday.The bug is fixed."

Always prepending a space is not the fix. It leaves a stray space in every empty
field, at the start of every line, and after every opening bracket. The decision
has to be made from what actually precedes the insertion point.
"""

from __future__ import annotations

import unicodedata

# Characters after which a space would be wrong. Written as escapes, the way
# the Swift is, so that a reader can tell the curly quotes and the two long
# dashes apart on any screen.
OPENERS = frozenset(
    [
        "(", "[", "{", "<",
        '"', "'", "\u201C", "\u2018",
        "/", "-", "\u2013", "\u2014",
        "@", "#", "$",
    ]
)


def _is_punctuation(character: str) -> bool:
    # Unicode general category P, which is what Swift's isPunctuation reports.
    # Note what this excludes: "$" is a currency symbol and "<" is a maths
    # symbol, so neither is punctuation here. Both are in OPENERS anyway, which
    # is the only reason that distinction never surfaces.
    return unicodedata.category(character).startswith("P")


def needed(after: str | None, inserting: str) -> bool:
    """True when a leading space should be added to `inserting`.

    `after` is the single character immediately before the cursor, or None when
    the field is empty, nothing has focus, or the application does not publish
    its contents. A string of any other length is a caller that has passed the
    whole field, which Swift's type signature made impossible and Python's does
    not.
    """
    if not isinstance(inserting, str):
        raise TypeError(f"inserting must be a str, got {inserting!r}")
    if after is None:
        return False
    if not isinstance(after, str):
        raise TypeError(f"after must be a single character or None, got {after!r}")
    if len(after) != 1:
        raise ValueError(
            f"after must be exactly one character or None, got {after!r}")

    if not inserting:
        return False
    first = inserting[0]

    # Already separated, or at the start of a line.
    if after.isspace():
        return False
    # A space after an opening bracket or quote is wrong.
    if after in OPENERS:
        return False
    # Text that opens with its own punctuation supplies its own spacing.
    if first.isspace():
        return False
    if _is_punctuation(first) and first not in OPENERS:
        return False

    return True


def prepare(text: str, preceding: str | None) -> str:
    """The text to paste, with a leading space when one is needed.

    Fails closed. When the focused application does not publish its contents,
    `preceding` is None and no space is added. A missing space between two
    utterances is a visible nuisance the user fixes in one keystroke. A spurious
    leading space appears on the *first* dictation into every such application,
    which is worse.
    """
    return " " + text if needed(preceding, text) else text
