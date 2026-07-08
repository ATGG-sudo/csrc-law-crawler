#!/usr/bin/env python3
"""抽样对照官网 API，校验 Pass 2/3/4 输出准确度。"""

from __future__ import annotations

import argparse
import random
import sys
from typing import Any

from api import (
    fetch_change_law,
    fetch_count_law_writ,
    fetch_relative_files,
    fetch_writ_list_page,
    paginate_relative_examples,
)
from client import HumanLikeClient
from revisions_graph import UnionFind, build_revisions_document, normalize_version_node
from runtime import log_event
from storage import (
    cases_path,
    load_checkpoint,
    load_json,
    load_reg_metadata,
    output_dir,
    reg_file_path,
    revisions_path,
    run_with_context,
    iter_writ_files,
    writ_file_path,
)
from writ_crawl import writ_has_body
from writ_parser import parse_law_writ_info_html


def _emit(message: str) -> None:
    log_event("validation_message", message=message)


def _expected_pass2(law_id: str, client: HumanLikeClient) -> dict[str, Any]:
    change_resp = fetch_change_law(client, law_id)
    law = change_resp.get("law") or {}
    current_id = str(law.get("secFutrsLawId") or law_id)
    evlt_ids = sorted(
        {
            str(x.get("secFutrsLawId"))
            for x in (change_resp.get("evltList") or [])
            if x.get("secFutrsLawId")
        }
    )
    rel_resp = fetch_relative_files(client, law_id)
    related = rel_resp.get("putLawList") or []
    related_ids = sorted(
        {
            str(
                x.get("secFutrsLawId")
                or x.get("putAndLawId")
                or x.get("lawId")
                or x.get("id")
            )
            for x in related
            if isinstance(x, dict)
        }
    )
    return {
        "current_id": current_id,
        "evlt_ids": evlt_ids,
        "related_count": len(related),
        "related_ids": related_ids,
    }


def _simulate_pass2_family(
    law_ids: list[str], client: HumanLikeClient
) -> dict[str, Any]:
    version_records: dict[str, dict[str, Any]] = {}
    evidence_records: list[dict[str, Any]] = []
    uf = UnionFind()
    for law_id in law_ids:
        change_resp = fetch_change_law(client, law_id)
        law = change_resp.get("law") or {}
        current_id = str(law.get("secFutrsLawId") or law_id)
        local_meta = load_reg_metadata(current_id) or load_reg_metadata(law_id)
        version_records[current_id] = normalize_version_node(law, local_meta=local_meta)
        uf.add(current_id)
        member_ids = {current_id}
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
            member_ids.add(evlt_id)
        if len(member_ids) > 1:
            evidence_records.append(
                {
                    "source": "neris.changeLaw",
                    "queried_law_id": law_id,
                    "member_ids": sorted(member_ids),
                }
            )
    return build_revisions_document(
        version_records,
        uf,
        evidence_records=evidence_records,
    )


def validate_pass2(client: HumanLikeClient, sample_size: int) -> list[str]:
    issues: list[str] = []
    cp = load_checkpoint()
    pass_state = cp.get("pass2", {})
    done = list(pass_state.get("completed_ids") or [])
    if not done:
        return ["Pass 2 尚无 completed_ids，跳过"]

    rev_doc = load_json(revisions_path(), {})
    by_law_id = rev_doc.get("by_law_id") or {}
    families = rev_doc.get("families") or {}
    picks = done if len(done) <= sample_size else random.sample(done, sample_size)
    _emit(f"\n=== Pass 2 抽样 {len(picks)} / {len(done)} 已完成 ===")

    for law_id in picks:
        meta = load_reg_metadata(law_id) or {}
        name = (meta.get("name") or law_id)[:40]
        exp = _expected_pass2(law_id, client)

        path = reg_file_path(law_id)
        if not path.exists():
            issues.append(f"[pass2] {law_id} 本地无法规文件")
            _emit(f"  FAIL {name}: 无 reg 文件")
            continue

        family_id = by_law_id.get(str(law_id))
        if family_id:
            fam = families.get(str(family_id))
            if not fam:
                issues.append(
                    f"[pass2] {law_id} family_id={family_id} 但 revisions.json 无此族"
                )
                _emit(f"  FAIL {name}: by_law_id 无对应 family")
            elif str(law_id) not in {str(v.get("id")) for v in fam.get("versions", [])}:
                issues.append(
                    f"[pass2] {law_id} 不在 family {family_id} 的 versions 中"
                )
                _emit(f"  FAIL {name}: 不在 versions 列表")
            else:
                _emit(
                    f"  OK   {name}: family={family_id}, "
                    f"evlt={len(exp['evlt_ids'])}, related={exp['related_count']}"
                )
        else:
            message = (
                f"{name}: revisions.json 的 by_law_id 缺少 {law_id}; "
                f"pass2 status={pass_state.get('status') or 'unknown'}"
            )
            if pass_state.get("status") == "complete":
                issues.append(f"[pass2] {message}")
                _emit(f"  FAIL {message}")
            else:
                _emit(
                    f"  PEND {message}; API evlt={len(exp['evlt_ids'])}, "
                    f"related={exp['related_count']}"
                )

        if exp["evlt_ids"]:
            missing_local = [
                eid for eid in exp["evlt_ids"] if not reg_file_path(eid).exists()
            ]
            if missing_local:
                _emit(
                    f"       提示: evltList 中 {len(missing_local)}/{len(exp['evlt_ids'])} "
                    f"版本尚未下载到 raw/neris/laws/"
                )

    # 多版本族专项：找有 evlt 的样本验证 supersedes 方向
    multi_candidates = []
    for law_id in picks:
        exp = _expected_pass2(law_id, client)
        if exp["evlt_ids"]:
            multi_candidates.append(law_id)
    if multi_candidates:
        lid = multi_candidates[0]
        sim = _simulate_pass2_family([lid], client)
        fams = sim.get("families") or {}
        _emit(f"\n  多版本样本 {lid[:8]}… → {len(fams)} 个 family, "
              f"versions={[len(f.get('versions',[])) for f in fams.values()]}, "
              f"edges={[len(f.get('edges',[])) for f in fams.values()]}")
        for fk, fam in fams.items():
            vers = fam.get("versions") or []
            if len(vers) >= 2:
                v0, v1 = vers[0].get("version"), vers[1].get("version")
                _emit(f"       family {fk}: 新版 {v0} supersedes 旧版 {v1}（按 version 数字降序）")

    n_fam = len(rev_doc.get("families") or {})
    if len(done) > 10 and n_fam < len(done) // 10:
        issues.append(
            f"[pass2] revisions.json 仅 {n_fam} 个 family，但已完成 {len(done)} 条；"
            "可能 pass2 仍在运行（relations 仅在全部结束后写入）"
        )
    return issues


def validate_pass3(client: HumanLikeClient, sample_size: int) -> list[str]:
    issues: list[str] = []
    cp = load_checkpoint()
    done = list(cp.get("pass3", {}).get("completed_ids") or [])
    cases_doc = load_json(cases_path(), {})
    if not done:
        return ["Pass 3 尚无 completed_ids，跳过"]

    picks = done if len(done) <= sample_size else random.sample(done, sample_size)
    _emit(f"\n=== Pass 3 抽样 {len(picks)} / {len(done)} 已完成 ===")

    for law_id in picks:
        meta = load_reg_metadata(law_id) or {}
        name = (meta.get("name") or law_id)[:40]
        local = (cases_doc.get("by_law") or {}).get(law_id)
        if not local:
            issues.append(f"[pass3] {law_id} 在 cases.json 无记录")
            _emit(f"  FAIL {name}: cases.json 缺失")
            continue

        count_resp = fetch_count_law_writ(client, law_id)
        api_entry_counts = {
            str(x.get("secFutrsLawEntryId")): int(x.get("wenShuCount") or 0)
            for x in (count_resp.get("list") or [])
            if x.get("secFutrsLawEntryId") and int(x.get("wenShuCount") or 0) > 0
        }
        local_counts = local.get("entry_counts") or {}

        if api_entry_counts != local_counts:
            issues.append(
                f"[pass3] {law_id} entry_counts 不一致 API={api_entry_counts} local={local_counts}"
            )
            _emit(f"  FAIL {name}: entry_counts 不一致")
        else:
            _emit(f"  OK   {name}: entry_counts={len(local_counts)}")

        api_law_cases = paginate_relative_examples(
            client, law_id=law_id, relative_type="law"
        )
        api_ids = sorted({str(x.get("lawWritId")) for x in api_law_cases if x.get("lawWritId")})
        local_ids = sorted(
            {str(x.get("law_writ_id")) for x in (local.get("law_level") or []) if x.get("law_writ_id")}
        )
        if api_ids != local_ids:
            issues.append(
                f"[pass3] {law_id} law_level writ_ids API={len(api_ids)} local={len(local_ids)}"
            )
            _emit(f"  FAIL {name}: law_level 案例数 API={len(api_ids)} local={len(local_ids)}")
        elif api_ids:
            _emit(f"       law_level 案例 {len(api_ids)} 条一致")

        for entry_id, cnt in list(api_entry_counts.items())[:2]:
            api_cases = paginate_relative_examples(
                client, law_id=law_id, relative_type="entry", entry_id=entry_id
            )
            api_eids = sorted({str(x.get("lawWritId")) for x in api_cases if x.get("lawWritId")})
            local_cases = (local.get("by_entry") or {}).get(entry_id) or []
            local_eids = sorted(
                {str(x.get("law_writ_id")) for x in local_cases if x.get("law_writ_id")}
            )
            if api_eids != local_eids:
                issues.append(
                    f"[pass3] {law_id} entry={entry_id} 案例不一致 API={len(api_eids)} local={len(local_eids)}"
                )
                _emit(f"  FAIL {name} entry {entry_id[:8]}…: 案例不一致")
            else:
                _emit(f"       entry {entry_id[:8]}… 案例 {len(api_eids)} 条一致")

    writ_ids = set(cases_doc.get("writ_ids") or [])
    all_case_ids: set[str] = set()
    for rec in (cases_doc.get("by_law") or {}).values():
        for c in rec.get("law_level") or []:
            if c.get("law_writ_id"):
                all_case_ids.add(str(c["law_writ_id"]))
        for cases in (rec.get("by_entry") or {}).values():
            for c in cases:
                if c.get("law_writ_id"):
                    all_case_ids.add(str(c["law_writ_id"]))
    if writ_ids != all_case_ids:
        issues.append(
            f"[pass3] writ_ids 索引不一致: writ_ids={len(writ_ids)} 实际引用={len(all_case_ids)}"
        )
    return issues


def validate_pass4(client: HumanLikeClient, sample_size: int) -> list[str]:
    issues: list[str] = []
    cp = load_checkpoint()
    done = list(cp.get("pass4", {}).get("completed_writ_ids") or [])
    _emit(f"\n=== Pass 4 本地 writ 文件校验（checkpoint 完成 {len(done)}）===")

    writ_files = iter_writ_files()
    no_body = []
    no_basis = []
    for wf in writ_files:
        doc = load_json(wf, {})
        if not writ_has_body(doc):
            no_body.append(wf.name)
        if not doc.get("legal_basis"):
            no_basis.append(wf.name)

    _emit(f"  writ 文件 {len(writ_files)} 个; 无正文 {len(no_body)}; 无 legal_basis {len(no_basis)}")
    if no_body:
        issues.append(f"[pass4] {len(no_body)} 个 writ 无正文: {no_body[:5]}")

    if not done:
        return issues + ["Pass 4 checkpoint 尚无 completed_writ_ids"]

    picks = done if len(done) <= sample_size else random.sample(done, sample_size)
    for writ_id in picks[:sample_size]:
        path = writ_file_path(writ_id)
        if not path.exists():
            issues.append(f"[pass4] {writ_id} checkpoint 有记录但文件不存在")
            continue
        doc = load_json(path, {})
        from api import fetch_writ_detail_html

        html = fetch_writ_detail_html(client, writ_id)
        parsed = parse_law_writ_info_html(html)
        local_body = (doc.get("body") or "").strip()
        api_body = (parsed.get("body") or "").strip()
        if not api_body:
            issues.append(f"[pass4] {writ_id} 官网详情页无正文")
            _emit(f"  FAIL writ_{writ_id[:8]}…: 官网无正文")
            continue
        if abs(len(local_body) - len(api_body)) > 5:
            issues.append(
                f"[pass4] {writ_id} 正文字数 local={len(local_body)} api={len(api_body)}"
            )
            _emit(f"  FAIL writ_{writ_id[:8]}…: 字数 {len(local_body)} vs {len(api_body)}")
        else:
            lb = len(doc.get("legal_basis") or [])
            pb = len(parsed.get("legal_basis") or [])
            _emit(f"  OK   writ_{writ_id[:8]}…: body={len(local_body)}, basis local={lb} api={pb}")

    # 列表总数对照
    try:
        first = fetch_writ_list_page(client, 1)
        row_count = first["pageUtil"]["rowCount"]
        _emit(f"  官网文书列表 rowCount={row_count}, 本地文件={len(writ_files)}")
    except Exception as exc:
        _emit(f"  列表总数对照失败: {exc}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 enhance 输出准确度")
    parser.add_argument("--sample", type=int, default=5, help="每 pass 抽样数")
    parser.add_argument("--pass", dest="passes", action="append", choices=["2", "3", "4", "all"])
    args = parser.parse_args()
    selected = args.passes or ["all"]
    if "all" in selected:
        selected = ["2", "3", "4"]

    cp = load_checkpoint()
    _emit(f"输出: {output_dir()}")
    _emit(f"pass2 进度: {len(cp.get('pass2',{}).get('completed_ids',[]))}/3422")
    _emit(f"pass3 进度: {len(cp.get('pass3',{}).get('completed_ids',[]))}/3422")
    _emit(f"pass4 进度: {len(cp.get('pass4',{}).get('completed_writ_ids',[]))}")

    client = HumanLikeClient()
    all_issues: list[str] = []
    if "2" in selected:
        all_issues.extend(validate_pass2(client, args.sample))
    if "3" in selected:
        all_issues.extend(validate_pass3(client, args.sample))
    if "4" in selected:
        all_issues.extend(validate_pass4(client, args.sample))

    _emit("\n=== 汇总 ===")
    if all_issues:
        for item in all_issues:
            _emit(f"  - {item}")
        _emit(f"\n共 {len(all_issues)} 条提示/问题")
    else:
        _emit("  抽样校验未发现不一致")
    return 1 if any(not x.endswith("跳过") for x in all_issues if x.startswith("[pass")) else 0


if __name__ == "__main__":
    sys.exit(run_with_context(main, "validate-enhance"))
