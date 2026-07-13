"""Evidence-first multi-source runner with resumable endpoint checkpoints."""

from __future__ import annotations

from datetime import datetime, timezone
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
from typing import Any, Callable

import requests

from asset_text import AssetTextExtractionTimeout, extract_local_asset_text
from config import MAX_DOWNLOAD_BYTES, USER_AGENT
from download_utils import read_binary_response
import runtime
from runtime import utc_now_iso
from storage import append_jsonl, load_json, output_dir, relative_to_output, save_bytes, save_json

from .adapters import SourceAdapter, adapter_for
from .evidence import record_fingerprints, sha256_bytes, source_record_id
from .registry import (
    endpoint_query_terms,
    load_registry,
    registry_query_sha256,
    source_tree_sha256,
)


COMPLETE = "complete"
INCOMPLETE = "incomplete"
FAILED = "failed"
REMOVAL_SCOPE_MODES = {"enumerable", "catalog_filter"}
CASE_TOKENS = (
    "处罚",
    "监管措施",
    "纪律处分",
    "自律措施",
    "市场禁入",
    "行政复议",
    "失联",
    "异常经营",
    "典型案例",
)
REFERENCE_TOKENS = ("解读", "答记者问", "问答", "征求意见", "培训", "统计")
ASSET_TEXT_TIMEOUT_SECONDS = 45


def _run_id(mode: str) -> str:
    if runtime.CURRENT_RUN_CONTEXT is not None:
        return f"{runtime.CURRENT_RUN_CONTEXT.run_id}_{mode}"
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}_sources_{os.getpid()}"


def _raw_extension(content_type: str, url: str) -> str:
    lowered = content_type.lower()
    if "json" in lowered:
        return ".json"
    if "html" in lowered:
        return ".html"
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix and len(suffix) <= 8:
        return suffix
    guessed = mimetypes.guess_extension(lowered.split(";", 1)[0].strip())
    return guessed or ".bin"


def _safe_file_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return text[:100] or "asset"


def _change_type(previous: dict[str, Any] | None, current: dict[str, Any]) -> str | None:
    if not previous:
        return "new"
    old = previous.get("fingerprints") or {}
    new = current.get("fingerprints") or {}
    if old.get("content_sha256") != new.get("content_sha256") or old.get(
        "assets_sha256"
    ) != new.get("assets_sha256"):
        return "content_changed"
    if old.get("metadata_sha256") != new.get("metadata_sha256"):
        return "metadata_changed"
    return None


def _material_lane(endpoint: dict[str, Any], metadata: dict[str, Any]) -> str:
    title = str(metadata.get("name") or "")
    nature = " ".join(str(profile.get("material_nature") or "") for profile in endpoint["profiles"])
    text = f"{title} {nature}"
    if any(token in text for token in CASE_TOKENS):
        return "case"
    if any(token in title for token in REFERENCE_TOKENS):
        return "reference"
    return str(endpoint["default_material_lane"])


def _matching_query_terms(
    endpoint: dict[str, Any],
    registry: dict[str, Any],
    item: dict[str, Any],
    parsed: dict[str, Any],
) -> list[str]:
    if endpoint["scope_mode"] not in {"catalog_filter", "query_exhaustive"}:
        return []
    terms = endpoint_query_terms(registry, endpoint)
    metadata = parsed.get("metadata") or {}
    text = " ".join(
        [
            str(item.get("title") or ""),
            str(parsed.get("plain_text") or ""),
            " ".join(str(value or "") for value in metadata.values()),
        ]
    ).casefold()
    return [term for term in terms if term.casefold() in text]


def _extract_asset_text_with_timeout(path: Path) -> str:
    if not hasattr(signal, "SIGALRM"):
        return extract_local_asset_text(path)

    def timeout_handler(signum: int, frame: Any) -> None:
        del signum, frame
        raise AssetTextExtractionTimeout(
            f"asset text extraction exceeded {ASSET_TEXT_TIMEOUT_SECONDS}s"
        )

    previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, ASSET_TEXT_TIMEOUT_SECONDS)
    try:
        return extract_local_asset_text(path)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


class SourceRunner:
    def __init__(
        self,
        *,
        registry: dict[str, Any] | None = None,
        adapter_factory: Callable[[str], SourceAdapter] = adapter_for,
        root: Path | None = None,
    ) -> None:
        self.registry = registry or load_registry()
        self.adapter_factory = adapter_factory
        self.root = root or output_dir()
        self.registry_sha256 = registry_query_sha256(self.registry)
        self.code_sha256 = source_tree_sha256()
        self.asset_session = requests.Session()
        self.asset_session.headers.update({"User-Agent": USER_AGENT})

    def _source_run_dir(self, run_id: str) -> Path:
        return self.root / "work" / "source_runs" / run_id

    def _manifest_path(self, run_id: str) -> Path:
        return self._source_run_dir(run_id) / "manifest.json"

    def _checkpoint_path(self, endpoint_id: str) -> Path:
        return self.root / "work" / "checkpoints" / "sources" / f"{endpoint_id}.json"

    def _endpoint_root(self, endpoint_id: str) -> Path:
        return self.root / "raw" / "sources" / endpoint_id

    def _records_dir(self, source_system: str) -> Path:
        return self.root / "raw" / "sources" / "records" / source_system

    def _load_or_create_manifest(
        self,
        *,
        run_id: str,
        mode: str,
        resume: bool,
        endpoint_ids: list[str],
    ) -> dict[str, Any]:
        path = self._manifest_path(run_id)
        if resume:
            manifest = load_json(path, None)
            if not isinstance(manifest, dict):
                raise FileNotFoundError(f"source run manifest does not exist: {path}")
            expected = {
                "schema_version": 1,
                "registry_query_sha256": self.registry_sha256,
                "code_sha256": self.code_sha256,
            }
            mismatches = [key for key, value in expected.items() if manifest.get(key) != value]
            if mismatches:
                raise RuntimeError("resume fingerprint mismatch: " + ", ".join(mismatches))
            return manifest
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "mode": mode,
            "status": "running",
            "started_at": utc_now_iso(),
            "registry_query_sha256": self.registry_sha256,
            "code_sha256": self.code_sha256,
            "query_set_version": self.registry.get("query_set_version"),
            "git_commit": self._git_commit(),
            "endpoint_ids": endpoint_ids,
            "endpoints": {},
        }
        save_json(path, manifest)
        return manifest

    @staticmethod
    def _git_commit() -> str | None:
        head = Path(".git/HEAD")
        if not head.exists():
            return None
        value = head.read_text(encoding="utf-8").strip()
        if value.startswith("ref: "):
            ref = Path(".git") / value.removeprefix("ref: ")
            return ref.read_text(encoding="utf-8").strip() if ref.exists() else None
        return value or None

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = utc_now_iso()
        save_json(self._manifest_path(manifest["run_id"]), manifest)

    def _save_raw_pages(
        self,
        endpoint: dict[str, Any],
        run_id: str,
        pages: list[dict[str, Any]],
    ) -> list[str]:
        outputs: list[str] = []
        directory = self._endpoint_root(endpoint["endpoint_id"]) / "lists" / run_id
        for index, page in enumerate(pages, start=1):
            body = bytes(page["body"])
            digest = sha256_bytes(body)
            suffix = _raw_extension(str(page.get("content_type") or ""), str(page["final_url"]))
            path = directory / f"{index:04d}_{digest}{suffix}"
            if not path.exists():
                save_bytes(path, body)
            outputs.append(relative_to_output(path) if self.root == output_dir() else str(path))
        return outputs

    def _save_raw_detail(
        self,
        endpoint_id: str,
        record_id: str,
        fetched: dict[str, Any],
    ) -> tuple[Path, str]:
        body = bytes(fetched["body"])
        digest = sha256_bytes(body)
        suffix = _raw_extension(str(fetched.get("content_type") or ""), str(fetched["final_url"]))
        path = self._endpoint_root(endpoint_id) / "details" / record_id / f"{digest}{suffix}"
        if not path.exists():
            save_bytes(path, body)
        return path, digest

    def _download_assets(
        self,
        endpoint: dict[str, Any],
        record_id: str,
        assets: list[dict[str, Any]],
        failures_path: Path,
        previous_record: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        completed: list[dict[str, Any]] = []
        attachment_documents: list[dict[str, Any]] = []
        all_ok = True
        previous_assets = {
            str(item.get("sha256")): item
            for item in (previous_record or {}).get("assets") or []
            if item.get("sha256")
        }
        previous_documents = {
            str((document.get("metadata") or {}).get("asset_sha256") or "")
            or str(document.get("source_record_id") or "").rsplit(":", 1)[-1]: document
            for document in (previous_record or {}).get("attachment_documents") or []
        }
        for index, asset in enumerate(assets, start=1):
            item = dict(asset)
            url = str(item.get("source_url") or "")
            if not url:
                item["download_status"] = "failed"
                item["failure_reason"] = "missing source_url"
                completed.append(item)
                all_ok = False
                continue
            try:
                response = self.asset_session.get(
                    url,
                    headers={"Referer": endpoint["url"]},
                    timeout=(10, 30),
                    allow_redirects=True,
                    stream=True,
                )
                try:
                    response.raise_for_status()
                    payload = read_binary_response(response, max_bytes=MAX_DOWNLOAD_BYTES)
                    response_url = response.url
                finally:
                    response.close()
                suffix = Path(urlsplit_path(response_url)).suffix
                if not suffix:
                    suffix = _raw_extension(payload.content_type, response_url)
                name = _safe_file_name(str(item.get("file_name") or item.get("label") or index))
                if not Path(name).suffix:
                    name += suffix
                path = (
                    self._endpoint_root(endpoint["endpoint_id"])
                    / "assets"
                    / record_id
                    / f"{payload.sha256}_{name}"
                )
                if not path.exists():
                    save_bytes(path, payload.data)
                local_file = relative_to_output(path) if self.root == output_dir() else str(path)
                item.update(
                    {
                        "download_status": "complete",
                        "sha256": payload.sha256,
                        "size_bytes": payload.size_bytes,
                        "content_type": payload.content_type,
                        "local_file": local_file,
                        "final_url": response_url,
                    }
                )
                previous_asset = previous_assets.get(payload.sha256)
                previous_document = previous_documents.get(payload.sha256)
                if previous_asset and previous_asset.get("text_extraction_status") in {
                    "complete",
                    "empty",
                    "failed",
                }:
                    item["text_extraction_status"] = previous_asset["text_extraction_status"]
                    if previous_asset.get("text_extraction_error"):
                        item["text_extraction_error"] = previous_asset["text_extraction_error"]
                    text = str(
                        ((previous_document or {}).get("content") or {}).get("plain_text") or ""
                    )
                else:
                    try:
                        text = _extract_asset_text_with_timeout(path)
                    except (Exception, AssetTextExtractionTimeout) as exc:
                        text = ""
                        item["text_extraction_status"] = "failed"
                        item["text_extraction_error"] = f"{type(exc).__name__}: {exc}"
                        append_jsonl(
                            failures_path,
                            {
                                "ts": utc_now_iso(),
                                "endpoint_id": endpoint["endpoint_id"],
                                "source_record_id": record_id,
                                "url": url,
                                "stage": "asset_text",
                                "error_type": type(exc).__name__,
                                "error_message": str(exc),
                            },
                        )
                    else:
                        item["text_extraction_status"] = "complete" if text.strip() else "empty"
                if item["text_extraction_status"] != "complete":
                    all_ok = False
                if text.strip():
                    if previous_document:
                        attachment_documents.append(previous_document)
                    else:
                        attachment_documents.append(
                            {
                                "schema_version": 1,
                                "source_record_id": f"{record_id}:asset:{payload.sha256}",
                                "metadata": {
                                    "name": item.get("label") or item.get("file_name"),
                                    "document_type": endpoint["profiles"][0].get("material_nature"),
                                    "asset_sha256": payload.sha256,
                                },
                                "content": {"plain_text": text},
                                "source": {"asset_url": url, "local_file": local_file},
                                "assets": [],
                            }
                        )
            except Exception as exc:
                all_ok = False
                item.update(
                    {
                        "download_status": "failed",
                        "failure_reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                append_jsonl(
                    failures_path,
                    {
                        "ts": utc_now_iso(),
                        "endpoint_id": endpoint["endpoint_id"],
                        "source_record_id": record_id,
                        "url": url,
                        "stage": "asset",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
            completed.append(item)
        return completed, attachment_documents, all_ok

    def _record_path(self, source_system: str, record_id: str) -> Path:
        return self._records_dir(source_system) / f"{record_id}.json"

    def _write_change(
        self,
        *,
        run_id: str,
        endpoint_id: str,
        record_id: str,
        change_type: str,
        previous: dict[str, Any] | None = None,
        current: dict[str, Any] | None = None,
    ) -> None:
        append_jsonl(
            self.root / "work" / "changes" / f"{run_id}.jsonl",
            {
                "schema_version": 1,
                "run_id": run_id,
                "endpoint_id": endpoint_id,
                "source_record_id": record_id,
                "change_type": change_type,
                "detected_at": utc_now_iso(),
                "previous_fingerprints": (previous or {}).get("fingerprints"),
                "current_fingerprints": (current or {}).get("fingerprints"),
            },
        )

    def _process_endpoint(
        self,
        *,
        endpoint: dict[str, Any],
        run_id: str,
        mode: str,
    ) -> dict[str, Any]:
        endpoint_id = endpoint["endpoint_id"]
        checkpoint_path = self._checkpoint_path(endpoint_id)
        checkpoint = load_json(
            checkpoint_path,
            {
                "schema_version": 1,
                "endpoint_id": endpoint_id,
                "registry_query_sha256": self.registry_sha256,
                "previous_discovery_complete": False,
                "records": {},
            },
        )
        state = {
            "attempted": True,
            "access_status": "network_error",
            "discovery_status": "not_started",
            "materialization_status": "not_started",
            "publish_status": "not_applicable",
            "profiles": [item["profile_id"] for item in endpoint["profiles"]],
            "discovered": 0,
            "materialized": 0,
            "in_scope": 0,
            "filtered_out": 0,
            "failed": 0,
            "assets_discovered": 0,
            "assets_completed": 0,
            "pages_completed": 0,
            "reported_total": None,
            "started_at": utc_now_iso(),
        }
        failures_path = self._source_run_dir(run_id) / "failures.jsonl"
        adapter = self.adapter_factory(endpoint["adapter"])
        health = adapter.healthcheck(endpoint)
        health_body = health.pop("_body", None)
        if health_body is not None:
            state["health_raw_files"] = self._save_raw_pages(
                endpoint,
                f"{run_id}_health",
                [
                    {
                        "body": health_body,
                        "content_type": health.get("content_type"),
                        "final_url": health.get("final_url") or endpoint["url"],
                    }
                ],
            )
            health["response_sha256"] = sha256_bytes(bytes(health_body))
        state["health"] = health
        state["access_status"] = health.get("access_status") or "network_error"
        if state["access_status"] != "reachable":
            state["finished_at"] = utc_now_iso()
            return state

        discovery = adapter.discover(endpoint, self.registry, checkpoint)
        state["discovery_status"] = discovery["discovery_status"]
        state["pages_completed"] = discovery.get("pages_completed") or 0
        state["reported_total"] = discovery.get("reported_total")
        state["raw_hit_count"] = discovery.get("raw_hit_count")
        state["filtered_count"] = discovery.get("filtered_count")
        state["pagination_links_seen"] = discovery.get("pagination_links_seen")
        state["completeness_evidence"] = discovery.get("completeness_evidence")
        state["query_execution"] = discovery.get("query_execution")
        state["result_limit_reached"] = bool(discovery.get("result_limit_reached"))
        if state["result_limit_reached"]:
            state["discovery_status"] = INCOMPLETE
        state["queries_completed"] = discovery.get("queries_completed")
        state["queries_total"] = discovery.get("queries_total")
        state["list_raw_files"] = self._save_raw_pages(
            endpoint, run_id, discovery.get("raw_pages") or []
        )
        for failure in discovery.get("failures") or []:
            append_jsonl(
                failures_path,
                {
                    "ts": utc_now_iso(),
                    "endpoint_id": endpoint_id,
                    "stage": "discovery",
                    **failure,
                },
            )
        items = discovery.get("items") or []
        state["discovered"] = len(items)
        discoveries_path = self._endpoint_root(endpoint_id) / "discoveries" / f"{run_id}.jsonl"
        seen_record_ids: set[str] = set()
        materialized_record_ids: list[str] = []

        for item in items:
            record_id = source_record_id(
                endpoint["source_system"],
                upstream_id=item.get("upstream_id"),
                final_url=item.get("url"),
            )
            item_in_scope = item.get("in_scope")
            filter_required = endpoint["scope_mode"] in {
                "catalog_filter",
                "query_exhaustive",
            }
            known_in_scope = item_in_scope is True or not filter_required
            matched_query_terms = list(item.get("matched_query_terms") or [])
            append_jsonl(
                discoveries_path,
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "endpoint_id": endpoint_id,
                    "source_record_id": record_id,
                    "url": item.get("url"),
                    "title": item.get("title"),
                    "scope_status": (
                        "matched"
                        if known_in_scope
                        else "out_of_scope"
                        if item_in_scope is False
                        else "pending_detail"
                    ),
                    "matched_query_terms": matched_query_terms,
                    "discovery_evidence": item.get("discovery_evidence") or [],
                    "discovered_at": utc_now_iso(),
                },
            )
            if item_in_scope is False:
                state["filtered_out"] += 1
                continue
            if known_in_scope:
                state["in_scope"] += 1
                seen_record_ids.add(record_id)
            try:
                record_path = self._record_path(endpoint["source_system"], record_id)
                previous = load_json(record_path, None)
                fetched = adapter.fetch(endpoint, item)
                raw_path, response_sha = self._save_raw_detail(endpoint_id, record_id, fetched)
                parsed = adapter.parse(endpoint, item, fetched)
                if item_in_scope is None and filter_required:
                    matched_query_terms = _matching_query_terms(
                        endpoint, self.registry, item, parsed
                    )
                    if not matched_query_terms:
                        state["filtered_out"] += 1
                        append_jsonl(
                            discoveries_path,
                            {
                                "schema_version": 1,
                                "run_id": run_id,
                                "endpoint_id": endpoint_id,
                                "source_record_id": record_id,
                                "url": item.get("url"),
                                "scope_status": "out_of_scope",
                                "matched_query_terms": [],
                                "evaluated_at": utc_now_iso(),
                            },
                        )
                        continue
                    state["in_scope"] += 1
                    seen_record_ids.add(record_id)
                if matched_query_terms:
                    append_jsonl(
                        discoveries_path,
                        {
                            "schema_version": 1,
                            "run_id": run_id,
                            "endpoint_id": endpoint_id,
                            "source_record_id": record_id,
                            "url": item.get("url"),
                            "scope_status": "matched",
                            "matched_query_terms": matched_query_terms,
                            "evaluated_at": utc_now_iso(),
                        },
                    )
                assets = parsed.get("assets") or []
                state["assets_discovered"] += len(assets)
                completed_assets, attachment_documents, assets_ok = self._download_assets(
                    endpoint,
                    record_id,
                    assets,
                    failures_path,
                    previous,
                )
                state["assets_completed"] += sum(
                    item.get("download_status") == COMPLETE for item in completed_assets
                )
                plain_text = str(parsed.get("plain_text") or "")
                if not plain_text.strip() and attachment_documents:
                    plain_text = "\n\n".join(
                        str((document.get("content") or {}).get("plain_text") or "")
                        for document in attachment_documents
                    ).strip()
                if not plain_text.strip():
                    raise ValueError("detail and attachments contain no acceptable text")
                ingest_status = COMPLETE if plain_text.strip() and assets_ok else INCOMPLETE
                metadata = dict(parsed.get("metadata") or {})
                fingerprints = record_fingerprints(
                    metadata=metadata,
                    plain_text=plain_text,
                    assets=completed_assets,
                    response_bytes=bytes(fetched["body"]),
                )
                fingerprints["response_sha256"] = response_sha
                raw_file = (
                    relative_to_output(raw_path) if self.root == output_dir() else str(raw_path)
                )
                record = {
                    "schema_version": 1,
                    "source_record_id": record_id,
                    "source_system": endpoint["source_system"],
                    "ingest_status": ingest_status,
                    "material_lane": _material_lane(endpoint, metadata),
                    "metadata": metadata,
                    "content": {
                        "plain_text": plain_text,
                        "html": parsed.get("content_html") or "",
                    },
                    "source": {
                        "endpoint_id": endpoint_id,
                        "scope_status": "matched",
                        "matched_query_terms": matched_query_terms,
                        "page_url": fetched["final_url"],
                        "raw_file": raw_file,
                        "profiles": [profile["profile_id"] for profile in endpoint["profiles"]],
                        "discovery_evidence": item.get("discovery_evidence") or [],
                        "run_id": run_id,
                        "fetched_at": utc_now_iso(),
                    },
                    "discovery_evidence": item.get("discovery_evidence") or [],
                    "assets": completed_assets,
                    "attachment_documents": attachment_documents,
                    "fingerprints": fingerprints,
                }
                if previous:
                    previous_source = previous.get("source") or {}
                    record["source"]["profiles"] = sorted(
                        set(previous_source.get("profiles") or [])
                        | set(record["source"]["profiles"])
                    )
                    evidence = list(previous_source.get("discovery_evidence") or [])
                    for entry in record["source"]["discovery_evidence"]:
                        if entry not in evidence:
                            evidence.append(entry)
                    record["source"]["discovery_evidence"] = evidence
                    record["discovery_evidence"] = evidence
                record_to_save = record
                previous_assets_publishable = all(
                    asset.get("download_status") == COMPLETE
                    and asset.get("text_extraction_status") in {None, COMPLETE}
                    for asset in (previous or {}).get("assets") or []
                )
                if (
                    previous
                    and previous.get("ingest_status") == COMPLETE
                    and previous_assets_publishable
                    and ingest_status != COMPLETE
                ):
                    record_to_save = previous
                    preserved_source = record_to_save.setdefault("source", {})
                    preserved_source["profiles"] = record["source"]["profiles"]
                    preserved_source["discovery_evidence"] = record["discovery_evidence"]
                    preserved_source["last_incomplete_attempt"] = {
                        "endpoint_id": endpoint_id,
                        "raw_file": raw_file,
                        "attempted_at": utc_now_iso(),
                    }
                    record_to_save["discovery_evidence"] = record["discovery_evidence"]
                save_json(record_path, record_to_save)
                materialized_record_ids.append(record_id)
                state["materialized"] += 1
                if ingest_status != COMPLETE:
                    state["failed"] += 1
                if mode == "incremental":
                    change_type = _change_type(previous, record_to_save)
                    if change_type:
                        self._write_change(
                            run_id=run_id,
                            endpoint_id=endpoint_id,
                            record_id=record_id,
                            change_type=change_type,
                            previous=previous,
                            current=record_to_save,
                        )
                record_state = checkpoint.setdefault("records", {}).setdefault(record_id, {})
                record_state.update(
                    {
                        "last_seen_at": utc_now_iso(),
                        "fingerprints": record_to_save.get("fingerprints") or fingerprints,
                    }
                )
                record_state.setdefault("missing_count", 0)
                checkpoint["updated_at"] = utc_now_iso()
                save_json(checkpoint_path, checkpoint)
            except Exception as exc:
                state["failed"] += 1
                append_jsonl(
                    failures_path,
                    {
                        "ts": utc_now_iso(),
                        "endpoint_id": endpoint_id,
                        "source_record_id": record_id,
                        "url": item.get("url"),
                        "stage": "detail",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )

        if not items and discovery.get("reported_total") != 0:
            state["materialization_status"] = INCOMPLETE
        elif state["failed"] or state["materialized"] != state["in_scope"]:
            state["materialization_status"] = INCOMPLETE
        else:
            state["materialization_status"] = COMPLETE
        endpoint_complete = (
            discovery["discovery_status"] == COMPLETE
            and state["materialization_status"] == COMPLETE
        )
        previous_complete = bool(checkpoint.get("previous_discovery_complete"))
        same_registry = checkpoint.get("registry_query_sha256") == self.registry_sha256
        supports_removal = endpoint["scope_mode"] in REMOVAL_SCOPE_MODES
        if (
            mode == "incremental"
            and endpoint_complete
            and previous_complete
            and same_registry
            and supports_removal
        ):
            for record_id, record_state in checkpoint.get("records", {}).items():
                if record_id in seen_record_ids:
                    if int(record_state.get("missing_count") or 0) >= 2:
                        current = load_json(
                            self._record_path(endpoint["source_system"], record_id), {}
                        )
                        self._write_change(
                            run_id=run_id,
                            endpoint_id=endpoint_id,
                            record_id=record_id,
                            change_type="restored",
                            current=current,
                        )
                    record_state["missing_count"] = 0
                    continue
                missing_count = int(record_state.get("missing_count") or 0) + 1
                record_state["missing_count"] = missing_count
                if missing_count == 2:
                    previous = load_json(
                        self._record_path(endpoint["source_system"], record_id), {}
                    )
                    self._write_change(
                        run_id=run_id,
                        endpoint_id=endpoint_id,
                        record_id=record_id,
                        change_type="removed",
                        previous=previous,
                    )
        elif mode == "incremental" and endpoint_complete and supports_removal:
            for record_id, record_state in checkpoint.get("records", {}).items():
                if record_id in seen_record_ids:
                    record_state["missing_count"] = 0
                elif same_registry:
                    record_state["missing_count"] = 1
                else:
                    record_state["missing_count"] = 0
        elif mode == "baseline" and endpoint_complete:
            for record_id in seen_record_ids:
                checkpoint.get("records", {}).get(record_id, {})["missing_count"] = 0

        checkpoint["registry_query_sha256"] = self.registry_sha256
        checkpoint["previous_discovery_complete"] = endpoint_complete
        checkpoint["last_run_id"] = run_id
        checkpoint["last_seen_record_ids"] = sorted(seen_record_ids)
        checkpoint["updated_at"] = utc_now_iso()
        save_json(checkpoint_path, checkpoint)

        state["materialized_record_ids"] = materialized_record_ids
        state["finished_at"] = utc_now_iso()
        return state

    def run(
        self,
        *,
        mode: str,
        endpoint_ids: list[str] | None = None,
        resume_run_id: str | None = None,
        before_subject_queries: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if mode not in {"baseline", "incremental"}:
            raise ValueError("mode must be baseline or incremental")
        selected = [
            endpoint
            for endpoint in self.registry["endpoints"]
            if endpoint_ids is None or endpoint["endpoint_id"] in endpoint_ids
        ]
        if endpoint_ids and len(selected) != len(set(endpoint_ids)):
            known = {endpoint["endpoint_id"] for endpoint in selected}
            missing = sorted(set(endpoint_ids) - known)
            raise ValueError("unknown endpoint ids: " + ", ".join(missing))
        run_id = resume_run_id or _run_id(mode)
        manifest = self._load_or_create_manifest(
            run_id=run_id,
            mode=mode,
            resume=resume_run_id is not None,
            endpoint_ids=[endpoint["endpoint_id"] for endpoint in selected],
        )
        subject_callback_called = False
        for registry_endpoint in selected:
            endpoint = dict(registry_endpoint)
            if endpoint["scope_mode"] == "subject_query":
                if before_subject_queries is not None and not subject_callback_called:
                    before_subject_queries()
                    subject_callback_called = True
                seed_doc = load_json(self.root / "work" / "subject_seeds.json", {})
                endpoint["subject_seeds"] = seed_doc.get("items") or []
            endpoint_id = endpoint["endpoint_id"]
            previous = (manifest.get("endpoints") or {}).get(endpoint_id) or {}
            if (
                resume_run_id
                and previous.get("discovery_status") == COMPLETE
                and previous.get("materialization_status") == COMPLETE
            ):
                continue
            try:
                state = self._process_endpoint(endpoint=endpoint, run_id=run_id, mode=mode)
            except Exception as exc:
                state = {
                    "attempted": True,
                    "access_status": "network_error",
                    "discovery_status": "not_started",
                    "materialization_status": "not_started",
                    "publish_status": "not_applicable",
                    "profiles": [item["profile_id"] for item in endpoint["profiles"]],
                    "failed": 1,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "finished_at": utc_now_iso(),
                }
                append_jsonl(
                    self._source_run_dir(run_id) / "failures.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "endpoint_id": endpoint_id,
                        "stage": "endpoint",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
            manifest.setdefault("endpoints", {})[endpoint_id] = state
            self._save_manifest(manifest)

        states = list((manifest.get("endpoints") or {}).values())
        complete = bool(states) and all(
            state.get("access_status") == "reachable"
            and state.get("discovery_status") == COMPLETE
            and state.get("materialization_status") == COMPLETE
            for state in states
        )
        manifest["status"] = COMPLETE if complete else INCOMPLETE
        manifest["finished_at"] = utc_now_iso()
        self._save_manifest(manifest)
        report = self._build_report(manifest, len(selected))
        report_path = self.root / "reports" / "source_baselines" / f"{run_id}.json"
        save_json(report_path, report)
        latest = report_path.parent / "latest.json"
        save_json(latest, report)
        return report

    @staticmethod
    def _build_report(manifest: dict[str, Any], selected_count: int) -> dict[str, Any]:
        states = list((manifest.get("endpoints") or {}).values())
        attempted = sum(bool(state.get("attempted")) for state in states)
        reachable = sum(state.get("access_status") == "reachable" for state in states)
        discovery_complete = sum(state.get("discovery_status") == COMPLETE for state in states)
        materialization_complete = sum(
            state.get("materialization_status") == COMPLETE for state in states
        )

        def ratio(value: int) -> float:
            return round(value / selected_count, 6) if selected_count else 0.0

        return {
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "mode": manifest["mode"],
            "status": manifest["status"],
            "generated_at": utc_now_iso(),
            "registry_query_sha256": manifest["registry_query_sha256"],
            "code_sha256": manifest["code_sha256"],
            "counts": {
                "selected_endpoints": selected_count,
                "attempted": attempted,
                "reachable": reachable,
                "discovery_complete": discovery_complete,
                "materialization_complete": materialization_complete,
                "profiles": sum(len(state.get("profiles") or []) for state in states),
                "discovered": sum(int(state.get("discovered") or 0) for state in states),
                "in_scope": sum(int(state.get("in_scope") or 0) for state in states),
                "filtered_out": sum(int(state.get("filtered_out") or 0) for state in states),
                "materialized": sum(int(state.get("materialized") or 0) for state in states),
                "failed": sum(int(state.get("failed") or 0) for state in states),
                "assets_discovered": sum(
                    int(state.get("assets_discovered") or 0) for state in states
                ),
                "assets_completed": sum(
                    int(state.get("assets_completed") or 0) for state in states
                ),
            },
            "rates": {
                "attempted": ratio(attempted),
                "reachable": ratio(reachable),
                "discovery_complete": ratio(discovery_complete),
                "materialization_complete": ratio(materialization_complete),
            },
            "endpoints": manifest.get("endpoints") or {},
        }


def urlsplit_path(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path


def clean_staging_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


__all__ = [
    "COMPLETE",
    "FAILED",
    "INCOMPLETE",
    "REMOVAL_SCOPE_MODES",
    "SourceRunner",
    "clean_staging_directory",
]
