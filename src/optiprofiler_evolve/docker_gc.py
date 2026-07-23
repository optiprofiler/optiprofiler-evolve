"""Explicit age-gated cleanup for labeled OptiProfiler Evolve Docker objects."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from .docker_runtime import (
    list_managed_resources,
    remove_managed_resources,
    select_gc_resources,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or remove stale optiprofiler-evolve Docker resources."
    )
    parser.add_argument("--older-than", type=int, required=True, metavar="SECONDS")
    parser.add_argument("--run", help="Restrict cleanup to one recorded run id.")
    parser.add_argument(
        "--include-active",
        action="store_true",
        help="Also select running containers and networks with attached containers.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Remove selected resources. Without this flag the command is a dry run.",
    )
    args = parser.parse_args(argv)
    if args.older_than < 1:
        parser.error("--older-than must be a positive number of seconds")

    selected = select_gc_resources(
        list_managed_resources(),
        now=datetime.now(timezone.utc),
        run_id=args.run,
        older_than_seconds=args.older_than,
        include_active=args.include_active,
    )
    print(
        json.dumps(
            [
                {
                    "kind": item.kind,
                    "id": item.identifier,
                    "name": item.name,
                    "run_id": item.run_id,
                    "trace_id": item.trace_id,
                    "created_at": item.created_at.isoformat(),
                    "active": item.active,
                }
                for item in selected
            ],
            indent=2,
            sort_keys=True,
        )
    )
    if not args.apply:
        return 0
    errors = remove_managed_resources(selected)
    for error in errors:
        print(f"cleanup error: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__: list[str] = []
