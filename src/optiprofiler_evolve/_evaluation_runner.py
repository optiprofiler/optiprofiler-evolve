"""Container entrypoint for trusted candidate evaluation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import EvaluationConfig
from .data import DataPlan
from .evaluation import PythonOptiProfilerEvaluator
from .solver import InterfaceSpec


def main() -> int:
    request_path = Path(sys.argv[1])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request_path.unlink(missing_ok=True)
    data_raw = request["data"]
    data_raw.pop("final", None)
    for key in ("universe", "public", "hidden", "smoke"):
        data_raw[key] = tuple(data_raw[key])
    data = DataPlan(**data_raw)
    evaluation = EvaluationConfig(**request["evaluation"])
    evaluator = PythonOptiProfilerEvaluator(
        reference=Path(request["reference"]),
        interface=InterfaceSpec.parse(request["interface"]),
        data=data,
        config=evaluation,
    )
    result = evaluator.evaluate(
        Path(request["candidate"]), request["mode"], Path(request["output_dir"])
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
