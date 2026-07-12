# Storage Core and Live Web Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move storage implementation into package modules and add opt-in live webpage tests for crawler stability.

**Architecture:** Keep root-level `storage.py` as a compatibility entrypoint. Implement focused package modules under `csrc_law_crawler.core` for paths, locking, IO, checkpoint/file iteration, and `FileStore`; add live tests that are skipped unless `CSRC_LIVE_WEB_TESTS=1`.

**Tech Stack:** Python 3.10+, stdlib pathlib/json/os/sys/contextlib/datetime, existing config/runtime/failure_taxonomy modules, pytest, requests/BeautifulSoup through existing AMAC source adapter.

## Global Constraints

- Preserve all existing root-level `storage.py` imports.
- Preserve output paths, manifest formats, checkpoint formats, and lock behavior.
- Do not run live web tests by default.
- Do not add runtime dependencies.
- Use TDD: package-boundary tests before moving production code.

---

### Task 1: Add Storage Package Boundary Tests

**Files:**
- Create: `tests/test_storage_package.py`

**Interfaces:**
- Consumes package imports from:
  - `csrc_law_crawler.core.paths`
  - `csrc_law_crawler.core.io`
  - `csrc_law_crawler.core.locking`
  - `csrc_law_crawler.core.file_store`
  - `csrc_law_crawler.core.checkpoints`
- Produces a failing test proving storage implementation has not yet moved.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

import unittest
from pathlib import Path

from csrc_law_crawler.core.checkpoints import load_checkpoint
from csrc_law_crawler.core.file_store import FileStore
from csrc_law_crawler.core.io import append_jsonl, load_json, save_json
from csrc_law_crawler.core.locking import strip_global_cli_options
from csrc_law_crawler.core.paths import output_path, relative_to_output


class StoragePackageBoundaryTests(unittest.TestCase):
    def test_package_storage_modules_expose_core_helpers(self) -> None:
        self.assertEqual(Path("output/work/example.json"), output_path("work/example.json"))
        self.assertEqual("work/example.json", relative_to_output(Path("output/work/example.json")))
        self.assertEqual(
            ["crawl", "--limit", "1"],
            strip_global_cli_options(["--output-root", "tmp/out", "crawl", "--limit", "1"]),
        )
        self.assertIn("completed_ids", load_checkpoint())

    def test_file_store_uses_package_io_helpers(self) -> None:
        root = Path("output/tests/storage-package")
        path = root / "sample.json"
        log_path = root / "events.jsonl"
        store = FileStore(root)

        store.save_json_atomic(path, {"ok": True})
        append_jsonl(log_path, {"event": "saved"})

        self.assertEqual({"ok": True}, store.load_json(path, {}))
        self.assertEqual({"ok": True}, load_json(path, {}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_storage_package.py -q`
Expected: FAIL with `ModuleNotFoundError` for one or more new package modules.

### Task 2: Extract Paths and Locking

**Files:**
- Create: `csrc_law_crawler/core/paths.py`
- Create: `csrc_law_crawler/core/locking.py`
- Modify: `storage.py`

**Interfaces:**
- Produces path helpers with the same names currently exported by `storage.py`.
- Produces locking/context helpers:
  - `strip_global_cli_options(argv: list[str]) -> list[str]`
  - `acquire_output_lock(reason: str = "write") -> Iterator[None]`
  - `run_with_output_lock(main: Any, reason: str) -> int`
  - `run_with_context(main: Any, reason: str) -> int`
  - `lock_depth() -> int`

- [ ] Move constants and path helpers into `paths.py`.
- [ ] Move global CLI option stripping, lock acquisition, and run-context wrapping into `locking.py`.
- [ ] Keep `storage.py` aliases for all moved names.

### Task 3: Extract IO, FileStore, and Checkpoints

**Files:**
- Create: `csrc_law_crawler/core/io.py`
- Create: `csrc_law_crawler/core/file_store.py`
- Create: `csrc_law_crawler/core/checkpoints.py`
- Modify: `storage.py`
- Modify: `csrc_law_crawler/core/storage.py`
- Modify: `csrc_law_crawler/core/__init__.py`

**Interfaces:**
- Produces:
  - `load_json(path: Path, default: Any) -> Any`
  - `listed_output_files(...) -> list[Path]`
  - `save_json(path: Path, data: Any) -> None`
  - `append_jsonl(path: Path, item: Any) -> None`
  - `publish_json_bundle(documents: dict[Path, Any]) -> None`
  - `FileStore`
  - checkpoint and source-file iterator helpers

- [ ] Move JSON/JSONL/publish helpers into `io.py`.
- [ ] Move `FileStore` into `file_store.py`.
- [ ] Move checkpoint, source-file iterator, and metadata helpers into `checkpoints.py`.
- [ ] Update package exports and root compatibility exports.

### Task 4: Add Opt-In Live Web Tests

**Files:**
- Create: `tests/test_live_web_crawling.py`

**Interfaces:**
- Consumes:
  - `csrc_law_crawler.sources.amac.client.AmacClient`
  - `csrc_law_crawler.sources.amac.discovery.discover_xwfb_rule_notice_candidates`
  - `csrc_law_crawler.sources.amac.parser.content_root`

- [ ] Add pytest tests gated by `CSRC_LIVE_WEB_TESTS=1`.
- [ ] Fetch a real AMAC list page and assert article anchors are present.
- [ ] Run AMAC xwfb discovery against the real site with `max_pages=1` and assert it returns a list without crashing.
- [ ] Fetch a known AMAC article page and assert parser extracts title/body text when the page remains available.

### Task 5: Verify and Commit

**Files:**
- All touched source, tests, and docs.

- [ ] Run targeted tests:
  - `.venv/bin/python -m pytest tests/test_storage_package.py -q`
  - `.venv/bin/python -m pytest tests/test_live_web_crawling.py -q`
- [ ] Run explicit live tests:
  - `CSRC_LIVE_WEB_TESTS=1 .venv/bin/python -m pytest tests/test_live_web_crawling.py -q`
- [ ] Run full verification:
  - `.venv/bin/python -m compileall -q .`
  - `.venv/bin/python -m ruff check .`
  - `.venv/bin/python -m pytest -q`
  - `.venv/bin/python -m mypy *.py tests`
  - `git diff --check`
- [ ] Review `git diff --stat` and commit with `refactor: split storage core modules`.
