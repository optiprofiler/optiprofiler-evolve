"""Bounded subprocess entrypoint; never import candidate modules."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from optiprofiler_evolve.criteria.complexity.analyzer import analyze_source

if __name__ == "__main__":
    print(json.dumps(asdict(analyze_source(Path(sys.argv[1])))))
