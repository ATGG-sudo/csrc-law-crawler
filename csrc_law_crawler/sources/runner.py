"""Evidence-first multi-source runner with resumable endpoint checkpoints."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import signal
from types import SimpleNamespace
import threading
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from asset_text import AssetTextExtractionTimeout, extract_local_asset_text
from config import MAX_DOWNLOAD_BYTES, USER_AGENT
from csrc_law_crawler.processing.catalog.classification import (
    enforcement_classification_for,
    source_web_classification,
)
from download_utils import read_binary_response
import runtime
from runtime import utc_now_iso
from storage import append_jsonl, load_json, output_dir, relative_to_output, save_bytes, save_json

from .adapters import SourceAdapter, adapter_for, subject_seed_matches_endpoint
from .evidence import canonical_final_url, record_fingerprints, sha256_bytes, source_record_id
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
MISSING_RUNS_BEFORE_REMOVAL = 2
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
MAX_SOURCE_WORKERS = 4
SUBJECT_PROGRESS_INTERVAL = 25


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
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        # ponytail: worker extraction relies on format-specific subprocess timeouts;
        # move extraction to processes only if in-process PDF parsing becomes a measured stall.
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


def _record_needs_save(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> bool:
    if previous is None:
        return True
    stable_keys = (
        "ingest_status",
        "material_lane",
        "metadata",
        "content",
        "assets",
        "attachment_documents",
        "web_category_leaf",
        "web_category_path",
        "web_category_provenance",
        "page_role",
        "enforcement_classification",
    )
    if any(previous.get(key) != current.get(key) for key in stable_keys):
        return True
    previous_source = previous.get("source") or {}
    current_source = current.get("source") or {}
    previous_fingerprints = previous.get("fingerprints") or {}
    current_fingerprints = current.get("fingerprints") or {}
    if any(
        previous_fingerprints.get(key) != current_fingerprints.get(key)
        for key in ("metadata_sha256", "content_sha256", "assets_sha256")
    ):
        return True
    return any(
        previous_source.get(key) != current_source.get(key)
        for key in ("profiles", "discovery_evidence", "http_validators")
    )


def _subject_query_fingerprint(
    endpoint: dict[str, Any],
    seed: dict[str, Any],
    registry_sha256: str,
) -> str:
    payload = {
        "endpoint_url": endpoint["url"],
        "entity_type": seed.get("entity_type"),
        "normalized_name": seed.get("normalized_name"),
        "query_targets": seed.get("query_targets") or [],
        "registry_query_sha256": registry_sha256,
        "seed_id": seed.get("seed_id"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
        self._asset_sessions = threading.local()
        self._append_lock = threading.Lock()

    def _source_run_dir(self, run_id: str) -> Path:
        return self.root / "work" / "source_runs" / run_id

    def _manifest_path(self, run_id: str) -> Path:
        return self._source_run_dir(run_id) / "manifest.json"

    def _checkpoint_path(self, endpoint_id: str) -> Path:
        return self.root / "work" / "checkpoints" / "sources" / f"{endpoint_id}.json"

    def _subject_journal_path(self, endpoint_id: str) -> Path:
        return self._checkpoint_path(endpoint_id).with_suffix(".subjects.jsonl")

    def _endpoint_root(self, endpoint_id: str) -> Path:
        return self.root / "raw" / "sources" / endpoint_id

    def _records_dir(self, source_system: str) -> Path:
        return self.root / "raw" / "sources" / "records" / source_system

    def _asset_http_session(self) -> requests.Session:
        if threading.current_thread() is threading.main_thread():
            return self.asset_session
        session = getattr(self._asset_sessions, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
            self._asset_sessions.session = session
        return session

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

    def _append_jsonl(self, path: Path, item: dict[str, Any]) -> None:
        with self._append_lock:
            append_jsonl(path, item)

    def _load_subject_journal(self, endpoint_id: str) -> dict[str, dict[str, Any]]:
        path = self._subject_journal_path(endpoint_id)
        latest: dict[str, dict[str, Any]] = {}
        if not path.is_file():
            return latest
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("query_fingerprint"):
                latest[str(item["query_fingerprint"])] = item
        return latest

    def _cached_subject_result_is_usable(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
    ) -> bool:
        if item.get("status") != COMPLETE:
            return False
        return all(
            self._record_path(endpoint["source_system"], str(record_id)).is_file()
            for record_id in item.get("materialized_record_ids") or []
        )

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
        previous_assets_by_url = {
            canonical_final_url(str(item.get("source_url") or "")): item
            for item in (previous_record or {}).get("assets") or []
            if item.get("source_url")
        }
        previous_documents = {
            str((document.get("metadata") or {}).get("asset_sha256") or "")
            or str(document.get("source_record_id") or "").rsplit(":", 1)[-1]: document
            for document in (previous_record or {}).get("attachment_documents") or []
        }
        for index, asset in enumerate(assets, start=1):
            item = dict(asset)
            prefetched_body = item.pop("_prefetched_body", None)
            prefetched_content_type = str(item.pop("_prefetched_content_type", "") or "")
            prefetched_final_url = str(item.pop("_prefetched_final_url", "") or "")
            url = str(item.get("source_url") or "")
            if not url:
                item["download_status"] = "failed"
                item["failure_reason"] = "missing source_url"
                completed.append(item)
                all_ok = False
                continue
            cached_asset = previous_assets_by_url.get(canonical_final_url(url))
            if cached_asset is not None and prefetched_body is None:
                cached_path = Path(str(cached_asset.get("local_file") or ""))
                if cached_path and not cached_path.is_absolute():
                    cached_path = self.root / cached_path
                if cached_asset.get("download_status") == COMPLETE and cached_path.is_file():
                    reused = dict(cached_asset)
                    completed.append(reused)
                    document = previous_documents.get(str(reused.get("sha256") or ""))
                    if document:
                        attachment_documents.append(document)
                    if reused.get("text_extraction_status") != COMPLETE:
                        all_ok = False
                    continue
            try:
                if prefetched_body is not None:
                    response_url = prefetched_final_url or url
                    payload = read_binary_response(
                        SimpleNamespace(
                            content=bytes(prefetched_body),
                            headers={"Content-Type": prefetched_content_type},
                        ),
                        max_bytes=MAX_DOWNLOAD_BYTES,
                    )
                else:
                    response = self._asset_http_session().get(
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
                        self._append_jsonl(
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
                self._append_jsonl(
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
        self._append_jsonl(
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

    def _consume_discovery(
        self,
        *,
        endpoint: dict[str, Any],
        adapter: SourceAdapter,
        discovery: dict[str, Any],
        run_id: str,
        mode: str,
        checkpoint: dict[str, Any],
        state: dict[str, Any],
        seen_record_ids: set[str],
        materialized_record_ids: list[str],
    ) -> tuple[int, int]:
        endpoint_id = endpoint["endpoint_id"]
        failures_path = self._source_run_dir(run_id) / "failures.jsonl"
        state.setdefault("list_raw_files", []).extend(
            self._save_raw_pages(endpoint, run_id, discovery.get("raw_pages") or [])
        )
        for failure in discovery.get("failures") or []:
            self._append_jsonl(
                failures_path,
                {
                    "ts": utc_now_iso(),
                    "endpoint_id": endpoint_id,
                    "stage": "discovery",
                    **failure,
                },
            )
        items = discovery.get("items") or []
        state["discovered"] += len(items)
        failures_before = int(state["failed"])
        discoveries_path = self._endpoint_root(endpoint_id) / "discoveries" / f"{run_id}.jsonl"

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
            self._append_jsonl(
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
                fetched = adapter.fetch(endpoint, item, previous)
                if fetched.get("not_modified"):
                    if previous is None:
                        raise RuntimeError("HTTP 304 received without a previous source record")
                    if not known_in_scope:
                        state["in_scope"] += 1
                        seen_record_ids.add(record_id)
                    state["not_modified"] = int(state.get("not_modified") or 0) + 1
                    state["materialized"] += 1
                    materialized_record_ids.append(record_id)
                    record_state = checkpoint.setdefault("records", {}).setdefault(record_id, {})
                    record_state.update(
                        {
                            "last_seen_at": utc_now_iso(),
                            "fingerprints": previous.get("fingerprints") or {},
                        }
                    )
                    record_state.setdefault("missing_count", 0)
                    continue
                raw_path, response_sha = self._save_raw_detail(endpoint_id, record_id, fetched)
                parsed = adapter.parse(endpoint, item, fetched)
                if item_in_scope is None and filter_required:
                    matched_query_terms = _matching_query_terms(
                        endpoint, self.registry, item, parsed
                    )
                    if not matched_query_terms:
                        state["filtered_out"] += 1
                        self._append_jsonl(
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
                    self._append_jsonl(
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
                    asset.get("download_status") == COMPLETE for asset in completed_assets
                )
                plain_text = str(parsed.get("plain_text") or "")
                if not plain_text.strip() and attachment_documents:
                    plain_text = "\n\n".join(
                        str((document.get("content") or {}).get("plain_text") or "")
                        for document in attachment_documents
                    ).strip()
                if not plain_text.strip():
                    raise ValueError("detail and attachments contain no acceptable text")
                ingest_status = COMPLETE if assets_ok else INCOMPLETE
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
                headers = fetched.get("headers") or {}
                http_validators = {
                    key: value
                    for key, value in {
                        "etag": headers.get("ETag") or headers.get("etag"),
                        "last_modified": headers.get("Last-Modified")
                        or headers.get("last-modified"),
                    }.items()
                    if value
                }
                material_lane = _material_lane(endpoint, metadata)
                web_classification = source_web_classification(
                    metadata,
                    page_url=fetched["final_url"],
                    material_lane=material_lane,
                    endpoint_profiles=endpoint["profiles"],
                )
                enforcement_classification = enforcement_classification_for(
                    {
                        "title": metadata.get("name"),
                        "metadata": metadata,
                        "sources": [{"page_url": fetched["final_url"], **web_classification}],
                    },
                    material_classification={
                        "lane": "reference" if material_lane == "case" else material_lane,
                        "category": (
                            "enforcement_reference" if material_lane == "case" else "unknown"
                        ),
                    },
                )
                record = {
                    "schema_version": 1,
                    "source_record_id": record_id,
                    "source_system": endpoint["source_system"],
                    "ingest_status": ingest_status,
                    "material_lane": material_lane,
                    **web_classification,
                    "enforcement_classification": enforcement_classification,
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
                        "http_validators": http_validators,
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
                    if not http_validators and previous_source.get("http_validators"):
                        record["source"]["http_validators"] = previous_source["http_validators"]
                    previous_fingerprints = previous.get("fingerprints") or {}
                    same_material_payload = all(
                        previous_fingerprints.get(key) == fingerprints.get(key)
                        for key in ("content_sha256", "assets_sha256")
                    )
                    if (
                        same_material_payload
                        and previous_source.get("endpoint_id")
                        and previous_source.get("endpoint_id") != endpoint_id
                    ):
                        # One URL can appear in several registry endpoints whose
                        # profile defaults disagree. Keep its stable owner metadata
                        # while still accumulating cross-endpoint evidence.
                        record["metadata"] = previous.get("metadata") or metadata
                        record["material_lane"] = (
                            previous.get("material_lane") or record["material_lane"]
                        )
                        previous_metadata_sha = previous_fingerprints.get("metadata_sha256")
                        if previous_metadata_sha:
                            record["fingerprints"]["metadata_sha256"] = previous_metadata_sha
                        stable_source = dict(previous_source)
                        stable_source["profiles"] = record["source"]["profiles"]
                        stable_source["discovery_evidence"] = evidence
                        if http_validators:
                            stable_source["http_validators"] = http_validators
                        record["source"] = stable_source
                record_to_save = record
                preserved_previous = False
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
                    preserved_previous = True
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
                if preserved_previous or _record_needs_save(previous, record_to_save):
                    save_json(record_path, record_to_save)
                else:
                    state["unchanged_records"] = int(state.get("unchanged_records") or 0) + 1
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
            except Exception as exc:
                state["failed"] += 1
                self._append_jsonl(
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
        return len(items), int(state["failed"]) - failures_before

    def _process_subject_queries(
        self,
        *,
        endpoint: dict[str, Any],
        adapter: SourceAdapter,
        checkpoint: dict[str, Any],
        checkpoint_path: Path,
        state: dict[str, Any],
        run_id: str,
        mode: str,
        refresh_subjects: bool,
        retry_failed: bool,
    ) -> dict[str, Any]:
        endpoint_id = endpoint["endpoint_id"]
        seeds = [
            seed
            for seed in endpoint.get("subject_seeds") or []
            if subject_seed_matches_endpoint(endpoint, seed)
        ]
        journal = self._load_subject_journal(endpoint_id)
        seen_record_ids: set[str] = set()
        materialized_record_ids: list[str] = []
        state.update(
            {
                "cached_queries": 0,
                "queries_completed": 0,
                "queries_total": len(seeds),
                "reported_total": 0,
                "list_raw_files": [],
            }
        )
        if (urlsplit(endpoint["url"]).hostname or "").lower() != "gs.amac.org.cn":
            discovery = adapter.discover(endpoint, self.registry, checkpoint)
            state["pages_completed"] = int(discovery.get("pages_completed") or 0)
            self._consume_discovery(
                endpoint=endpoint,
                adapter=adapter,
                discovery=discovery,
                run_id=run_id,
                mode=mode,
                checkpoint=checkpoint,
                state=state,
                seen_record_ids=seen_record_ids,
                materialized_record_ids=materialized_record_ids,
            )
            state["failed"] += 1
            state["discovery_status"] = INCOMPLETE
            state["materialization_status"] = COMPLETE
            checkpoint["registry_query_sha256"] = self.registry_sha256
            checkpoint["previous_discovery_complete"] = False
            checkpoint["last_run_id"] = run_id
            checkpoint["last_seen_record_ids"] = []
            checkpoint["updated_at"] = utc_now_iso()
            save_json(checkpoint_path, checkpoint)
            state["materialized_record_ids"] = []
            state["execution_status"] = "finished"
            state["finished_at"] = utc_now_iso()
            return state

        for index, seed in enumerate(seeds, start=1):
            fingerprint = _subject_query_fingerprint(endpoint, seed, self.registry_sha256)
            cached = journal.get(fingerprint)
            if (
                not refresh_subjects
                and cached is not None
                and self._cached_subject_result_is_usable(endpoint, cached)
            ):
                record_ids = [str(value) for value in cached.get("materialized_record_ids") or []]
                seen_record_ids.update(record_ids)
                materialized_record_ids.extend(record_ids)
                result_count = int(cached.get("result_count") or 0)
                state["cached_queries"] += 1
                state["queries_completed"] += 1
                state["reported_total"] += result_count
                state["discovered"] += result_count
                state["in_scope"] += result_count
                state["materialized"] += len(record_ids)
                continue
            if (
                not retry_failed
                and cached is not None
                and cached.get("status") == FAILED
                and cached.get("run_id") == run_id
            ):
                state["failed"] += 1
                continue

            single_seed_endpoint = dict(endpoint)
            single_seed_endpoint["subject_seeds"] = [seed]
            materialized_before = len(materialized_record_ids)
            try:
                discovery = adapter.discover(single_seed_endpoint, self.registry, checkpoint)
                state["pages_completed"] += int(discovery.get("pages_completed") or 0)
                state["reported_total"] += int(discovery.get("reported_total") or 0)
                _, failed_delta = self._consume_discovery(
                    endpoint=endpoint,
                    adapter=adapter,
                    discovery=discovery,
                    run_id=run_id,
                    mode=mode,
                    checkpoint=checkpoint,
                    state=state,
                    seen_record_ids=seen_record_ids,
                    materialized_record_ids=materialized_record_ids,
                )
                success = discovery.get("discovery_status") == COMPLETE and failed_delta == 0
                if success:
                    state["queries_completed"] += 1
                elif failed_delta == 0:
                    state["failed"] += 1
                journal_item = {
                    "schema_version": 1,
                    "run_id": run_id,
                    "endpoint_id": endpoint_id,
                    "query_fingerprint": fingerprint,
                    "seed_id": seed.get("seed_id"),
                    "entity_type": seed.get("entity_type"),
                    "normalized_name": seed.get("normalized_name"),
                    "status": COMPLETE if success else FAILED,
                    "result_count": int(discovery.get("reported_total") or 0),
                    "materialized_record_ids": materialized_record_ids[materialized_before:],
                    "updated_at": utc_now_iso(),
                }
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                state["failed"] += 1
                journal_item = {
                    "schema_version": 1,
                    "run_id": run_id,
                    "endpoint_id": endpoint_id,
                    "query_fingerprint": fingerprint,
                    "seed_id": seed.get("seed_id"),
                    "entity_type": seed.get("entity_type"),
                    "normalized_name": seed.get("normalized_name"),
                    "status": FAILED,
                    "result_count": 0,
                    "materialized_record_ids": [],
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "updated_at": utc_now_iso(),
                }
                self._append_jsonl(
                    self._source_run_dir(run_id) / "failures.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "endpoint_id": endpoint_id,
                        "seed_id": seed.get("seed_id"),
                        "stage": "subject_query",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
            self._append_jsonl(self._subject_journal_path(endpoint_id), journal_item)
            journal[fingerprint] = journal_item
            if index % SUBJECT_PROGRESS_INTERVAL == 0 or index == len(seeds):
                checkpoint["subject_progress"] = {
                    "completed": index,
                    "total": len(seeds),
                    "cached": state["cached_queries"],
                    "failed": state["failed"],
                    "updated_at": utc_now_iso(),
                }
                save_json(checkpoint_path, checkpoint)
                runtime.log_event(
                    "subject_query_progress",
                    message=(
                        f"{endpoint_id}: {index}/{len(seeds)}，"
                        f"缓存 {state['cached_queries']}，失败 {state['failed']}"
                    ),
                    endpoint_id=endpoint_id,
                    completed=index,
                    total=len(seeds),
                    cached=state["cached_queries"],
                    failed=state["failed"],
                )

        complete = int(state["queries_completed"]) == len(seeds) and not state["failed"]
        state["discovery_status"] = COMPLETE if complete else INCOMPLETE
        state["materialization_status"] = COMPLETE if complete else INCOMPLETE
        checkpoint["registry_query_sha256"] = self.registry_sha256
        checkpoint["previous_discovery_complete"] = complete
        checkpoint["last_run_id"] = run_id
        checkpoint["last_seen_record_ids"] = sorted(seen_record_ids)
        checkpoint["updated_at"] = utc_now_iso()
        save_json(checkpoint_path, checkpoint)
        state["materialized_record_ids"] = materialized_record_ids
        state["execution_status"] = "finished"
        state["finished_at"] = utc_now_iso()
        return state

    def _process_endpoint(
        self,
        *,
        endpoint: dict[str, Any],
        run_id: str,
        mode: str,
        refresh_subjects: bool = False,
        retry_failed: bool = False,
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
        state: dict[str, Any] = {
            "attempted": True,
            "execution_status": "running",
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
            state["http_stats"] = dict(getattr(adapter, "stats", {}))
            state["execution_status"] = "finished"
            state["finished_at"] = utc_now_iso()
            return state

        if endpoint["scope_mode"] == "subject_query":
            state = self._process_subject_queries(
                endpoint=endpoint,
                adapter=adapter,
                checkpoint=checkpoint,
                checkpoint_path=checkpoint_path,
                state=state,
                run_id=run_id,
                mode=mode,
                refresh_subjects=refresh_subjects,
                retry_failed=retry_failed,
            )
            state["http_stats"] = dict(getattr(adapter, "stats", {}))
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
        items = discovery.get("items") or []
        seen_record_ids: set[str] = set()
        materialized_record_ids: list[str] = []
        self._consume_discovery(
            endpoint=endpoint,
            adapter=adapter,
            discovery=discovery,
            run_id=run_id,
            mode=mode,
            checkpoint=checkpoint,
            state=state,
            seen_record_ids=seen_record_ids,
            materialized_record_ids=materialized_record_ids,
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
                    if int(record_state.get("missing_count") or 0) >= MISSING_RUNS_BEFORE_REMOVAL:
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
                if missing_count == MISSING_RUNS_BEFORE_REMOVAL:
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
        state["http_stats"] = dict(getattr(adapter, "stats", {}))
        state["execution_status"] = "finished"
        state["finished_at"] = utc_now_iso()
        return state

    def run(
        self,
        *,
        mode: str,
        endpoint_ids: list[str] | None = None,
        resume_run_id: str | None = None,
        workers: int = 1,
        refresh_subjects: bool = False,
        retry_failed: bool = False,
        retry_incomplete: bool = False,
    ) -> dict[str, Any]:
        if mode not in {"baseline", "incremental"}:
            raise ValueError("mode must be baseline or incremental")
        if not 1 <= workers <= MAX_SOURCE_WORKERS:
            raise ValueError(f"workers must be between 1 and {MAX_SOURCE_WORKERS}")
        requested_ids = set(endpoint_ids) if endpoint_ids is not None else None
        selected = [
            endpoint
            for endpoint in self.registry["endpoints"]
            if requested_ids is None or endpoint["endpoint_id"] in requested_ids
        ]
        if requested_ids and len(selected) != len(requested_ids):
            known = {endpoint["endpoint_id"] for endpoint in selected}
            missing = sorted(requested_ids - known)
            raise ValueError("unknown endpoint ids: " + ", ".join(missing))
        run_id = resume_run_id or _run_id(mode)
        manifest = self._load_or_create_manifest(
            run_id=run_id,
            mode=mode,
            resume=resume_run_id is not None,
            endpoint_ids=[endpoint["endpoint_id"] for endpoint in selected],
        )
        manifest["execution_status"] = "running"

        def should_skip(endpoint: dict[str, Any]) -> bool:
            if resume_run_id is None:
                return False
            endpoint_id = endpoint["endpoint_id"]
            previous = (manifest.get("endpoints") or {}).get(endpoint_id) or {}
            execution_status = previous.get("execution_status")
            if execution_status is None and previous.get("finished_at"):
                execution_status = "finished"
            if execution_status in {"running", "interrupted", None}:
                return False
            if execution_status == FAILED:
                return not retry_failed
            incomplete = (
                previous.get("discovery_status") != COMPLETE
                or previous.get("materialization_status") != COMPLETE
            )
            return not (incomplete and retry_incomplete)

        def running_state(endpoint: dict[str, Any]) -> dict[str, Any]:
            return {
                "attempted": True,
                "execution_status": "running",
                "access_status": "not_started",
                "discovery_status": "not_started",
                "materialization_status": "not_started",
                "publish_status": "not_applicable",
                "profiles": [item["profile_id"] for item in endpoint["profiles"]],
                "started_at": utc_now_iso(),
            }

        def process(endpoint: dict[str, Any]) -> dict[str, Any]:
            endpoint_id = endpoint["endpoint_id"]
            try:
                return self._process_endpoint(
                    endpoint=endpoint,
                    run_id=run_id,
                    mode=mode,
                    refresh_subjects=refresh_subjects,
                    retry_failed=retry_failed,
                )
            except Exception as exc:
                self._append_jsonl(
                    self._source_run_dir(run_id) / "failures.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "endpoint_id": endpoint_id,
                        "stage": "endpoint",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
                return {
                    "attempted": True,
                    "execution_status": FAILED,
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

        def record_started(endpoint: dict[str, Any]) -> None:
            manifest.setdefault("endpoints", {})[endpoint["endpoint_id"]] = running_state(endpoint)
            self._save_manifest(manifest)

        def record_finished(endpoint: dict[str, Any], state: dict[str, Any]) -> None:
            manifest.setdefault("endpoints", {})[endpoint["endpoint_id"]] = state
            self._save_manifest(manifest)

        non_subject = [
            dict(endpoint)
            for endpoint in selected
            if endpoint["scope_mode"] != "subject_query" and not should_skip(endpoint)
        ]
        subject = [
            dict(endpoint)
            for endpoint in selected
            if endpoint["scope_mode"] == "subject_query" and not should_skip(endpoint)
        ]

        try:
            if workers == 1:
                for endpoint in non_subject:
                    record_started(endpoint)
                    record_finished(endpoint, process(endpoint))
            elif non_subject:
                queues: dict[str, list[dict[str, Any]]] = {}
                for endpoint in non_subject:
                    host = (urlsplit(endpoint["url"]).hostname or endpoint["endpoint_id"]).lower()
                    queues.setdefault(host, []).append(endpoint)
                ready_hosts = list(queues)
                active: dict[Future[dict[str, Any]], tuple[str, dict[str, Any]]] = {}
                executor = ThreadPoolExecutor(max_workers=workers)

                def submit(host: str) -> None:
                    endpoint = queues[host].pop(0)
                    manifest.setdefault("endpoints", {})[endpoint["endpoint_id"]] = running_state(
                        endpoint
                    )
                    active[executor.submit(process, endpoint)] = (host, endpoint)

                def submit_ready_hosts() -> None:
                    submitted = False
                    while ready_hosts and len(active) < workers:
                        submit(ready_hosts.pop(0))
                        submitted = True
                    if submitted:
                        self._save_manifest(manifest)

                try:
                    submit_ready_hosts()
                    while active:
                        done, _ = wait(active, return_when=FIRST_COMPLETED)
                        for future in done:
                            host, endpoint = active.pop(future)
                            record_finished(endpoint, future.result())
                            if queues[host]:
                                ready_hosts.append(host)
                        submit_ready_hosts()
                except KeyboardInterrupt:
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                else:
                    executor.shutdown(wait=True)

            if subject:
                from .subjects import build_subject_seeds

                seed_path = self.root / "work" / "subject_seeds.json"
                changes_path = self.root / "work" / "changes" / f"{run_id}.jsonl"
                if mode == "baseline" or not seed_path.is_file() or changes_path.is_file():
                    seed_doc = build_subject_seeds(self.root)
                else:
                    seed_doc = load_json(seed_path, {})
                for endpoint in subject:
                    endpoint["subject_seeds"] = seed_doc.get("items") or []
                    record_started(endpoint)
                    record_finished(endpoint, process(endpoint))
        except KeyboardInterrupt:
            for state in (manifest.get("endpoints") or {}).values():
                if state.get("execution_status") == "running":
                    state["execution_status"] = "interrupted"
                    state["interrupted_at"] = utc_now_iso()
            manifest["status"] = "interrupted"
            manifest["execution_status"] = "interrupted"
            manifest["interrupted_at"] = utc_now_iso()
            self._save_manifest(manifest)
            raise

        states = list((manifest.get("endpoints") or {}).values())
        complete = bool(states) and all(
            state.get("access_status") == "reachable"
            and state.get("discovery_status") == COMPLETE
            and state.get("materialization_status") == COMPLETE
            for state in states
        )
        manifest["status"] = COMPLETE if complete else INCOMPLETE
        manifest["execution_status"] = "finished"
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
                "subject_cache_hits": sum(
                    int(state.get("cached_queries") or 0) for state in states
                ),
                "not_modified": sum(int(state.get("not_modified") or 0) for state in states),
                "unchanged_records": sum(
                    int(state.get("unchanged_records") or 0) for state in states
                ),
                "http_request_attempts": sum(
                    int((state.get("http_stats") or {}).get("request_attempts") or 0)
                    for state in states
                ),
                "http_retries": sum(
                    int((state.get("http_stats") or {}).get("retries") or 0) for state in states
                ),
                "http_request_seconds": round(
                    sum(
                        float((state.get("http_stats") or {}).get("request_seconds") or 0)
                        for state in states
                    ),
                    3,
                ),
                "http_sleep_seconds": round(
                    sum(
                        float((state.get("http_stats") or {}).get("sleep_seconds") or 0)
                        for state in states
                    ),
                    3,
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


__all__ = [
    "COMPLETE",
    "FAILED",
    "INCOMPLETE",
    "MISSING_RUNS_BEFORE_REMOVAL",
    "REMOVAL_SCOPE_MODES",
    "SourceRunner",
]
