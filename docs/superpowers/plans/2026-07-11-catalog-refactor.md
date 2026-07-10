# Catalog Builder Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split catalog construction domain logic out of `build_catalog.py` while preserving legacy imports and behavior.

**Architecture:** Keep `build_catalog.py` as the root CLI and compatibility module. Move cohesive helper implementations into package modules under `csrc_law_crawler.processing.catalog`, then re-export the legacy helper names from `build_catalog.py` and stable public names from `csrc_law_crawler.processing.catalog`.

**Tech Stack:** Python 3.10+, stdlib dataclasses/typing/pathlib/re/hashlib, existing project modules, pytest, ruff.

## Global Constraints

- Preserve existing root-level script entrypoints.
- Preserve existing helper imports from `build_catalog.py`.
- Do not change canonical catalog schemas, output paths, or runtime dependencies.
- Do not fix repository-wide mojibake in this refactor.
- Add tests before production code changes.

---

### Task 1: Add Package Boundary Tests

**Files:**
- Create: `tests/test_catalog_package.py`

**Interfaces:**
- Consumes: package imports from `csrc_law_crawler.processing.catalog`
- Produces: failing tests that require package-level identity, matching, relations, and manifest helpers

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

import unittest

from csrc_law_crawler.processing.catalog import (
    build_catalog_relations,
    catalog_manifest,
    choose_neris_match_with_rule,
    normalize_title,
    review_queue_items,
)


class CatalogPackageBoundaryTests(unittest.TestCase):
    def test_package_exports_catalog_identity_and_matching_helpers(self) -> None:
        neris = {
            "record_id": "n1",
            "metadata": {"name": " Example Rule ", "fileno": "No. 1", "pub_date": "2026-01-01"},
            "assets": [],
        }
        amac = {
            "record_id": "a1",
            "metadata": {"name": "Example Rule", "fileno": "No. 1", "pub_date": "2026-01-02"},
            "assets": [],
        }

        match, status, confidence, evidence, rule_id = choose_neris_match_with_rule(
            amac,
            {normalize_title("Example Rule"): [neris]},
        )

        self.assertIs(match, neris)
        self.assertEqual("same_document", status)
        self.assertGreater(confidence, 0.9)
        self.assertTrue(evidence)
        self.assertTrue(rule_id)

    def test_package_exports_catalog_relations_and_manifest_helpers(self) -> None:
        neris_records = [
            {
                "record_id": "parent",
                "metadata": {"name": "publish Example Rule"},
            }
        ]
        amac_records = [
            {"record_id": "parent", "parent_record_id": None, "metadata": {}},
            {"record_id": "child", "parent_record_id": "parent", "metadata": {}},
        ]
        entities = {
            "law_parent": {
                "id": "law_parent",
                "title": "publish Example Rule",
                "document_type": "regulation",
                "status": "current",
                "sources": [],
                "metadata": {},
            },
            "law_child": {
                "id": "law_child",
                "title": "Example Rule",
                "document_type": "regulation",
                "status": "current",
                "sources": [],
                "metadata": {},
            },
        }
        source_to_entity = {
            ("neris", "parent"): "law_parent",
            ("amac", "parent"): "law_parent",
            ("amac", "child"): "law_child",
        }

        relations = build_catalog_relations(
            neris_records=neris_records,
            amac_records=amac_records,
            entities=entities,
            source_to_entity=source_to_entity,
        )
        review_items = review_queue_items(
            [{"record_id": "child", "metadata": {"document_type": "self_regulatory_rule", "status": "unknown"}}],
            source_to_entity=source_to_entity,
            matches={"child": {"match_status": "new_to_neris", "confidence": 0.5}},
        )
        manifest = catalog_manifest(
            neris_records=neris_records,
            amac_records=amac_records,
            entities=entities,
            relations=relations,
            review_items=review_items,
            matches={"child": {"match_status": "new_to_neris"}},
        )

        self.assertIn(
            {
                "from": "law_parent",
                "to": "law_child",
                "relation": "publishes",
                "source": "amac.page_attachment",
                "evidence": {
                    "source": "amac.page_attachment",
                    "rule_id": relations[0]["rule_id"],
                    "parent_source_record_id": "parent",
                    "attachment_source_record_id": "child",
                    "confidence": relations[0]["confidence"],
                },
                "confidence": relations[0]["confidence"],
                "rule_id": relations[0]["rule_id"],
            },
            relations,
        )
        self.assertEqual(1, len(review_items))
        self.assertEqual(2, manifest["canonical_laws"])
        self.assertEqual({"new_to_neris": 1}, manifest["match_counts"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_catalog_package.py -q`

Expected: FAIL during import because `build_catalog_relations`,
`catalog_manifest`, `choose_neris_match_with_rule`, `normalize_title`, or
`review_queue_items` are not yet exported from
`csrc_law_crawler.processing.catalog`.

### Task 2: Extract Identity and Matching Helpers

**Files:**
- Create: `csrc_law_crawler/processing/catalog/identity.py`
- Create: `csrc_law_crawler/processing/catalog/matching.py`
- Modify: `build_catalog.py`
- Modify: `csrc_law_crawler/processing/catalog/__init__.py`

**Interfaces:**
- Produces:
  - `clean_title(value: Any) -> str`
  - `normalize_title(value: Any) -> str`
  - `is_trial_title(value: Any) -> bool`
  - `normalize_title_without_trial(value: Any) -> str`
  - `normalize_fileno(value: Any) -> str`
  - `canonical_id(seed: str) -> str`
  - `choose_neris_match_with_rule(amac, title_index)`
  - `choose_neris_match(amac, title_index)`
  - `infer_trial_replacement_relations(entities)`
  - `infer_known_successor_relations(entities)`

- [ ] **Step 1: Move identity constants and helpers**

Move regex constants used by identity/matching/entity logic and these helper
functions from `build_catalog.py` into `identity.py`:

```python
def clean_title(value: Any) -> str: ...
def normalize_title(value: Any) -> str: ...
def is_trial_title(value: Any) -> bool: ...
def normalize_title_without_trial(value: Any) -> str: ...
def normalize_fileno(value: Any) -> str: ...
def canonical_id(seed: str) -> str: ...
def date_distance(left: Any, right: Any) -> int | None: ...
def date_sort_value(value: Any) -> int | None: ...
```

Keep legacy aliases in `build_catalog.py`:

```python
from csrc_law_crawler.processing.catalog.identity import (
    ATTACHMENT_TEXT_SIGNAL_RE,
    DEDUP_MIN_BODY_CHARS,
    PUBLISHING_TITLE_RE,
    QUOTED_TITLE_RE,
    SECTION_TOKEN_RE,
    SPACE_PUNCT_RE,
    clean_title,
    canonical_id,
    date_distance as _date_distance,
    date_sort_value as _date_sort_value,
    is_trial_title,
    normalize_fileno,
    normalize_title,
    normalize_title_without_trial,
)
```

- [ ] **Step 2: Move matching helpers**

Move matching and successor functions into `matching.py`, importing identity
helpers and existing rule constants from `catalog_rules`.

Keep legacy aliases in `build_catalog.py`:

```python
from csrc_law_crawler.processing.catalog.matching import (
    KNOWN_SUCCESSOR_CHAINS,
    choose_neris_match,
    choose_neris_match_with_rule,
    infer_known_successor_relations,
    infer_trial_replacement_relations,
)
```

- [ ] **Step 3: Run targeted tests**

Run: `.venv/bin/python -m pytest tests/test_catalog_package.py::CatalogPackageBoundaryTests::test_package_exports_catalog_identity_and_matching_helpers -q`

Expected: PASS.

### Task 3: Extract Entity, Relation, and Manifest Helpers

**Files:**
- Create: `csrc_law_crawler/processing/catalog/entities.py`
- Create: `csrc_law_crawler/processing/catalog/relations.py`
- Create: `csrc_law_crawler/processing/catalog/manifest.py`
- Modify: `build_catalog.py`
- Modify: `csrc_law_crawler/processing/catalog/__init__.py`

**Interfaces:**
- Produces:
  - `seed_neris_entities(records)`
  - `match_amac_records(records, matcher, entities, source_to_entity)`
  - `deduplicate_catalog_entities(entities, source_to_entity, matches)`
  - `build_catalog_relations(...)`
  - `review_queue_items(...)`
  - `catalog_manifest(...)`

- [ ] **Step 1: Move entity helpers**

Move entity construction, NERIS seeding, AMAC merging, and dedupe functions
into `entities.py`. Preserve root private aliases:

```python
from csrc_law_crawler.processing.catalog.entities import (
    deduplicate_catalog_entities,
    entity_from_record as _entity_from_record,
    match_amac_records as _match_amac_records,
    record_plain_text as _record_plain_text,
    seed_neris_entities as _seed_neris_entities,
)
```

- [ ] **Step 2: Move relation helpers**

Move relation assembly into `relations.py`. Preserve root private aliases:

```python
from csrc_law_crawler.processing.catalog.relations import (
    add_amac_page_attachment_relations as _add_amac_page_attachment_relations,
    add_known_successor_relations as _add_known_successor_relations,
    add_neris_title_relations as _add_neris_title_relations,
    add_trial_replacement_relations as _add_trial_replacement_relations,
    build_catalog_relations as _build_catalog_relations,
)
```

- [ ] **Step 3: Move manifest helpers**

Move manifest and review queue helpers into `manifest.py`. Preserve root private
aliases:

```python
from csrc_law_crawler.processing.catalog.manifest import (
    catalog_manifest as _catalog_manifest,
    catalog_manifest_items as _catalog_manifest_items,
    match_counts as _match_counts,
    review_queue_items as _review_queue_items,
)
```

- [ ] **Step 4: Run targeted tests**

Run: `.venv/bin/python -m pytest tests/test_catalog_package.py -q`

Expected: PASS.

### Task 4: Verify Compatibility and Full Suite

**Files:**
- Modify only if verification exposes compatibility gaps.

**Interfaces:**
- Consumes: existing legacy helper imports from `tests/test_core.py`
- Produces: passing targeted and full verification

- [ ] **Step 1: Run existing catalog-focused tests**

Run: `.venv/bin/python -m pytest tests/test_core.py::CatalogMatchingTests tests/test_core.py::CatalogNormalizationTests -q`

Expected: PASS.

- [ ] **Step 2: Run full test and static checks**

Run:

```bash
.venv/bin/python -m compileall -q .
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
```

Expected: all commands exit 0.

- [ ] **Step 3: Inspect diff**

Run: `git diff --stat && git diff -- build_catalog.py csrc_law_crawler/processing/catalog tests/test_catalog_package.py`

Expected: `build_catalog.py` shrinks materially, new package modules contain
the extracted implementations, and root-level helper imports remain present.
