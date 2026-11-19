"""Tests for synthetic generation and ground truth."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from skewlens.synth import generate


class SynthTests(unittest.TestCase):
    def test_generates_rows_and_matching_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data.jsonl"
            truth_path = Path(tmp) / "truth.json"
            truth = generate(2000, data, truth_path, seed=3)

            lines = data.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2000)
            self.assertEqual(truth.rows, 2000)

            users = Counter()
            countries = Counter()
            nulls = 0
            for line in lines:
                record = json.loads(line)
                users[record["user_id"]] += 1
                countries[record["country"]] += 1
                if record["referrer"] is None:
                    nulls += 1

            self.assertEqual(truth.columns["user_id"]["distinct"], len(users))
            self.assertEqual(truth.columns["country"]["distinct"], len(countries))
            self.assertEqual(truth.columns["referrer"]["nulls"], nulls)
            self.assertEqual(truth.columns["session_id"]["distinct"], 2000)

            stored = json.loads(truth_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["rows"], 2000)
            self.assertEqual(stored["schema_version"], 1)

    def test_same_seed_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.jsonl"
            second = Path(tmp) / "b.jsonl"
            generate(500, first, None, seed=5)
            generate(500, second, None, seed=5)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_rejects_empty_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            generate(0, Path(tmp) / "x.jsonl", None)


if __name__ == "__main__":
    unittest.main()
