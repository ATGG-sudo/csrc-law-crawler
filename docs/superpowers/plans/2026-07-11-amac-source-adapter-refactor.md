# AMAC Source Adapter Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans or superpowers:subagent-driven-development to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for
> tracking.

**Goal:** Split AMAC source-adapter logic out of `amac_crawl.py` into package
modules while preserving CLI behavior and legacy imports.

**Architecture:** Keep `amac_crawl.py` as the root CLI and compatibility
module. Move cohesive source-adapter logic into
`csrc_law_crawler.sources.amac.identity`, `client`, `discovery`, `parser`, and
`pipeline`; re-export stable names from `csrc_law_crawler.sources.amac`.

**Tech Stack:** Python 3.10+, stdlib pathlib/hashlib/html/re/random/time,
existing project HTTP/storage/runtime utilities, BeautifulSoup, pytest, ruff,
mypy.

## Global Constraints

- Preserve root-level `amac_crawl.py` and all existing CLI flags.
- Preserve existing imports from `amac_crawl.py`.
- Do not change output paths or source record schema.
- Do not add runtime dependencies.
- Add tests before production code changes.

---

### Task 1: Add Package Boundary Tests

**Files:**
- Create: `tests/test_amac_package.py`

**Interfaces:**
- Consumes imports from `csrc_law_crawler.sources.amac.identity`,
  `discovery`, `parser`, and `pipeline`.
- Produces failing tests that require real package modules instead of root
  script re-exports.

- [ ] **Step 1: Write failing tests**
  - Verify `canonical_url` and `source_record_id` are importable from
    `identity.py`.
  - Verify xwfb title filtering and list discovery are importable from
    `discovery.py`.
  - Verify `asset_links` is importable from `parser.py`.
  - Verify `crawl_candidate` is importable from `pipeline.py` and handles an
    HTML page without asset downloads.

- [ ] **Step 2: Run test to confirm failure**
  - Run: `.venv/bin/python -m pytest tests/test_amac_package.py -q`
  - Expected: import failure because the package modules do not exist yet.

### Task 2: Extract Identity Helpers

**Files:**
- Create: `csrc_law_crawler/sources/amac/identity.py`
- Modify: `amac_crawl.py`
- Modify: `csrc_law_crawler/sources/amac/__init__.py`

**Interfaces:**
- Produces:
  - `canonical_url(url: str) -> str`
  - `source_record_id(url: str) -> str`
  - `clean_text(value: str) -> str`
  - `clean_attachment_title(value: str) -> str`
  - `classify_document(title: str, url: str) -> str`
  - `classified_document_metadata(title: str, url: str) -> dict[str, str]`

- [ ] Move URL, ID, text cleanup, attachment title, and document
  classification helpers into `identity.py`.
- [ ] Keep private legacy aliases in `amac_crawl.py` for
  `_clean_text`, `_clean_attachment_title`, and
  `_classified_document_metadata`.

### Task 3: Extract Client and Discovery

**Files:**
- Modify: `csrc_law_crawler/sources/amac/client.py`
- Create: `csrc_law_crawler/sources/amac/discovery.py`
- Modify: `amac_crawl.py`

**Interfaces:**
- Produces:
  - `AmacClient`
  - `discover_policy_candidates`
  - `discover_site_candidates`
  - `discover_xwfb_rule_notice_candidates`
  - `deduplicate_candidates`
  - `is_xwfb_rule_notice_title`
  - default keyword and xwfb constants

- [ ] Move `AmacClient` into package `client.py`.
- [ ] Move discovery constants and functions into `discovery.py`.
- [ ] Re-export `random` and `time` from `amac_crawl.py` using the same module
  objects as `client.py` so existing tests that patch root aliases remain
  effective.

### Task 4: Extract Parser and Pipeline

**Files:**
- Create: `csrc_law_crawler/sources/amac/parser.py`
- Create: `csrc_law_crawler/sources/amac/pipeline.py`
- Modify: `amac_crawl.py`
- Modify: `csrc_law_crawler/sources/amac/__init__.py`

**Interfaces:**
- Produces:
  - `content_root`
  - `title_from_page`
  - `metadata_from_page`
  - `asset_links`
  - `amac_assets_root`
  - `amac_manifest_path`
  - `crawl_candidate`
  - `crawl_amac`

- [ ] Move parser helpers into `parser.py`.
- [ ] Move asset download, candidate crawl, and full AMAC crawl orchestration
  into `pipeline.py`.
- [ ] Preserve root private aliases `_content_root`, `_title_from_page`,
  `_metadata_from_page`, `_asset_links`, `_extract_asset_text`, and
  `_download_asset`.

### Task 5: Verify and Commit

**Files:**
- All touched source, tests, and docs.

- [ ] Run AMAC-focused tests:
  - `.venv/bin/python -m pytest tests/test_amac_package.py tests/test_core.py::AmacDiscoveryTests -q`
- [ ] Run full verification:
  - `.venv/bin/python -m compileall -q .`
  - `.venv/bin/python -m ruff check .`
  - `.venv/bin/python -m pytest -q`
  - `.venv/bin/python -m mypy *.py tests`
  - `git diff --check`
- [ ] Review `git diff --stat` and commit with a focused message.
