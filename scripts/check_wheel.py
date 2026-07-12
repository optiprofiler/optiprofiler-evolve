"""Fail if source-only development surfaces enter the built wheel."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path


wheel = Path(sys.argv[1])
forbidden = ("/tests/", "/examples/", "/recipes/", "/legacy/")
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
bad = [name for name in names if any(marker in f"/{name}" for marker in forbidden)]
if bad:
    raise SystemExit(f"Source-only files found in wheel: {bad}")
print(f"wheel surface ok: {len(names)} files")
