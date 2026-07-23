"""Checked-in source registry loading and resume fingerprints."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any


DEFAULT_REGISTRY_PATH = Path(__file__).with_name("csrc_sources.json")
CURATED_OVERLAY_PATH = Path(__file__).with_name(
    "court_judicial_interpretation_sources.json"
)
EXPECTED_BASE_ENDPOINT_COUNT = 85
EXPECTED_BASE_PROFILE_COUNT = 86
EXPECTED_ENDPOINT_COUNT = 87
EXPECTED_PROFILE_COUNT = 88
ALLOWED_SCOPE_MODES = {
    "enumerable",
    "catalog_filter",
    "query_exhaustive",
    "subject_query",
}
ALLOWED_MATERIAL_LANES = {"rule", "case", "reference", "subject_snapshot", "clue"}


def registry_path() -> Path:
    configured = os.environ.get("CSRC_SOURCES_FILE")
    return Path(configured).expanduser() if configured else DEFAULT_REGISTRY_PATH


def load_registry(path: Path | None = None) -> dict[str, Any]:
    target = path or registry_path()
    with target.open("r", encoding="utf-8") as f:
        base = json.load(f)
    with CURATED_OVERLAY_PATH.open("r", encoding="utf-8") as f:
        overlay = json.load(f)
    registry = _merge_registry_overlay(base, overlay)
    validate_registry(registry)
    return registry


def _merge_registry_overlay(
    base: dict[str, Any], overlay: dict[str, Any]
) -> dict[str, Any]:
    if base.get("schema_version") != overlay.get("schema_version"):
        raise ValueError("source registry and curated overlay schema versions differ")
    registry = copy.deepcopy(base)
    registry.setdefault("query_sets", {})
    for name, terms in (overlay.get("query_sets") or {}).items():
        if name in registry["query_sets"] and registry["query_sets"][name] != terms:
            raise ValueError(f"curated overlay conflicts with query set {name!r}")
        registry["query_sets"][name] = copy.deepcopy(terms)
    endpoints = overlay.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise ValueError("curated source overlay requires endpoints")
    registry.setdefault("endpoints", []).extend(copy.deepcopy(endpoints))
    return registry


def validate_registry(
    registry: dict[str, Any],
    *,
    expected_endpoint_count: int = EXPECTED_ENDPOINT_COUNT,
    expected_profile_count: int = EXPECTED_PROFILE_COUNT,
) -> None:
    if registry.get("schema_version") != 1:
        raise ValueError("source registry schema_version must be 1")
    query_sets = registry.get("query_sets")
    endpoints = registry.get("endpoints")
    if not isinstance(query_sets, dict) or not isinstance(endpoints, list):
        raise ValueError("source registry requires query_sets and endpoints")

    endpoint_ids: set[str] = set()
    urls: set[str] = set()
    profile_ids: set[str] = set()
    profile_count = 0
    for endpoint in endpoints:
        endpoint_id = str(endpoint.get("endpoint_id") or "")
        url = str(endpoint.get("url") or "")
        if not endpoint_id or endpoint_id in endpoint_ids:
            raise ValueError(f"duplicate or empty endpoint_id: {endpoint_id!r}")
        if not url or url in urls:
            raise ValueError(f"duplicate or empty endpoint URL: {url!r}")
        endpoint_ids.add(endpoint_id)
        urls.add(url)
        if endpoint.get("scope_mode") not in ALLOWED_SCOPE_MODES:
            raise ValueError(f"invalid scope_mode for {endpoint_id}")
        if endpoint.get("default_material_lane") not in ALLOWED_MATERIAL_LANES:
            raise ValueError(f"invalid default_material_lane for {endpoint_id}")
        for name in endpoint.get("query_sets") or []:
            if name not in query_sets:
                raise ValueError(f"unknown query set {name!r} for {endpoint_id}")
        profiles = endpoint.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            raise ValueError(f"endpoint {endpoint_id} has no profiles")
        for profile in profiles:
            profile_id = str(profile.get("profile_id") or "")
            if not profile_id or profile_id in profile_ids:
                raise ValueError(f"duplicate or empty profile_id: {profile_id!r}")
            profile_ids.add(profile_id)
            profile_count += 1

    if len(endpoints) != expected_endpoint_count or profile_count != expected_profile_count:
        raise ValueError(
            f"registry must contain {expected_endpoint_count} endpoints and "
            f"{expected_profile_count} profiles, got "
            f"{len(endpoints)} and {profile_count}"
        )


def endpoint_query_terms(registry: dict[str, Any], endpoint: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for set_name in endpoint.get("query_sets") or []:
        for term in registry["query_sets"].get(set_name) or []:
            if term not in terms:
                terms.append(term)
    return terms


def registry_query_sha256(registry: dict[str, Any]) -> str:
    payload = {
        "schema_version": registry["schema_version"],
        "query_set_version": registry.get("query_set_version"),
        "query_sets": registry["query_sets"],
        "endpoints": registry["endpoints"],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_tree_sha256(project_root: Path | None = None) -> str:
    root = project_root or Path(__file__).resolve().parents[2]
    files = sorted(root.glob("*.py")) + sorted((root / "csrc_law_crawler").rglob("*.py"))
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


__all__ = [
    "ALLOWED_MATERIAL_LANES",
    "ALLOWED_SCOPE_MODES",
    "CURATED_OVERLAY_PATH",
    "DEFAULT_REGISTRY_PATH",
    "EXPECTED_BASE_ENDPOINT_COUNT",
    "EXPECTED_BASE_PROFILE_COUNT",
    "EXPECTED_ENDPOINT_COUNT",
    "EXPECTED_PROFILE_COUNT",
    "endpoint_query_terms",
    "load_registry",
    "registry_path",
    "registry_query_sha256",
    "source_tree_sha256",
    "validate_registry",
]
