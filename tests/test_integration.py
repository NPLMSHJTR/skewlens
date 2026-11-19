"""End-to-end test: generate, scan with Node, and verify against truth."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from skewlens.report import write_reports
from skewlens.scan import scan
from skewlens.synth import generate

HAS_NODE = shutil.which("node") is not None


@unittest.skipUnless(HAS_NODE, "node is required for the scanner")
class EndToEndTests(unittest.TestCase):
    def test_estimates_track_ground_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data.jsonl"
            truth_path = Path(tmp) / "truth.json"
            truth = generate(20_000, data, truth_path, seed=9)

            profile = scan(data, workers=2)
            self.assertEqual(profile["rows"], 20_000)
            self.assertGreater(float(profile["mb_per_s"]), 0.0)

            columns = {column["column"]: column for column in profile["columns"]}
            self.assertEqual(columns["session_id"]["distinct_method"], "hll")
            self.assertEqual(columns["country"]["distinct_method"], "exact")
            self.assertEqual(columns["country"]["distinct"], 40)

            exact = truth.columns["user_id"]["distinct"]
            estimated = columns["user_id"]["distinct"]
            self.assertLess(abs(estimated - exact) / exact, 0.05)

            expected_top = truth.columns["user_id"]["top"][0]
            actual_top = columns["user_id"]["top"][0]
            self.assertEqual(actual_top["value"], expected_top["value"])
            self.assertLess(
                abs(actual_top["share"] - expected_top["share"]) / expected_top["share"], 0.05
            )

            # ``country`` is generated uniformly over 40 values, so any top share
            # above 3% means the frequency sketch is colliding with itself.
            country_top = columns["country"]["top"][0]
            self.assertEqual(country_top["value"], truth.columns["country"]["top"][0]["value"])
            self.assertLess(country_top["share"], 0.03)

            report_path = Path(tmp) / "report.md"
            json_path = Path(tmp) / "report.json"
            risks = write_reports(profile, truth.as_dict(), report_path, json_path)
            self.assertTrue(risks)
            text = report_path.read_text(encoding="utf-8")
            self.assertIn("## Risk ranking", text)
            self.assertIn("## Accuracy against ground truth", text)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["rows"], 20_000)


if __name__ == "__main__":
    unittest.main()
