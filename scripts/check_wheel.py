"""Fail if source-only development surfaces enter the built wheel."""

from __future__ import annotations

import hashlib
import runpy
import sys
import zipfile
from pathlib import Path

wheel = Path(sys.argv[1])
forbidden = ("/tests/", "/examples/", "/recipes/", "/legacy/")
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    metadata = Path(__file__).resolve().parents[1] / "src/optiprofiler_evolve/observability/schemas/__init__.py"
    schemas = runpy.run_path(str(metadata))["SCHEMA_DIGESTS"]
    for name, digest in schemas.items():
        path = f"optiprofiler_evolve/observability/schemas/{name}"
        if path not in names or hashlib.sha256(archive.read(path)).hexdigest() != digest:
            raise SystemExit(f"Missing or modified pinned schema in wheel: {path}")
bad = [name for name in names if any(marker in f"/{name}" for marker in forbidden)]
if bad:
    raise SystemExit(f"Source-only files found in wheel: {bad}")
print(f"wheel surface ok: {len(names)} files")
