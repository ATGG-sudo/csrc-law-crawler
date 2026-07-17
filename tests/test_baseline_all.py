from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import baseline_all


class BaselineAllTests(unittest.TestCase):
    def test_incremental_verification_is_opt_in(self) -> None:
        calls: list[tuple[str, dict]] = []

        class Runner:
            def __init__(self, **kwargs) -> None:
                pass

            def run(self, *, mode: str, **kwargs) -> dict:
                calls.append((mode, kwargs))
                return {
                    "run_id": f"run-{mode}-{len(calls)}",
                    "status": "complete",
                    "endpoints": {},
                    "counts": {},
                }

        registry: dict[str, list[dict]] = {"endpoints": []}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(baseline_all, "output_dir", return_value=Path(tmp)),
            patch.object(baseline_all, "load_registry", return_value=registry),
            patch.object(baseline_all, "SourceRunner", Runner),
            patch.object(
                baseline_all,
                "load_json",
                return_value={"count": 0, "queryable_count": 0},
            ),
            patch.object(baseline_all, "publish_case_records", return_value={}),
            patch.object(baseline_all, "stage_and_publish_canonical", return_value={}),
            patch.object(baseline_all, "_mark_publication", side_effect=lambda report, *_: report),
            patch.object(baseline_all, "build_digest", return_value={"counts": {}}),
        ):
            baseline_only = baseline_all.run_baseline_all()
            self.assertEqual(["baseline"], [mode for mode, _ in calls])
            self.assertEqual("skipped", baseline_only["incremental_verification"])
            self.assertIsNone(baseline_only["incremental"])

            calls.clear()
            verified = baseline_all.run_baseline_all(verify_incremental=True)
            self.assertEqual(["baseline", "incremental"], [mode for mode, _ in calls])
            self.assertEqual("complete", verified["incremental_verification"])


if __name__ == "__main__":
    unittest.main()
