"""Property: any past state of a case version can be rebuilt exactly from the audit trail.

Random edit scripts run against the real case store. After every step the test keeps the
stored content and a timestamp; rebuilding at each timestamp must give that content back.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pharmasafe_api.cases import service
from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.changes import AddItem, RemoveItem, SetField
from pharmasafe_domain.provenance import Tracked
from pharmasafe_domain.rules.clocks import seed_registry
from pharmasafe_platform.audit import Actor
from pharmasafe_platform.auth import Principal
from pharmasafe_platform.db import Database
from pharmasafe_platform.signatures import content_hash

REGISTRY = seed_registry()
START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def hv(value: Any, at: datetime) -> dict[str, Any]:
    return Tracked[Any].human(value, by="proc-1", at=at).model_dump(mode="json")


STEP = st.one_of(
    st.tuples(st.just("initials"), st.text("ABCDEFGH", min_size=1, max_size=3)),
    st.tuples(st.just("sex"), st.sampled_from(["male", "female", None])),
    st.tuples(st.just("receipt"), st.integers(0, 30)),
    st.tuples(st.just("add"), st.sampled_from(["r1", "r2"])),
    st.tuples(st.just("remove"), st.sampled_from(["r1", "r2"])),
    st.tuples(st.just("term"), st.sampled_from(["r1", "r2"])),
)


def _change(step: tuple[str, Any], present: set[str], at: datetime) -> service.Change | None:
    kind, arg = step
    if kind == "initials":
        return SetField(path="/patient/initials", value=hv(arg, at))
    if kind == "sex":
        return SetField(path="/patient/sex", value=None if arg is None else hv(arg, at))
    if kind == "receipt":
        receipt = (date(2026, 1, 1) + timedelta(days=arg)).isoformat()
        return SetField(path="/admin/latest_receipt_date", value=hv(receipt, at))
    if kind == "add" and arg not in present:
        present.add(arg)
        return AddItem(path="/reactions", item={"id": arg})
    if kind == "remove" and arg in present:
        present.remove(arg)
        return RemoveItem(path=f"/reactions/{arg}")
    if kind == "term" and arg in present:
        return SetField(path=f"/reactions/{arg}/reported_term", value=hv("rash", at))
    return None


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(steps=st.lists(st.lists(STEP, min_size=1, max_size=4), min_size=1, max_size=6))
def test_every_past_state_rebuilds_exactly(
    steps: list[list[tuple[str, Any]]],
    db: Database,
    make_tenant: Callable[[], UUID],
    make_principal: Callable[..., Principal],
) -> None:
    tenant = make_tenant()
    principal = make_principal(sub="proc-1", tenant_id=str(tenant), roles=["case_processor"])
    at = START
    with db.tenant_session(tenant) as session:
        case, version = service.create_case(
            session,
            tenant,
            Actor.from_principal(principal),
            SafetyCase(),
            service.CaseContext(REGISTRY, at),
        )
        history = [(at, dict(version.content))]

    present: set[str] = set()
    for batch in steps:
        at += timedelta(minutes=1)
        changes = [c for step in batch if (c := _change(step, present, at)) is not None]
        if not changes:
            continue
        with db.tenant_session(tenant) as session:
            case = service.get_case(session, case.id, principal.scope)
            version = service.get_version(session, case.id, 1)
            service.apply_changes(session, case, 1, expected_revision=version.revision,
                                  changes=changes, principal=principal,
                                  ctx=service.CaseContext(REGISTRY, at))  # fmt: skip
            history.append((at, dict(service.get_version(session, case.id, 1).content)))

    with db.tenant_session(tenant) as session:
        stored = service.get_version(session, case.id, 1)
        now = service.rebuild_version(session, case, 1)
        assert now.matches_stored is True
        assert content_hash(now.case.to_json()) == stored.content_hash
        for moment, content in history:
            # Halfway to the next minute: after this step, before the next one.
            past = service.rebuild_version(session, case, 1, as_of=moment + timedelta(seconds=30))
            assert past.case.to_json() == content
