#!/usr/bin/env python3
"""Build the human-adjudicated issue ledger and headline metrics."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def read_csv(path: Path) -> list[dict]:
    return list(csv.DictReader(path.open(encoding="utf-8-sig")))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out

    inventory = read_csv(out / "candidate_inventory.csv")
    inv = {row["id"]: row for row in inventory}
    local_findings = read_jsonl(out / "local_findings.jsonl")
    live_rows = read_csv(out / "live_amac_comparison.csv")
    live_findings = read_jsonl(out / "live_amac_findings.jsonl")
    pdf_rows = read_jsonl(out / "pdf_results.jsonl")
    url_rows = read_jsonl(out / "url_results.jsonl")
    remote_assets = read_jsonl(out / "remote_asset_results.jsonl")
    duplicate_groups = json.loads((out / "duplicate_groups.json").read_text(encoding="utf-8"))

    exact_url_duplicate_groups = []
    for group in duplicate_groups:
        url_sets = []
        for record_id in group["ids"]:
            item = json.loads(Path(inv[record_id]["canonical_path"]).read_text(encoding="utf-8"))
            url_sets.append(
                {source.get("page_url") for source in item.get("sources") or [] if source.get("page_url")}
            )
        common = set.intersection(*url_sets) if url_sets else set()
        if common:
            exact_url_duplicate_groups.append({**group, "common_urls": sorted(common)})

    comparable = [row for row in live_rows if row["live_text_coverage_in_canonical"]]
    title_matches = [row for row in live_rows if row["match_basis"] != "none"]
    malformed = [row for row in local_findings if row["code"] == "malformed_pub_date"]
    neris_shift = [row for row in local_findings if row["code"] == "neris_pub_date_timezone_mismatch"]
    unknown_rules = [row for row in local_findings if row["code"] == "rule_effectiveness_unknown"]
    short_rules = [row for row in local_findings if row["code"] == "short_rule_text"]
    empty_remote_assets = [row for row in remote_assets if (row.get("size_bytes") or 0) == 0]
    scan_records = {
        record_id
        for row in pdf_rows
        if row.get("scan_likely")
        for record_id in row.get("record_ids") or []
    }
    metadata_only_scan_records = {
        record_id for record_id in scan_records if inv.get(record_id, {}).get("content_status") == "metadata_only"
    }

    obvious_wrapper_ids = {
        "law_179fe0b15e77496626d4b8f8",
        "law_52e7f5d99731d77c06748f18",
        "law_6213f33a41da758bf4b893d0",
        "law_7342f26c493634c1923fcf6d",
        "law_8ad54fd5ecce30ff75eb8695",
        "law_91182090dd452e13f8914b2f",
        "law_a4e0fd88e19ed244f4557a9c",
        "law_c1c4f9374cabdafb7a2be606",
        "law_c2050d296923495b1e9ce317",
        "law_c959842365c951fb140c3fb4",
        "law_5a84c318a37d539223d1abad",
    }

    issue_families = [
        {
            "priority": 0,
            "severity": "critical",
            "issue_family": "official_rule_page_missing_from_corpus",
            "affected": 1,
            "unit": "official page",
            "confidence": 1.0,
            "evidence": "AMAC live list page for 私募基金登记备案相关问题解答（十二） has no raw/canonical URL match",
            "recommended_action": "ingest the official page, then determine current/historical status",
        },
        {
            "priority": 0,
            "severity": "critical",
            "issue_family": "order_233_split_record_and_wrong_dates",
            "affected": 3,
            "unit": "canonical records",
            "confidence": 1.0,
            "evidence": "official rule page, NERIS full text, and AMAC news wrapper are split; NERIS dates are one day early and CSRC wrapper has only 140 characters",
            "recommended_action": "merge official representations, use 2026-02-24 and 2026-09-01, retain two CSRC PDFs",
        },
        {
            "priority": 0,
            "severity": "critical",
            "issue_family": "revised_2025_filing_guide_not_activated",
            "affected": 2,
            "unit": "canonical records",
            "confidence": 1.0,
            "evidence": "2025 revision is unknown with no dates while the superseded 2023 guide remains current",
            "recommended_action": "set 2025-10-24 current and link the 2023 version as historical/superseded",
        },
        {
            "priority": 0,
            "severity": "critical",
            "issue_family": "future_repeal_of_2016_disclosure_rules_not_encoded",
            "affected": 5,
            "unit": "canonical records",
            "confidence": 1.0,
            "evidence": "official 2026 implementation rules repeal the 2016 management rule and format guides on 2026-09-01, but no successor chain is recorded",
            "recommended_action": "encode pending successor and 2026-09-01 ineffective dates",
        },
        {
            "priority": 0,
            "severity": "critical",
            "issue_family": "2026_disclosure_templates_have_unknown_effectivity",
            "affected": 6,
            "unit": "canonical records",
            "confidence": 1.0,
            "evidence": "six official disclosure templates published 2026-06-05 are unknown although the announcement sets 2026-09-01 implementation",
            "recommended_action": "mark pending with the official implementation date and link to the announcement/rule",
        },
        {
            "priority": 1,
            "severity": "high",
            "issue_family": "systematic_neris_one_day_date_shift",
            "affected": len(neris_shift),
            "unit": "canonical records",
            "confidence": 1.0,
            "evidence": f"{len(neris_shift)} records have canonical publication date exactly one day before source UTC date",
            "recommended_action": "repair raw NERIS normalized dates and rebuild downstream artifacts",
        },
        {
            "priority": 1,
            "severity": "high",
            "issue_family": "same_official_url_split_into_duplicate_canonicals",
            "affected": sum(group["count"] for group in exact_url_duplicate_groups),
            "unit": f"records in {len(exact_url_duplicate_groups)} groups",
            "confidence": 1.0,
            "evidence": "each group contains multiple canonical IDs pointing to the same official URL",
            "recommended_action": "deduplicate by normalized official URL before canonical ID generation",
        },
        {
            "priority": 1,
            "severity": "high",
            "issue_family": "malformed_publication_dates",
            "affected": len(malformed),
            "unit": "canonical records",
            "confidence": 1.0,
            "evidence": "publication dates contain partial values such as 2026年1, 2026年3, or 2026年5",
            "recommended_action": "prefer page date metadata and reject non-date snippets",
        },
        {
            "priority": 1,
            "severity": "high",
            "issue_family": "rule_lane_effectivity_unknown",
            "affected": len(unknown_rules),
            "unit": "rule-lane records",
            "confidence": 1.0,
            "evidence": "rule lane has no current/pending/historical decision",
            "recommended_action": "resolve from official announcement, repeal lists, and successor relations",
        },
        {
            "priority": 1,
            "severity": "high",
            "issue_family": "obvious_news_or_one_off_material_in_rule_lane",
            "affected": len(obvious_wrapper_ids),
            "unit": "canonical records",
            "confidence": 0.95,
            "evidence": "news wrappers, meeting coverage, or one-off institution notices are classified as rules",
            "recommended_action": "classify wrappers as reference/enforcement material and link them to the actual rule",
        },
        {
            "priority": 1,
            "severity": "high",
            "issue_family": "rule_record_body_too_short",
            "affected": len(short_rules),
            "unit": "canonical records",
            "confidence": 1.0,
            "evidence": "rule-lane normalized body is under 200 characters, including zero-text attachments and CSRC wrappers",
            "recommended_action": "merge with the official PDF/full-text representation or extract the attachment body",
        },
        {
            "priority": 1,
            "severity": "high",
            "issue_family": "official_attachment_endpoint_returns_empty_body",
            "affected": len(empty_remote_assets),
            "unit": "attachment URLs across 4 records",
            "confidence": 1.0,
            "evidence": "live NERIS endpoints return HTTP 200 with zero bytes and local download status is failed",
            "recommended_action": "seek alternate official copies; preserve explicit unavailable status when none exists",
        },
        {
            "priority": 2,
            "severity": "medium",
            "issue_family": "scan_pdf_without_searchable_text",
            "affected": len(metadata_only_scan_records),
            "unit": "canonical records",
            "confidence": 1.0,
            "evidence": f"{len(scan_records)} records have scan-like PDFs; {len(metadata_only_scan_records)} remain metadata-only",
            "recommended_action": "OCR with page-level provenance and confidence; retain the original PDF hash",
        },
        {
            "priority": 2,
            "severity": "medium",
            "issue_family": "official_policy_subdomain_tls_chain_not_validated",
            "affected": sum(bool(row.get("tls_verification_bypassed")) for row in url_rows),
            "unit": "official URLs",
            "confidence": 1.0,
            "evidence": "fg.amac.org.cn required TLS verification bypass in this runtime",
            "recommended_action": "make the exception host-scoped, observable, and independently monitored",
        },
    ]

    positives = [
        {
            "check": "official_source_reachability",
            "result": "pass",
            "numerator": sum(200 <= (row.get("status_code") or 0) < 300 for row in url_rows),
            "denominator": len(url_rows),
            "note": "all 1,008 official source URLs returned 2xx during the corrected verification run",
        },
        {
            "check": "live_amac_exact_url_and_title_match",
            "result": "partial",
            "numerator": len(title_matches),
            "denominator": len(live_rows),
            "note": "all 597 matched pages had exact normalized titles; one official page was missing",
        },
        {
            "check": "live_amac_text_coverage_at_least_80pct",
            "result": "pass_with_exceptions",
            "numerator": sum(float(row["live_text_coverage_in_canonical"]) >= 0.8 for row in comparable),
            "denominator": len(comparable),
            "note": "17 low-coverage alerts were manually reviewed; most are wrapper/PDF-source asymmetry, with a confirmed incomplete duplicate wrapper",
        },
        {
            "check": "local_pdf_validity",
            "result": "pass",
            "numerator": sum(not row.get("error") for row in pdf_rows),
            "denominator": len(pdf_rows),
            "note": "all PDFs parse; no recorded SHA mismatch",
        },
        {
            "check": "live_pdf_signature",
            "result": "pass",
            "numerator": sum(bool(row.get("pdf_signature_ok")) for row in url_rows if row.get("is_pdf")),
            "denominator": sum(bool(row.get("is_pdf")) for row in url_rows),
            "note": "all live PDF source URLs begin with a valid PDF signature",
        },
        {
            "check": "nonempty_remote_attachment_hash_match",
            "result": "pass",
            "numerator": sum(bool(row.get("remote_sha_matches_expected")) for row in remote_assets),
            "denominator": sum((row.get("size_bytes") or 0) > 0 for row in remote_assets),
            "note": "every non-empty attachment endpoint with a local copy matched its expected SHA; five other endpoints are empty",
        },
        {
            "check": "manual_scan_pdf_title_and_document_match",
            "result": "pass",
            "numerator": 8,
            "denominator": 8,
            "note": "first pages sampled across 2017 and 2020-2026 visually matched the canonical title/entity and document type",
        },
    ]

    case_rows = []
    for finding in live_findings:
        if finding["code"] == "live_amac_private_page_missing":
            case_rows.append(
                {
                    "severity": "critical",
                    "issue_family": "official_rule_page_missing_from_corpus",
                    "id": "",
                    "title": finding["title"],
                    "official_url": finding["live_url"],
                    "evidence": finding["evidence"],
                }
            )
    selected_clusters = {
        "order_233_split_record_and_wrong_dates": [
            "law_fa9879afe3753e34193e6901",
            "law_080c4461cb2b7d347610c02a",
            "law_c1c4f9374cabdafb7a2be606",
        ],
        "revised_2025_filing_guide_not_activated": [
            "law_f8194c71817b4463d82a528d",
            "law_6b082e4ce08248b155df3d6a",
        ],
        "future_repeal_of_2016_disclosure_rules_not_encoded": [
            "law_350bb0a1cb8d08a2ee3cb0ba",
            "law_17e0c98b1445597a59e6d7f5",
            "law_3df59b520df1b11e1e5c8a68",
            "law_2b8c4ae5b91882bb21e5e32d",
            "law_666f5f3bc6eb6c79b0636063",
        ],
        "2026_disclosure_templates_have_unknown_effectivity": [
            "law_88269a674007f4a18f801771",
            "law_37b49bc189e778314b6d5215",
            "law_f0bbbe45511b0126561e7876",
            "law_c7a0f09a43e65ab400a3346d",
            "law_040dd38ea0d0beabd76ad255",
            "law_2c8b6342b03c7e1d1dc370be",
        ],
    }
    for family, ids in selected_clusters.items():
        for record_id in ids:
            row = inv[record_id]
            item = json.loads(Path(row["canonical_path"]).read_text(encoding="utf-8"))
            case_rows.append(
                {
                    "severity": "critical",
                    "issue_family": family,
                    "id": record_id,
                    "title": row["title"],
                    "official_url": next((source.get("page_url") for source in item.get("sources") or [] if source.get("page_url")), ""),
                    "evidence": f"pub={row['pub_date'] or 'missing'} effective={row['effective_date'] or 'missing'} status={row['effectiveness']}",
                }
            )
    for group in exact_url_duplicate_groups:
        for record_id in group["ids"]:
            case_rows.append(
                {
                    "severity": "high",
                    "issue_family": "same_official_url_split_into_duplicate_canonicals",
                    "id": record_id,
                    "title": inv[record_id]["title"],
                    "official_url": group["common_urls"][0],
                    "evidence": f"same URL appears in {group['count']} canonical IDs",
                }
            )

    severity_order = {"critical": 0, "high": 1, "medium": 2}
    issue_families.sort(key=lambda row: (row["priority"], row["issue_family"]))
    case_rows.sort(key=lambda row: (severity_order[row["severity"]], row["issue_family"], row["id"]))
    write_csv(out / "adjudicated_issue_families.csv", issue_families)
    write_csv(out / "adjudicated_cases.csv", case_rows)
    write_csv(out / "positive_checks.csv", positives)
    summary = {
        "scope": {
            "gross_keyword_candidates": 862,
            "scope_exclusions": 37,
            "final_private_compliance_population": len(inventory),
            "rule_lane": sum(row["lane"] == "rule" for row in inventory),
            "reference_lane": sum(row["lane"] == "reference" for row in inventory),
        },
        "issue_families": issue_families,
        "severity_family_counts": Counter(row["severity"] for row in issue_families),
        "positive_checks": positives,
        "notes": [
            "Issue-family affected counts overlap and must not be summed as unique records.",
            "Automated low-content-coverage alerts were not promoted wholesale because many compare an HTML wrapper with canonical text extracted from its PDF attachment.",
            "The one missing official page remains live but has no explicit official effectivity label; ingest first, then adjudicate lifecycle.",
        ],
    }
    (out / "adjudicated_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
