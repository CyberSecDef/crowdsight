"""Split document text into overlapping chunks on semantic boundaries.

Each chunk becomes one LLM extraction call in Step 4, so this module decides
both what the model can see and how much the whole ingestion costs. Two
properties matter more than they appear to:

**Boundaries.** A chunk that ends mid-sentence hands the extractor a fragment,
and "Councillor Jane Doe of the" yields either nothing or an invented entity.
Paragraphs are the preferred unit, sentences the fallback, and a hard cut at a
word boundary the last resort — reached only by a single sentence longer than
the whole chunk size.

**Contiguity.** Every chunk is an exact slice of the source: ``text ==
source[chunk.start:chunk.end]``. Phase 3 Step 5 has to trace each graph node
back to the text that produced it, and offsets that merely approximate the
source make that provenance a guess. Building chunks as spans rather than as
concatenated strings makes the property structural instead of something to
remember.

Overlap is taken as whole trailing sentences, not a raw character count. The
point of overlap is that a mention severed at a boundary appears intact in the
next chunk; a tail that begins mid-clause reproduces the fragment problem it
was meant to solve.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterator, Sequence

from app.config import Config, get_config

logger = logging.getLogger(__name__)

__all__ = ["Chunk", "chunk_text", "iter_paragraph_spans", "iter_sentence_spans"]


@dataclass(frozen=True)
class Chunk:
    """One unit of text, and exactly where it came from."""

    index: int
    text: str
    start: int
    end: int

    def __len__(self) -> int:
        return len(self.text)


Span = tuple[int, int]


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")

# Words that end in a full stop without ending a sentence. Without these,
# "Cllr. Jane Doe" splits into two "sentences" and the name is severed from
# its title — which is exactly the context the extractor needs to classify it.
ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "rev", "hon",
    "sen", "rep", "gov", "gen", "col", "lt", "sgt", "capt", "cllr", "cmdr",
    "co", "inc", "ltd", "llc", "plc", "corp", "dept", "univ", "assn", "bros",
    "etc", "eg", "ie", "vs", "cf", "al", "approx", "est", "fig", "figs",
    "no", "nos", "vol", "vols", "pp", "ed", "eds", "min", "max", "avg",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
})

# A candidate sentence end: terminal punctuation, optional closing quote or
# bracket, then whitespace.
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]’”]*(?=\s)")

# The token immediately before the punctuation.
_TRAILING_WORD = re.compile(r"([A-Za-z][A-Za-z0-9]*)$")


def iter_paragraph_spans(text: str) -> Iterator[Span]:
    """Yield ``(start, end)`` for each non-empty paragraph."""
    position = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        span = _trimmed(text, position, match.start())
        if span:
            yield span
        position = match.end()
    span = _trimmed(text, position, len(text))
    if span:
        yield span


def iter_sentence_spans(text: str, offset: int = 0) -> Iterator[Span]:
    """Yield ``(start, end)`` for each sentence, offsets relative to ``offset``.

    Regex-based, deliberately. A statistical sentence splitter would be another
    model to provision inside a sealed deployment, for a task where a short
    abbreviation list gets the overwhelming majority of cases right.
    """
    start = 0
    for match in _SENTENCE_END.finditer(text):
        if not _is_sentence_end(text, match):
            continue
        end = match.end()
        span = _trimmed(text, start, end, offset)
        if span:
            yield span
        start = end
    span = _trimmed(text, start, len(text), offset)
    if span:
        yield span


def _is_sentence_end(text: str, match: re.Match[str]) -> bool:
    """Decide whether a punctuation run actually terminates a sentence."""
    preceding = text[: match.start()]
    word_match = _TRAILING_WORD.search(preceding)
    if word_match:
        word = word_match.group(1)
        # "Dr." and friends.
        if word.lower() in ABBREVIATIONS:
            return False
        # A single capital is an initial: "J. Smith", "U.S. Senate".
        if len(word) == 1 and word.isupper():
            return False

    # Decimal points and version numbers: "3.5", "v1.2".
    if match.start() > 0 and text[match.start() - 1].isdigit():
        following = text[match.end() : match.end() + 2].lstrip()
        if following[:1].isdigit():
            return False

    # A continuation in lower case is almost always an abbreviation we missed.
    remainder = text[match.end() :].lstrip()
    if remainder[:1].islower():
        return False

    return True


def _trimmed(text: str, start: int, end: int, offset: int = 0) -> Span | None:
    """Shrink a span to exclude surrounding whitespace; None if empty."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start + offset, end + offset) if end > start else None


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


def _split_units(text: str, size: int) -> list[Span]:
    """Break the document into spans that each fit within ``size``.

    Paragraph first, then sentence, then a hard cut at a word boundary. Each
    level is only reached when the level above produced something too large to
    place, so ordinary prose never sees the fallbacks.
    """
    units: list[Span] = []
    for para_start, para_end in iter_paragraph_spans(text):
        if para_end - para_start <= size:
            units.append((para_start, para_end))
            continue

        paragraph = text[para_start:para_end]
        for sent_start, sent_end in iter_sentence_spans(paragraph, offset=para_start):
            if sent_end - sent_start <= size:
                units.append((sent_start, sent_end))
            else:
                units.extend(_hard_cut(text, sent_start, sent_end, size))
    return units


def _hard_cut(text: str, start: int, end: int, size: int) -> list[Span]:
    """Last resort: cut an over-long sentence at word boundaries."""
    spans: list[Span] = []
    position = start
    while end - position > size:
        window_end = position + size
        boundary = text.rfind(" ", position, window_end)
        # No space in a whole chunk-width: an unbroken token, so cut it.
        cut = boundary if boundary > position else window_end
        span = _trimmed(text, position, cut)
        if span:
            spans.append(span)
        position = cut
    span = _trimmed(text, position, end)
    if span:
        spans.append(span)
    return spans


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def chunk_text(
    text: str,
    *,
    size: int | None = None,
    overlap: int | None = None,
    config: Config | None = None,
) -> list[Chunk]:
    """Split ``text`` into overlapping chunks on semantic boundaries.

    Returns chunks in document order. Each is an exact slice of ``text``.
    """
    if size is None or overlap is None:
        config = config or get_config()
        size = size if size is not None else config.CHUNK_SIZE
        overlap = overlap if overlap is not None else config.CHUNK_OVERLAP

    if size < 1:
        raise ValueError("size must be at least 1")
    if overlap < 0:
        raise ValueError("overlap must not be negative")
    if overlap >= size:
        # Config validation already refuses this, but the function is public.
        raise ValueError(
            f"overlap ({overlap}) must be smaller than size ({size}); otherwise "
            f"each chunk starts before the previous one ended and the split "
            f"never advances."
        )

    if not text or not text.strip():
        return []

    units = _split_units(text, size)
    if not units:
        return []

    return _build(text, units, size, overlap)


def _build(text: str, units: Sequence[Span], size: int, overlap: int) -> list[Chunk]:
    """Pack units into chunks, each opening with overlap from its predecessor.

    ``size`` is a hard ceiling on the finished chunk, overlap included. The
    alternative — size counting only new content — means a chunk's real length
    depends on how long the preceding sentence happened to be, and a 200-char
    setting quietly produces 340-char chunks. Anything downstream sizing a
    prompt window against CHUNK_SIZE would be wrong by an amount nobody can
    predict.
    """
    chunks: list[Chunk] = []
    previous: list[int] = []
    previous_start = -1
    index = 0
    total = len(units)

    while index < total:
        carry = _overlap_start(text, units, previous, previous_start, size, overlap)
        taken: list[int] = []

        while index < total:
            span_start = carry if carry is not None else (
                units[taken[0]][0] if taken else units[index][0]
            )
            if units[index][1] - span_start <= size or (not taken and carry is None):
                taken.append(index)
                index += 1
            elif not taken:
                # The carried overlap leaves no room for even one new unit.
                # New content is the point of the chunk; the overlap is not.
                carry = None
            else:
                break

        start = carry if carry is not None else units[taken[0]][0]
        end = units[taken[-1]][1]
        chunks.append(Chunk(index=len(chunks), text=text[start:end], start=start, end=end))
        previous = taken
        previous_start = start

    return chunks


def _overlap_start(
    text: str,
    units: Sequence[Span],
    previous: Sequence[int],
    previous_start: int,
    size: int,
    overlap: int,
) -> int | None:
    """Where a chunk should begin so it repeats the tail of its predecessor.

    Backs up over whole *sentences*, not whole units. When paragraphs fit
    inside the chunk size they become the units, and a paragraph is routinely
    larger than the overlap budget — so a unit-granular search finds nothing
    that fits and overlap silently does nothing at all. Sentences are the
    granularity that was wanted: repeated text stays readable prose, and the
    extractor sees a complete statement rather than a fragment.

    Returns ``None`` when no whole sentence fits the budget, or when
    overlapping would not advance past the previous chunk's own start.
    """
    if overlap <= 0 or not previous:
        return None

    region_start = units[previous[0]][0]
    region_end = units[previous[-1]][1]
    budget = min(overlap, size // 2)

    sentences = list(iter_sentence_spans(text[region_start:region_end], region_start))
    if not sentences:
        return None

    start: int | None = None
    covered = 0
    for sentence_start, sentence_end in reversed(sentences):
        length = sentence_end - sentence_start
        if covered + length > budget:
            break
        start = sentence_start
        covered += length

    if start is None or start <= previous_start:
        return None
    return start
