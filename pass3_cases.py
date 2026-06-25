"""Pass 3：相关案例。"""

from __future__ import annotations

from typing import Any

from api import fetch_count_law_writ, paginate_relative_examples, summarize_writ
from client import HumanLikeClient
from config import OUTPUT_DIR
from storage import (
    cases_path,
    iter_reg_law_ids,
    load_checkpoint,
    load_reg_metadata,
    load_json,
    save_checkpoint,
    save_json,
    utc_now_iso,
    writ_file_path,
)


def _normalize_case(row: dict[str, Any]) -> dict[str, Any]:
    writ = summarize_writ(row)
    writ_id = writ.get("id")
    local_file = (
        str(writ_file_path(str(writ_id)).relative_to(OUTPUT_DIR))
        if writ_id
        else None
    )
    return {
        "law_writ_id": writ_id,
        "name": writ["name"],
        "fileno": writ["fileno"],
        "issue_org": writ["issue_org"],
        "dspt_date_ms": writ["dspt_date_ms"],
        "link_addr": writ.get("link_addr"),
        "local_file": local_file,
        "detail_url": (
            f"https://neris.csrc.gov.cn/falvfagui/"
            f"rdqsHeader/lawWritInfo?navbarId=1&lawWritId={writ_id}"
            if writ_id
            else None
        ),
    }


def run_pass3(
    client: HumanLikeClient,
    *,
    limit: int | None = None,
    skip_law_level: bool = False,
) -> None:
    checkpoint = load_checkpoint()
    pass_state = checkpoint.setdefault("pass3", {"completed_ids": [], "failures": []})
    done: set[str] = set(pass_state.get("completed_ids") or [])

    cases_doc = load_json(
        cases_path(),
        {"updated_at": None, "by_law": {}, "writ_ids": []},
    )
    by_law: dict[str, Any] = cases_doc.setdefault("by_law", {})
    writ_ids: set[str] = set(cases_doc.get("writ_ids") or [])

    law_ids = iter_reg_law_ids(limit=limit)
    total = len(law_ids)
    print(f"\n=== Pass 3：相关案例（{total} 条法规）===")

    for idx, law_id in enumerate(law_ids, start=1):
        if law_id in done:
            continue
        meta = load_reg_metadata(law_id) or {}
        name = meta.get("name") or law_id
        print(f"[{idx}/{total}] {name}")

        record: dict[str, Any] = {
            "law_level": [],
            "by_entry": {},
            "entry_counts": {},
        }

        try:
            count_resp = fetch_count_law_writ(client, law_id)
            for item in count_resp.get("list") or []:
                entry_id = item.get("secFutrsLawEntryId")
                count = int(item.get("wenShuCount") or 0)
                if entry_id and count > 0:
                    record["entry_counts"][str(entry_id)] = count

            if not skip_law_level:
                law_cases = paginate_relative_examples(
                    client, law_id=law_id, relative_type="law"
                )
                record["law_level"] = [_normalize_case(x) for x in law_cases]
                for case in record["law_level"]:
                    if case.get("law_writ_id"):
                        writ_ids.add(str(case["law_writ_id"]))

            entries_with_cases = [
                eid for eid, cnt in record["entry_counts"].items() if cnt > 0
            ]
            for entry_id in entries_with_cases:
                entry_cases = paginate_relative_examples(
                    client,
                    law_id=law_id,
                    relative_type="entry",
                    entry_id=entry_id,
                )
                if entry_cases:
                    record["by_entry"][entry_id] = [
                        _normalize_case(x) for x in entry_cases
                    ]
                    for case in record["by_entry"][entry_id]:
                        if case.get("law_writ_id"):
                            writ_ids.add(str(case["law_writ_id"]))

            by_law[law_id] = record
            cases_doc["writ_ids"] = sorted(writ_ids)
            cases_doc["updated_at"] = utc_now_iso()
            save_json(cases_path(), cases_doc)

            done.add(law_id)
            if law_id not in pass_state["completed_ids"]:
                pass_state["completed_ids"].append(law_id)
            save_checkpoint(checkpoint)

            law_n = len(record["law_level"])
            entry_n = sum(len(v) for v in record["by_entry"].values())
            if law_n or entry_n:
                print(f"  案例: 法规级 {law_n}，条文级 {entry_n}")

        except Exception as exc:
            print(f"  !! 失败: {exc}")
            pass_state.setdefault("failures", []).append(
                {"id": law_id, "error": str(exc), "at": utc_now_iso()}
            )
            save_checkpoint(checkpoint)

    cases_doc["updated_at"] = utc_now_iso()
    save_json(cases_path(), cases_doc)
    pass_state["finished_at"] = utc_now_iso()
    save_checkpoint(checkpoint)
    print(f"  累计文书 ID: {len(writ_ids)} 个")
    print(f"  输出: {cases_path()}")
