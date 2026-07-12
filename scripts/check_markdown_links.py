"""Check repository-relative Markdown links."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATTERN = re.compile(r"\[[^]]+\]\(([^)]+)\)")
errors: list[str] = []

for document in sorted(ROOT.rglob("*.md")):
    if any(part.startswith(".") or part in {"build", "dist"} for part in document.parts):
        continue
    for target in PATTERN.findall(document.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path_text = target.split("#", 1)[0]
        if path_text and not (document.parent / path_text).resolve().exists():
            errors.append(f"{document.relative_to(ROOT)} -> {target}")

if errors:
    raise SystemExit("Broken Markdown links:\n" + "\n".join(errors))
print("Markdown links ok")
