"""Service helpers for canonical catalog construction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from storage import save_json, utc_now_iso


JsonRecord = dict[str, Any]
CatalogMatch = tuple[JsonRecord | None, str, float, list[str], str]


@dataclass(frozen=True)
class CatalogSourceRecords:
    neris: list[JsonRecord]
    amac: list[JsonRecord]


@dataclass(frozen=True)
class CatalogSourceLoader:
    load_neris_records: Callable[[], list[JsonRecord]]
    load_amac_records: Callable[[], list[JsonRecord]]

    def load(self) -> CatalogSourceRecords:
        return CatalogSourceRecords(
            neris=self.load_neris_records(),
            amac=self.load_amac_records(),
        )


@dataclass(frozen=True)
class CatalogMatcher:
    title_index: dict[str, list[JsonRecord]]
    choose_match: Callable[
        [JsonRecord, dict[str, list[JsonRecord]]],
        CatalogMatch,
    ]

    def choose_neris_match(
        self,
        record: JsonRecord,
    ) -> CatalogMatch:
        return self.choose_match(record, self.title_index)


@dataclass
class CatalogRelationIngestor:
    items: list[JsonRecord] = field(default_factory=list)
    keys: set[tuple[str, str, str]] = field(default_factory=set)

    def add(
        self,
        from_id: str,
        to_id: str,
        relation: str,
        evidence: JsonRecord,
    ) -> None:
        key = (from_id, to_id, relation)
        if from_id == to_id or key in self.keys:
            return
        self.keys.add(key)
        item = {
            "from": from_id,
            "to": to_id,
            "relation": relation,
            "source": evidence.get("source"),
            "evidence": evidence,
            "confidence": evidence.get("confidence", 1.0),
        }
        if evidence.get("rule_id"):
            item["rule_id"] = evidence.get("rule_id")
        self.items.append(item)


@dataclass(frozen=True)
class CatalogEntityWriter:
    def write_entities(
        self,
        directory: Path,
        entities: dict[str, JsonRecord],
    ) -> None:
        for entity_id, entity in entities.items():
            save_json(directory / f"{entity_id}.json", entity)

    def write_source_matches(
        self,
        path: Path,
        source_to_entity: dict[tuple[str, str], str],
        matches: dict[str, JsonRecord],
    ) -> None:
        save_json(
            path,
            {
                "schema_version": 2,
                "updated_at": utc_now_iso(),
                "by_source": {
                    f"{system}:{record_id}": entity_id
                    for (system, record_id), entity_id in sorted(source_to_entity.items())
                },
                "items": matches,
            },
        )

    def write_relations(self, path: Path, relations: list[JsonRecord]) -> None:
        save_json(
            path,
            {
                "schema_version": 1,
                "updated_at": utc_now_iso(),
                "items": relations,
            },
        )

    def write_review_queue(
        self,
        path: Path,
        *,
        rules: list[JsonRecord],
        rule_calibration: JsonRecord,
        items: list[JsonRecord],
    ) -> None:
        save_json(
            path,
            {
                "schema_version": 1,
                "updated_at": utc_now_iso(),
                "count": len(items),
                "rules": rules,
                "rule_calibration": rule_calibration,
                "items": items,
            },
        )

    def write_manifest(self, path: Path, manifest: JsonRecord) -> None:
        save_json(path, manifest)
