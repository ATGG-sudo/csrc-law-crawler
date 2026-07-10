# Catalog Builder Refactor Design

## Context

The highest-value refactor target is `build_catalog.py`. It is the main
canonical catalog construction entrypoint, but it currently owns too many
responsibilities in one 1,400+ line file:

- source record loading from NERIS and AMAC outputs
- title, fileno, date, and trial-rule matching helpers
- canonical entity creation and source/asset merging
- duplicate detection and duplicate repair
- catalog relation inference
- review queue and manifest construction
- file writing and CLI orchestration

The repository already has a package namespace under `csrc_law_crawler/` and
some service helpers in `catalog_services.py`, but `build_catalog.py` remains
the real implementation surface. Existing tests also import several private
helpers from `build_catalog.py`, so the first refactor must keep compatibility
while creating clearer boundaries.

## Goals

- Reduce `build_catalog.py` into a compatibility-oriented CLI/orchestration
  module.
- Move cohesive catalog domain logic into focused package modules under
  `csrc_law_crawler.processing.catalog`.
- Preserve existing root-level imports and helper names used by tests and
  scripts.
- Keep behavior unchanged for `build_catalog(clean=True)` and
  `python build_catalog.py`.
- Add regression tests that prove the new package-level boundaries are usable
  directly.
- Avoid broad migrations of unrelated crawler, HTTP, storage, encoding, or CLI
  code.

## Non-Goals

- Do not rewrite the full package layout in one pass.
- Do not remove legacy root-level script entrypoints.
- Do not fix repository-wide mojibake text in this refactor.
- Do not change canonical catalog schema, manifest schema, relation schema, or
  output paths.
- Do not add new runtime dependencies.

## Proposed Architecture

Create package-level catalog modules that own pure or near-pure domain logic:

- `csrc_law_crawler.processing.catalog.identity`
  - title cleanup and normalization
  - fileno normalization
  - canonical ID generation
  - date distance helpers

- `csrc_law_crawler.processing.catalog.matching`
  - NERIS/AMAC match selection
  - trial replacement relation inference
  - known official successor relation inference

- `csrc_law_crawler.processing.catalog.entities`
  - entity creation from source records
  - NERIS seeding
  - AMAC record merge behavior
  - duplicate detection, duplicate merging, and content repair

- `csrc_law_crawler.processing.catalog.relations`
  - AMAC page attachment relations
  - quoted-title NERIS publication relations
  - trial replacement and known successor relation assembly

- `csrc_law_crawler.processing.catalog.manifest`
  - match counts
  - manifest item construction
  - review queue item construction
  - catalog manifest construction

`build_catalog.py` will import these functions and re-export the legacy helper
names. Its remaining direct responsibilities will be:

- source record loading from current storage paths
- cleaning the output directory when requested
- invoking the package-level helpers in the existing order
- writing outputs through `CatalogEntityWriter` and `save_json`
- CLI argument parsing and output-lock wrapping

This keeps the refactor small enough to verify while making the next migration
step obvious.

## Data Flow

1. `build_catalog()` loads NERIS and AMAC source records.
2. `entities.seed_neris_entities()` creates initial canonical entities and a
   title index.
3. `matching.choose_neris_match_with_rule()` is used through
   `CatalogMatcher`.
4. `entities.match_amac_records()` merges AMAC records into existing entities
   or creates AMAC-only entities.
5. `entities.deduplicate_catalog_entities()` merges equivalent canonical
   entities and records the dedupe report.
6. `relations.build_catalog_relations()` builds catalog relation edges.
7. `manifest.review_queue_items()` and `manifest.catalog_manifest()` build
   report artifacts.
8. `build_catalog()` writes entities, indexes, relations, dedupe report,
   review queue, and manifest exactly as before.

## Compatibility

- Existing imports such as `from build_catalog import normalize_title` continue
  to work.
- Existing tests that import private helpers from `build_catalog.py` continue
  to work during this first refactor.
- New code can import stable package-level helpers from
  `csrc_law_crawler.processing.catalog`.
- The root-level `catalog_services.py` remains in place for now.

## Testing

Use TDD for the implementation:

1. Add package-boundary tests that import representative helpers from
   `csrc_law_crawler.processing.catalog` and exercise existing behavior:
   - title normalization
   - match selection
   - relation ingestion/building
   - manifest/review queue construction
2. Run the targeted tests and confirm they fail before implementation.
3. Move helpers into package modules and re-export them from `build_catalog.py`
   and `csrc_law_crawler.processing.catalog.__init__`.
4. Run targeted tests, then the full test suite, lint, and compile checks.

## Risks

- Moving helpers can introduce circular imports because current package modules
  still re-export root modules. Mitigation: new package modules should not
  import `build_catalog.py`; `build_catalog.py` imports them.
- Existing private helper tests make renaming risky. Mitigation: preserve
  root-level names as aliases or wrappers.
- Source files contain mojibake text. Mitigation: avoid semantic edits to
  those literals unless a moved function requires exact copying.

## Success Criteria

- `build_catalog.py` no longer contains the moved catalog domain helper
  implementations.
- `csrc_law_crawler.processing.catalog` exposes package-level catalog
  identity, matching, entity, relation, and manifest helpers.
- Existing root-level helper imports still work.
- `build_catalog(clean=True)` behavior is unchanged for tests.
- Verification commands pass in a local environment matching CI as closely as
  practical:
  - `python -m compileall -q .`
  - `python -m ruff check .`
  - `python -m pytest -q`
