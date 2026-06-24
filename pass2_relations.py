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
    patch_reg_revision_ref,
    related_laws_path,
    revisions_path,
    save_checkpoint,
    save_json,
    utc_now_iso,
)


def _normalize_related_item(item: dict[str, Any]) -> dict[str, Any]:
    to_id = (
        item.get("secFutrsLawId")
        or item.get("putAndLawId")
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
) -> None:
    existing = load_json(revisions_path(), {})
    for family in (existing.get("families") or {}).values():
        for node in family.get("versions") or []:
            law_id = str(node.get("id") or "")
            if not law_id:
                continue
            version_records[law_id] = node
            uf.add(law_id)
        for edge in family.get("edges") or []:
            src = str(edge.get("from") or "")
            dst = str(edge.get("to") or "")
            if src and dst:
                uf.union(src, dst)


def run_pass2(
    client: HumanLikeClient,
    *,
    limit: int | None = None,
    patch_revision_ref: bool = True,
) -> None:
    checkpoint = load_checkpoint()
    pass_state = checkpoint.setdefault("pass2", {"completed_ids": [], "failures": []})
    done: set[str] = set(pass_state.get("completed_ids") or [])

    law_ids = iter_reg_law_ids(limit=limit)
    total = len(law_ids)
    print(f"\n=== Pass 2：修订关系 + 关联法规（{total} 条法规）===")

    version_records: dict[str, dict[str, Any]] = {}
    related_items: dict[str, list[dict[str, Any]]] = load_json(
        related_laws_path(), {"updated_at": None, "items": {}}
    ).get("items", {})
    uf = UnionFind()
    _load_existing_revisions_state(version_records, uf)

    for idx, law_id in enumerate(law_ids, start=1):
        if law_id in done:
            continue
        meta = load_reg_metadata(law_id)
        name = (meta or {}).get("name") or law_id
        print(f"[{idx}/{total}] {name}")

        try:
            change_resp = fetch_change_law(client, law_id)
            law = change_resp.get("law") or {}
            current_id = str(law.get("secFutrsLawId") or law_id)
            local_meta = load_reg_metadata(current_id) or meta

            current_node = normalize_version_node(law, local_meta=local_meta)
            version_records[current_id] = current_node
            uf.add(current_id)

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

            rel_resp = fetch_relative_files(client, law_id)
            put_list = rel_resp.get("putLawList") or []
            normalized = [_normalize_related_item(x) for x in put_list if isinstance(x, dict)]
            if normalized:
                existing = related_items.get(law_id, [])
                seen = {(x.get("to_law_id"), x.get("name")) for x in existing}
                for item in normalized:
                    key = (item.get("to_law_id"), item.get("name"))
                    if key not in seen:
                        existing.append(item)
                        seen.add(key)
                related_items[law_id] = existing

            done.add(law_id)
            if law_id not in pass_state["completed_ids"]:
                pass_state["completed_ids"].append(law_id)
            save_checkpoint(checkpoint)

        except Exception as exc:
            print(f"  !! 失败: {exc}")
            pass_state.setdefault("failures", []).append(
                {"id": law_id, "error": str(exc), "at": utc_now_iso()}
            )
            save_checkpoint(checkpoint)

    revisions_doc = build_revisions_document(version_records, uf)
    save_json(revisions_path(), revisions_doc)
    save_json(
        related_laws_path(),
        {"updated_at": utc_now_iso(), "items": related_items},
    )

    if patch_revision_ref:
        for law_id, family_key in revisions_doc.get("by_law_id", {}).items():
            patch_reg_revision_ref(law_id, family_key)

    pass_state["finished_at"] = utc_now_iso()
    save_checkpoint(checkpoint)
    print(f"  修订族: {len(revisions_doc.get('families', {}))} 个")
    print(f"  关联法规条目: {len(related_items)} 条法规有数据")
    print(f"  输出: {revisions_path()} , {related_laws_path()}")
