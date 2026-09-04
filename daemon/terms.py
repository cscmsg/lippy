"""Names the speech model has never heard, matched by shape rather than spelling.

`rules.dictionary` fixes a mis-hearing you have already seen, one spelling at a
time. That works until a name comes back a different way every time it is
spoken, which is what happens with an invented product name: one recording says
"lex clock", the next "lexi cloak", the next runs it together as a single
mangled word. Listing every variant is a losing race.

So a protected term is written once, in the form you want, and any span of
speech close enough to it is snapped onto that form.

The danger is obvious and it is the whole design problem: a matcher loose enough
to catch "lexiclook" is loose enough to rewrite an ordinary English word, and a
silently rewritten word is far worse than a mis-spelled product name. Two guards
keep that from happening.

**A length guard runs before the similarity test.** A candidate whose normalised
length differs from the term's by more than LENGTH_TOLERANCE is never scored at
all. This is what protects single common words: against an eight character term,
a five character word is rejected on length and never gets a chance to clear the
threshold on similarity. Tightening the threshold alone could not do this, because
a short word can score deceptively well against a longer term.

**Terms are opt-in and auditable.** How safe a term is depends on the term. A
distinctive two word name collides with almost nothing, while a short term that
looks like ordinary English collides with a great deal, and no threshold fixes
the second case. `lippyctl terms` reports what a term would capture, so the
answer is measured before it is trusted rather than discovered in pasted text.

URLs are handled here too, because the same names appear in them and the
transformation is the opposite one. In prose the term wants its display form,
"Lex Cloak". In a host it wants its URL form, "lexcloak", run together and
lowercased. A term substitution that does not know the difference turns a
correctly heard "lexcloak.com" into "Lex Cloak.com", which is not a name any
more, it is a broken address.
"""

from __future__ import annotations

import difflib
import pathlib
import re
from dataclasses import dataclass

DEFAULT_THRESHOLD = 0.80

# A candidate is scored only when its normalised length is within this fraction
# of the term's. See the module docstring: this guard, not the threshold, is
# what stops an ordinary short word being rewritten.
LENGTH_TOLERANCE = 0.25

# A host label is relaxed by this much against the prose threshold. Nothing in
# a hostname is ordinary English, so the guard that prose needs is wasted there,
# and it costs real corrections: an observed mis-hearing of a two word name
# scored 0.75, under the prose bar. Measured against the system word list, the
# relaxed bar still captures only three real words for a distinctive term, while
# the prose bar cannot be lowered to meet it because "the cloak" scores 0.75 too.
URL_RELAXATION = 0.10
URL_FLOOR = 0.60

# How many words a match may span. Mis-hearings split a name into more pieces
# than it has, never fewer, so this is the term's own word count plus one.
MAX_EXTRA_WORDS = 1

# Suffixes that make the token before them a hostname. Deliberately a list and
# not a pattern: "X dot Y" is only an address when Y is actually a suffix, and
# a rule that accepted anything would rewrite "the dot product" into a domain.
#
# Two letter country codes that are also ordinary English words are left out on
# purpose ("in", "it", "at", "be", "us", "no", "me", "so", "my", "am"). They
# cost a rare correct join and they buy back the common English phrase.
TLDS = frozenset("""
com org net edu gov mil int io co dev ai app cloud xyz online site tech store
blog shop news live email info biz tv studio design page wiki
uk ca de fr jp au nz eu es nl se ch dk fi pl cz gr pt ie br mx ar cl
""".split())

# Words that cannot begin a hostname. Without this, "the dot com bubble" and
# "a dot org" become addresses. Only closed class words are listed, because
# those are the ones that can never be a host and are common enough to matter.
NOT_HOST = frozenset("""
the a an this that these those it he she we they you i and or but of in on at
to for with from as is are was were be been am my your our their his her its
""".split())

_TLD_ALTERNATION = "|".join(sorted(TLDS, key=len, reverse=True))

# A written address. Covers an explicit scheme, a www host, a bare host with a
# known suffix, and an email address. The trailing path is included so that a
# term never gets substituted inside one.
URL_RE = re.compile(
    r"""(?ix)
    (?:
        https?://[^\s]+
      | www\.[^\s]+
      | [\w.+-]+@[\w-]+(?:\.[\w-]+)+
      | \b[\w-]+(?:\.[\w-]+)*\.(?:""" + _TLD_ALTERNATION + r""")\b(?:/[^\s]*)?
    )"""
)

# "lexcloak dot com", "www dot lexcloak dot app", "lexcloak dot co dot uk".
_SPOKEN_URL_RE = re.compile(r"(?i)\b([A-Za-z0-9-]+)((?:\s+dot\s+[A-Za-z0-9-]+)+)")

_WORD_RE = re.compile(r"[A-Za-z0-9'’-]+")

# An address spoken as "<name> at <host>" only becomes an address when the
# utterance says it is one. "look at example.com" and "the docs are at
# example.com" are ordinary prose, and a rule keying on "at" plus a host would
# rewrite both into something that reads as a valid address and is not. A
# plausible wrong address is worse than visibly unfinished text, so the cue is
# required rather than inferred.
EMAIL_CUES = frozenset("""
email emails e-mail mail mailed mailing cc bcc send sends sent sending
write writes wrote writing reach contact contacted forward forwarded
message messaged invite invited copy copied reply replied
""".split())

# Tokens that cannot be the local part of an address, so "sent it to him at
# example.com" does not become "him@example.com".
NOT_LOCAL = frozenset("""
i me my he him his she her it its we us our they them their you your
the a an this that these those there here is are was were be been being
and or but of to in on at for with from as if so no not now then
""".split())

_SPOKEN_EMAIL_RE = re.compile(r"(?i)(?<![\w'])([A-Za-z0-9][A-Za-z0-9._-]*)\s+at\s+(?=\S)")

# A window may only span whitespace and hyphens. Anything else, a comma or a
# full stop, means the words belong to different thoughts and joining them
# would cross a boundary the speaker put there.
_JOINABLE_GAP_RE = re.compile(r"^[\s-]*$")

_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")


@dataclass(frozen=True)
class Term:
    display: str   # what prose should say, "Lex Cloak"
    key: str       # what a host should say, "lexcloak"
    words: int


def normalise(text: str) -> str:
    """Letters and digits only, lowercased. The form both guards compare."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def prepare(terms: list[str]) -> list[Term]:
    out = []
    for raw in terms:
        display = raw.strip()
        key = normalise(display)
        if key:
            out.append(Term(display, key, len(display.split())))
    return out


def score(candidate: str, term: Term) -> float:
    """Similarity, or zero if the length guard rejects the candidate first."""
    a = normalise(candidate)
    if not a or not term.key:
        return 0.0
    if abs(1 - len(a) / len(term.key)) > LENGTH_TOLERANCE:
        return 0.0
    return difflib.SequenceMatcher(None, a, term.key).ratio()


def url_threshold(threshold: float) -> float:
    """The bar for a host label. See URL_RELAXATION."""
    return max(URL_FLOOR, threshold - URL_RELAXATION)


def best_match(candidate: str, terms: list[Term], threshold: float) -> tuple[Term | None, float]:
    best, best_score = None, 0.0
    for term in terms:
        value = score(candidate, term)
        if value > best_score:
            best, best_score = term, value
    return (best, best_score) if best_score >= threshold else (None, best_score)


# --------------------------------------------------------------------------
# URL spans, for the callers that must not substitute inside one.
# --------------------------------------------------------------------------

def protect_urls(text: str) -> tuple[str, list[str]]:
    """Replace addresses with placeholders no other rule will match."""
    spans: list[str] = []

    def stash(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"\x00{len(spans) - 1}\x00"

    return URL_RE.sub(stash, text), spans


def restore_urls(text: str, spans: list[str]) -> str:
    if not spans:
        return text
    return _PLACEHOLDER_RE.sub(lambda m: spans[int(m.group(1))], text)


# --------------------------------------------------------------------------
# Leftward absorption, shared by the spoken and written URL paths.
# --------------------------------------------------------------------------

def _absorb_left(prefix: str, label: str, terms: list[Term],
                 threshold: float) -> tuple[Term | None, int]:
    """Match `label` against the terms, pulling in words to its left if that helps.

    A two word name dictated into an address arrives split by the suffix rule:
    "Lex Cloak dot app" leaves "Cloak" as the host and "Lex" stranded in the
    prose before it. Scoring the label alone can never recover that, so the
    words immediately to the left are offered to the match as well.

    Returns the term and how many characters of `prefix` it consumed.
    """
    best_term, best_score, best_eaten = None, 0.0, 0

    term, value = best_match(label, terms, threshold)
    if term is not None:
        best_term, best_score, best_eaten = term, value, 0

    tokens = list(_WORD_RE.finditer(prefix))
    limit = max((t.words for t in terms), default=1)
    for count in range(1, min(limit, len(tokens)) + 1):
        start = tokens[-count].start()
        # Every gap between the absorbed words and the label must be joinable,
        # otherwise these words are not part of one name.
        if not _JOINABLE_GAP_RE.match(_WORD_RE.sub(" ", prefix[start:])):
            break
        candidate = prefix[start:] + label
        term, value = best_match(candidate, terms, threshold)
        if term is not None and value > best_score:
            best_term, best_score, best_eaten = term, value, len(prefix) - start

    return best_term, best_eaten


# --------------------------------------------------------------------------
# Spoken addresses: "lexcloak dot com" -> "lexcloak.com".
# --------------------------------------------------------------------------

def join_spoken_urls(text: str, terms: list[Term], threshold: float) -> str:
    out: list[str] = []
    cursor = 0

    for match in _SPOKEN_URL_RE.finditer(text):
        if match.start() < cursor:
            continue
        head = match.group(1)
        tail = re.findall(r"(?i)dot\s+([A-Za-z0-9-]+)", match.group(2))
        if not tail or tail[-1].lower() not in TLDS:
            continue
        if head.lower() in NOT_HOST:
            continue

        prefix = text[cursor:match.start()]
        term, eaten = _absorb_left(prefix, head, terms, url_threshold(threshold))
        host = term.key if term is not None else head.lower()

        out.append(prefix[:len(prefix) - eaten] if eaten else prefix)
        out.append(".".join([host] + [segment.lower() for segment in tail]))
        cursor = match.end()

    out.append(text[cursor:])
    return "".join(out)


# --------------------------------------------------------------------------
# Spoken addresses with a local part: "name at example.com".
# --------------------------------------------------------------------------

def join_spoken_emails(text: str) -> str:
    """Turn "<name> at <host>" into "<name>@<host>", when the utterance says so.

    Runs after `join_spoken_urls`, so "child forensic dot com" is already a host
    by the time this looks for one. Requires an EMAIL_CUE earlier in the
    utterance and refuses a NOT_LOCAL token as the local part. See the comment
    on EMAIL_CUES for why the cue is mandatory.

    The local part is lowercased, which is the convention for an address and
    undoes the capital the speech model puts on a name.
    """
    out: list[str] = []
    cursor = 0

    for match in _SPOKEN_EMAIL_RE.finditer(text):
        if match.start() < cursor:
            continue
        local = match.group(1)
        if local.lower() in NOT_LOCAL:
            continue

        # The cue has to be said before the address, not after it.
        before = {w.lower() for w in _WORD_RE.findall(text[cursor:match.start()])}
        if not (before & EMAIL_CUES):
            continue

        host = URL_RE.match(text, match.end())
        if host is None:
            continue
        span = host.group(0)
        # Only a bare host. A scheme is a link and an "@" is already an address.
        if "@" in span or "//" in span:
            continue

        out.append(text[cursor:match.start()])
        out.append(f"{local.lower()}@{span}")
        cursor = host.end()

    out.append(text[cursor:])
    return "".join(out)


# --------------------------------------------------------------------------
# Written addresses: fix the host, never expand a term inside one.
# --------------------------------------------------------------------------

def _fix_written_url(url: str, prefix: str, terms: list[Term],
                     threshold: float) -> tuple[str, int]:
    """Snap a mis-heard host onto a term's URL form. Returns (url, prefix eaten).

    Only the host is considered. A path segment is not a name the speaker said,
    it is part of an address that either works or does not, and rewriting one
    breaks the link rather than tidying it.
    """
    host_end = url.find("/", url.find("//") + 2 if "//" in url else 0)
    if host_end == -1:
        host_end = len(url)
    labels = [m for m in re.finditer(r"[A-Za-z0-9-]+", url) if m.end() <= host_end]
    eaten = 0
    pieces: list[str] = []
    last = 0

    for index, label in enumerate(labels):
        word = label.group(0)
        if word.lower() in TLDS or word.lower() in {"www", "http", "https"}:
            continue
        # Only the first label can reach back into the prose before the address.
        if index == 0 or (index == 1 and labels[0].group(0).lower() == "www"):
            term, eaten_here = _absorb_left(prefix, word, terms,
                                            url_threshold(threshold))
            if term is not None:
                pieces.append(url[last:label.start()])
                pieces.append(term.key)
                last = label.end()
                eaten = eaten_here
            continue
        term, _ = best_match(word, terms, url_threshold(threshold))
        if term is not None:
            pieces.append(url[last:label.start()])
            pieces.append(term.key)
            last = label.end()

    pieces.append(url[last:])
    return "".join(pieces), eaten


# --------------------------------------------------------------------------
# Prose: slide a window over the words and snap the best match.
# --------------------------------------------------------------------------

def _replace_in_prose(text: str, terms: list[Term], threshold: float) -> str:
    if not text.strip():
        return text

    tokens = list(_WORD_RE.finditer(text))
    if not tokens:
        return text

    widest = min(4, max(t.words for t in terms) + MAX_EXTRA_WORDS)
    out: list[str] = []
    last = 0
    index = 0

    while index < len(tokens):
        best_term, best_score, best_end = None, 0.0, index

        for size in range(1, widest + 1):
            end = index + size - 1
            if end >= len(tokens):
                break
            span = text[tokens[index].start():tokens[end].end()]
            # A window may not cross a boundary the speaker punctuated.
            if size > 1 and not _JOINABLE_GAP_RE.match(_WORD_RE.sub(" ", span)):
                break
            term, value = best_match(span, terms, threshold)
            if term is not None and value > best_score:
                best_term, best_score, best_end = term, value, end

        if best_term is not None:
            start = tokens[index].start()
            out.append(text[last:start])
            out.append(best_term.display)
            last = tokens[best_end].end()
            index = best_end + 1
        else:
            index += 1

    out.append(text[last:])
    return "".join(out)


# --------------------------------------------------------------------------

def apply(text: str, protected: list[str], threshold: float = DEFAULT_THRESHOLD,
          join_urls: bool = True, join_emails: bool = False) -> str:
    """Snap near misses of every protected term onto the form you wrote.

    Joining spoken addresses does not depend on having any terms configured,
    so "google dot com" is still an address on an install that protects nothing.
    """
    terms = prepare(protected)
    if not text or (not terms and not join_urls and not join_emails):
        return text

    if join_urls:
        text = join_spoken_urls(text, terms, threshold)
    if join_emails:
        text = join_spoken_emails(text)
    if not terms:
        return text

    out: list[str] = []
    cursor = 0
    for match in URL_RE.finditer(text):
        if match.start() < cursor:
            continue
        prefix = text[cursor:match.start()]
        url, eaten = _fix_written_url(match.group(0), prefix, terms, threshold)
        kept = prefix[:len(prefix) - eaten] if eaten else prefix
        out.append(_replace_in_prose(kept, terms, threshold))
        out.append(url)
        cursor = match.end()

    out.append(_replace_in_prose(text[cursor:], terms, threshold))
    return "".join(out)


# --------------------------------------------------------------------------
# Auditing, so a term's safety is measured rather than assumed.
# --------------------------------------------------------------------------

WORDLIST_PATHS = ("/usr/share/dict/words", "/usr/dict/words")


def load_wordlist(paths=WORDLIST_PATHS) -> list[str] | None:
    """The system word list, or None where the platform has none."""
    for path in paths:
        candidate = pathlib.Path(path)
        if candidate.exists():
            return [w.strip() for w in candidate.read_text(
                encoding="utf-8", errors="ignore").splitlines() if w.strip()]
    return None


def audit(term: str, words: list[str], threshold: float = DEFAULT_THRESHOLD) -> list[str]:
    """Real words this term would rewrite. Short is safe, long is a warning."""
    prepared = prepare([term])
    if not prepared:
        return []
    single = prepared[0]
    return [w for w in words if score(w, single) >= threshold
            and normalise(w) != single.key]
