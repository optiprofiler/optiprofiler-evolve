"""Provider-native worker usage accounting without pricing assumptions."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any


_NUMERIC_FIELDS = (
    "calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
)


@dataclasses.dataclass(frozen=True)
class UsageRecord:
    """One invocation's native usage; unavailable values stay ``None``."""

    source: str
    calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    schema: str = dataclasses.field(default="usage_record/1", init=False)
    incomplete: bool = dataclasses.field(init=False)
    missing_fields: tuple[str, ...] = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("UsageRecord.source must be nonempty.")
        for name in _NUMERIC_FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or value < 0:
                raise ValueError(f"Usage field {name!r} must be nonnegative or None.")
            if name != "cost_usd" and not isinstance(value, int):
                raise TypeError(f"Usage field {name!r} must be an integer or None.")
        missing = tuple(name for name in _NUMERIC_FIELDS if getattr(self, name) is None)
        object.__setattr__(self, "missing_fields", missing)
        object.__setattr__(self, "incomplete", bool(missing))

    @classmethod
    def from_values(cls, *, source: str, **values: Any) -> UsageRecord:
        """Construct a record from provider-native values."""

        return cls(source=source, **values)

    @classmethod
    def unavailable(cls, source: str = "unavailable") -> UsageRecord:
        return cls(source=source)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def aggregate_usage(records: Iterable[UsageRecord]) -> UsageRecord:
    """Sum only fields known for every record; never coerce missing values to zero."""

    items = tuple(records)
    values = {
        name: _sum_if_complete(items, name)
        for name in _NUMERIC_FIELDS
    }
    return UsageRecord(source="aggregate", **values)


def _sum_if_complete(records: tuple[UsageRecord, ...], name: str) -> int | float | None:
    if not records:
        return None
    values = tuple(getattr(record, name) for record in records)
    if any(value is None for value in values):
        return None
    return sum(values)  # type: ignore[arg-type]


__all__: list[str] = []
