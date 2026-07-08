"""Explicit data contracts for crawler JSON artifacts.

The project still writes plain JSON files, but these contracts give validators,
tests, and downstream readers a stable place to check required fields and
schema versions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


JsonObject = dict[str, Any]
JsonSchema = dict[str, Any]


@dataclass(frozen=True)
class SourceRecord:
    schema_version: int
    source_record_id: str
    source_system: str
    metadata: JsonObject
    content: JsonObject
    source: JsonObject
    assets: list[JsonObject] = field(default_factory=list)


@dataclass(frozen=True)
class LawDocument:
    metadata: JsonObject
    entries: list[JsonObject]
    full_text: str
    source: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class WritDocument:
    metadata: JsonObject
    body: str
    legal_basis: list[JsonObject] = field(default_factory=list)
    parties: list[JsonObject] = field(default_factory=list)
    source: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    download_status: str
    label: str | None = None
    kind: str | None = None
    source_url: str | None = None
    local_file: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class CatalogEntity:
    schema_version: int
    id: str
    title: str
    document_type: str
    status: str
    metadata: JsonObject
    preferred_content: JsonObject
    sources: list[JsonObject]


@dataclass(frozen=True)
class CanonicalLaw:
    schema_version: int
    id: str
    title: str
    document_type: str
    status: str
    effectiveness: JsonObject
    metadata: JsonObject
    sources: list[JsonObject]
    full_text_plain: str
    full_text_markdown: str


@dataclass(frozen=True)
class RelationEdge:
    from_id: str
    to_id: str
    relation: str
    source: str
    confidence: float
    evidence: JsonObject


def _object(
    required: list[str],
    properties: dict[str, JsonSchema],
    *,
    title: str,
) -> JsonSchema:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": title,
        "type": "object",
        "required": required,
        "properties": properties,
        "additionalProperties": True,
    }


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}
OBJECT = {"type": "object"}
ARRAY = {"type": "array"}
NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_INTEGER = {"type": ["integer", "null"]}


SOURCE_RECORD_SCHEMA = _object(
    ["schema_version", "source_record_id", "source_system", "metadata", "content", "source"],
    {
        "schema_version": INTEGER,
        "source_record_id": STRING,
        "source_system": STRING,
        "metadata": OBJECT,
        "content": OBJECT,
        "assets": {"type": "array", "items": OBJECT},
        "attachment_documents": {"type": "array", "items": OBJECT},
        "source": OBJECT,
    },
    title="SourceRecord",
)

LAW_DOCUMENT_SCHEMA = _object(
    ["metadata", "entries", "full_text"],
    {
        "metadata": OBJECT,
        "entries": {"type": "array", "items": OBJECT},
        "full_text": STRING,
        "entry_class_code": {},
        "source": OBJECT,
    },
    title="LawDocument",
)

WRIT_DOCUMENT_SCHEMA = _object(
    ["metadata", "body", "legal_basis", "parties"],
    {
        "metadata": OBJECT,
        "body": STRING,
        "legal_basis": {"type": "array", "items": OBJECT},
        "parties": {"type": "array", "items": OBJECT},
        "list_summary": {"type": ["object", "null"]},
        "source": OBJECT,
    },
    title="WritDocument",
)

ASSET_RECORD_SCHEMA = _object(
    ["asset_id", "download_status"],
    {
        "asset_id": STRING,
        "kind": NULLABLE_STRING,
        "label": NULLABLE_STRING,
        "source_url": NULLABLE_STRING,
        "local_file": NULLABLE_STRING,
        "content_type": NULLABLE_STRING,
        "sha256": NULLABLE_STRING,
        "size_bytes": NULLABLE_INTEGER,
        "download_status": STRING,
    },
    title="AssetRecord",
)

CATALOG_ENTITY_SCHEMA = _object(
    [
        "schema_version",
        "id",
        "title",
        "document_type",
        "status",
        "metadata",
        "preferred_content",
        "sources",
    ],
    {
        "schema_version": INTEGER,
        "id": STRING,
        "title": STRING,
        "document_type": STRING,
        "status": STRING,
        "metadata": OBJECT,
        "preferred_content": OBJECT,
        "sources": {"type": "array", "items": OBJECT},
    },
    title="CatalogEntity",
)

CANONICAL_LAW_SCHEMA = _object(
    [
        "schema_version",
        "id",
        "title",
        "document_type",
        "status",
        "effectiveness",
        "metadata",
        "sources",
        "full_text_plain",
        "full_text_markdown",
    ],
    {
        "schema_version": INTEGER,
        "id": STRING,
        "title": STRING,
        "document_type": {"type": ["string", "null"]},
        "status": STRING,
        "effectiveness": OBJECT,
        "metadata": OBJECT,
        "preferred_source": OBJECT,
        "sources": {"type": "array", "items": OBJECT},
        "revision_ref": {"type": ["object", "null"]},
        "full_text_plain": STRING,
        "full_text_markdown": STRING,
        "tables": {"type": "array", "items": OBJECT},
        "assets": {"type": "array", "items": OBJECT},
    },
    title="CanonicalLaw",
)

RELATION_EDGE_SCHEMA = _object(
    ["from", "to", "relation", "source", "confidence", "evidence"],
    {
        "from": STRING,
        "to": STRING,
        "relation": STRING,
        "source": STRING,
        "rule_id": NULLABLE_STRING,
        "confidence": NUMBER,
        "evidence": OBJECT,
    },
    title="RelationEdge",
)

RELATION_GRAPH_SCHEMA = _object(
    ["schema_version", "updated_at", "nodes", "edges", "counts"],
    {
        "schema_version": INTEGER,
        "updated_at": STRING,
        "nodes": {"type": "array", "items": OBJECT},
        "edges": {"type": "array", "items": RELATION_EDGE_SCHEMA},
        "counts": OBJECT,
    },
    title="RelationGraph",
)


JSON_SCHEMAS: dict[str, JsonSchema] = {
    "source_record": SOURCE_RECORD_SCHEMA,
    "law_document": LAW_DOCUMENT_SCHEMA,
    "writ_document": WRIT_DOCUMENT_SCHEMA,
    "asset_record": ASSET_RECORD_SCHEMA,
    "catalog_entity": CATALOG_ENTITY_SCHEMA,
    "canonical_law": CANONICAL_LAW_SCHEMA,
    "relation_edge": RELATION_EDGE_SCHEMA,
    "relation_graph": RELATION_GRAPH_SCHEMA,
}


SCHEMA_SNAPSHOT_DIR = Path("schemas")


def schema_snapshot_files() -> dict[str, Path]:
    return {
        name: SCHEMA_SNAPSHOT_DIR / f"{name}.schema.json"
        for name in JSON_SCHEMAS
    }


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    return type(value).__name__


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema(schema: JsonSchema, value: Any, *, path: str = "$") -> list[str]:
    expected_type = schema.get("type")
    if expected_type:
        expected_types = (
            expected_type if isinstance(expected_type, list) else [expected_type]
        )
        if not any(_matches_type(value, str(item)) for item in expected_types):
            return [
                f"{path}: expected {'/'.join(str(item) for item in expected_types)}, "
                f"got {_type_name(value)}"
            ]

    issues: list[str] = []
    if isinstance(value, dict):
        for field_name in schema.get("required") or []:
            if field_name not in value:
                issues.append(f"{path}.{field_name}: missing required field")
        properties = schema.get("properties") or {}
        for field_name, field_schema in properties.items():
            if field_name in value and isinstance(field_schema, dict):
                issues.extend(
                    validate_schema(
                        field_schema,
                        value[field_name],
                        path=f"{path}.{field_name}",
                    )
                )
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                issues.extend(
                    validate_schema(item_schema, item, path=f"{path}[{index}]")
                )
    return issues


def validate_model(name: str, value: Any) -> list[str]:
    schema = JSON_SCHEMAS[name]
    return validate_schema(schema, value)


def format_model_issues(name: str, label: str, value: Any) -> list[str]:
    return [f"{label}: {issue}" for issue in validate_model(name, value)]
