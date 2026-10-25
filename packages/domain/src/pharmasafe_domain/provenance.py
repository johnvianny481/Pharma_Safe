"""Field provenance: every case field carries where its value came from.

A :class:`Tracked` value stores the value itself, its source (AI, human or import), the
evidence span in the source document, a confidence score, and who confirmed it and when.

The invariants are the build prompt's core principle in code form:

- An AI value must cite evidence and give a confidence. A value without evidence is
  rejected here, before it can reach a case.
- A human value is confirmed by the person who entered it.
- An AI or imported value stays unconfirmed until a person confirms it. Approval
  (see :mod:`pharmasafe_domain.rules.checks`) requires every value to be confirmed.
"""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Source(StrEnum):
    AI = "ai"
    HUMAN = "human"
    IMPORT = "import"


class EvidenceSpan(BaseModel):
    """A quote from a source document, located by character offsets ``[start, end)``.

    The quoted text is stored with the offsets so a reviewer (and the Phase 5 pipeline)
    can check that the evidence really is at that place in the document.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def _offsets_match_text(self) -> Self:
        if self.end <= self.start:
            raise ValueError("evidence end must be after start")
        if len(self.text) != self.end - self.start:
            raise ValueError("evidence text length must equal end - start")
        return self


class Tracked[T](BaseModel):
    """One field value with its provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: T
    source: Source
    evidence: EvidenceSpan | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confirmed_by: str | None = Field(default=None, min_length=1)
    confirmed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _provenance_is_complete(self) -> Self:
        if (self.confirmed_by is None) != (self.confirmed_at is None):
            raise ValueError("confirmed_by and confirmed_at must be set together")
        if self.source is Source.AI and (self.evidence is None or self.confidence is None):
            raise ValueError("an AI-proposed value needs an evidence span and a confidence")
        if self.source is Source.HUMAN and self.confirmed_by is None:
            raise ValueError("a human-entered value must record who entered it")
        return self

    @property
    def confirmed(self) -> bool:
        return self.confirmed_by is not None

    def confirm(self, by: str, at: datetime) -> "Tracked[T]":
        """The same value and evidence, confirmed by a person."""
        return self.model_copy(update={"confirmed_by": by, "confirmed_at": at})

    @classmethod
    def human(cls, value: T, *, by: str, at: datetime) -> "Tracked[T]":
        return cls(value=value, source=Source.HUMAN, confirmed_by=by, confirmed_at=at)
