"""Pure text helpers: Threads length measurement, chain splitting, link counting.

No I/O, no async, no client state. Everything here is unit-testable in
isolation, which is the point — the splitter and the link counter are where
a subtle bug turns into a rejected or mangled post.
"""

from __future__ import annotations

import re

#: Threads text-post limit. Verified 2026-08-12 against Meta's docs.
THREADS_TEXT_LIMIT = 500

#: Max unique links per text post before the API returns
#: ``THREADS_API__LINK_LIMIT_EXCEEDED``.
THREADS_LINK_LIMIT = 5

# Paragraph break: one or more blank lines.
_PARA_RE = re.compile(r"\n\s*\n+")

# Sentence break: terminal punctuation followed by whitespace. Keeps the
# punctuation with the preceding sentence.
_SENT_RE = re.compile(r"(?<=[.!?…])[ \t]+(?=\S)")

# URL matcher. Deliberately greedy on the path, then trailing punctuation is
# trimmed below so "see https://x.com/a." does not capture the full stop.
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)

_TRAILING_PUNCT = ".,;:!?…"


def threads_length(text: str) -> int:
    """Return the Threads-counted length of ``text``.

    Threads counts emoji as UTF-8 **bytes**, not as single characters, so
    ``len(str)`` under-measures any post containing emoji and lets an
    over-limit post through to a 400. This measures the whole string in UTF-8
    bytes, which is exact for ASCII and never *under*-counts anything else.

    Deliberately conservative for non-ASCII scripts (a CJK character is 3
    UTF-8 bytes and will be counted as 3). That errs toward splitting one
    segment too early, which is harmless; the opposite error is a rejected
    post.

    Example::

        >>> threads_length("hi")
        2
        >>> threads_length("hi \U0001f600")   # emoji is 4 UTF-8 bytes
        7
    """
    return len(text.encode("utf-8"))


def fits(text: str, limit: int = THREADS_TEXT_LIMIT) -> bool:
    """True if ``text`` is within the Threads length limit."""
    return threads_length(text) <= limit


def extract_urls(text: str) -> list[str]:
    """Return every URL in ``text``, in order, with trailing punctuation trimmed.

    Duplicates are preserved; call :func:`count_unique_links` for the count
    that the API actually enforces.

    Example::

        >>> extract_urls("see https://a.example/x. and https://b.example")
        ['https://a.example/x', 'https://b.example']
    """
    found: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        cleaned = raw.rstrip(_TRAILING_PUNCT)
        if cleaned:
            found.append(cleaned)
    return found


def count_unique_links(text: str, link_attachment: str | None = None) -> int:
    """Count links the way the Threads API counts them.

    All unique URLs found in ``text``, plus ``link_attachment`` if it differs
    from every URL already present in the text. Exceeding
    :data:`THREADS_LINK_LIMIT` returns ``THREADS_API__LINK_LIMIT_EXCEEDED``
    from the API, so callers validate with this first.

    Example::

        >>> count_unique_links("a https://x.example b https://x.example",
        ...                    "https://y.example")
        2
    """
    unique = {u.rstrip(_TRAILING_PUNCT) for u in extract_urls(text)}
    if link_attachment:
        unique.add(link_attachment.strip().rstrip(_TRAILING_PUNCT))
    return len(unique)


def _pack(chunks: list[str], joiner: str, limit: int) -> list[str]:
    """Greedily pack ``chunks`` into segments no longer than ``limit``.

    Chunks that individually exceed the limit are emitted alone; the caller
    is responsible for splitting them further at a finer boundary.
    """
    segments: list[str] = []
    current = ""
    for chunk in chunks:
        if not chunk:
            continue
        candidate = f"{current}{joiner}{chunk}" if current else chunk
        if fits(candidate, limit):
            current = candidate
            continue
        if current:
            segments.append(current)
        current = chunk
    if current:
        segments.append(current)
    return segments


def _split_hard(text: str, limit: int) -> list[str]:
    """Last resort: split a single over-limit word at a UTF-8-safe boundary.

    Never splits a multi-byte character in half — it walks characters and
    measures the encoded length, so an emoji stays intact.
    """
    out: list[str] = []
    current = ""
    for ch in text:
        candidate = current + ch
        if threads_length(candidate) > limit and current:
            out.append(current)
            current = ch
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def split_for_chain(text: str, limit: int = THREADS_TEXT_LIMIT) -> list[str]:
    """Split ``text`` into Threads-sized segments for a reply chain.

    Boundary preference, strongest first:

    1. Paragraph boundaries (blank lines).
    2. Sentence boundaries (``.``/``!``/``?``/``…`` followed by whitespace).
    3. Word boundaries (whitespace).
    4. Character boundaries — only when a single word exceeds the limit on its
       own (a very long URL). Never splits a multi-byte character.

    Length is measured with :func:`threads_length` (UTF-8 bytes), so emoji are
    counted the way Threads counts them.

    Returns a list with at least one element. Empty/whitespace-only input
    returns ``[]``.

    Example::

        >>> len(split_for_chain("short post"))
        1
    """
    text = (text or "").strip()
    if not text:
        return []
    if fits(text, limit):
        return [text]

    paragraphs = [p.strip() for p in _PARA_RE.split(text) if p.strip()]
    segments: list[str] = []

    for para in _pack(paragraphs, "\n\n", limit):
        if fits(para, limit):
            segments.append(para)
            continue

        # Paragraph alone is too big -> sentences.
        sentences = [s.strip() for s in _SENT_RE.split(para) if s.strip()]
        for sent_group in _pack(sentences, " ", limit):
            if fits(sent_group, limit):
                segments.append(sent_group)
                continue

            # Sentence alone is too big -> words.
            words = sent_group.split()
            for word_group in _pack(words, " ", limit):
                if fits(word_group, limit):
                    segments.append(word_group)
                    continue
                # Single word too big -> hard character split.
                segments.extend(_split_hard(word_group, limit))

    return segments
