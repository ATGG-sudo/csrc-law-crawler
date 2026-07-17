#!/usr/bin/env python3
"""Run all registered sources, publish validated outputs, then verify incrementally."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from csrc_law_crawler.sources.cases import publish_case_records
from csrc_law_crawler.sources.digest import build_digest
from csrc_law_crawler.sources.publish import stage_and_publish_canonical
from csrc_law_crawler.sources.registry import load_registry
from csrc_law_crawler.sources.runner import SourceRunner
from config import WORKERS
from runtime import log_event
from storage import load_json, output_dir, run_with_output_lock, save_json, utc_now_iso


def _change_count(root: Path, run_id: str) -> int:
    path = root / "work" / "changes" / f"{run_id}.jsonl"
    if not path.exists():
        return 0
    return sum(bool(line.strip()) for line in path.read_text(encoding="utf-8").splitlines())


def _mark_publication(
    report: dict[str, Any],
    registry: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    lanes = {
        endpoint["endpoint_id"]: endpoint["default_material_lane"]
        for endpoint in registry["endpoints"]
    }
    publish_counts = {"published": 0, "quarantined": 0, "not_applicable": 0}
    for endpoint_id, state in report["endpoints"].items():
        lane = lanes.get(endpoint_id)
        if lane in {"rule", "case"} and int(state.get("materialized") or 0) > 0:
            publish_status = "published"
        elif lane in {"rule", "case"} and int(state.get("failed") or 0) > 0:
            publish_status = "quarantined"
        else:
            publish_status = "not_applicable"
        state["publish_status"] = publish_status
        publish_counts[publish_status] += 1
    report["counts"]["publish_status"] = publish_counts
    report_path = root / "reports" / "source_baselines" / f"{report['run_id']}.json"
    save_json(report_path, report)
    save_json(report_path.parent / "latest.json", report)
    manifest_path = root / "work" / "source_runs" / report["run_id"] / "manifest.json"
    manifest = load_json(manifest_path, {})
    for endpoint_id, state in report["endpoints"].items():
        if endpoint_id in (manifest.get("endpoints") or {}):
            manifest["endpoints"][endpoint_id]["publish_status"] = state["publish_status"]
    save_json(manifest_path, manifest)
    return report


def run_baseline_all(
    *,
    resume_run_id: str | None = None,
    verify_incremental: bool = False,
    refresh_subjects: bool = False,
    retry_failed: bool = False,
    retry_incomplete: bool = False,
    workers: int = WORKERS,
) -> dict[str, Any]:
    root = output_dir()
    registry = load_registry()
    runner = SourceRunner(registry=registry, root=root)

    baseline = runner.run(
        mode="baseline",
        resume_run_id=resume_run_id,
        workers=workers,
        refresh_subjects=refresh_subjects,
        retry_failed=retry_failed,
        retry_incomplete=retry_incomplete,
    )
    seeds = load_json(root / "work" / "subject_seeds.json", {})
    cases = publish_case_records(root)
    publication = stage_and_publish_canonical(run_id=baseline["run_id"], root=root)
    baseline = _mark_publication(baseline, registry, root)
    baseline_digest = build_digest(run_id=baseline["run_id"], root=root)

    incremental = None
    incremental_changes = None
    incremental_publication = None
    incremental_digest = None
    if verify_incremental:
        incremental = SourceRunner(registry=registry, root=root).run(
            mode="incremental",
            workers=workers,
            refresh_subjects=refresh_subjects,
            retry_failed=retry_failed,
            retry_incomplete=retry_incomplete,
        )
        incremental_changes = _change_count(root, incremental["run_id"])
        if incremental_changes:
            publish_case_records(root)
            incremental_publication = stage_and_publish_canonical(
                run_id=incremental["run_id"],
                root=root,
            )
        incremental = _mark_publication(incremental, registry, root)
        incremental_digest = build_digest(run_id=incremental["run_id"], root=root)
    status = (
        "complete"
        if baseline["status"] == "complete"
        and (incremental is None or incremental["status"] == "complete")
        else "incomplete"
    )
    report = {
        "schema_version": 2,
        "status": status,
        "generated_at": utc_now_iso(),
        "registry_endpoints": len(registry["endpoints"]),
        "registry_profiles": sum(len(item["profiles"]) for item in registry["endpoints"]),
        "baseline": baseline,
        "subject_seeds": {
            "count": seeds["count"],
            "queryable_count": seeds["queryable_count"],
        },
        "cases": cases,
        "publication": publication,
        "baseline_digest": baseline_digest["counts"],
        "incremental_verification": (
            "skipped"
            if incremental is None
            else "complete"
            if incremental["status"] == "complete"
            else "incomplete"
        ),
        "incremental": incremental,
        "incremental_changes": incremental_changes,
        "incremental_publication": incremental_publication,
        "incremental_digest": incremental_digest["counts"] if incremental_digest else None,
    }
    save_json(root / "reports" / "source_baselines" / "baseline_all_latest.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="执行工作簿全部信源基线，可选立即增量核验")
    parser.add_argument("--resume", metavar="RUN_ID")
    parser.add_argument("--verify-incremental", action="store_true")
    parser.add_argument("--refresh-subjects", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--retry-incomplete", action="store_true")
    args = parser.parse_args()
    try:
        report = run_baseline_all(
            resume_run_id=args.resume,
            verify_incremental=args.verify_incremental,
            refresh_subjects=args.refresh_subjects,
            retry_failed=args.retry_failed,
            retry_incomplete=args.retry_incomplete,
            workers=WORKERS,
        )
    except Exception as exc:
        log_event(
            "baseline_all_failed",
            level="ERROR",
            message=f"失败: {type(exc).__name__}: {exc}",
        )
        return 1
    log_event(
        "baseline_all_result",
        message=json.dumps(
            {
                "status": report["status"],
                "baseline_run_id": report["baseline"]["run_id"],
                "incremental_run_id": (
                    report["incremental"]["run_id"] if report["incremental"] else None
                ),
                "incremental_changes": report["incremental_changes"],
            },
            ensure_ascii=False,
        ),
    )
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(run_with_output_lock(main, "baseline-all"))
