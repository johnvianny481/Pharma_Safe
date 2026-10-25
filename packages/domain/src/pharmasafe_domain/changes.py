"""Changes to a case version, and replaying them.

Every edit to a case is one of three operations on a path:

- ``set`` a field: ``/patient/age``, ``/reactions/r1/seriousness/death``
- ``add`` an item to a repeating section: ``/drugs``
- ``remove`` an item: ``/drugs/d1``

List items are addressed by ``id``, never by position, so a change still means the same
thing after other items come and go.

The case store applies changes with :func:`apply_change` and records each one in the
audit ledger. :func:`replay` applies a recorded sequence to a base case. Because the
store and the rebuild use the same function, replaying the ledger must reproduce the
stored version exactly; the stored content hash proves that it did.
"""

from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pharmasafe_domain.case import ITEM_SECTIONS, SafetyCase


class ChangeError(ValueError):
    """The change does not apply to this case (bad path, wrong type, broken reference)."""


class _Change(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(pattern=r"^(/[A-Za-z0-9_-]+)+$")


class SetField(_Change):
    """Set a tracked field. ``value`` is a Tracked value as JSON, or ``None`` to clear."""

    op: Literal["set"] = "set"
    value: dict[str, Any] | None


class AddItem(_Change):
    """Append an item (with a new ``id``) to a repeating section."""

    op: Literal["add"] = "add"
    item: dict[str, Any]


class RemoveItem(_Change):
    op: Literal["remove"] = "remove"


Change = Annotated[SetField | AddItem | RemoveItem, Field(discriminator="op")]


def apply_change(
    case: SafetyCase, change: SetField | AddItem | RemoveItem
) -> tuple[SafetyCase, Any]:
    """Apply one change. Returns the new case and the value it replaced (for the audit)."""
    data = case.to_json()
    segments = change.path.strip("/").split("/")
    old: Any
    if isinstance(change, AddItem):
        items = _section(data, segments)
        item_id = change.item.get("id")
        if any(existing["id"] == item_id for existing in items):
            raise ChangeError(f"{change.path} already has an item with id {item_id!r}")
        items.append(change.item)
        old = None
    else:
        parent, key = _parent(data, segments)
        if isinstance(change, RemoveItem):
            if not isinstance(parent, list):
                raise ChangeError(f"{change.path} is not a list item")
            index = _item_index(parent, key, change.path)
            old = parent.pop(index)
        else:
            if not isinstance(parent, dict) or key not in parent:
                raise ChangeError(f"{change.path} is not a field")
            old = parent[key]
            if not _is_tracked_slot(old):
                raise ChangeError(f"{change.path} is not a tracked field")
            parent[key] = change.value
    try:
        return SafetyCase.from_json(data), old
    except ValidationError as exc:
        raise ChangeError(f"{change.path}: {_first_error(exc)}") from exc


def replay(base: SafetyCase, changes: Iterable[SetField | AddItem | RemoveItem]) -> SafetyCase:
    case = base
    for change in changes:
        case, _ = apply_change(case, change)
    return case


def tracked_value_at(case: SafetyCase, path: str) -> dict[str, Any] | None:
    """The tracked field at ``path`` as JSON (``None`` if empty), e.g. to confirm it."""
    parent, key = _parent(case.to_json(), path.strip("/").split("/"))
    if not isinstance(parent, dict) or not _is_tracked_slot(parent.get(key, "")):
        raise ChangeError(f"{path} is not a tracked field")
    value: dict[str, Any] | None = parent[key]
    return value


def _section(data: dict[str, Any], segments: list[str]) -> list[Any]:
    if len(segments) != 1 or segments[0] not in ITEM_SECTIONS:
        raise ChangeError(f"items can only be added to {', '.join(ITEM_SECTIONS)}")
    section: list[Any] = data[segments[0]]
    return section


def _parent(data: dict[str, Any], segments: list[str]) -> tuple[Any, str]:
    node: Any = data
    for segment in segments[:-1]:
        node = _child(node, segment)
    return node, segments[-1]


def _child(node: Any, segment: str) -> Any:
    if isinstance(node, list):
        return node[_item_index(node, segment, segment)]
    if isinstance(node, dict) and segment in node and not _is_tracked_slot(node[segment]):
        return node[segment]
    raise ChangeError(f"no such path segment {segment!r}")


def _item_index(items: list[Any], item_id: str, path: str) -> int:
    for index, item in enumerate(items):
        if item["id"] == item_id:
            return index
    raise ChangeError(f"{path}: no item with id {item_id!r}")


def _is_tracked_slot(value: Any) -> bool:
    """Tracked fields dump as ``None`` or an object with a ``source``; sections never do."""
    return value is None or (isinstance(value, dict) and "source" in value)


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = "/".join(str(part) for part in error["loc"])
    return f"{error['msg']} at {location}" if location else str(error["msg"])
