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
    ingest_status: str = "complete"
    material_lane: str = "rule"
    discovery_evidence: list[JsonObject] = field(default_factory=list)
    fingerprints: JsonObject = field(default_factory=dict)
    assets: list[JsonObject] = field(default_factory=list)
    attachment_documents: list[JsonObject] = field(default_factory=list)
    web_category_leaf: str | None = None
    web_category_path: list[str] = field(default_factory=list)
    web_category_provenance: str | None = None
    page_role: str = "unknown"
    enforcement_classification: JsonObject | None = None


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
    enforcement_classification: JsonObject | None = None


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
    material_classification: JsonObject
    metadata: JsonObject
    preferred_content: JsonObject
    sources: list[JsonObject]
    case_id: str | None = None
    document_role: str | None = None


@dataclass(frozen=True)
class CanonicalLaw:
    schema_version: int
    id: str
    title: str
    document_type: str
    status: str
    effectiveness: JsonObject
    material_classification: JsonObject
    metadata: JsonObject
    sources: list[JsonObject]
    full_text_plain: str
    full_text_markdown: str
    case_id: str | None = None
    document_role: str | None = None


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

MATERIAL_CLASSIFICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "lane": {"enum": ["rule", "reference", "unknown"]},
        "category": {
            "enum": [
                "law_regulation",
                "normative_document",
                "self_regulatory_rule",
                "business_rule",
                "publication_consultation",
                "interpretation_qa",
                "template_guidance",
                "research_statistics",
                "enforcement_reference",
                "other_reference",
                "unknown",
            ]
        },
        "basis": STRING,
        "rule_id": STRING,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": OBJECT,
    },
    "required": ["lane", "category", "basis", "rule_id", "confidence", "evidence"],
}
MATERIAL_CLASSIFICATION_REF = {"$ref": "#/$defs/materialClassification"}
EFFECTIVENESS_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "status": {
            "enum": [
                "current",
                "pending",
                "historical",
                "unknown",
                "not_applicable",
            ]
        },
        "as_of": {"type": "string", "format": "date"},
        "basis": STRING,
        "rule_id": STRING,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["status", "as_of", "basis", "rule_id", "confidence"],
}
ENFORCEMENT_CLASSIFICATION_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": True,
    "properties": {
        "category": {
            "enum": [
                "penalties",
                "self_regulatory_measure",
                "abnormal_operation",
                "missing_institution",
                "other_enforcement",
            ]
        },
        "subtype": {
            "enum": [
                "disciplinary_decision",
                "disciplinary_prior_notice",
                "disciplinary_review_decision",
                "other",
            ]
        },
    },
}
REFERENCE_LIFECYCLE_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "status": {"enum": ["not_applicable", "unfinalized", "finalized", "withdrawn", "unknown"]}
    },
    "required": ["status"],
}


SOURCE_RECORD_SCHEMA = _object(
    ["schema_version", "source_record_id", "source_system", "metadata", "content", "source"],
    {
        "schema_version": INTEGER,
        "source_record_id": STRING,
        "source_system": STRING,
        "metadata": OBJECT,
        "content": OBJECT,
        "ingest_status": STRING,
        "material_lane": STRING,
        "discovery_evidence": {"type": "array", "items": OBJECT},
        "fingerprints": OBJECT,
        "assets": {"type": "array", "items": OBJECT},
        "attachment_documents": {"type": "array", "items": OBJECT},
        "source": OBJECT,
        "enforcement_classification": ENFORCEMENT_CLASSIFICATION_SCHEMA,
        "web_category_leaf": NULLABLE_STRING,
        "web_category_path": {"type": "array", "items": STRING},
        "web_category_provenance": {
            "enum": ["page_breadcrumb", "api_channel", "endpoint_profile", "url_inference", None]
        },
        "page_role": {
            "enum": [
                "normative_instrument",
                "case_document",
                "publication_wrapper",
                "supporting_material",
                "unknown",
            ]
        },
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
        "enforcement_classification": ENFORCEMENT_CLASSIFICATION_SCHEMA,
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
        "material_classification",
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
        "material_classification": MATERIAL_CLASSIFICATION_REF,
        "enforcement_classification": ENFORCEMENT_CLASSIFICATION_SCHEMA,
        "reference_lifecycle": REFERENCE_LIFECYCLE_SCHEMA,
        "case_id": NULLABLE_STRING,
        "document_role": NULLABLE_STRING,
        "metadata": OBJECT,
        "preferred_content": OBJECT,
        "sources": {"type": "array", "items": OBJECT},
    },
    title="CatalogEntity",
)
CATALOG_ENTITY_SCHEMA["$defs"] = {"materialClassification": MATERIAL_CLASSIFICATION_SCHEMA}

CANONICAL_LAW_SCHEMA = _object(
    [
        "schema_version",
        "id",
        "title",
        "document_type",
        "status",
        "effectiveness",
        "material_classification",
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
        "effectiveness": EFFECTIVENESS_SCHEMA,
        "material_classification": MATERIAL_CLASSIFICATION_REF,
        "enforcement_classification": ENFORCEMENT_CLASSIFICATION_SCHEMA,
        "reference_lifecycle": REFERENCE_LIFECYCLE_SCHEMA,
        "case_id": NULLABLE_STRING,
        "document_role": NULLABLE_STRING,
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
CANONICAL_LAW_SCHEMA["$defs"] = {"materialClassification": MATERIAL_CLASSIFICATION_SCHEMA}

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
    return {name: SCHEMA_SNAPSHOT_DIR / f"{name}.schema.json" for name in JSON_SCHEMAS}


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
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: value {value!r} is not in allowed enum"]
    expected_type = schema.get("type")
    if expected_type:
        expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
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
                issues.extend(validate_schema(item_schema, item, path=f"{path}[{index}]"))
    return issues


def validate_model(name: str, value: Any) -> list[str]:
    schema = JSON_SCHEMAS[name]
    return validate_schema(schema, value)


def format_model_issues(name: str, label: str, value: Any) -> list[str]:
    return [f"{label}: {issue}" for issue in validate_model(name, value)]
