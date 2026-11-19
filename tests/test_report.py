"""Tests for risk scoring and ground-truth comparison."""

from __future__ import annotations

import unittest

from skewlens.report import assess, rank, verify


def column(**overrides):
    base = {
        "column": "user_id",
        "count": 1000,
        "nulls": 0,
        "coverage": 1.0,
        "types": {"string": 1000},
        "distinct": 500,
        "distinct_method": "hll",
        "distinct_ratio": 0.5,
        "top": [{"value": "u_1", "count": 100, "share": 0.10}],
        "numeric": None,
    }
    base.update(overrides)
    return base


class ScoringTests(unittest.TestCase):
    def test_skew_dominates_the_score(self) -> None:
        risk = assess(column(top=[{"value": "u_1", "count": 400, "share": 0.40}]))
        self.assertAlmostEqual(risk.components["skew"], 1.0)
        self.assertEqual(risk.grade, "critical")
        self.assertIn("most frequent value", risk.reasons[0])

    def test_nulls_are_scored_independently(self) -> None:
        risk = assess(column(nulls=850, types={"string": 150, "null": 850}, top=[]))
        self.assertAlmostEqual(risk.components["nulls"], 1.0)
        self.assertAlmostEqual(risk.components["skew"], 0.0)
        self.assertIn("null", risk.reasons[0])

    def test_numeric_columns_skip_cardinality(self) -> None:
        risk = assess(column(column="amount", types={"number": 1000}, distinct_ratio=1.0, top=[]))
        self.assertEqual(risk.components["cardinality"], 0.0)
        self.assertEqual(risk.grade, "low")

    def test_text_columns_with_unique_values_get_cardinality_risk(self) -> None:
        risk = assess(column(column="session_id", distinct_ratio=1.0, top=[]))
        self.assertAlmostEqual(risk.components["cardinality"], 1.0)

    def test_coverage_gap_is_type_drift(self) -> None:
        risk = assess(column(coverage=0.80, top=[]))
        self.assertEqual(risk.components["drift"], 1.0)
        self.assertTrue(any("appears in only" in reason for reason in risk.reasons))

    def test_rank_sorts_descending(self) -> None:
        risks = rank(
            {
                "columns": [
                    column(column="a", top=[]),
                    column(column="b", top=[{"value": "x", "count": 500, "share": 0.5}]),
                ]
            }
        )
        self.assertEqual(risks[0].column, "b")


class VerificationTests(unittest.TestCase):
    def test_reports_relative_error_and_top_match(self) -> None:
        profile = {
            "columns": [
                {
                    "column": "user_id",
                    "distinct": 102,
                    "distinct_method": "hll",
                    "top": [{"value": "u_1", "count": 10, "share": 0.10}],
                }
            ]
        }
        truth = {
            "columns": {
                "user_id": {"distinct": 100, "top": [{"value": "u_1", "count": 10, "share": 0.10}]}
            }
        }
        checks = verify(profile, truth)
        self.assertEqual(len(checks), 1)
        self.assertAlmostEqual(checks[0]["relative_error"], 0.02)
        self.assertTrue(checks[0]["top_value_matches"])

    def test_columns_without_exact_distinct_are_skipped(self) -> None:
        profile = {"columns": [{"column": "amount", "distinct": 900}]}
        truth = {"columns": {"amount": {"distinct": None}}}
        self.assertEqual(verify(profile, truth), [])


if __name__ == "__main__":
    unittest.main()
