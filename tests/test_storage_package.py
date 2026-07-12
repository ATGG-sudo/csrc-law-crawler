from __future__ import annotations

import unittest

from csrc_law_crawler.core.checkpoints import load_checkpoint
from csrc_law_crawler.core.file_store import FileStore
from csrc_law_crawler.core.io import append_jsonl, load_json
from csrc_law_crawler.core.locking import strip_global_cli_options
from csrc_law_crawler.core.paths import output_dir, output_path, relative_to_output


class StoragePackageBoundaryTests(unittest.TestCase):
    def test_package_storage_modules_expose_core_helpers(self) -> None:
        self.assertEqual(
            output_dir() / "work/example.json",
            output_path("work/example.json"),
        )
        self.assertEqual(
            "work/example.json",
            relative_to_output(output_dir() / "work/example.json"),
        )
        self.assertEqual(
            ["crawl", "--limit", "1"],
            strip_global_cli_options(
                ["--output-root", "tmp/out", "crawl", "--limit", "1"]
            ),
        )
        self.assertIn("completed_ids", load_checkpoint())

    def test_file_store_uses_package_io_helpers(self) -> None:
        root = output_dir() / "tests/storage-package"
        path = root / "sample.json"
        log_path = root / "events.jsonl"
        store = FileStore(root)

        store.save_json_atomic(path, {"ok": True})
        append_jsonl(log_path, {"event": "saved"})

        self.assertEqual({"ok": True}, store.load_json(path, {}))
        self.assertEqual({"ok": True}, load_json(path, {}))
        self.assertEqual({"missing": True}, load_json(root / "missing.json", {"missing": True}))

        path.unlink(missing_ok=True)
        log_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
