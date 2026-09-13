"""Provider-native worker usage accounting without pricing assumptions."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from optiprofiler_evolve.models import _NUMERIC_FIELDS, UsageRecord


def read_native_usage(path: Path, harness: str, *, complete: bool = True) -> UsageRecord:
    """Read complete native CLI totals, without treating a turn as one API call."""
    source = f"{harness}_native"
    records = []
    incomplete = not complete
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if harness == "codex" and event.get("type") == "turn.completed":
                        usage = event["usage"]
                        records.append(UsageRecord.from_values(source=source,
                            input_tokens=usage.get("input_tokens"),
                            output_tokens=usage.get("output_tokens"),
                            cache_read_tokens=usage.get("cached_input_tokens")))
                    elif harness == "claude" and event.get("type") == "result":
                        usage = event.get("usage", {})
                        records.append(UsageRecord.from_values(source=source,
                            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                            cache_read_tokens=usage.get("cache_read_input_tokens"),
                            cache_write_tokens=usage.get("cache_creation_input_tokens"),
                            cost_usd=event.get("total_cost_usd")))
                except (ValueError, TypeError, KeyError, AttributeError):
                    incomplete = True
    except (OSError, UnicodeError):
        incomplete = True
    totals = aggregate_usage(records)
    if incomplete:
        return UsageRecord(source=source, lower_bounds={
            **totals.lower_bounds,
            **{key: getattr(totals, key) for key in _NUMERIC_FIELDS if getattr(totals, key) is not None}})
    return UsageRecord(source=source, lower_bounds=totals.lower_bounds,
                       **{key: getattr(totals, key) for key in _NUMERIC_FIELDS})


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
