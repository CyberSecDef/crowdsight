"""Phase 3 Step 1 — parsing PDF, Markdown and plain text.

Everything downstream is built on what the parser returns, so its failures are
quiet and expensive: subtly wrong text becomes a subtly wrong graph, then a
population of agents reacting to something nobody said.
"""

from __future__ import annotations

import pytest

from app.utils.file_parser import (
    EncryptedDocument,
    FileTooLarge,
    UnparseableDocument,
    UnsupportedFileType,
    detect_encoding,
    normalise_markdown,
    normalise_text,
    parse_bytes,
    parse_file,
)

# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def test_single_column_pdf_parses(make_pdf, config):
    doc = parse_bytes(make_pdf(pages=3), "report.pdf", config)
    assert doc.page_count == 3
    assert doc.char_count > 200
    assert doc.text.count("Page") == 3
    assert doc.extension == "pdf"


def test_two_column_pdf_reads_column_by_column(make_pdf, config):
    """Naive extraction splices columns, inventing sentences that never existed."""
    doc = parse_bytes(
        make_pdf(columns=2, title="Annual Housing Report"), "two-col.pdf", config
    )
    left_end = doc.text.find("consultation period")
    right_start = doc.text.find("Meanwhile in Ward Four")
    title = doc.text.find("Annual Housing Report")

    assert left_end > 0 and right_start > 0
    assert left_end < right_start, "left column must finish before the right begins"
    assert 0 <= title < left_end, "a full-width title precedes both columns"


def test_scanned_pdf_is_rejected_by_name(make_pdf, config):
    """An empty graph three stages later reads as a modelling failure."""
    with pytest.raises(UnparseableDocument) as excinfo:
        parse_bytes(make_pdf(pages=3, scanned=True), "scan.pdf", config)
    assert "scanned" in str(excinfo.value)
    assert "OCR" in str(excinfo.value)


def test_encrypted_pdf_is_rejected(make_pdf, config):
    with pytest.raises(EncryptedDocument, match="password"):
        parse_bytes(make_pdf(encrypt=True), "locked.pdf", config)


def test_corrupt_pdf_is_rejected(config):
    with pytest.raises(UnparseableDocument):
        parse_bytes(b"this is not a pdf", "fake.pdf", config)


def test_pdf_metadata_is_returned(make_pdf, config):
    doc = parse_bytes(make_pdf(), "report.pdf", config)
    assert doc.filename == "report.pdf"
    assert doc.byte_size > 0
    assert "report.pdf" in doc.summary()


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

MARKDOWN = """<!-- internal note -->
# Housing Policy Review

See [the draft](https://example.com/draft) and <https://example.com/more>.

## Objections

- Cllr **Jane Doe** objected on _procedural_ grounds
- The `parish_council` was not consulted

> Quote: the process was rushed.

| Name | Role |
|------|------|
| Jane | Chair |

```python
def build(): return 1
```

![diagram](img/chart.png)

---
Bare url https://example.com/x ends here.
"""


@pytest.fixture
def rendered() -> str:
    return normalise_markdown(MARKDOWN)


@pytest.mark.parametrize(
    "fragment",
    ["Housing Policy Review", "the draft", "Jane Doe", "parish_council",
     "Quote: the process was rushed.", "Chair"],
)
def test_markdown_keeps_meaningful_text(rendered, fragment):
    assert fragment in rendered


@pytest.mark.parametrize(
    "noise",
    ["#", "**", "`", "example.com/draft", "example.com/more", "example.com/x",
     "def build", "chart.png", "internal note", "|"],
)
def test_markdown_drops_noise(rendered, noise):
    """A URL or a code identifier would otherwise be offered up as an entity."""
    assert noise not in rendered


def test_markdown_list_markers_removed(rendered):
    assert not any(line.startswith("- ") for line in rendered.splitlines())


def test_markdown_parses_through_the_public_entry_point(config):
    doc = parse_bytes(MARKDOWN.encode(), "notes.md", config)
    assert "Housing Policy Review" in doc.text
    assert doc.page_count is None


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------

FRENCH = (
    "Le conseil municipal a approuvé la politique du logement. "
    "Le conseiller Jean Dupont s'est exprimé en faveur de la mesure. " * 4
)
RUSSIAN = "Совет одобрил жилищную политику. Депутат выступил за меру. " * 4
JAPANESE = "評議会は住宅政策を承認しました。議員は措置に賛成しました。" * 4


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Café Ström — naïve".encode("utf-8"), "Café"),
        ("﻿Café Ström".encode("utf-8"), "Café"),
        ("Café Ström".encode("latin-1"), "Café"),
        ("Café Ström".encode("utf-16"), "Café"),
        (FRENCH.encode("latin-1"), "approuvé"),
        (RUSSIAN.encode("cp1251"), "Совет"),
        (JAPANESE.encode("shift_jis"), "評議会"),
    ],
)
def test_encodings_decode_correctly(raw, expected, config):
    """Guessing wrong turns names into mojibake, which becomes an entity."""
    assert expected in parse_bytes(raw, "notes.txt", config).text


def test_short_latin1_is_not_mistaken_for_cyrillic():
    """Ten bytes carry no evidence; the detector answers anyway."""
    text, _ = detect_encoding("Café Ström".encode("latin-1"))
    assert "Café" in text


def test_genuine_cyrillic_survives_the_fallback():
    text, encoding = detect_encoding(RUSSIAN.encode("cp1251"))
    assert "Совет" in text
    assert encoding == "cp1251"


def test_undecodable_bytes_still_produce_text(config):
    doc = parse_bytes(b"\xff\xfe\x00broken\x81\x82 bytes", "bad.txt", config)
    assert doc.char_count > 0


def test_non_utf8_decode_is_flagged(config):
    doc = parse_bytes(FRENCH.encode("latin-1"), "notes.txt", config)
    assert any("Decoded as" in warning for warning in doc.warnings)


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\r\nb\rc", "a\nb\nc"),
        ("eﬃcient", "efficient"),          # ligature, folded by NFKC
        ("Ja​ne", "Jane"),                 # zero-width space
        ("Jane Doe", "Jane Doe"),          # non-breaking space
        ("co­operate", "cooperate"),       # soft hyphen
        ("a\n\n\n\n\nb", "a\n\nb"),
        ("a   \nb", "a\nb"),
    ],
)
def test_normalisation(raw, expected):
    """Invisible differences make deduplication silently miss."""
    assert normalise_text(raw) == expected


def test_dehyphenation_repairs_line_breaks():
    assert normalise_text("govern-\nment", dehyphenate=True) == "government"


def test_dehyphenation_spares_capitalised_compounds():
    assert normalise_text("Smith-\nJones", dehyphenate=True) == "Smith-\nJones"


def test_dehyphenation_is_off_by_default():
    assert "-\n" in normalise_text("govern-\nment")


# --------------------------------------------------------------------------
# Gate-keeping
# --------------------------------------------------------------------------


@pytest.mark.parametrize("filename", ["notes.exe", "archive.zip", "noext", "a.docx"])
def test_disallowed_extensions_rejected(filename, config):
    with pytest.raises(UnsupportedFileType):
        parse_bytes(b"hello world", filename, config)


def test_oversized_file_rejected(make_config):
    small = make_config(MAX_CONTENT_LENGTH=100)
    with pytest.raises(FileTooLarge, match="over the"):
        parse_bytes(b"x" * 200, "big.txt", small)


@pytest.mark.parametrize("data", [b"", b"   \n\n  "])
def test_empty_input_rejected(data, config):
    with pytest.raises(UnparseableDocument):
        parse_bytes(data, "empty.txt", config)


def test_parse_file_reads_from_disk(tmp_path, config):
    path = tmp_path / "doc.md"
    path.write_text("# Title\n\nSome prose about Jane Doe.\n")
    doc = parse_file(path, config)
    assert "Jane Doe" in doc.text
    assert doc.filename == "doc.md"


def test_parse_file_checks_size_before_reading(tmp_path, make_config):
    path = tmp_path / "big.txt"
    path.write_text("x" * 500)
    with pytest.raises(FileTooLarge):
        parse_file(path, make_config(MAX_CONTENT_LENGTH=100))


def test_missing_file_rejected(tmp_path, config):
    with pytest.raises(UnparseableDocument):
        parse_file(tmp_path / "nope.txt", config)


def test_filename_is_a_basename_not_a_path(tmp_path, config):
    path = tmp_path / "doc.txt"
    path.write_text("Some prose about Jane Doe.")
    assert "/" not in parse_file(path, config).filename
