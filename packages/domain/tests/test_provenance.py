from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from pharmasafe_domain.provenance import EvidenceSpan, Source, Tracked

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SPAN = EvidenceSpan(document_id="doc-1", start=10, end=14, text="rash")


def test_ai_value_needs_evidence_and_confidence() -> None:
    with pytest.raises(ValidationError, match="evidence span and a confidence"):
        Tracked[str](value="rash", source=Source.AI, confidence=0.9)
    with pytest.raises(ValidationError, match="evidence span and a confidence"):
        Tracked[str](value="rash", source=Source.AI, evidence=SPAN)
    proposed = Tracked[str](value="rash", source=Source.AI, evidence=SPAN, confidence=0.9)
    assert not proposed.confirmed


def test_confidence_is_a_probability() -> None:
    with pytest.raises(ValidationError):
        Tracked[str](value="x", source=Source.AI, evidence=SPAN, confidence=1.5)


def test_evidence_offsets_must_match_the_quote() -> None:
    with pytest.raises(ValidationError, match="length"):
        EvidenceSpan(document_id="doc-1", start=0, end=10, text="rash")
    with pytest.raises(ValidationError, match="after start"):
        EvidenceSpan(document_id="doc-1", start=5, end=5, text="x")


def test_human_value_records_who_entered_it() -> None:
    with pytest.raises(ValidationError, match="who entered it"):
        Tracked[str](value="x", source=Source.HUMAN)
    value = Tracked[str].human("x", by="proc-1", at=T0)
    assert value.confirmed
    assert value.confirmed_by == "proc-1"


def test_confirmation_needs_both_who_and_when() -> None:
    with pytest.raises(ValidationError, match="together"):
        Tracked[str](value="x", source=Source.IMPORT, confirmed_by="proc-1")


def test_confirming_keeps_value_source_and_evidence() -> None:
    proposed = Tracked[str](value="rash", source=Source.AI, evidence=SPAN, confidence=0.7)
    confirmed = proposed.confirm("proc-1", T0)
    assert confirmed.confirmed
    assert (confirmed.value, confirmed.source, confirmed.evidence) == ("rash", Source.AI, SPAN)
    assert not proposed.confirmed  # immutable: the original is unchanged


def test_confirmation_time_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        Tracked[date](value=date(2026, 1, 1), source=Source.IMPORT, confirmed_by="u",
                      confirmed_at=datetime(2026, 1, 1))  # fmt: skip
