"""Pass 2：历次修订 + 关联法规。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api import fetch_change_law, fetch_relative_files
from client import HumanLikeClient
from runtime import log_event
from revisions_graph import (
    UnionFind,
    build_revisions_document,
    normalize_version_node,
)
from storage import (
    iter_reg_law_ids,
    load_checkpoint,
    load_reg_metadata,
    load_json,
    publish_json_bundle,
    related_laws_path,
    reports_dir,
    revision_evidence_cache_path,
    revisions_path,
    save_checkpoint,
    save_json,
    utc_now_iso,
)


@dataclass(frozen=True)
class Pass2RevisionResult:
    current_id: str
    evidence_record: dict[str, Any] | None


def _normalize_related_item(item: dict[str, Any]) -> dict[str, Any]:
    to_id = (
        item.get("putAndLawId")
        or item.get("secFutrsLawId")
        or item.get("lawId")
        or item.get("id")
    )
    return {
        "to_law_id": to_id,
        "name": item.get("secFutrsLawName") or item.get("name") or item.get("title"),
        "fileno": item.get("fileno"),
        "relation_type": item.get("relationType") or item.get("rltnType"),
        "raw": item,
    }


def _load_existing_revisions_state(
    version_records: dict[str, dict[str, Any]],
    uf: UnionFind,
) -> list[dict[str, Any]]:
    existing = load_json(revisions_path(), {})
    if existing.get("schema_version") != 2:
        return []
    evidence_records: list[dict[str, Any]] = []
    for family in (existing.get("families") or {}).values():
        member_ids: list[str] = []
        for node in family.get("versions") or []:
            law_id = str(node.get("id") or "")
            if not law_id:
                continue
            version_records[law_id] = node
            uf.add(law_id)
            member_ids.append(law_id)
        if len(member_ids) > 1:
            for law_id in member_ids[1:]:
                uf.union(member_ids[0], law_id)
        evidence_records.extend(family.get("evidence") or [])
    return evidence_records


def _initialize_pass2_state(
    checkpoint: dict[str, Any],
    *,
    rebuild: bool,
) -> dict[str, Any]:
    pass_state = checkpoint.setdefault("pass2", {"completed_ids": [], "failures": []})
    if rebuild:
        pass_state["completed_ids"] = []
        pass_state["failures"] = []
        pass_state.pop("finished_at", None)
    pass_state["status"] = "in_progress"
    pass_state["started_at"] = utc_now_iso()
    save_checkpoint(checkpoint)
    return pass_state


def _fetch_revision_response(
    client: HumanLikeClient,
    law_id: str,
    *,
    refresh_revision_cache: bool,
) -> dict[str, Any]:
    cache_path = revision_evidence_cache_path(law_id)
    if cache_path.exists() and not refresh_revision_cache:
        return load_json(cache_path, {})
    change_resp = fetch_change_law(client, law_id)
    save_json(cache_path, change_resp)
    return change_resp


def _apply_revision_response(
    *,
    queried_law_id: str,
    local_meta: dict[str, Any] | None,
    change_resp: dict[str, Any],
    version_records: dict[str, dict[str, Any]],
    uf: UnionFind,
) -> Pass2RevisionResult:
    law = change_resp.get("law") or {}
    current_id = str(law.get("secFutrsLawId") or queried_law_id)
    current_meta = load_reg_metadata(current_id) or local_meta

    current_node = normalize_version_node(law, local_meta=current_meta)
    version_records[current_id] = current_node
    uf.add(current_id)

    revision_member_ids = {current_id}
    for evlt in change_resp.get("evltList") or []:
        evlt_id = str(evlt.get("secFutrsLawId") or "")
        if not evlt_id:
            continue
        evlt_meta = load_reg_metadata(evlt_id)
        version_records[evlt_id] = normalize_version_node(evlt, local_meta=evlt_meta)
        uf.add(evlt_id)
        uf.union(current_id, evlt_id)
        revision_member_ids.add(evlt_id)

    evidence_record = None
    if len(revision_member_ids) > 1:
        evidence_record = {
            "source": "neris.changeLaw",
            "queried_law_id": queried_law_id,
            "member_ids": sorted(revision_member_ids),
            "retrieved_at": utc_now_iso(),
        }
    return Pass2RevisionResult(current_id=current_id, evidence_record=evidence_record)


def _merge_related_items(
    related_items: dict[str, list[dict[str, Any]]],
    law_id: str,
    put_list: list[Any],
) -> None:
    normalized = [
        _normalize_related_item(item) for item in put_list if isinstance(item, dict)
    ]
    if not normalized:
        return
    existing = related_items.get(law_id, [])
    seen = {(item.get("to_law_id"), item.get("name")) for item in existing}
    for item in normalized:
        key = (item.get("to_law_id"), item.get("name"))
        if key not in seen:
            existing.append(item)
            seen.add(key)
    related_items[law_id] = existing


def _fetch_and_merge_related_items(
    client: HumanLikeClient,
    *,
    law_id: str,
    related_items: dict[str, list[dict[str, Any]]],
) -> None:
    rel_resp = fetch_relative_files(client, law_id)
    _merge_related_items(related_items, law_id, rel_resp.get("putLawList") or [])


def _record_pass2_failure(
    checkpoint: dict[str, Any],
    pass_state: dict[str, Any],
    *,
    law_id: str,
    exc: Exception,
) -> dict[str, Any]:
    failure = {"id": law_id, "error": str(exc), "at": utc_now_iso()}
    pass_state.setdefault("failures", []).append(failure)
    save_checkpoint(checkpoint)
    return failure


def _write_pass2_failure_report(
    *,
    rebuild: bool,
    total: int,
    completed: int,
    failures: list[dict[str, Any]],
) -> None:
    failure_doc = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "status": "incomplete",
        "rebuild": rebuild,
        "requested_laws": total,
        "completed_laws": completed,
        "failures": failures,
    }
    save_json(reports_dir() / "pass2_failures.json", failure_doc)


def _mark_pass2_incomplete(
    checkpoint: dict[str, Any],
    pass_state: dict[str, Any],
) -> None:
    pass_state["status"] = "incomplete"
    pass_state.pop("finished_at", None)
    save_checkpoint(checkpoint)


def _complete_pass2_state(
    checkpoint: dict[str, Any],
    pass_state: dict[str, Any],
) -> None:
    pass_state["finished_at"] = utc_now_iso()
    pass_state["status"] = "complete"
    pass_state["failures"] = []
    save_checkpoint(checkpoint)


def run_pass2(
    client: HumanLikeClient,
    *,
    limit: int | None = None,
    rebuild: bool = False,
    fetch_related: bool = True,
    refresh_revision_cache: bool = False,
) -> dict[str, Any]:
    if rebuild and limit is not None:
        raise ValueError(
            "--rebuild-relations 不能与 --limit 同时使用；"
            "全量关系图禁止被局部结果覆盖"
        )
    checkpoint = load_checkpoint()
    pass_state = _initialize_pass2_state(checkpoint, rebuild=rebuild)
    done: set[str] = set(pass_state.get("completed_ids") or [])

    law_ids = iter_reg_law_ids(limit=limit)
    total = len(law_ids)
    log_event(
        "pass2_started",
        message=f"\n=== Pass 2：修订关系 + 关联法规（{total} 条法规）===",
        total=total,
    )

    version_records: dict[str, dict[str, Any]] = {}
    related_items: dict[str, list[dict[str, Any]]] = load_json(
        related_laws_path(), {"updated_at": None, "items": {}}
    ).get("items", {})
    uf = UnionFind()
    evidence_records = (
        [] if rebuild else _load_existing_revisions_state(version_records, uf)
    )
    if not rebuild and done and not version_records:
        raise RuntimeError(
            "检测到旧版或缺失的 revisions.json；请使用 --rebuild-relations 重建"
        )

    run_failures: list[dict[str, Any]] = []
    for idx, law_id in enumerate(law_ids, start=1):
        if law_id in done:
            continue
        meta = load_reg_metadata(law_id)
        name = (meta or {}).get("name") or law_id
        log_event(
            "pass2_law_started",
            message=f"[{idx}/{total}] {name}",
            index=idx,
            total=total,
            law_id=law_id,
            name=name,
        )

        try:
            change_resp = _fetch_revision_response(
                client,
                law_id,
                refresh_revision_cache=refresh_revision_cache,
            )
            revision_result = _apply_revision_response(
                queried_law_id=law_id,
                local_meta=meta,
                change_resp=change_resp,
                version_records=version_records,
                uf=uf,
            )
            if revision_result.evidence_record:
                evidence_records.append(revision_result.evidence_record)

            if fetch_related:
                _fetch_and_merge_related_items(
                    client,
                    law_id=law_id,
                    related_items=related_items,
                )

            done.add(law_id)
            if law_id not in pass_state["completed_ids"]:
                pass_state["completed_ids"].append(law_id)
            if idx % 25 == 0 or idx == total:
                save_checkpoint(checkpoint)

        except Exception as exc:
            log_event(
                "pass2_law_failed",
                level="ERROR",
                message=f"  !! 失败: {exc}",
                law_id=law_id,
                error_message=str(exc),
            )
            failure = _record_pass2_failure(
                checkpoint,
                pass_state,
                law_id=law_id,
                exc=exc,
            )
            run_failures.append(failure)

    if run_failures:
        _mark_pass2_incomplete(checkpoint, pass_state)
        _write_pass2_failure_report(
            rebuild=rebuild,
            total=total,
            completed=len(done & set(law_ids)),
            failures=run_failures,
        )
        raise RuntimeError(
            f"Pass 2 有 {len(run_failures)} 条失败；正式关系图保持不变"
        )

    revisions_doc = build_revisions_document(
        version_records,
        uf,
        evidence_records=evidence_records,
    )
    missing_current = sorted(set(law_ids) - set(revisions_doc.get("by_law_id") or {}))
    if missing_current:
        _mark_pass2_incomplete(checkpoint, pass_state)
        raise RuntimeError(
            f"Pass 2 关系图缺少 {len(missing_current)} 个当前法规节点；"
            "正式关系图保持不变"
        )
    related_doc = {"updated_at": utc_now_iso(), "items": related_items}
    publish_json_bundle(
        {
            revisions_path(): revisions_doc,
            related_laws_path(): related_doc,
        }
    )

    _complete_pass2_state(checkpoint, pass_state)
    log_event(
        "pass2_finished",
        message=f"  修订族: {len(revisions_doc.get('families', {}))} 个",
        families=len(revisions_doc.get("families", {})),
    )
    log_event(
        "pass2_related_finished",
        message=f"  关联法规条目: {len(related_items)} 条法规有数据",
        related_laws=len(related_items),
    )
    log_event(
        "pass2_outputs",
        message=f"  输出: {revisions_path()} , {related_laws_path()}",
        revisions_file=str(revisions_path()),
        related_laws_file=str(related_laws_path()),
    )
    return {
        "status": "complete",
        "laws": total,
        "families": len(revisions_doc.get("families", {})),
        "related_laws": len(related_items),
    }
