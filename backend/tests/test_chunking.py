"""Phase 3 Step 2 — chunk size, overlap and semantic boundaries.

Two properties carry the most weight. Chunks must be exact slices of the source,
because Step 5 traces graph nodes back through their offsets and an approximate
offset makes provenance a guess. And `size` must be a hard ceiling *including*
overlap, or a chunk's real length depends on how long the preceding sentence
happened to be.
"""

from __future__ import annotations

import pytest

from app.utils.chunker import (
    chunk_text,
    iter_paragraph_spans,
    iter_sentence_spans,
)


def sentences(text: str) -> list[str]:
    return [text[a:b] for a, b in iter_sentence_spans(text)]


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("The council met. Cllr. Jane Doe objected. The vote passed.", 3),
        ("J. R. Smith spoke. He was brief.", 2),
        ("Support rose 3.5 points. Then it fell.", 2),
        ("See fig. 4 for detail. The trend is clear.", 2),
        ('She asked "Is it done?" Then she left.', 2),
        ("One! Two? Three.", 3),
    ],
)
def test_sentence_splitting(text, count):
    assert len(sentences(text)) == count


def test_title_stays_attached_to_the_name():
    """A title severed from its name removes the context the extractor needs."""
    assert "Cllr. Jane Doe objected." in sentences(
        "The council met. Cllr. Jane Doe objected. The vote passed."
    )


def test_sentences_carry_no_surrounding_whitespace():
    assert all(s == s.strip() for s in sentences("A one. B two.  C three."))


def test_paragraphs_split_on_blank_lines():
    text = "Para one line one.\nStill para one.\n\nPara two.\n\n\nPara three."
    paragraphs = [text[a:b] for a, b in iter_paragraph_spans(text)]
    assert len(paragraphs) == 3
    assert "\n" in paragraphs[0], "a single newline stays inside a paragraph"


# --------------------------------------------------------------------------
# Contiguity and ordering
# --------------------------------------------------------------------------


def test_every_chunk_is_an_exact_slice(council_text):
    """Step 5 traces nodes back through these offsets."""
    chunks = chunk_text(council_text, size=200, overlap=40)
    assert all(c.text == council_text[c.start : c.end] for c in chunks)


def test_chunks_are_ordered_and_indexed(council_text):
    chunks = chunk_text(council_text, size=200, overlap=40)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(chunks[i].start < chunks[i + 1].start for i in range(len(chunks) - 1))


def test_no_chunk_is_empty(council_text):
    assert all(c.text.strip() for c in chunk_text(council_text, size=150, overlap=30))


def test_no_content_falls_between_chunks(council_text):
    text = (council_text + "\n\n") * 20
    chunks = chunk_text(text, size=1500, overlap=150)
    assert chunks[0].start == 0
    assert chunks[-1].end == len(text.strip())
    assert not [(a.end, b.start) for a, b in zip(chunks, chunks[1:]) if b.start > a.end]


# --------------------------------------------------------------------------
# Size and overlap
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", [120, 200, 400, 1500])
def test_size_is_honoured_without_overlap(council_text, size):
    assert all(len(c) <= size for c in chunk_text(council_text, size=size, overlap=0))


@pytest.mark.parametrize(("size", "overlap"), [(120, 30), (200, 40), (250, 60), (400, 80)])
def test_size_is_a_ceiling_including_overlap(council_text, size, overlap):
    """Otherwise a 200-char setting quietly produces 340-char chunks."""
    chunks = chunk_text(council_text, size=size, overlap=overlap)
    assert max(len(c) for c in chunks) <= size


def test_consecutive_chunks_actually_overlap(council_text):
    chunks = chunk_text(council_text, size=250, overlap=60)
    assert any(b.start < a.end for a, b in zip(chunks, chunks[1:]))


def test_overlap_begins_at_a_sentence_boundary(council_text):
    """A tail starting mid-clause reproduces the problem overlap solves."""
    chunks = chunk_text(council_text, size=250, overlap=60)
    for a, b in zip(chunks, chunks[1:]):
        if b.start < a.end:
            shared = council_text[b.start : a.end]
            assert shared == shared.strip()
            assert shared[0].isupper() or shared[0].isdigit()


def test_zero_overlap_means_chunks_abut(council_text):
    chunks = chunk_text(council_text, size=250, overlap=0)
    assert all(b.start >= a.end for a, b in zip(chunks, chunks[1:]))


def test_chunks_prefer_sentence_boundaries(council_text):
    chunks = chunk_text(council_text, size=250, overlap=0)
    assert all(c.text.rstrip()[-1] in ".!?" for c in chunks)
    assert all(c.text.lstrip()[0].isupper() for c in chunks)


# --------------------------------------------------------------------------
# Degenerate input
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   \n\n  "])
def test_empty_text_yields_no_chunks(text):
    assert chunk_text(text, size=100, overlap=10) == []


def test_document_shorter_than_one_chunk_yields_exactly_one():
    text = "A single short sentence."
    chunks = chunk_text(text, size=1500, overlap=150)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert (chunks[0].start, chunks[0].end) == (0, len(text))


def test_over_long_sentence_is_hard_cut_at_word_boundaries():
    chunks = chunk_text(("word " * 200).strip(), size=100, overlap=0)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    assert all(not c.text.startswith(" ") for c in chunks)


def test_unbroken_token_is_split_and_reassembles():
    giant = "x" * 350
    chunks = chunk_text(giant, size=100, overlap=0)
    assert len(chunks) == 4
    assert "".join(c.text for c in chunks) == giant


@pytest.mark.parametrize(
    "kwargs",
    [
        {"size": 0, "overlap": 0},
        {"size": 100, "overlap": -1},
        {"size": 100, "overlap": 100},
        {"size": 100, "overlap": 200},
    ],
)
def test_invalid_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        chunk_text("some text here", **kwargs)


def test_defaults_come_from_config(council_text, config):
    chunks = chunk_text(council_text, config=config)
    assert chunks
    assert all(len(c) <= config.CHUNK_SIZE for c in chunks)
