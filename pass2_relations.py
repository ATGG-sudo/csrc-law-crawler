"""Pass 2：历次修订 + 关联法规。"""

from __future__ import annotations

from typing import Any

from api import fetch_change_law, fetch_relative_files
from client import HumanLikeClient
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
    pass_state = checkpoint.setdefault("pass2", {"completed_ids": [], "failures": []})
    if rebuild:
        pass_state["completed_ids"] = []
        pass_state["failures"] = []
        pass_state.pop("finished_at", None)
    pass_state["status"] = "in_progress"
    pass_state["started_at"] = utc_now_iso()
    save_checkpoint(checkpoint)
    done: set[str] = set(pass_state.get("completed_ids") or [])

    law_ids = iter_reg_law_ids(limit=limit)
    total = len(law_ids)
    print(f"\n=== Pass 2：修订关系 + 关联法规（{total} 条法规）===")

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
        print(f"[{idx}/{total}] {name}")

        try:
            cache_path = revision_evidence_cache_path(law_id)
            if cache_path.exists() and not refresh_revision_cache:
                change_resp = load_json(cache_path, {})
            else:
                change_resp = fetch_change_law(client, law_id)
                save_json(cache_path, change_resp)
            law = change_resp.get("law") or {}
            current_id = str(law.get("secFutrsLawId") or law_id)
            local_meta = load_reg_metadata(current_id) or meta

            current_node = normalize_version_node(law, local_meta=local_meta)
            version_records[current_id] = current_node
            uf.add(current_id)

            revision_member_ids = {current_id}
            for evlt in change_resp.get("evltList") or []:
                evlt_id = str(evlt.get("secFutrsLawId") or "")
                if not evlt_id:
                    continue
                evlt_meta = load_reg_metadata(evlt_id)
                version_records[evlt_id] = normalize_version_node(
                    evlt, local_meta=evlt_meta
                )
                uf.add(evlt_id)
                uf.union(current_id, evlt_id)
                revision_member_ids.add(evlt_id)

            if len(revision_member_ids) > 1:
                evidence_records.append(
                    {
                        "source": "neris.changeLaw",
                        "queried_law_id": law_id,
                        "member_ids": sorted(revision_member_ids),
                        "retrieved_at": utc_now_iso(),
                    }
                )

            if fetch_related:
                rel_resp = fetch_relative_files(client, law_id)
                put_list = rel_resp.get("putLawList") or []
                normalized = [
                    _normalize_related_item(x)
                    for x in put_list
                    if isinstance(x, dict)
                ]
                if normalized:
                    existing = related_items.get(law_id, [])
                    seen = {
                        (x.get("to_law_id"), x.get("name")) for x in existing
                    }
                    for item in normalized:
                        key = (item.get("to_law_id"), item.get("name"))
                        if key not in seen:
                            existing.append(item)
                            seen.add(key)
                    related_items[law_id] = existing

            done.add(law_id)
            if law_id not in pass_state["completed_ids"]:
                pass_state["completed_ids"].append(law_id)
            if idx % 25 == 0 or idx == total:
                save_checkpoint(checkpoint)

        except Exception as exc:
            print(f"  !! 失败: {exc}")
            failure = {"id": law_id, "error": str(exc), "at": utc_now_iso()}
            run_failures.append(failure)
            pass_state.setdefault("failures", []).append(failure)
            save_checkpoint(checkpoint)

    if run_failures:
        pass_state["status"] = "incomplete"
        pass_state.pop("finished_at", None)
        save_checkpoint(checkpoint)
        failure_doc = {
            "schema_version": 1,
            "updated_at": utc_now_iso(),
            "status": "incomplete",
            "rebuild": rebuild,
            "requested_laws": total,
            "completed_laws": len(done & set(law_ids)),
            "failures": run_failures,
        }
        save_json(reports_dir() / "pass2_failures.json", failure_doc)
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
        pass_state["status"] = "incomplete"
        pass_state.pop("finished_at", None)
        save_checkpoint(checkpoint)
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

    pass_state["finished_at"] = utc_now_iso()
    pass_state["status"] = "complete"
    pass_state["failures"] = []
    save_checkpoint(checkpoint)
    print(f"  修订族: {len(revisions_doc.get('families', {}))} 个")
    print(f"  关联法规条目: {len(related_items)} 条法规有数据")
    print(f"  输出: {revisions_path()} , {related_laws_path()}")
    return {
        "status": "complete",
        "laws": total,
        "families": len(revisions_doc.get("families", {})),
        "related_laws": len(related_items),
    }
