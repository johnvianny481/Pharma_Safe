"""Pre-processing without I/O: extraction, language detection, report splitting, email parsing."""

from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pharmasafe_api.intake.email_intake import parse_email
from pharmasafe_api.intake.extraction import (
    TesseractOcr,
    TextStatus,
    extract_text,
    html_to_text,
    parse_language_list,
)
from pharmasafe_api.intake.language import UNDETERMINED, detect_language
from pharmasafe_api.intake.splitting import split_reports
from pharmasafe_platform.blobstore import sha256_hex

OCR_IMAGE = Path(__file__).parent / "fixtures" / "ocr_report.png"


class _Ocr:
    name = "fake"

    def supports(self, content_type: str) -> bool:
        return content_type == "image/png"

    def recognize(self, data: bytes, content_type: str) -> str:
        return "recognised text"


# ---------------------------------------------------------------- language


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("The patient took the tablet and after two days she had a rash on her arms.", "en"),
        ("Die Patientin nahm das Medikament und hatte nach zwei Tagen einen Ausschlag.", "de"),
        ("La patiente a pris le comprimé et après deux jours elle a eu une éruption.", "fr"),
        (
            "La paciente tomó el medicamento y tras dos días presentó una erupción en los brazos.",
            "es",
        ),
        (
            "Il paziente ha preso la compressa e dopo due giorni è stato ricoverato per una reazione.",
            "it",
        ),
        ("O paciente tomou o comprimido e após dois dias apresentou uma erupção na pele.", "pt"),
        ("मरीज़ ने दवा ली और दो दिन बाद उसे चकत्ते हो गए।", "hi"),
        ("Пациент принял таблетку и через два дня у него появилась сыпь.", "ru"),  # noqa: RUF001
        ("患者は薬を服用した後、発疹が出ました。", "ja"),
        ("患者服药后两天出现皮疹。", "zh"),
    ],
)
def test_detects_common_report_languages(text: str, language: str) -> None:
    assert detect_language(text).language == language


@pytest.mark.parametrize("text", ["", "12/03/2026 5mg", "Acmezol rash"])
def test_short_or_unclear_text_is_undetermined(text: str) -> None:
    assert detect_language(text).language == UNDETERMINED


# ---------------------------------------------------------------- splitting


THREE_REPORTS = """Line listing from distributor

Report 1
Patient JD, 45 years, took Acmezol and developed a rash.

Report 2:
Patient KL, 30 years, took Acmezol and had a headache.

REPORT #3
Patient MN, 61 years, hospitalised with liver injury.
"""


def test_splits_numbered_reports() -> None:
    spans = split_reports(THREE_REPORTS)
    parts = [THREE_REPORTS[s.start : s.end] for s in spans]
    assert len(parts) == 3
    assert parts[0].startswith("Report 1")
    assert "rash" in parts[0]
    assert parts[2].startswith("REPORT #3")
    assert "liver" in parts[2]


def test_splits_text_with_crlf_line_endings() -> None:
    """Email bodies arrive with CRLF line endings."""
    assert len(split_reports(THREE_REPORTS.replace("\n", "\r\n"))) == 3
    divided = "A" * 50 + "\r\n-----\r\n" + "B" * 50
    assert len(split_reports(divided)) == 2


def test_splits_on_divider_lines_between_substantial_blocks() -> None:
    text = (
        "Patient AB took Acmezol and developed a severe rash on day two.\n"
        "----------\n"
        "Patient CD took Acmezol and was admitted to hospital with jaundice.\n"
    )
    assert len(split_reports(text)) == 2


@pytest.mark.parametrize(
    "text",
    [
        "Patient JD took Acmezol and developed a rash.",
        "Case 1\nOnly one numbered heading, so this is a single report.",
        "Signature block\n-----\nThanks",  # a divider without two substantial blocks
        "The patient saw report 1 and report 2 in the leaflet.",  # headings must be on their own line
    ],
)
def test_single_reports_are_not_split(text: str) -> None:
    assert len(split_reports(text)) == 1


def test_blank_text_has_no_reports() -> None:
    assert split_reports("  \n\t ") == []


TOKENS = [*"ab \n-=", "Report 1\n", "Case 2\n", "\n-----\n", "x" * 45]


@given(st.lists(st.sampled_from(TOKENS), max_size=40).map("".join))
def test_spans_are_ordered_disjoint_and_trimmed(text: str) -> None:
    spans = split_reports(text)
    previous_end = 0
    for span in spans:
        assert previous_end <= span.start < span.end <= len(text)
        assert not text[span.start].isspace()
        assert not text[span.end - 1].isspace()
        previous_end = span.end
    assert (spans == []) == (not text.strip())


# ---------------------------------------------------------------- extraction


def test_plain_text_uses_the_declared_charset() -> None:
    data = "Fièvre après la dose".encode("iso-8859-1")
    result = extract_text(data, "text/plain; charset=iso-8859-1", None)
    assert (result.status, result.text) == (TextStatus.EXTRACTED, "Fièvre après la dose")
    # A wrong or unknown charset still yields text (Latin-1 never fails).
    assert extract_text(b"\xff\xfe ok", "text/plain; charset=nonsense", None).text.endswith("ok")


def test_html_is_reduced_to_text() -> None:
    html = "<html><head><style>p{}</style></head><body><p>Rash after <b>Acmezol</b></p><script>x()</script><p>Day 2</p></body></html>"
    assert html_to_text(html) == "Rash after Acmezol\n\nDay 2"


def test_pdf_text_is_extracted(make_pdf: Callable[[list[str]], bytes]) -> None:
    pdf = make_pdf(["Patient JD took Acmezol 5 mg", "and developed a rash two days later."])
    result = extract_text(pdf, "application/pdf", None)
    assert result.status is TextStatus.EXTRACTED
    assert "Acmezol" in result.text


def test_pdf_without_text_is_a_scan_needing_ocr(make_pdf: Callable[[list[str]], bytes]) -> None:
    assert extract_text(make_pdf([]), "application/pdf", None).status is TextStatus.OCR_UNAVAILABLE


def test_broken_pdf_fails_without_raising() -> None:
    assert extract_text(b"%PDF-1.4 garbage", "application/pdf", None).status is TextStatus.FAILED


def test_images_go_through_ocr_when_available() -> None:
    assert extract_text(b"png", "image/png", _Ocr()).text == "recognised text"
    assert extract_text(b"png", "image/png", None).status is TextStatus.OCR_UNAVAILABLE
    assert extract_text(b"x", "image/x-unknown", _Ocr()).status is TextStatus.OCR_UNAVAILABLE


def test_unknown_types_are_kept_but_unsupported() -> None:
    assert (
        extract_text(b"\x00\x01", "application/octet-stream", None).status is TextStatus.UNSUPPORTED
    )


def test_tesseract_reads_the_text_of_an_image(tesseract_cmd: str) -> None:
    engine = TesseractOcr.find(tesseract_cmd)
    assert engine is not None
    result = extract_text(OCR_IMAGE.read_bytes(), "image/png", engine)
    assert (result.method, result.status) == ("ocr:tesseract", TextStatus.EXTRACTED)
    text = " ".join(result.text.split())
    assert "The patient developed a severe rash" in text
    assert "Acmezol 20 mg" in text


def test_tesseract_failure_marks_the_document_failed(tesseract_cmd: str) -> None:
    engine = TesseractOcr.find(tesseract_cmd)
    assert engine is not None
    assert extract_text(b"not an image", "image/png", engine).status is TextStatus.FAILED


def test_tesseract_uses_only_installed_languages(tesseract_cmd: str) -> None:
    engine = TesseractOcr.find(tesseract_cmd, ("eng", "zzz"))
    assert engine is not None
    assert engine.languages == ("eng",)
    assert TesseractOcr.find(tesseract_cmd, ("zzz",)) is None  # nothing usable: unavailable
    assert TesseractOcr.find("no-such-tesseract") is None


@pytest.mark.parametrize(
    "output",
    [
        'List of available languages in "/usr/share/tesseract-ocr/5/tessdata/" (3):\neng\nosd\ndeu\n',
        "List of available languages (3):\r\neng\r\nosd\r\ndeu\r\n",  # Tesseract 4, Windows
    ],
)
def test_language_list_is_parsed(output: str) -> None:
    assert parse_language_list(output) == {"eng", "osd", "deu"}
    assert parse_language_list("unexpected output") == frozenset()


# ---------------------------------------------------------------- email parsing


def _email(message_id: str | None = "<abc@example.org>") -> bytes:
    message = EmailMessage()
    message["From"] = "Dr Silva <silva@example.org>"
    message["To"] = "intake@acme.pharmasafe.local"
    message["Subject"] = "Adverse event report"
    if message_id:
        message["Message-ID"] = message_id
    message.set_content("Patient JD developed a rash after Acmezol.")
    message.add_alternative(
        "<p>Patient JD developed a rash after <b>Acmezol</b>.</p>", subtype="html"
    )
    message.add_attachment(b"%PDF-1.4 binary \x00\xff", maintype="application", subtype="pdf",
                           filename="report.pdf")  # fmt: skip
    message.add_attachment(
        b"\x89PNG\r\n\x1a\n\x00", maintype="image", subtype="png", filename="photo.png"
    )
    return message.as_bytes()


def test_email_becomes_raw_message_body_and_attachments() -> None:
    raw = _email()
    submission = parse_email(raw)
    assert submission.external_id == "<abc@example.org>"
    assert submission.request_hash == sha256_hex(raw)
    assert submission.subject == "Adverse event report"
    parts = [(d.part, d.content_type, d.filename) for d in submission.documents]
    assert parts == [
        ("message", "message/rfc822", "message.eml"),
        ("body", "text/plain; charset=utf-8", None),
        ("attachment", "application/pdf", "report.pdf"),
        ("attachment", "image/png", "photo.png"),
    ]
    assert submission.documents[0].data == raw
    assert submission.documents[2].data == b"%PDF-1.4 binary \x00\xff"
    assert submission.documents[3].data == b"\x89PNG\r\n\x1a\n\x00"


def test_email_without_message_id_is_keyed_by_its_hash() -> None:
    raw = _email(message_id=None)
    assert parse_email(raw).external_id == f"sha256:{sha256_hex(raw)}"
