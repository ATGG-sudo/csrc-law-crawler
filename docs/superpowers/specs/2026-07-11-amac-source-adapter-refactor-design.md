# AMAC Source Adapter Refactor Design

## Context

The next highest-value refactor target is `amac_crawl.py`. It is a root-level
script that currently owns several separate responsibilities:

- AMAC HTTP session policy, retries, metrics, and binary downloads
- policy-index discovery
- public-site keyword discovery
- xwfb notice-list discovery and rule-notice filtering
- page title, metadata, body, and attachment-link parsing
- asset persistence and extracted text handling
- candidate crawling, manifest creation, and CLI orchestration

The package namespace already has `csrc_law_crawler.sources.amac`, but its
`client.py` is only a compatibility re-export from `amac_crawl.py`. This keeps
the real source adapter hidden in the root script and makes future tests or
feature work pay the cost of importing a large CLI module.

## Goals

- Move AMAC source-adapter logic into focused modules under
  `csrc_law_crawler.sources.amac`.
- Keep `amac_crawl.py` as the CLI and legacy compatibility entrypoint.
- Preserve existing imports from `amac_crawl.py` used by tests and scripts.
- Preserve crawling behavior, output paths, schema fields, retry behavior, and
  command-line flags.
- Add package-boundary tests that prove the package API is usable without
  importing the root script.
- Avoid unrelated changes to catalog logic, storage paths, network policy, or
  schema definitions.

## Non-Goals

- Do not remove root-level `amac_crawl.py`.
- Do not change AMAC candidate selection rules or keywords.
- Do not add new runtime dependencies.
- Do not rewrite generic HTTP, storage, logging, or asset-text utilities.
- Do not normalize repository-wide Chinese encoding or text literals in this
  refactor.

## Proposed Architecture

Create package-level AMAC modules:

- `csrc_law_crawler.sources.amac.identity`
  - URL canonicalization
  - source record ID creation
  - text and attachment-title cleanup
  - AMAC document classification metadata

- `csrc_law_crawler.sources.amac.client`
  - `AmacClient`
  - HTTP retry and retry-metric behavior
  - JSON and binary payload helpers

- `csrc_law_crawler.sources.amac.discovery`
  - policy-search candidates
  - site-search candidates
  - xwfb rule-notice title filtering and list crawling
  - candidate deduplication
  - default site, notice, and xwfb constants

- `csrc_law_crawler.sources.amac.parser`
  - content-root selection
  - page-title extraction
  - metadata extraction
  - asset-link extraction

- `csrc_law_crawler.sources.amac.pipeline`
  - AMAC asset output paths
  - asset download and extraction
  - `crawl_candidate`
  - `crawl_amac`

`amac_crawl.py` will import and re-export these names, then keep only CLI
argument parsing, error handling, and output-lock wrapping.

## Compatibility

- Existing code such as `from amac_crawl import AmacClient` continues to work.
- Existing mock points such as `patch("amac_crawl.random.uniform")` and
  `patch("amac_crawl.time.sleep")` continue to affect `AmacClient`, because the
  root script re-exports the same module objects used by the client module.
- Package users can import from `csrc_law_crawler.sources.amac` or from focused
  submodules.
- `csrc_law_crawler.sources.amac.client` becomes the real client
  implementation instead of importing from the root script.

## Testing

Use TDD for the implementation:

1. Add package-boundary tests that import representative helpers from
   `csrc_law_crawler.sources.amac` submodules.
2. Run the new test and confirm it fails before the package modules exist.
3. Extract modules and keep root-level compatibility aliases.
4. Run AMAC-focused tests, then full verification:
   - `.venv/bin/python -m compileall -q .`
   - `.venv/bin/python -m ruff check .`
   - `.venv/bin/python -m pytest -q`
   - `.venv/bin/python -m mypy *.py tests`
   - `git diff --check`

## Risks

- Circular imports can appear if package modules import `amac_crawl.py`.
  Mitigation: package modules must not import the root script; the root script
  imports package modules.
- Test mocks currently patch root-level `random` and `time`. Mitigation:
  `amac_crawl.py` should expose the exact `random` and `time` module objects
  from `client.py`.
- Moving helper names can break private imports. Mitigation: preserve
  root-level aliases for all moved functions and constants.
- `pipeline.py` needs helpers from several new modules. Mitigation: keep the
  data flow one-way: identity/client/discovery/parser feed pipeline, and root
  CLI imports pipeline.

## Success Criteria

- `amac_crawl.py` is reduced to CLI and compatibility exports.
- AMAC client, discovery, parser, identity, and pipeline logic live under
  `csrc_law_crawler.sources.amac`.
- Existing root-level imports and tests remain valid.
- New package-boundary tests pass.
- Full test, lint, type-check, compile, and diff-whitespace verification pass.
