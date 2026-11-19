"""Synthetic dataset generation with exact ground truth.

The generator exists so that every number the scanner reports can be checked
against a known answer. It produces a Zipf-skewed key, a low-cardinality
categorical, a near-unique identifier, a numeric measure, and a mostly-null
field -- the five shapes that decide whether a join or a group-by will hurt.
"""

from __future__ import annotations

import itertools
import json
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

USERS = 50_000
COUNTRIES = 40
ZIPF_EXPONENT = 1.1
NULL_RATE = 0.85


@dataclass
class Truth:
    """Exact facts about a generated dataset, recorded while generating it."""

    rows: int = 0
    seed: int = 0
    path: str = ""
    bytes: int = 0
    columns: dict[str, dict[str, object]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "rows": self.rows,
            "seed": self.seed,
            "path": self.path,
            "bytes": self.bytes,
            "columns": self.columns,
        }


def _summarise(counter: dict[str, int], rows: int, nulls: int) -> dict[str, object]:
    top = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:5]
    return {
        "distinct": len(counter),
        "nulls": nulls,
        "null_rate": round(nulls / rows, 6) if rows else 0.0,
        "top": [
            {"value": value, "count": count, "share": round(count / rows, 6)}
            for value, count in top
        ],
    }


def generate(
    rows: int,
    destination: Path,
    truth_path: Path | None = None,
    seed: int = 11,
) -> Truth:
    """Write ``rows`` JSONL records and return their exact statistics."""
    if rows <= 0:
        raise ValueError("rows must be positive")

    rng = random.Random(seed)
    users = [f"u_{index:05d}" for index in range(USERS)]
    weights = [1.0 / (index + 1) ** ZIPF_EXPONENT for index in range(USERS)]
    cumulative = list(itertools.accumulate(weights))
    countries = [f"C{index:02d}" for index in range(COUNTRIES)]

    picked_users = rng.choices(users, cum_weights=cumulative, k=rows)
    picked_countries = rng.choices(countries, k=rows)
    picked_sessions = [uuid.UUID(int=rng.getrandbits(128)).hex for _ in range(rows)]
    amounts = [round(rng.lognormvariate(4.6, 0.7), 2) for _ in range(rows)]
    referrers = [
        None if rng.random() < NULL_RATE else f"ref{rng.randrange(12)}" for _ in range(rows)
    ]

    user_counts: dict[str, int] = {}
    country_counts: dict[str, int] = {}
    referrer_counts: dict[str, int] = {}
    amount_sum = 0.0
    amount_square = 0.0

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        for index in range(rows):
            record: dict[str, Any] = {
                "user_id": picked_users[index],
                "country": picked_countries[index],
                "session_id": picked_sessions[index],
                "amount": amounts[index],
                "referrer": referrers[index],
            }
            handle.write(json.dumps(record, separators=(",", ":")))
            handle.write("\n")

            user = record["user_id"]
            user_counts[user] = user_counts.get(user, 0) + 1
            country = record["country"]
            country_counts[country] = country_counts.get(country, 0) + 1
            referrer = record["referrer"]
            if referrer is not None:
                referrer_counts[referrer] = referrer_counts.get(referrer, 0) + 1
            amount = record["amount"]
            amount_sum += amount
            amount_square += amount * amount

    truth = Truth(rows=rows, seed=seed, path=destination.name)
    truth.bytes = destination.stat().st_size
    truth.columns = {
        "user_id": _summarise(user_counts, rows, 0),
        "country": _summarise(country_counts, rows, 0),
        "session_id": {
            "distinct": rows,
            "nulls": 0,
            "null_rate": 0.0,
            "top": [],
            "note": "one UUID per row, distinct equals row count by construction",
        },
        "amount": {
            "distinct": None,
            "nulls": 0,
            "null_rate": 0.0,
            "mean": round(amount_sum / rows, 4),
            "std": round(max(0.0, amount_square / rows - (amount_sum / rows) ** 2) ** 0.5, 4),
            "top": [],
        },
        "referrer": _summarise(referrer_counts, rows, rows - sum(referrer_counts.values())),
    }

    if truth_path is not None:
        truth_path.parent.mkdir(parents=True, exist_ok=True)
        truth_path.write_text(
            json.dumps(truth.as_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return truth
