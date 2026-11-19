"""Risk scoring and report rendering.

A profile tells you what a column looks like; a report tells you which column
will hurt first. Every component is normalised to 0..1 with a documented
saturation point, then combined into a weighted score:

===========  ======  ==============================================
Component    Weight  Saturates at
===========  ======  ==============================================
skew         0.55    one value holding 10% of rows
nulls        0.25    a 50% null rate
cardinality  0.12    a distinct ratio of 1.0 (skipped for numbers)
drift        0.08    any coverage gap or mixed non-null types
===========  ======  ==============================================

Skew carries the largest weight because a hot join key is the failure this tool
exists to catch, and because a single extreme value can legitimately reach the
top grade on its own. A unique identifier, by contrast, is normal: high
cardinality is reported, but it cannot by itself push a column past "medium".

The weights are a judgement call, not a measurement. They are stated here so a
team can disagree with them explicitly instead of guessing what the number meant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WEIGHTS = {"skew": 0.55, "nulls": 0.25, "cardinality": 0.12, "drift": 0.08}
GRADES = ((0.55, "critical"), (0.33, "high"), (0.14, "medium"), (0.0, "low"))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _grade(score: float) -> str:
    for threshold, name in GRADES:
        if score >= threshold:
            return name
    return "low"


@dataclass
class Risk:
    column: str
    score: float
    grade: str
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "score": round(self.score, 4),
            "grade": self.grade,
            "components": {name: round(value, 4) for name, value in self.components.items()},
            "reasons": self.reasons,
        }


def assess(column: dict[str, Any]) -> Risk:
    """Score one column profile."""
    count = int(column.get("count") or 0)
    nulls = int(column.get("nulls") or 0)
    top = column.get("top") or []
    top_share = float(top[0]["share"]) if top else 0.0
    null_rate = nulls / count if count else 0.0
    distinct_ratio = float(column.get("distinct_ratio") or 0.0)
    types = column.get("types") or {}
    non_null_types = [name for name, value in types.items() if name != "null" and value]
    coverage = float(column.get("coverage") or 0.0)

    skew = _clamp((top_share - 0.01) / 0.09)
    nulls_score = _clamp((null_rate - 0.01) / 0.49)
    is_text = "string" in non_null_types
    cardinality = _clamp((distinct_ratio - 0.30) / 0.70) if is_text else 0.0
    drift = 1.0 if (coverage < 0.995 or len(non_null_types) > 1) else 0.0

    components = {
        "skew": skew,
        "nulls": nulls_score,
        "cardinality": cardinality,
        "drift": drift,
    }
    score = sum(WEIGHTS[name] * value for name, value in components.items())

    reasons: list[str] = []
    if skew > 0.05:
        reasons.append(f"the most frequent value holds {top_share:.1%} of rows")
    if nulls_score > 0.05:
        reasons.append(f"{null_rate:.1%} of rows are null")
    if cardinality > 0.05:
        reasons.append(
            f"distinct ratio is {distinct_ratio:.2f}, so group-by memory grows with rows"
        )
    if drift > 0:
        if coverage < 0.995:
            reasons.append(f"the column appears in only {coverage:.1%} of rows")
        if len(non_null_types) > 1:
            reasons.append("mixed types: " + ", ".join(sorted(non_null_types)))
    if not reasons:
        reasons.append("no structural risk detected")

    return Risk(
        column=str(column.get("column")),
        score=score,
        grade=_grade(score),
        components=components,
        reasons=reasons,
        profile=column,
    )


def rank(profile: dict[str, Any]) -> list[Risk]:
    columns = profile.get("columns") or []
    risks = [assess(column) for column in columns if isinstance(column, dict)]
    risks.sort(key=lambda item: (-item.score, item.column))
    return risks


def verify(profile: dict[str, Any], truth: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare scanner estimates with the generator's exact counts."""
    exact_columns = truth.get("columns") or {}
    rows: list[dict[str, Any]] = []
    for column in profile.get("columns") or []:
        name = str(column.get("column"))
        expected = exact_columns.get(name)
        if not isinstance(expected, dict):
            continue
        exact_distinct = expected.get("distinct")
        if exact_distinct is None:
            continue
        estimated = int(column.get("distinct") or 0)
        error = abs(estimated - int(exact_distinct)) / max(1, int(exact_distinct))
        row: dict[str, Any] = {
            "column": name,
            "exact_distinct": int(exact_distinct),
            "estimated_distinct": estimated,
            "relative_error": round(error, 4),
            "method": column.get("distinct_method"),
        }
        expected_top = expected.get("top") or []
        actual_top = column.get("top") or []
        if expected_top and actual_top:
            row["top_value_matches"] = expected_top[0]["value"] == actual_top[0]["value"]
            row["top_share_exact"] = expected_top[0]["share"]
            row["top_share_estimated"] = actual_top[0]["share"]
        rows.append(row)
    return rows


def _thousands(value: Any) -> str:
    return f"{int(value):,}"


def render_markdown(
    profile: dict[str, Any], risks: list[Risk], checks: list[dict[str, Any]]
) -> str:
    total_rows = int(profile.get("rows") or 0)
    size_mb = float(profile.get("bytes") or 0) / 1048576
    source = str(profile.get("input") or "").replace("\\", "/")
    lines = [
        "# skewlens report",
        "",
        f"- rows: {_thousands(total_rows)}",
        f"- file: {source} ({size_mb:.1f} MB)",
        f"- scan: {profile.get('elapsed_s')} s at {profile.get('mb_per_s')} MB/s "
        f"with {profile.get('workers')} worker(s)",
        f"- peak worker memory: {profile.get('peak_worker_rss_mb')} MB RSS, "
        f"{profile.get('peak_worker_heap_mb')} MB live heap",
        "",
        "## Risk ranking",
        "",
        "| # | column | score | grade | top share | null rate | distinct |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, risk in enumerate(risks, start=1):
        column = risk.profile
        top = column.get("top") or []
        share = f"{float(top[0]['share']):.1%}" if top else "-"
        count = int(column.get("count") or 0)
        null_rate = f"{int(column.get('nulls') or 0) / count:.1%}" if count else "-"
        lines.append(
            f"| {index} | `{risk.column}` | {risk.score:.2f} | {risk.grade} | "
            f"{share} | {null_rate} | {_thousands(column.get('distinct') or 0)} |"
        )

    lines.extend(["", "## Why", ""])
    for risk in risks[:3]:
        lines.append(f"**`{risk.column}` — {risk.grade} ({risk.score:.2f})**")
        for reason in risk.reasons:
            lines.append(f"- {reason}")
        lines.append("")

    if checks:
        lines.extend(
            [
                "## Accuracy against ground truth",
                "",
                "| column | exact distinct | estimated | error | method | top value matched |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for check in checks:
            matched = check.get("top_value_matches")
            lines.append(
                f"| `{check['column']}` | {_thousands(check['exact_distinct'])} | "
                f"{_thousands(check['estimated_distinct'])} | "
                f"{float(check['relative_error']):.2%} | {check['method']} | "
                f"{'-' if matched is None else ('yes' if matched else 'no')} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_reports(
    profile: dict[str, Any],
    truth: dict[str, Any] | None,
    markdown_path: Path,
    json_path: Path | None,
) -> list[Risk]:
    risks = rank(profile)
    checks = verify(profile, truth) if truth else []
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(
        render_markdown(profile, risks, checks), encoding="utf-8", newline="\n"
    )
    if json_path is not None:
        payload = {
            "schema_version": 1,
            "summary": {
                "rows": profile.get("rows"),
                "bytes": profile.get("bytes"),
                "elapsed_s": profile.get("elapsed_s"),
                "mb_per_s": profile.get("mb_per_s"),
                "workers": profile.get("workers"),
            },
            "risks": [risk.as_dict() for risk in risks],
            "checks": checks,
        }
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return risks
