"""Fail if source-only development surfaces enter the built wheel."""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

wheel = Path(sys.argv[1])
forbidden = ("/tests/", "/examples/", "/recipes/", "/legacy/")
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    schemas = {
        "eval_report.schema.json": "9b8f76fd9b2d3eff323304b758ff61c5c456d35b1d81fcf05d3ab9d64453a8a3",
        "plot_data.schema.json": "76f83e1b7922a1479d8770888bd417c86fe5f837a450789891618960582d323c",
    }
    for name, digest in schemas.items():
        path = f"optiprofiler_evolve/observability/schemas/{name}"
        if path not in names or hashlib.sha256(archive.read(path)).hexdigest() != digest:
            raise SystemExit(f"Missing or modified pinned schema in wheel: {path}")
    if "optiprofiler_evolve/eval_reports.py" in names or any(
        name.startswith("optiprofiler_evolve/schemas/") for name in names
    ):
        raise SystemExit("Obsolete EvalReport module paths found in wheel")
bad = [name for name in names if any(marker in f"/{name}" for marker in forbidden)]
if bad:
    raise SystemExit(f"Source-only files found in wheel: {bad}")
print(f"wheel surface ok: {len(names)} files")
