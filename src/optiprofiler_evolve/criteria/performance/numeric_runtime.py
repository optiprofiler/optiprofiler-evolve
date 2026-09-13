"""Numeric-only solver protocol. This file also runs alone in the solver image.

No evaluator modules, callback objects, pickle payloads, or filesystem paths from
the evaluator cross this boundary. Array shape and IEEE non-finite values do.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

MAX_FRAME = 8 * 1024 * 1024
MAX_VALUES = 200_000


def pack(value: Any) -> dict:
    array = np.asarray(value, dtype=float)
    if array.size > MAX_VALUES:
        raise ValueError("Numeric array exceeds the protocol limit.")
    values = [
        float(v) if np.isfinite(v) else ("nan" if np.isnan(v) else "+inf" if v > 0 else "-inf")
        for v in array.flat
    ]
    return {"shape": list(array.shape), "values": values}


def unpack(raw: dict) -> Any:
    if not isinstance(raw, dict) or set(raw) != {"shape", "values"}:
        raise ValueError("Expected a numeric array.")
    shape, values = raw["shape"], raw["values"]
    if (not isinstance(shape, list) or len(shape) > 2
            or any(type(n) is not int or n < 0 or n > MAX_VALUES for n in shape)
            or not isinstance(values, list) or len(values) > MAX_VALUES):
        raise ValueError("Invalid numeric shape or size.")
    if any(type(v) not in (int, float) and v not in ("nan", "+inf", "-inf") for v in values):
        raise ValueError("Invalid numeric value.")
    array = np.asarray(values, dtype=float).reshape(shape)
    return float(array) if array.ndim == 0 else array


def read_frame(stream: Any) -> dict:
    line = stream.readline(MAX_FRAME + 1)
    if not line:
        raise EOFError("Numeric peer disconnected.")
    if len(line) > MAX_FRAME or not line.endswith(b"\n"):
        raise ValueError("Numeric frame exceeds the protocol limit.")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Expected a numeric protocol object.")
    return value


def write_frame(stream: Any, value: dict) -> None:
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > MAX_FRAME:
        raise ValueError("Numeric frame exceeds the protocol limit.")
    stream.write(encoded)
    stream.flush()


def main() -> int:
    # Retain a protocol FD, but send solver print/native-library stdout to logs.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    reader = sys.stdin.buffer
    initial = read_frame(reader)
    oracle_errors: list[tuple[Exception, str | None]] = []

    def oracle(index: int):
        def call(x):
            write_frame(protocol, {"kind": "oracle", "index": index, "x": pack(x)})
            reply = read_frame(reader)
            if reply.get("kind") == "exception":
                cls = getattr(builtins, reply.get("type", ""), RuntimeError)
                if not isinstance(cls, type) or not issubclass(cls, Exception):
                    cls = RuntimeError
                error = cls("Objective or constraint evaluation failed.")
                oracle_errors.append((error, reply.get("oracle_error_id")))
                raise error
            if reply.get("kind") != "value":
                raise RuntimeError("Invalid oracle reply.")
            return unpack(reply["value"])
        return call

    try:
        args = [
            oracle(i) if raw == {"oracle": True} else unpack(raw)
            for i, raw in enumerate(initial["args"])
        ]
        root, file, function = Path("/candidate"), sys.argv[1], sys.argv[2]
        # A real package namespace supports relative and lazy repository imports.
        sys.path.insert(0, str(root.parent))
        module_name = "candidate." + ".".join(Path(file).with_suffix("").parts)
        sys.path.insert(0, str(root))
        spec = importlib.util.spec_from_file_location(module_name, root / file)
        if spec is None or spec.loader is None:
            raise ImportError("Cannot load solver entrypoint.")
        importlib.import_module(module_name.rpartition(".")[0])
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        solver = getattr(module, function)
        value = solver(*args)
        write_frame(protocol, {"kind": "return", "value": None if value is None else pack(value)})
        return 0
    except Exception as exc:
        # Private error details stay in the candidate log, not the scorer state.
        import traceback
        traceback.print_exc(file=sys.stderr)
        # Only the same exception object preserves oracle provenance. A solver
        # catching a callback failure then crashing later must not replay it.
        error_id = next((token for error, token in oracle_errors if error is exc), None)
        write_frame(protocol, {"kind": "error", "error": "Solver raised an exception.",
                               "oracle_error_id": error_id})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
