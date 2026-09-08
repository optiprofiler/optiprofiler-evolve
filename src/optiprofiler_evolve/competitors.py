"""Trusted OptiProfiler-compatible DFO competitor registry.

The registry describes optional competitors without importing them into the
controller process. A selected competitor is materialized as a private solver
tree and executed only by the evaluator boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .solver import InterfaceSpec


_PTYPES = ("u", "b", "l", "n")


@dataclass(frozen=True)
class CompetitorSpec:
    name: str
    family: str
    method: str | None
    supported_ptypes: tuple[str, ...]
    required_module: str
    description: str


_SPECS = (
    CompetitorSpec(
        "scipy_nelder_mead",
        "scipy",
        "Nelder-Mead",
        ("u",),
        "scipy",
        "SciPy Nelder-Mead simplex method.",
    ),
    CompetitorSpec(
        "scipy_powell",
        "scipy",
        "Powell",
        ("u", "b"),
        "scipy",
        "SciPy modified Powell direction-set method.",
    ),
    CompetitorSpec(
        "scipy_cobyla",
        "scipy",
        "COBYLA",
        _PTYPES,
        "scipy",
        "SciPy COBYLA with OptiProfiler constraint dispatch.",
    ),
    CompetitorSpec(
        "scipy_cobyqa",
        "scipy",
        "COBYQA",
        _PTYPES,
        "scipy",
        "SciPy COBYQA with OptiProfiler constraint dispatch.",
    ),
    CompetitorSpec("pdfo", "pdfo", None, _PTYPES, "pdfo", "PDFO automatic method selection."),
    CompetitorSpec("pdfo_uobyqa", "pdfo", "uobyqa", ("u",), "pdfo", "PDFO UOBYQA."),
    CompetitorSpec("pdfo_newuoa", "pdfo", "newuoa", ("u",), "pdfo", "PDFO NEWUOA."),
    CompetitorSpec("pdfo_bobyqa", "pdfo", "bobyqa", ("u", "b"), "pdfo", "PDFO BOBYQA."),
    CompetitorSpec(
        "pdfo_lincoa", "pdfo", "lincoa", ("u", "b", "l"), "pdfo", "PDFO LINCOA."
    ),
    CompetitorSpec("pdfo_cobyla", "pdfo", "cobyla", _PTYPES, "pdfo", "PDFO COBYLA."),
    CompetitorSpec(
        "prima", "prima", None, _PTYPES, "prima", "Independent PRIMA automatic method selection."
    ),
    CompetitorSpec(
        "prima_uobyqa", "prima", "uobyqa", ("u",), "prima", "Independent PRIMA UOBYQA."
    ),
    CompetitorSpec(
        "prima_newuoa", "prima", "newuoa", ("u",), "prima", "Independent PRIMA NEWUOA."
    ),
    CompetitorSpec(
        "prima_bobyqa", "prima", "bobyqa", ("u", "b"), "prima", "Independent PRIMA BOBYQA."
    ),
    CompetitorSpec(
        "prima_lincoa",
        "prima",
        "lincoa",
        ("u", "b", "l"),
        "prima",
        "Independent PRIMA LINCOA.",
    ),
    CompetitorSpec(
        "prima_cobyla", "prima", "cobyla", _PTYPES, "prima", "Independent PRIMA COBYLA."
    ),
)
_REGISTRY = {spec.name: spec for spec in _SPECS}


def competitor_names(ptype: str | None = None) -> tuple[str, ...]:
    """Return stable registry names, optionally filtered by problem type."""

    if ptype is not None and ptype not in _PTYPES:
        raise ValueError(f"Unknown OptiProfiler problem type: {ptype!r}")
    return tuple(
        spec.name
        for spec in _SPECS
        if ptype is None or ptype in spec.supported_ptypes
    )


def get_competitor(name: str) -> CompetitorSpec:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        available = ", ".join(competitor_names())
        raise KeyError(f"Unknown competitor {name!r}; available: {available}") from exc


def materialize_competitor(
    name: str,
    *,
    destination: Path,
    interface: InterfaceSpec,
    max_eval_factor: float,
    ptype: str | None = None,
) -> Path:
    """Create one private competitor tree with the exact solver interface."""

    spec = get_competitor(name)
    if ptype is not None and ptype not in spec.supported_ptypes:
        raise ValueError(
            f"Competitor {name!r} does not support problem type {ptype!r}; "
            f"supported: {spec.supported_ptypes!r}."
        )
    if max_eval_factor <= 0:
        raise ValueError("max_eval_factor must be positive")
    if Path(interface.file).suffix.lower() != ".py":
        raise ValueError("The Python competitor registry requires a .py interface file.")
    entrypoint = destination / interface.file
    entrypoint.parent.mkdir(parents=True, exist_ok=False)
    entrypoint.write_text(
        _render_source(spec, interface.function, float(max_eval_factor)),
        encoding="utf-8",
    )
    return destination


def _render_source(spec: CompetitorSpec, function_name: str, factor: float) -> str:
    if spec.family == "scipy":
        return _scipy_source(spec, function_name, factor)
    if spec.family in {"pdfo", "prima"}:
        return _powell_family_source(spec, function_name, factor)
    raise AssertionError(f"Unhandled competitor family: {spec.family}")


def _shared_header(spec: CompetitorSpec, factor: float) -> str:
    return f'''"""Generated trusted competitor: {spec.name}."""
from __future__ import annotations

import numpy as np

_MAX_EVAL_FACTOR = {factor!r}
_SUPPORTED = {spec.supported_ptypes!r}


def _ptype(args):
    mapping = {{0: "u", 2: "b", 6: "l", 8: "n"}}
    try:
        value = mapping[len(args)]
    except KeyError as exc:
        raise TypeError("OptiProfiler solver expects 0, 2, 6, or 8 extra arguments") from exc
    if value not in _SUPPORTED:
        raise ValueError("{spec.name} does not support problem type " + value)
    return value
'''


def _scipy_source(spec: CompetitorSpec, function_name: str, factor: float) -> str:
    method = spec.method
    assert method is not None
    budget_option = "maxiter" if method == "COBYLA" else "maxfev"
    equality = (
        '''            constraints.append(LinearConstraint(aeq, -np.inf, beq))
            constraints.append(LinearConstraint(-aeq, -np.inf, -beq))'''
        if method == "COBYLA"
        else '''            constraints.append(LinearConstraint(aeq, beq, beq))'''
    )
    return _shared_header(spec, factor) + f'''

from scipy.optimize import Bounds, LinearConstraint, NonlinearConstraint, minimize


def {function_name}(fun, x0, *args):
    ptype = _ptype(args)
    x0 = np.asarray(x0, dtype=float).reshape(-1)
    budget = max(1, int(np.ceil(_MAX_EVAL_FACTOR * x0.size)))
    kwargs = {{"method": {method!r}, "options": {{{budget_option!r}: budget}}}}
    if ptype != "u":
        kwargs["bounds"] = Bounds(np.asarray(args[0]), np.asarray(args[1]))
    constraints = []
    if ptype in {{"l", "n"}}:
        aub, bub, aeq, beq = args[2:6]
        if np.asarray(aub).size:
            constraints.append(LinearConstraint(aub, -np.inf, bub))
        if np.asarray(aeq).size:
{equality}
    if ptype == "n":
        cub, ceq = args[6:8]
        constraints.append({{"type": "ineq", "fun": lambda x, f=cub: -np.asarray(f(x))}})
        constraints.append({{"type": "eq", "fun": ceq}})
    if constraints:
        kwargs["constraints"] = constraints
    return np.asarray(minimize(fun, x0, **kwargs).x, dtype=float)
'''


def _powell_family_source(spec: CompetitorSpec, function_name: str, factor: float) -> str:
    method_line = "" if spec.method is None else f'    kwargs["method"] = {spec.method!r}\n'
    if spec.family == "prima":
        imported = ["minimize"]
        if "l" in spec.supported_ptypes or "n" in spec.supported_ptypes:
            imported.append("LinearConstraint")
        if "n" in spec.supported_ptypes:
            imported.append("NonlinearConstraint")
        imports = f"from prima import {', '.join(imported)}"
        call = "minimize(fun, x0, **kwargs)"
        bounds = "list(zip(np.asarray(args[0]), np.asarray(args[1])))"
        nonlinear = '''        constraints.append(NonlinearConstraint(cub, -np.inf, 0.0))
        constraints.append(NonlinearConstraint(ceq, 0.0, 0.0))'''
    else:
        scipy_imports = []
        if "b" in spec.supported_ptypes:
            scipy_imports.append("Bounds")
        if "l" in spec.supported_ptypes or "n" in spec.supported_ptypes:
            scipy_imports.append("LinearConstraint")
        if "n" in spec.supported_ptypes:
            scipy_imports.append("NonlinearConstraint")
        imports = "from pdfo import pdfo"
        if scipy_imports:
            imports += f"\nfrom scipy.optimize import {', '.join(scipy_imports)}"
        call = "pdfo(fun, x0, **kwargs)"
        bounds = "Bounds(np.asarray(args[0]), np.asarray(args[1]))"
        nonlinear = '''        constraints.append({"type": "ineq", "fun": lambda x, f=cub: -np.asarray(f(x))})
        constraints.append({"type": "eq", "fun": ceq})'''
    return _shared_header(spec, factor) + f'''

import warnings
{imports}


def {function_name}(fun, x0, *args):
    ptype = _ptype(args)
    x0 = np.asarray(x0, dtype=float).reshape(-1)
    maxfev = max(1, int(np.ceil(_MAX_EVAL_FACTOR * x0.size)))
    kwargs = {{"options": {{"maxfev": maxfev, "quiet": True}}}}
{method_line}    if ptype != "u":
        kwargs["bounds"] = {bounds}
    constraints = []
    if ptype in {{"l", "n"}}:
        aub, bub, aeq, beq = args[2:6]
        if np.asarray(aub).size:
            constraints.append(LinearConstraint(aub, -np.inf, bub))
        if np.asarray(aeq).size:
            constraints.append(LinearConstraint(aeq, beq, beq))
    if ptype == "n":
        cub, ceq = args[6:8]
{nonlinear}
    if constraints:
        kwargs["constraints"] = constraints
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = {call}
    return np.asarray(result.x, dtype=float)
'''


__all__: list[str] = []
