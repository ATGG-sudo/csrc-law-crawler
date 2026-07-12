# Storage Core and Live Web Tests Design

## Context

After the catalog builder and AMAC source adapter splits, the next shared
refactor target is `storage.py`. It is used across almost every pipeline stage,
but it still owns unrelated responsibilities in one root-level module:

- global CLI option stripping
- output locking and run-context wrapping
- output path factories
- JSON and JSONL persistence
- manifest-backed file listing
- checkpoint helpers
- `FileStore`
- bundle publishing rollback behavior

The package namespace already exposes `csrc_law_crawler.core.storage`, but it
is only a root-module re-export. This keeps package consumers tied to the
legacy script layout.

The user also asked for real webpage tests to keep crawling stable. Those tests
should exercise live official pages without making the default test suite flaky
or dependent on public website uptime.

## Goals

- Move storage implementation into focused package modules under
  `csrc_law_crawler.core`.
- Keep root-level `storage.py` as a compatibility entrypoint for existing
  scripts and tests.
- Preserve all existing function names, paths, output schemas, and lock
  behavior.
- Add package-boundary tests for the new storage modules.
- Add opt-in live webpage tests that fetch real AMAC pages and verify the
  parser/discovery surface still handles real HTML.
- Keep the default test suite offline and deterministic.

## Non-Goals

- Do not change `OUTPUT_DIR`, subdirectory names, manifest formats, or
  checkpoint formats.
- Do not rewrite crawler network policy in this refactor.
- Do not make live tests run by default.
- Do not remove root-level `storage.py`.
- Do not add new runtime dependencies.

## Proposed Architecture

Create focused core modules:

- `csrc_law_crawler.core.paths`
  - `utc_now_iso`
  - output-relative path factories
  - checkpoint, manifest, relation, source, and catalog path factories

- `csrc_law_crawler.core.io`
  - `load_json`
  - `listed_output_files`
  - `save_json`
  - `append_jsonl`
  - `publish_json_bundle`

- `csrc_law_crawler.core.locking`
  - `strip_global_cli_options`
  - `acquire_output_lock`
  - `run_with_output_lock`
  - `run_with_context`

- `csrc_law_crawler.core.file_store`
  - `FileStore`

- `csrc_law_crawler.core.checkpoints`
  - checkpoint load/save
  - source file iterators
  - metadata convenience helpers

`storage.py` will import and re-export these names. Existing imports from
`storage` continue to work; new code can import package modules directly.

## Live Web Testing Design

Add `tests/test_live_web_crawling.py` with pytest `skipif` gating:

- Tests run only when `CSRC_LIVE_WEB_TESTS=1`.
- Tests use short zero-delay clients and real official AMAC URLs.
- Tests assert stable structural signals rather than volatile content:
  - AMAC xwfb list page returns HTML with article anchors.
  - The discovery helper can process the real list page without crashing.
  - A known public AMAC article page can be fetched and parsed into a title or
    body text when available.
- If official pages are unreachable, the live run fails loudly; default offline
  runs remain unaffected.

## Testing

Use TDD:

1. Add storage package-boundary tests that import from the new package modules.
2. Run them and confirm they fail before modules exist.
3. Move implementation from `storage.py` into package modules.
4. Add opt-in live webpage tests.
5. Run targeted storage tests, default full tests, and one explicit live-web
   test command if network access is available.

## Compatibility

- `from storage import save_json`, `run_with_output_lock`, and all existing
  helpers continue to work.
- `from csrc_law_crawler.core import FileStore` continues to work.
- `from csrc_law_crawler.core.storage import save_json` continues to work.
- Package users can import focused modules such as
  `csrc_law_crawler.core.paths.output_path`.

## Risks

- Circular imports are the main risk. Mitigation: package modules must not
  import root-level `storage.py`; the root module imports package modules.
- Lock-depth state must stay shared between IO and locking. Mitigation:
  `_LOCK_DEPTH` lives in `locking.py`, and `io.py` calls accessor functions
  instead of keeping its own copy.
- Live tests may be flaky by nature. Mitigation: gate them behind
  `CSRC_LIVE_WEB_TESTS=1` and keep default tests offline.

## Success Criteria

- `storage.py` becomes a compatibility re-export module.
- Storage implementation lives under `csrc_law_crawler.core`.
- Package-boundary storage tests pass.
- Default test suite remains offline and passes.
- Explicit live test command reaches real AMAC pages and passes when network
  and the official site are available.
