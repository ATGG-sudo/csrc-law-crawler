#!/usr/bin/env python3
"""Export normalized canonical catalog entities to Markdown."""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path
from typing import Any

from markdown_utils import (
    assets_section,
    clean_table_value,
    filename_stem,
    relative_markdown_link,
    replace_asset_links,
    strip_leading_title,
    yaml_scalar,
)
from normalize_catalog import catalog_normalized_manifest_path
from runtime import log_event
from storage import (
    canonical_dir,
    catalog_dir,
    catalog_markdown_dir,
    catalog_normalized_dir,
    listed_output_files,
    load_json,
    relative_to_output,
    run_with_output_lock,
    save_json,
    utc_now_iso,
)


def catalog_markdown_bucket_dir(bucket: str) -> Path:
    return catalog_markdown_dir() / bucket


def catalog_markdown_manifest_path() -> Path:
    return catalog_dir() / "markdown_manifest.json"


def catalog_library_dir() -> Path:
    return canonical_dir() / "library"


def catalog_library_manifest_path() -> Path:
    return catalog_library_dir() / "manifest.json"


def bucket_for_document(doc: dict[str, Any]) -> str:
    effectiveness = (doc.get("effectiveness") or {}).get("status")
    return {
        "current": "current",
        "pending": "pending",
        "historical": "historical",
        "not_applicable": "reference",
    }.get(str(effectiveness), "unknown")


def _target_path_in_dir(
    doc: dict[str, Any],
    entity_id: str,
    used_paths: set[Path],
    target_dir: Path,
) -> Path:
    metadata = doc.get("metadata") or {}
    stem = filename_stem(metadata, entity_id)
    candidate = target_dir / f"{stem}.md"
    if candidate not in used_paths:
        used_paths.add(candidate)
        return candidate
    suffix = entity_id.removeprefix("law_")[:8]
    candidate = target_dir / f"{stem} - {suffix}.md"
    counter = 2
    while candidate in used_paths:
        candidate = target_dir / f"{stem} - {suffix}-{counter}.md"
        counter += 1
    used_paths.add(candidate)
    return candidate


def _target_path(
    doc: dict[str, Any],
    entity_id: str,
    used_paths: set[Path],
) -> Path:
    return _target_path_in_dir(
        doc,
        entity_id,
        used_paths,
        catalog_markdown_bucket_dir(bucket_for_document(doc)),
    )


REFERENCE_LIBRARY_DIRS = {
    "publication_consultation": "01_发布与征求意见",
    "interpretation_qa": "02_解读问答",
    "template_guidance": "03_办事指南与模板",
    "research_statistics": "04_行业研究与统计",
    "enforcement_reference": "05_监管案例与自律措施",
    "other_reference": "99_其他参考",
}


def _web_category_summary(doc: dict[str, Any]) -> tuple[str, str, str]:
    sources = doc.get("sources") or []
    ranked = {"page_breadcrumb": 0, "api_channel": 1, "endpoint_profile": 2, "url_inference": 3}
    candidates = sorted(
        (source for source in sources if source.get("web_category_leaf")),
        key=lambda source: ranked.get(str(source.get("web_category_provenance")), 9),
    )
    if not candidates:
        return "", "", ""
    source = candidates[0]
    path = source.get("web_category_path") or []
    if isinstance(path, list):
        path = " / ".join(str(value) for value in path)
    return (
        str(source.get("web_category_leaf") or ""),
        str(path or ""),
        str(source.get("web_category_provenance") or ""),
    )


def library_relative_dir_for_document(doc: dict[str, Any]) -> Path:
    material = doc.get("material_classification") or {}
    lane = str(material.get("lane") or "unknown")
    status = str((doc.get("effectiveness") or {}).get("status") or "unknown")
    if lane == "rule" and status == "current":
        return Path("01_现行制度")
    if lane == "rule" and status == "pending":
        return Path("02_待生效制度")
    if lane == "rule" and status == "historical":
        return Path("03_失效制度")
    if lane == "reference":
        category = str(material.get("category") or "other_reference")
        return Path("04_参考资料") / REFERENCE_LIBRARY_DIRS.get(category, "99_其他参考")
    if lane == "rule":
        return Path("05_待核验") / "01_制度效力待核验"
    return Path("05_待核验") / "02_材料性质待核验"


def _front_matter(doc: dict[str, Any]) -> str:
    metadata = doc.get("metadata") or {}
    preferred = doc.get("preferred_source") or {}
    effectiveness = doc.get("effectiveness") or {}
    material = doc.get("material_classification") or {}
    reference_lifecycle = doc.get("reference_lifecycle") or {}
    enforcement = doc.get("enforcement_classification") or {}
    web_leaf, web_path, web_provenance = _web_category_summary(doc)
    values = {
        "id": doc.get("id"),
        "title": doc.get("title"),
        "document_type": doc.get("document_type"),
        "status": doc.get("status"),
        "material_lane": material.get("lane"),
        "material_category": material.get("category"),
        "material_basis": material.get("basis"),
        "material_confidence": material.get("confidence"),
        "effectiveness": effectiveness.get("status"),
        "effectiveness_label": effectiveness.get("label"),
        "effectiveness_basis": effectiveness.get("basis"),
        "reference_lifecycle": reference_lifecycle.get("status"),
        "enforcement_category": enforcement.get("category"),
        "enforcement_subtype": enforcement.get("subtype"),
        "web_category_leaf": web_leaf,
        "web_category_path": web_path,
        "web_category_provenance": web_provenance,
        "fileno": metadata.get("fileno"),
        "pub_org": metadata.get("pub_org"),
        "pub_date": metadata.get("pub_date"),
        "effective_date": metadata.get("effective_date"),
        "preferred_source_system": preferred.get("system"),
        "preferred_source_record_id": preferred.get("record_id"),
        "content_status": doc.get("content_status"),
        "source_file": doc.get("source_file"),
    }
    lines = ["---"]
    lines.extend(f"{key}: {yaml_scalar(value)}" for key, value in values.items())
    revision_ref = doc.get("revision_ref") or {}
    if revision_ref:
        lines.append(f"revision_ref: {yaml_scalar(revision_ref.get('family_id'))}")
    lines.append("---")
    return "\n".join(lines)


def _metadata_table(doc: dict[str, Any]) -> str:
    metadata = doc.get("metadata") or {}
    effectiveness = doc.get("effectiveness") or {}
    material = doc.get("material_classification") or {}
    reference_lifecycle = doc.get("reference_lifecycle") or {}
    enforcement = doc.get("enforcement_classification") or {}
    web_leaf, web_path, web_provenance = _web_category_summary(doc)
    rows = [
        ("统一法规 ID", doc.get("id")),
        ("文件类型", doc.get("document_type")),
        ("材料性质", material.get("lane")),
        ("材料类别", material.get("category")),
        ("性质分类依据", material.get("basis")),
        ("性质分类置信度", material.get("confidence")),
        ("文号", metadata.get("fileno")),
        ("发布机构", metadata.get("pub_org")),
        ("发布日期", metadata.get("pub_date")),
        ("施行日期", metadata.get("effective_date")),
        ("效力状态", doc.get("status")),
        ("归一化效力", effectiveness.get("status")),
        ("归一化效力标签", effectiveness.get("label")),
        ("归一化效力依据", effectiveness.get("basis")),
        ("参考材料生命周期", reference_lifecycle.get("status")),
        ("监管材料类别", enforcement.get("category")),
        ("监管材料子类型", enforcement.get("subtype")),
        ("网页原生栏目", web_leaf),
        ("网页栏目路径", web_path),
        ("网页栏目证据来源", web_provenance),
        ("首选来源", (doc.get("preferred_source") or {}).get("system")),
    ]
    lines = ["| 字段 | 值 |", "| --- | --- |"]
    lines.extend(
        f"| {clean_table_value(key)} | {clean_table_value(value)} |" for key, value in rows
    )
    return "\n".join(lines)


def _sources_section(doc: dict[str, Any], markdown_path: Path) -> str:
    sources = doc.get("sources") or []
    if not sources:
        return ""
    lines = [
        "## 官方来源",
        "",
        "| 来源 | 角色 | 网页栏目 | 证据来源 | 记录 ID | 链接 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for source in sources:
        local_link = relative_markdown_link(markdown_path, source.get("local_file"))
        page_url = source.get("page_url")
        links = []
        if page_url:
            links.append(f"[官网]({page_url})")
        if local_link:
            links.append(f"[本地]({local_link})")
        lines.append(
            "| "
            + " | ".join(
                [
                    clean_table_value(source.get("system")),
                    clean_table_value(source.get("role")),
                    clean_table_value(source.get("web_category_leaf")),
                    clean_table_value(source.get("web_category_provenance")),
                    clean_table_value(source.get("record_id")),
                    " / ".join(links),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def build_catalog_markdown(doc: dict[str, Any], markdown_path: Path) -> str:
    title = str(doc.get("title") or doc.get("id") or markdown_path.stem)
    assets: dict[str, dict[str, Any]] = {}
    for asset in doc.get("assets") or []:
        asset_id = str(asset.get("asset_id") or "")
        if not asset_id:
            continue
        local_link = relative_markdown_link(markdown_path, asset.get("local_file"))
        assets[asset_id] = {
            **asset,
            "markdown_link": local_link or asset.get("source_url") or f"asset:{asset_id}",
        }
    body = strip_leading_title(str(doc.get("full_text_markdown") or ""), title)
    body = replace_asset_links(body, assets)
    parts = [_front_matter(doc), f"# {title}", _metadata_table(doc)]
    if body:
        parts.append(body)
    elif doc.get("content_status") == "metadata_only":
        parts.append("> 正文未能从官方文件中自动抽取；请参阅下方官方来源或本地附件。")
    sources = _sources_section(doc, markdown_path)
    if sources:
        parts.append(sources)
    asset_section = assets_section(assets)
    if asset_section:
        parts.append(asset_section)
    return "\n\n".join(parts).rstrip() + "\n"


def _write_library_index_and_manifest(
    items: list[dict[str, Any]],
    *,
    bucket_counts: dict[str, int],
) -> None:
    index_path = catalog_library_dir() / "index.csv"
    fieldnames = [
        "id",
        "title",
        "pub_org",
        "fileno",
        "pub_date",
        "effective_date",
        "material_lane",
        "material_category",
        "material_confidence",
        "material_basis",
        "effectiveness",
        "effectiveness_basis",
        "reference_lifecycle",
        "enforcement_category",
        "enforcement_subtype",
        "web_category_leaf",
        "web_category_path",
        "web_category_provenance",
        "library_file",
        "official_sources",
    ]
    with index_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            row = {key: item.get(key) for key in fieldnames}
            row["official_sources"] = ";".join(item.get("official_sources") or [])
            writer.writerow(row)
    directory_counts: dict[str, int] = {}
    library_items: list[dict[str, Any]] = []
    for item in items:
        relative_file = str(item.get("library_file") or "")
        directory = str(Path(relative_file).parent.relative_to("canonical/library"))
        directory_counts[directory] = directory_counts.get(directory, 0) + 1
        library_items.append(
            {
                **item,
                "file": relative_file,
            }
        )
    save_json(
        catalog_library_manifest_path(),
        {
            "schema_version": 1,
            "updated_at": utc_now_iso(),
            "count": len(items),
            "bucket_counts": bucket_counts,
            "directory_counts": dict(sorted(directory_counts.items())),
            "index_csv": relative_to_output(index_path),
            "items": library_items,
        },
    )


def export_catalog_markdown(
    *,
    limit: int | None = None,
    force: bool = False,
    clean: bool = False,
) -> dict[str, Any]:
    normalized_files = listed_output_files(
        catalog_normalized_manifest_path(),
        field="file",
        fallback_dir=catalog_normalized_dir(),
        pattern="law_*.json",
        limit=limit,
    )
    if not normalized_files:
        raise FileNotFoundError("canonical/json 不存在，请先运行 python normalize_catalog.py")
    if clean and catalog_markdown_dir().exists():
        shutil.rmtree(catalog_markdown_dir())
    if clean and catalog_library_dir().exists():
        shutil.rmtree(catalog_library_dir())
    for bucket in ("current", "pending", "unknown", "historical", "reference"):
        catalog_markdown_bucket_dir(bucket).mkdir(parents=True, exist_ok=True)
    library_dirs = [
        Path("01_现行制度"),
        Path("02_待生效制度"),
        Path("03_失效制度"),
        *(Path("04_参考资料") / value for value in REFERENCE_LIBRARY_DIRS.values()),
        Path("05_待核验") / "01_制度效力待核验",
        Path("05_待核验") / "02_材料性质待核验",
    ]
    for relative_dir in library_dirs:
        (catalog_library_dir() / relative_dir).mkdir(parents=True, exist_ok=True)

    used_paths: set[Path] = set()
    used_library_paths: set[Path] = set()
    items: list[dict[str, Any]] = []
    written = 0
    library_written = 0
    skipped = 0
    bucket_counts = {
        "current": 0,
        "pending": 0,
        "unknown": 0,
        "historical": 0,
        "reference": 0,
    }
    for index, path in enumerate(normalized_files, start=1):
        doc = load_json(path, {})
        entity_id = str(doc.get("id") or path.stem)
        out_path = _target_path(doc, entity_id, used_paths)
        library_dir = catalog_library_dir() / library_relative_dir_for_document(doc)
        library_path = _target_path_in_dir(
            doc,
            entity_id,
            used_library_paths,
            library_dir,
        )
        bucket = bucket_for_document(doc)
        bucket_counts[bucket] += 1
        if out_path.exists() and not force:
            skipped += 1
        else:
            out_path.write_text(
                build_catalog_markdown(doc, out_path),
                encoding="utf-8",
            )
            written += 1
        if library_path.exists() and not force:
            pass
        else:
            library_path.write_text(
                build_catalog_markdown(doc, library_path),
                encoding="utf-8",
            )
            library_written += 1
        material = doc.get("material_classification") or {}
        reference_lifecycle = doc.get("reference_lifecycle") or {}
        enforcement = doc.get("enforcement_classification") or {}
        web_leaf, web_path, web_provenance = _web_category_summary(doc)
        metadata = doc.get("metadata") or {}
        official_sources = sorted(
            {
                str(source.get("page_url"))
                for source in doc.get("sources") or []
                if source.get("page_url")
            }
        )
        items.append(
            {
                "id": entity_id,
                "title": doc.get("title"),
                "status": doc.get("status"),
                "material_lane": material.get("lane"),
                "material_category": material.get("category"),
                "material_basis": material.get("basis"),
                "material_confidence": material.get("confidence"),
                "effectiveness": (doc.get("effectiveness") or {}).get("status"),
                "effectiveness_basis": (doc.get("effectiveness") or {}).get("basis"),
                "reference_lifecycle": reference_lifecycle.get("status"),
                "enforcement_category": enforcement.get("category"),
                "enforcement_subtype": enforcement.get("subtype"),
                "web_category_leaf": web_leaf,
                "web_category_path": web_path,
                "web_category_provenance": web_provenance,
                "bucket": bucket,
                "source_file": relative_to_output(path),
                "file": relative_to_output(out_path),
                "library_file": relative_to_output(library_path),
                "pub_org": metadata.get("pub_org"),
                "fileno": metadata.get("fileno"),
                "pub_date": metadata.get("pub_date"),
                "effective_date": metadata.get("effective_date"),
                "official_sources": official_sources,
                "text_length": len(str(doc.get("full_text_plain") or "")),
            }
        )
        if index % 100 == 0 or index == len(normalized_files):
            log_event(
                "export_progress",
                message=f"  exported catalog {index}/{len(normalized_files)}",
                index=index,
                total=len(normalized_files),
            )

    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "source_dir": relative_to_output(catalog_normalized_dir()),
        "markdown_dir": relative_to_output(catalog_markdown_dir()),
        "count": len(items),
        "bucket_counts": bucket_counts,
        "written": written,
        "library_written": library_written,
        "skipped": skipped,
        "filename_pattern": "title - fileno - effective_date.md",
        "items": items,
    }
    save_json(catalog_markdown_manifest_path(), manifest)
    _write_library_index_and_manifest(items, bucket_counts=bucket_counts)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="导出统一法规目录 Markdown")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    try:
        manifest = export_catalog_markdown(
            limit=args.limit,
            force=args.force,
            clean=args.clean,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1
    log_event(
        "cli_result",
        message=(
            f"完成: count={manifest['count']} written={manifest['written']} "
            f"skipped={manifest['skipped']} buckets={manifest['bucket_counts']} "
            f"-> {catalog_markdown_manifest_path()}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "export-markdown-catalog"))
