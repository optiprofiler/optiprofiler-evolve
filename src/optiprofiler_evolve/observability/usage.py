"""Provider-native worker usage accounting without pricing assumptions."""

from __future__ import annotations

from collections.abc import Iterable

from optiprofiler_evolve.models import _NUMERIC_FIELDS, UsageRecord


def aggregate_usage(records: Iterable[UsageRecord]) -> UsageRecord:
    """Sum only fields known for every record; never coerce missing values to zero."""

    items = tuple(records)
    values = {
        name: _sum_if_complete(items, name)
        for name in _NUMERIC_FIELDS
    }
    lower_bounds = {}
    for name, total in values.items():
        if total is not None:
            continue
        observed = [getattr(item, name) if getattr(item, name) is not None
                    else item.lower_bounds.get(name) for item in items]
        known = [value for value in observed if value is not None]
        if known:
            lower_bounds[name] = sum(known)
    return UsageRecord(source="aggregate", lower_bounds=lower_bounds, **values)


def _sum_if_complete(records: tuple[UsageRecord, ...], name: str) -> int | float | None:
    if not records:
        return None
    values = tuple(getattr(record, name) for record in records)
    if any(value is None for value in values):
        return None
    return sum(values)  # type: ignore[arg-type]
