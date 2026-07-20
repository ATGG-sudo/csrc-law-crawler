#!/usr/bin/env python3
"""Compare an isolated live AMAC crawl with the audited canonical population."""

from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from full_private_audit import title_hits


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def norm_text(value: str | None) -> str:
    return "".join(
        ch
        for ch in str(value or "")
        if unicodedata.category(ch)[0] in {"L", "N"}
    ).lower()


def norm_title(value: str | None) -> str:
    text = re.sub(r"^[【\[]?第?\d+号(?:令|公告)?[】\]]?", "", str(value or ""))
    text = re.sub(r"^(?:中国证监会|协会|中国证券投资基金业协会)(?:发布)?", "", text)
    return norm_text(text)


def norm_url(value: str | None) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def shingles(source: str, target: str, width: int = 24, step: int = 24) -> float | None:
    source = norm_text(source)
    target = norm_text(target)
    if not source:
        return None
    if len(source) < width:
        return float(source in target)
    chunks = [source[i : i + width] for i in range(0, len(source) - width + 1, step)]
    return sum(chunk in target for chunk in chunks) / len(chunks)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/mnt/d/FUND_COMPLIANCE/CSRC"))
    parser.add_argument(
        "--live",
        type=Path,
        default=Path("/tmp/csrc_private_full_audit_live/raw/amac/records"),
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    inventory = list(csv.DictReader(args.inventory.open(encoding="utf-8-sig")))
    canonical = {}
    by_url: dict[str, set[str]] = defaultdict(set)
    by_title: dict[str, set[str]] = defaultdict(set)
    for row in inventory:
        item = read_json(Path(row["canonical_path"]))
        canonical[item["id"]] = item
        by_title[norm_title(item.get("title"))].add(item["id"])
        for source in item.get("sources") or []:
            url = norm_url(source.get("page_url"))
            if url:
                by_url[url].add(item["id"])

    live_rows = []
    findings = []
    seen_live_urls: set[str] = set()
    for path in sorted(args.live.glob("*.json")):
        raw = read_json(path)
        title = (raw.get("metadata") or {}).get("name") or ""
        hits = title_hits(title)
        if not hits:
            continue
        source = raw.get("source") or {}
        live_url = norm_url(source.get("page_url"))
        if live_url:
            seen_live_urls.add(live_url)
        title_key = norm_title(title)
        candidates = by_url.get(live_url, set()) or by_title.get(title_key, set())
        match_basis = "url" if by_url.get(live_url) else ("title" if candidates else "none")
        matched_id = sorted(candidates)[0] if candidates else ""
        matched = canonical.get(matched_id) if matched_id else None
        live_text = (raw.get("content") or {}).get("plain_text") or ""
        live_assets = [
            asset
            for asset in raw.get("assets") or []
            if asset.get("source_url")
        ]
        row = {
            "live_record_id": raw.get("source_record_id"),
            "live_title": title,
            "live_pub_date": (raw.get("metadata") or {}).get("pub_date") or "",
            "live_url": source.get("page_url") or "",
            "live_text_chars": len(norm_text(live_text)),
            "live_asset_count": len(live_assets),
            "scope_hits": "|".join(hits),
            "match_basis": match_basis,
            "matched_canonical_id": matched_id,
            "canonical_title": (matched or {}).get("title") or "",
            "canonical_pub_date": ((matched or {}).get("metadata") or {}).get("pub_date") or "",
            "canonical_text_chars": len(norm_text((matched or {}).get("full_text_plain") or "")),
            "live_text_coverage_in_canonical": (
                shingles(live_text, (matched or {}).get("full_text_plain") or "")
                if matched
                else None
            ),
        }
        live_rows.append(row)
        if not matched:
            findings.append(
                {
                    "code": "live_amac_private_page_missing",
                    "severity": "critical",
                    "confidence": 0.95,
                    "title": title,
                    "live_url": source.get("page_url") or "",
                    "evidence": "live AMAC page had no exact URL or normalized-title match in canonical population",
                }
            )
            continue
        canonical_date = row["canonical_pub_date"]
        if row["live_pub_date"] and canonical_date and row["live_pub_date"] != canonical_date:
            findings.append(
                {
                    "code": "live_amac_pub_date_mismatch",
                    "severity": "high",
                    "confidence": 0.95,
                    "id": matched_id,
                    "title": title,
                    "live_url": source.get("page_url") or "",
                    "evidence": f"live={row['live_pub_date']} canonical={canonical_date}",
                }
            )
        coverage = row["live_text_coverage_in_canonical"]
        if coverage is not None and len(norm_text(live_text)) >= 200 and coverage < 0.8:
            findings.append(
                {
                    "code": "live_amac_content_low_coverage",
                    "severity": "high",
                    "confidence": 0.9,
                    "id": matched_id,
                    "title": title,
                    "live_url": source.get("page_url") or "",
                    "evidence": f"live text shingle coverage in canonical={coverage:.1%}",
                }
            )

    amac_candidates = []
    for item in canonical.values():
        urls = {
            norm_url(source.get("page_url"))
            for source in item.get("sources") or []
            if source.get("system") == "amac" and source.get("page_url")
        }
        if not urls:
            continue
        amac_candidates.append(item["id"])
        if not (urls & seen_live_urls):
            findings.append(
                {
                    "code": "canonical_amac_page_not_in_live_lists",
                    "severity": "medium",
                    "confidence": 0.75,
                    "id": item["id"],
                    "title": item.get("title"),
                    "live_url": "",
                    "evidence": "no AMAC source URL for this canonical record appeared in the isolated live list crawl",
                }
            )

    summary = {
        "live_raw_records": len(list(args.live.glob("*.json"))),
        "live_private_candidates": len(live_rows),
        "live_exact_url_matches": sum(row["match_basis"] == "url" for row in live_rows),
        "live_title_only_matches": sum(row["match_basis"] == "title" for row in live_rows),
        "live_unmatched": sum(row["match_basis"] == "none" for row in live_rows),
        "canonical_private_candidates_with_amac_sources": len(amac_candidates),
        "finding_counts": Counter(row["code"] for row in findings),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out / "live_amac_comparison.csv", live_rows)
    write_csv(args.out / "live_amac_findings.csv", findings)
    (args.out / "live_amac_findings.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in findings),
        encoding="utf-8",
    )
    (args.out / "live_amac_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
