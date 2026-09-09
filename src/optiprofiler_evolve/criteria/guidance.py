"""Compose criterion-owned instructions without executing an assessment."""

from dataclasses import replace

from optiprofiler_evolve.config import AssessmentConfig, CriterionConfig
from optiprofiler_evolve.controller.registry import resolve
from optiprofiler_evolve.criteria.contracts import GuidanceRequest


def build_generation_guidance(config: AssessmentConfig, feedback_mode: str) -> str:
    sections = []
    profiles = {name: config.resolve(name) for name in ("smoke", "public", "validation")}
    for name in config.criteria:
        selected = [(profile, specs[name]) for profile, specs in profiles.items() if specs[name].enabled]
        if not selected:
            continue
        criterion = resolve("criterion", name)
        groups: list[tuple[CriterionConfig, list[str]]] = []
        for profile, spec in selected:
            spec = replace(spec, options={key: spec.options[key] for key in criterion.guidance_fields
                                          if key in spec.options})
            for previous, names in groups:
                if spec == previous:
                    names.append(profile)
                    break
            else:
                groups.append((spec, [profile]))
        for spec, names in groups:
            request = GuidanceRequest(
                profiles=tuple(names),
                options=spec.options,
                weight=spec.weight, mode=spec.mode, feedback_mode=feedback_mode,
            )
            text = criterion.generation_guidance(request)
            sections.append(
                f"{name} ({', '.join(names)}; weight={spec.weight:g}; {spec.mode})\n{text}"
            )
    return (
        "Assessment criteria\n"
        "Each profile uses its own enabled standards and weights. The total is the weighted "
        "average of enabled criterion scores; higher is better, without a [0, 1] cap. "
        "Zero-weight checks still apply constraints. Enforce blocks unmet constraints; warn "
        "does not, but its score still contributes. Missing positive-weight evidence makes "
        "the total unavailable, not zero. Validation rules describe admission, not permission "
        "to access private evaluation data or results.\n\n" + "\n\n".join(sections)
    )
