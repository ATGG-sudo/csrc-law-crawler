"""Build minimal subject-query seeds from acquired case material."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any

from storage import iter_writ_files, load_json, output_dir, save_json, utc_now_iso


INSTITUTION_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·\-]{2,50}"
    r"(?:私募基金管理有限公司|基金管理有限公司|投资管理有限公司|资产管理有限公司|有限公司)"
)
PRODUCT_RE = re.compile(
    r"[《“]([^》”\n]{2,40}"
    r"(?:私募投资基金|私募基金|股权投资基金|创业投资基金))[》”]"
)
PRIVATE_FUND_TOKENS = (
    "私募基金",
    "私募投资基金",
    "私募股权基金",
    "股权投资基金",
    "创业投资基金",
    "基金管理人",
    "基金业协会",
)
PRODUCT_STOP_TOKENS = ("未备案", "相关", "上述", "各类", "本案", "所涉")


def _private_fund_relevant(value: str) -> bool:
    return any(token in value for token in PRIVATE_FUND_TOKENS)


def _normalized_name(value: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or "")).strip("，。；：,.;:")


def _seed_id(entity_type: str, normalized_name: str) -> str:
    digest = hashlib.sha256(f"{entity_type}:{normalized_name}".encode("utf-8")).hexdigest()
    return f"subject_{digest[:24]}"


def _add_seed(
    seeds: dict[tuple[str, str], dict[str, Any]],
    *,
    entity_type: str,
    name: Any,
    source_record_id: str,
    ambiguous: bool,
) -> None:
    normalized = _normalized_name(name)
    if len(normalized) < 2:
        return
    if entity_type == "product" and any(token in normalized for token in PRODUCT_STOP_TOKENS):
        return
    key = (entity_type, normalized)
    seed = seeds.setdefault(
        key,
        {
            "seed_id": _seed_id(entity_type, normalized),
            "entity_type": entity_type,
            "name": str(name).strip(),
            "normalized_name": normalized,
            "source_record_ids": [],
            "query_targets": {
                "institution": ["eid", "amac_institution"],
                "product": ["amac_product"],
                "person": ["eid"],
            }[entity_type],
            "ambiguous": ambiguous,
        },
    )
    if source_record_id not in seed["source_record_ids"]:
        seed["source_record_ids"].append(source_record_id)


def build_subject_seeds(root: Path | None = None) -> dict[str, Any]:
    output_root = root or output_dir()
    seeds: dict[tuple[str, str], dict[str, Any]] = {}
    records_root = output_root / "raw" / "sources" / "records"
    for path in sorted(records_root.rglob("*.json")) if records_root.exists() else []:
        record = load_json(path, {})
        if record.get("material_lane") != "case":
            continue
        record_id = str(record.get("source_record_id") or path.stem)
        metadata = record.get("metadata") or {}
        content = record.get("content") or {}
        text = f"{metadata.get('name') or ''}\n{content.get('plain_text') or ''}"
        if not _private_fund_relevant(text):
            continue
        parties = metadata.get("parties") or content.get("parties") or []
        for party in parties:
            if not isinstance(party, dict):
                continue
            party_type = str(party.get("entity_type") or party.get("type") or "")
            entity_type = (
                "institution"
                if party_type in {"institution", "organization", "company"}
                else "person"
            )
            _add_seed(
                seeds,
                entity_type=entity_type,
                name=party.get("name"),
                source_record_id=record_id,
                ambiguous=entity_type == "person" and not bool(party.get("role")),
            )
        if not parties:
            for name in INSTITUTION_RE.findall(text):
                _add_seed(
                    seeds,
                    entity_type="institution",
                    name=name,
                    source_record_id=record_id,
                    ambiguous=False,
                )
        for name in PRODUCT_RE.findall(text):
            _add_seed(
                seeds,
                entity_type="product",
                name=name,
                source_record_id=record_id,
                ambiguous=False,
            )

    if root is None:
        writ_paths = iter_writ_files()
    else:
        writ_paths = sorted((output_root / "raw" / "neris" / "writs").glob("writ_*.json"))
    for path in writ_paths:
        writ = load_json(path, {})
        record_id = str((writ.get("metadata") or {}).get("id") or path.stem)
        text = f"{(writ.get('metadata') or {}).get('name') or ''}\n{writ.get('body') or ''}"
        if not _private_fund_relevant(text):
            continue
        parties = writ.get("parties") or []
        for party in parties:
            if not isinstance(party, dict):
                continue
            name = party.get("name")
            role = str(party.get("role") or "")
            entity_type = (
                "institution"
                if any(token in str(name or "") for token in ("公司", "基金", "中心", "企业"))
                else "person"
            )
            _add_seed(
                seeds,
                entity_type=entity_type,
                name=name,
                source_record_id=record_id,
                ambiguous=entity_type == "person" and not bool(role),
            )
        if not parties:
            for name in INSTITUTION_RE.findall(text):
                _add_seed(
                    seeds,
                    entity_type="institution",
                    name=name,
                    source_record_id=record_id,
                    ambiguous=False,
                )
        for name in PRODUCT_RE.findall(text):
            _add_seed(
                seeds,
                entity_type="product",
                name=name,
                source_record_id=record_id,
                ambiguous=False,
            )

    items = sorted(seeds.values(), key=lambda item: (item["entity_type"], item["normalized_name"]))
    result = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "count": len(items),
        "queryable_count": sum(not item["ambiguous"] for item in items),
        "items": items,
    }
    save_json(output_root / "work" / "subject_seeds.json", result)
    return result


__all__ = ["build_subject_seeds"]
