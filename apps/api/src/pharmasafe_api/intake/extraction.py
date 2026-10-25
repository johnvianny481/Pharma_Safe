"""Text extraction from source documents, with OCR behind an interface.

| Content type | Method |
|---|---|
| ``text/plain``, ``text/csv`` | decode with the declared charset |
| ``text/html`` | strip markup (scripts and styles dropped) |
| ``application/pdf`` | embedded text via pypdf; a PDF without it is a scan |
| ``image/*`` | the configured :class:`OcrEngine` |
| anything else | stored and marked ``unsupported`` for manual entry |

Scanned PDFs need pages rendered to images before OCR; that rendering (and Azure AI
Document Intelligence, which reads PDFs directly) is a later adapter. Until then they
are marked ``ocr_unavailable`` and go to manual data entry. Nothing is ever dropped:
the source bytes are stored whatever extraction finds.
"""

import io
import logging
import shutil
import subprocess  # nosec B404
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from typing import Protocol

from pypdf import PdfReader
from pypdf.errors import PyPdfError

log = logging.getLogger(__name__)

# A PDF page with fewer visible characters than this is treated as a scanned image.
MIN_CHARS_PER_PDF_PAGE = 20
OCR_TIMEOUT_SECONDS = 120
# Tesseract language data to use when installed (``eng`` is the minimum useful set).
OCR_LANGUAGES = ("eng", "deu", "fra", "spa", "ita")


class TextStatus(StrEnum):
    EXTRACTED = "extracted"
    OCR_UNAVAILABLE = "ocr_unavailable"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True)
class ExtractedText:
    method: str
    status: TextStatus
    text: str = ""


class OcrError(Exception):
    pass


class OcrEngine(Protocol):
    name: str

    def supports(self, content_type: str) -> bool: ...

    def recognize(self, data: bytes, content_type: str) -> str: ...


class TesseractOcr:
    """Local OCR with the Tesseract command line (``tesseract stdin stdout``).

    Tesseract silently skips requested languages whose data is not installed, and fails
    only when none is. :meth:`find` therefore asks which languages are installed, uses
    only those, and logs the missing ones, so a gap is visible rather than a quiet drop
    in accuracy for reports in that language.
    """

    name = "tesseract"
    _TYPES = frozenset({"image/png", "image/jpeg", "image/tiff", "image/bmp", "image/gif"})

    def __init__(self, executable: str, languages: Sequence[str] = OCR_LANGUAGES) -> None:
        self._executable = executable
        self.languages = tuple(languages)

    @classmethod
    def find(
        cls, command: str = "tesseract", languages: Sequence[str] = OCR_LANGUAGES
    ) -> "TesseractOcr | None":
        """The engine, or None (OCR unavailable) if Tesseract or all its languages are missing."""
        path = shutil.which(command)
        if not path:
            return None
        installed = installed_languages(path)
        usable = [lang for lang in languages if lang in installed]
        missing = [lang for lang in languages if lang not in installed]
        if missing:
            log.warning("tesseract language data missing", extra={"missing": ",".join(missing)})
        if not usable:
            log.error("no requested tesseract language is installed; OCR is unavailable")
            return None
        return cls(path, usable)

    def supports(self, content_type: str) -> bool:
        return content_type in self._TYPES

    def recognize(self, data: bytes, content_type: str) -> str:
        try:
            # Runs only the configured Tesseract binary, with a fixed argv and no shell.
            result = subprocess.run(  # noqa: S603  # nosec B603
                [self._executable, "stdin", "stdout", "-l", "+".join(self.languages)],
                input=data,
                capture_output=True,
                timeout=OCR_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OcrError(f"tesseract did not run: {type(exc).__name__}") from exc
        if result.returncode != 0:
            raise OcrError(f"tesseract failed with exit code {result.returncode}")
        return result.stdout.decode("utf-8", errors="replace")


def installed_languages(executable: str) -> frozenset[str]:
    """Languages the Tesseract install has data for (empty if it cannot be asked)."""
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [executable, "--list-langs"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    # Tesseract 5 prints the list on stdout, older versions on stderr.
    return parse_language_list((result.stdout + result.stderr).decode("utf-8", errors="replace"))


def parse_language_list(output: str) -> frozenset[str]:
    """Parse ``tesseract --list-langs``: a heading line, then one language per line."""
    lines = output.splitlines()
    start = next((i for i, line in enumerate(lines) if "available languages" in line), None)
    if start is None:
        return frozenset()
    return frozenset(line.strip() for line in lines[start + 1 :] if line.strip())


def extract_text(data: bytes, content_type: str, ocr: OcrEngine | None) -> ExtractedText:
    media_type, _, params = content_type.partition(";")
    media_type = media_type.strip().lower()
    if media_type in ("text/plain", "text/csv"):
        return ExtractedText("plain", TextStatus.EXTRACTED, _decode(data, params))
    if media_type == "text/html":
        return ExtractedText("html", TextStatus.EXTRACTED, html_to_text(_decode(data, params)))
    if media_type == "application/pdf":
        return _pdf(data)
    if media_type.startswith("image/"):
        if ocr is None or not ocr.supports(media_type):
            return ExtractedText("ocr", TextStatus.OCR_UNAVAILABLE)
        try:
            return ExtractedText(
                f"ocr:{ocr.name}", TextStatus.EXTRACTED, ocr.recognize(data, media_type)
            )
        except OcrError as exc:
            log.warning("ocr failed", extra={"reason": str(exc)})
            return ExtractedText(f"ocr:{ocr.name}", TextStatus.FAILED)
    return ExtractedText("none", TextStatus.UNSUPPORTED)


def _decode(data: bytes, params: str) -> str:
    charset = "utf-8"
    for param in params.split(";"):
        key, _, value = param.strip().partition("=")
        if key.lower() == "charset" and value:
            charset = value.strip('"').lower()
    try:
        return data.decode(charset)
    except (LookupError, UnicodeDecodeError):
        # Latin-1 maps every byte, so the text is never lost, only possibly mis-decoded.
        return data.decode("latin-1")


def _pdf(data: bytes) -> ExtractedText:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return ExtractedText("pdf", TextStatus.FAILED)
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PyPdfError, ValueError, KeyError, TypeError) as exc:
        log.warning("pdf text extraction failed", extra={"reason": type(exc).__name__})
        return ExtractedText("pdf", TextStatus.FAILED)
    text = "\n\n".join(pages).strip()
    visible = sum(1 for c in text if not c.isspace())
    if visible < MIN_CHARS_PER_PDF_PAGE * max(1, len(pages)):
        return ExtractedText("pdf", TextStatus.OCR_UNAVAILABLE, text)
    return ExtractedText("pdf", TextStatus.EXTRACTED, text)


class _TextCollector(HTMLParser):
    _SKIP = frozenset({"script", "style", "head", "title"})
    _BLOCK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    collector = _TextCollector()
    collector.feed(html)
    collector.close()
    lines = (" ".join(line.split()) for line in "".join(collector.parts).splitlines())
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()
