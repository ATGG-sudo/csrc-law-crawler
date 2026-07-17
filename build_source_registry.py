#!/usr/bin/env python3
"""Build the checked-in multi-source registry from an artifact-tool inspection JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from csrc_law_crawler.sources.registry import validate_registry


QUERY_SETS = {
    "private_fund_core": [
        "私募基金",
        "私募投资基金",
        "私募股权基金",
        "股权投资基金",
        "创业投资基金",
        "基金管理人",
        "登记备案",
    ],
    "state_capital": [
        "政府投资基金",
        "政府引导基金",
        "国资基金",
        "母基金",
        "产权转让",
        "资产评估",
    ],
    "cross_border_exit": [
        "QFLP",
        "跨境直接投资",
        "资本项目",
        "减持",
        "协议转让",
        "S基金",
    ],
    "tax_aml": [
        "创业投资税收",
        "合伙企业",
        "股权转让",
        "反洗钱",
        "受益所有人",
    ],
}


def _short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _source_system(url: str) -> str:
    host = (urlsplit(url).hostname or "unknown").lower()
    if host.endswith("csrc.gov.cn"):
        return "neris" if host.startswith("neris.") else "csrc"
    if host.endswith("amac.org.cn"):
        return "amac"
    return host.removeprefix("www.").replace(".", "_")


def _adapter(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host == "neris.csrc.gov.cn":
        return "neris"
    if host == "www.csrc.gov.cn":
        return "csrc"
    if host in {"eid.csrc.gov.cn", "gs.amac.org.cn"}:
        return "subject_query"
    if host.endswith("amac.org.cn"):
        return "amac"
    return "http_html"


def _scope_mode(scope: str) -> str:
    if "按主体" in scope or "机构/产品" in scope:
        return "subject_query"
    if any(token in scope for token in ("目录全量+正文", "元数据全量", "全量后")):
        return "catalog_filter"
    if any(
        token in scope
        for token in (
            "关键词",
            "重点主题",
            "专题",
            "问答",
            "基金相关事项",
            "国内规则",
        )
    ):
        return "query_exhaustive"
    return "enumerable"


def _material_lane(material: str, scope_mode: str) -> str:
    if scope_mode == "subject_query" or "主体/产品公示" in material or "诚信公示" in material:
        return "subject_snapshot"
    if any(
        token in material
        for token in (
            "行政处罚",
            "监管措施",
            "纪律处分",
            "自律措施",
            "案例",
            "行政复议",
            "市场禁入",
            "异常经营",
            "失联机构",
            "执法动态",
            "通讯与通报",
        )
    ):
        return "case"
    if any(
        token in material
        for token in (
            "正式",
            "规则",
            "规章",
            "法规",
            "政策文件",
            "现行有效目录",
            "外商投资",
        )
    ):
        return "rule"
    return "reference"


def _query_sets(row: dict[str, Any], scope_mode: str) -> list[str]:
    if scope_mode not in {"query_exhaustive", "catalog_filter"}:
        return []
    text = " ".join(str(row.get(field) or "") for field in ("覆盖合规范围", "范围策略", "名称"))
    result = ["private_fund_core"]
    if any(token in text for token in ("国资", "政府投资基金", "引导基金", "产权", "母基金")):
        result.append("state_capital")
    if any(token in text for token in ("QFLP", "跨境", "外汇", "减持", "协议转让", "S基金")):
        result.append("cross_border_exit")
    if any(token in text for token in ("税", "反洗钱", "受益所有人", "合伙企业")):
        result.append("tax_aml")
    return result


def build_registry(inspection: dict[str, Any]) -> dict[str, Any]:
    sheet = next(item for item in inspection["sheets"] if item["name"] == "信源主表")
    headers = sheet["values"][0]
    rows = [
        dict(zip(headers, values, strict=False))
        for values in sheet["values"][1:]
        if values and values[0]
    ]
    endpoints: dict[str, dict[str, Any]] = {}
    for row_number, row in enumerate(rows, start=2):
        url = str(row["链接"]).strip()
        endpoint = endpoints.get(url)
        scope_mode = _scope_mode(str(row.get("范围策略") or ""))
        if endpoint is None:
            system = _source_system(url)
            endpoint = {
                "endpoint_id": f"{system}_{_short_hash(url)}",
                "url": url,
                "source_system": system,
                "adapter": _adapter(url),
                "scope_mode": scope_mode,
                "query_sets": _query_sets(row, scope_mode),
                "default_material_lane": _material_lane(str(row.get("材料性质") or ""), scope_mode),
                "crawlability": row.get("可爬取性等级"),
                "recommended_access": row.get("推荐接入模式"),
                "profiles": [],
            }
            endpoints[url] = endpoint
        elif endpoint["scope_mode"] != scope_mode:
            raise ValueError(f"duplicate URL has conflicting scope modes: {url}")
        endpoint["profiles"].append(
            {
                "profile_id": f"profile_{_short_hash(url + chr(0) + str(row['名称']))}",
                "workbook_row": row_number,
                "name": row.get("名称"),
                "description": row.get("说明"),
                "coverage": row.get("覆盖合规范围"),
                "effect": row.get("效力（综合）"),
                "publisher": row.get("发布主体"),
                "region": row.get("地域"),
                "legal_level": row.get("法律效力层级"),
                "material_nature": row.get("材料性质"),
                "officiality": row.get("官方性"),
                "priority": row.get("优先级"),
                "content_carrier": row.get("内容载体"),
                "update_frequency": row.get("更新频率"),
                "scope_strategy": row.get("范围策略"),
            }
        )

    result: dict[str, Any] = {
        "schema_version": 1,
        "source_workbook": inspection.get("source"),
        "source_verified_at": "2026-07-13",
        "query_set_version": "private-fund-query-v1",
        "query_sets": QUERY_SETS,
        "endpoints": sorted(endpoints.values(), key=lambda item: item["endpoint_id"]),
        "wechat": {
            "wechat_jixiaolv": {
                "account_name": "基小律",
                "expected_fakeid": None,
                "material_lane": "clue",
                "exporter_commit": "15b391bbf2d18ecd7e48b382e0d723fcfded92c1",
            }
        },
    }
    validate_registry(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inspection_json", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    inspection = json.loads(args.inspection_json.read_text(encoding="utf-8"))
    result = build_registry(inspection)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "endpoints": len(result["endpoints"]),
                "profiles": sum(len(item["profiles"]) for item in result["endpoints"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
