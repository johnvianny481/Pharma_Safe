"""Coding search for the review UI's coding picker: candidates from the dictionary."""

from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from pharmasafe_api.cases.router import Reader
from pharmasafe_api.coding.dictionary import Kind

router = APIRouter(prefix="/coding", tags=["coding"])


class CandidateOut(BaseModel):
    code: str
    label: str
    score: float


class SearchOut(BaseModel):
    kind: Kind
    release: str
    candidates: list[CandidateOut]


@router.get("/{kind}/search", response_model=SearchOut)
def search(
    kind: Kind,
    principal: Reader,
    request: Request,
    term: Annotated[str, Query(min_length=2, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> SearchOut:
    dictionary = request.app.state.dictionary
    return SearchOut(
        kind=kind,
        release=dictionary.release(kind),
        candidates=[CandidateOut(**c.__dict__) for c in dictionary.search(kind, term, limit)],
    )
