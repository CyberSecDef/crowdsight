"""Turn an uploaded file into normalised plain text plus metadata.

Everything downstream — chunking, entity extraction, the whole graph — is built
on whatever this module returns, so its failures are expensive and quiet. A
document that yields subtly wrong text produces a subtly wrong graph, then a
population of agents reacting to something that was never said. The bias here
is therefore towards refusing input we cannot read, loudly, at upload time.

Three formats, three different problems:

* **PDF** is positioned glyphs, not text. Reading a two-column page in naive
  document order splices sentences across the gutter, inventing text that
  exists nowhere in the source. Blocks are ordered by layout instead.
* **Markdown** is prose wrapped in syntax. Headings carry real meaning and are
  kept; URLs, code fences and image references are noise that extraction will
  otherwise offer up as "entities".
* **Plain text** is bytes of unknown encoding. Guessing wrong turns names into
  mojibake, which then becomes an entity in its own right.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from app.config import Config, get_config

logger = logging.getLogger(__name__)

try:  # PyMuPDF renamed its import in 1.24.3; both names ship for now.
    import pymupdf  # type: ignore
except ImportError:  # pragma: no cover - older PyMuPDF
    import fitz as pymupdf  # type: ignore

__all__ = [
    "EncryptedDocument",
    "FileParseError",
    "FileTooLarge",
    "ParsedDocument",
    "UnparseableDocument",
    "UnsupportedFileType",
    "normalise_markdown",
    "normalise_text",
    "parse_bytes",
    "parse_file",
]


class FileParseError(ValueError):
    """Base class for every rejection this module makes."""


class UnsupportedFileType(FileParseError):
    """The extension is not in the configured allowlist."""


class FileTooLarge(FileParseError):
    """The file exceeds MAX_CONTENT_LENGTH."""


class EncryptedDocument(FileParseError):
    """The PDF is password-protected."""


class UnparseableDocument(FileParseError):
    """The file was read but yielded no usable text."""


@dataclass(frozen=True)
class ParsedDocument:
    """Normalised text and everything known about where it came from."""

    text: str
    filename: str
    extension: str
    byte_size: int
    char_count: int
    page_count: int | None = None
    encoding: str | None = None
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        pages = f", {self.page_count} page(s)" if self.page_count else ""
        return (
            f"{self.filename}: {self.char_count:,} characters{pages}, "
            f"{self.byte_size:,} bytes"
        )


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

# Zero-width and formatting characters that survive copy-paste and PDF
# extraction. They are invisible, so a name containing one silently fails to
# match the same name without it during deduplication.
_INVISIBLE = dict.fromkeys(
    [0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00A0], None
)
_INVISIBLE[0x00A0] = " "  # non-breaking space becomes an ordinary one

_LINE_ENDINGS = re.compile(r"\r\n?")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_EXCESS_BLANKS = re.compile(r"\n{3,}")
# Joins a lowercase fragment to a lowercase continuation. This repairs
# "govern-\nment", and also — unavoidably — collapses a genuine compound that
# happens to break at its own hyphen, turning "Mayor-\nelect" into
# "Mayorelect". Telling the two apart needs a lexicon we do not have, and
# typographic hyphenation is far and away the commoner case in a PDF. Capital
# letters are excluded, which spares "Smith-\nJones".
_LINE_BREAK_HYPHEN = re.compile(r"([a-z])-\n([a-z])")


def normalise_text(raw: str, *, dehyphenate: bool = False) -> str:
    """Canonicalise whitespace, invisible characters and Unicode form.

    NFKC is deliberate: PDFs are full of ligatures (``ﬁ``, ``ﬄ``) and
    typographic variants that are visually identical to their plain
    equivalents but compare as different strings. Entity deduplication
    compares strings, so leaving them distinct means the same organisation
    appears twice in the graph.
    """
    text = _LINE_ENDINGS.sub("\n", raw)
    text = text.translate(_INVISIBLE)
    text = unicodedata.normalize("NFKC", text)
    if dehyphenate:
        text = _LINE_BREAK_HYPHEN.sub(r"\1\2", text)
    text = _TRAILING_SPACE.sub("", text)
    text = _EXCESS_BLANKS.sub("\n\n", text)
    return text.strip()


# --- Markdown -------------------------------------------------------------

_MD_FENCED_CODE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", re.DOTALL | re.MULTILINE)
_MD_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_INLINE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_MD_LINK_DEF = re.compile(r"^[ \t]*\[[^\]]+\]:[ \t]*\S+.*$", re.MULTILINE)
_MD_AUTOLINK = re.compile(r"<(https?://[^>]+)>")
_MD_BARE_URL = re.compile(r"\bhttps?://\S+")
_MD_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]*", re.MULTILINE)
_MD_SETEXT = re.compile(r"^[ \t]*(=+|-{2,})[ \t]*$", re.MULTILINE)
_MD_RULE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.MULTILINE)
_MD_BLOCKQUOTE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_MD_LIST_MARKER = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+", re.MULTILINE)
_MD_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3})(\S.*?\S|\S)\1", re.DOTALL)
_MD_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_MD_TABLE_DIVIDER = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$", re.MULTILINE)
_MD_PIPE = re.compile(r"[ \t]*\|[ \t]*")


def normalise_markdown(raw: str) -> str:
    """Reduce Markdown to prose, keeping the text that carries meaning.

    Headings and list items survive as sentences; link *labels* survive while
    their URLs do not. Code fences go entirely — a Python snippet in an
    incident report contributes identifiers, not entities, and the extractor
    will happily propose ``self`` as a person.
    """
    text = _MD_FENCED_CODE.sub("", raw)
    text = _MD_HTML_COMMENT.sub("", text)
    text = _MD_LINK_DEF.sub("", text)
    text = _MD_IMAGE.sub("", text)
    text = _MD_INLINE_LINK.sub(r"\1", text)
    text = _MD_REF_LINK.sub(r"\1", text)
    text = _MD_AUTOLINK.sub("", text)
    text = _MD_BARE_URL.sub("", text)
    text = _MD_TABLE_DIVIDER.sub("", text)
    text = _MD_RULE.sub("", text)
    text = _MD_SETEXT.sub("", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BLOCKQUOTE.sub("", text)
    text = _MD_LIST_MARKER.sub("", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    text = _MD_EMPHASIS.sub(r"\2", text)
    text = _MD_HTML_TAG.sub("", text)
    text = _MD_PIPE.sub(" ", text)
    return normalise_text(text)


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------


# Encodings a document in this system might plausibly be in: Western European,
# Cyrillic, and the common CJK families. Deliberately not "every codepage
# Python knows". Statistical detection over the full space picks absurd
# answers on short inputs — a 10-byte Latin-1 string was confidently reported
# as cp1006, an Arabic codepage, yielding mojibake. Narrowing the candidates
# trades exotic-encoding support, which this system does not need, for
# reliability on the short inputs it will actually see.
CANDIDATE_ENCODINGS = (
    "utf_8", "cp1252", "latin_1", "iso8859_15", "mac_roman",
    "cp1251", "koi8_r", "utf_16", "utf_32",
    "gb18030", "big5", "shift_jis", "euc_jp", "euc_kr",
)

# Two independent reasons to trust a statistical guess, because neither alone
# is sufficient.
#
# Coherence scores how much the decoding looks like real language, but only for
# alphabetic scripts: genuine French in cp1252 scores 0.62 and Russian in
# cp1251 scores 0.33, while correctly-detected Japanese in shift_jis scores
# 0.000 — the metric simply does not apply to CJK.
#
# Length is the other signal. Ten bytes of Latin-1 were confidently reported as
# Cyrillic, because ten bytes contain no evidence at all. Real documents are
# kilobytes; only a fragment lands under this bound.
MIN_COHERENCE = 0.1
MIN_DETECTION_BYTES = 128


def detect_encoding(data: bytes) -> tuple[str, str | None]:
    """Return ``(decoded_text, encoding_name)``.

    Order matters. A BOM is definitive. Failing that, a strict UTF-8 decode is
    tried first: it is what most files actually are, and it either succeeds
    exactly or fails cleanly, which beats any guess. Only then does statistical
    detection run, restricted to :data:`CANDIDATE_ENCODINGS`. cp1252 is the
    last structured attempt before giving up, being the commonest legacy
    Western encoding and a superset of Latin-1 over the printable range.

    The final fallback still produces text rather than raising: a few
    substituted characters in a long document is recoverable, and refusing the
    upload outright is not obviously better.
    """
    if not data:
        return "", None

    for encoding in _bom_encoding(data):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:  # pragma: no cover - BOM implies decodable
            break

    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data, cp_isolation=list(CANDIDATE_ENCODINGS)).best()
        if best is not None and best.encoding:
            trustworthy = (
                best.coherence >= MIN_COHERENCE or len(data) >= MIN_DETECTION_BYTES
            )
            if trustworthy:
                return str(best), best.encoding
            logger.debug(
                "Ignoring detection %s for %d bytes (coherence %.3f): too little "
                "evidence to judge",
                best.encoding, len(data), best.coherence,
            )
    except Exception:  # noqa: BLE001 - detection is best-effort by nature
        logger.debug("charset-normalizer failed; falling back to chardet")

    try:
        import chardet

        guess = chardet.detect(data)
        encoding = guess.get("encoding")
        if encoding and (guess.get("confidence") or 0) >= 0.7:
            return data.decode(encoding, errors="replace"), encoding
    except Exception:  # noqa: BLE001
        logger.debug("chardet failed; falling back to cp1252")

    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8"


def _bom_encoding(data: bytes) -> list[str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return ["utf-8-sig"]
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return ["utf-32"]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return ["utf-16"]
    return []


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

# A page yielding fewer characters than this, on average, has no text layer.
MIN_CHARS_PER_PAGE = 25

# Fraction of page width a block must span to count as full-width rather than
# as belonging to a column.
_FULL_WIDTH_RATIO = 0.65


def _order_blocks(blocks: Sequence[Any], page_width: float) -> list[str]:
    """Order text blocks so a multi-column page reads column by column.

    Full-width blocks — titles, section headers, footers — act as separators.
    Everything between two of them is one region, and a region is emitted left
    column first, then right. Sorting purely by vertical position instead
    would interleave the columns line by line and produce sentences that never
    existed in the document.
    """
    if not blocks:
        return []

    midpoint = page_width / 2
    ordered: list[str] = []
    region: list[Any] = []

    def flush() -> None:
        if not region:
            return
        left = [b for b in region if b[2] <= midpoint]
        right = [b for b in region if b[0] >= midpoint]
        straddling = [b for b in region if b not in left and b not in right]
        for group in (left, right, straddling):
            for block in sorted(group, key=lambda b: (b[1], b[0])):
                ordered.append(block[4])
        region.clear()

    for block in sorted(blocks, key=lambda b: (b[1], b[0])):
        width = block[2] - block[0]
        if width >= page_width * _FULL_WIDTH_RATIO:
            flush()
            ordered.append(block[4])
        else:
            region.append(block)
    flush()
    return ordered


def _parse_pdf(data: bytes, filename: str) -> tuple[str, int, dict[str, Any]]:
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - any failure here is a bad file
        raise UnparseableDocument(
            f"{filename} could not be opened as a PDF: {exc}"
        ) from exc

    with document:
        if document.needs_pass:
            raise EncryptedDocument(
                f"{filename} is password-protected. Decrypt it before uploading; "
                f"CrowdSight will not prompt for a password."
            )

        page_count = document.page_count
        pages: list[str] = []
        for page in document:
            blocks = [b for b in page.get_text("blocks") if len(b) > 6 and b[6] == 0]
            pages.append("\n".join(_order_blocks(blocks, page.rect.width)))

        metadata = {
            key: value
            for key, value in (document.metadata or {}).items()
            if key in {"title", "author", "subject", "creationDate"} and value
        }

    text = normalise_text("\n\n".join(pages), dehyphenate=True)

    if page_count and len(text) < MIN_CHARS_PER_PAGE * page_count:
        raise UnparseableDocument(
            f"{filename} has {page_count} page(s) but yielded only {len(text)} "
            f"characters of text. This is almost certainly a scanned or "
            f"image-only PDF with no text layer. OCR it before uploading — "
            f"CrowdSight does not perform OCR, and proceeding would build an "
            f"empty knowledge graph."
        )

    return text, page_count, metadata


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def _extension_of(filename: str) -> str:
    return Path(filename).suffix.lstrip(".").lower()


def _validate(filename: str, byte_size: int, config: Config) -> str:
    extension = _extension_of(filename)
    if extension not in config.ALLOWED_EXTENSIONS:
        raise UnsupportedFileType(
            f"{filename!r} has extension {extension or '(none)'!r}, which is not "
            f"allowed. Permitted: {', '.join(sorted(config.ALLOWED_EXTENSIONS))}."
        )
    if byte_size > config.MAX_CONTENT_LENGTH:
        raise FileTooLarge(
            f"{filename!r} is {byte_size:,} bytes, over the "
            f"{config.MAX_CONTENT_LENGTH:,} byte limit."
        )
    if byte_size == 0:
        raise UnparseableDocument(f"{filename!r} is empty.")
    return extension


def parse_bytes(
    data: bytes, filename: str, config: Config | None = None
) -> ParsedDocument:
    """Parse an in-memory upload."""
    config = config or get_config()
    extension = _validate(filename, len(data), config)

    warnings: list[str] = []
    page_count: int | None = None
    encoding: str | None = None
    metadata: dict[str, Any] = {}

    if extension == "pdf":
        text, page_count, metadata = _parse_pdf(data, filename)
    else:
        decoded, encoding = detect_encoding(data)
        if encoding and encoding.lower().replace("_", "-") not in {"utf-8", "utf-8-sig", "ascii"}:
            warnings.append(
                f"Decoded as {encoding}; characters may have been substituted "
                f"if that guess is wrong."
            )
        if extension in {"md", "markdown"}:
            text = normalise_markdown(decoded)
        else:
            text = normalise_text(decoded)

    if not text.strip():
        raise UnparseableDocument(
            f"{filename!r} yielded no text after normalisation."
        )

    return ParsedDocument(
        text=text,
        filename=Path(filename).name,
        extension=extension,
        byte_size=len(data),
        char_count=len(text),
        page_count=page_count,
        encoding=encoding,
        warnings=tuple(warnings),
        metadata=metadata,
    )


def parse_file(path: str | Path, config: Config | None = None) -> ParsedDocument:
    """Parse a file from disk.

    Size is checked with ``stat`` before reading, so an oversized upload is
    rejected without first pulling it into memory.
    """
    config = config or get_config()
    path = Path(path)
    if not path.is_file():
        raise UnparseableDocument(f"{path} is not a file.")

    _validate(path.name, path.stat().st_size, config)
    return parse_bytes(path.read_bytes(), path.name, config)
