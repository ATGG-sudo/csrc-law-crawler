"""Thin dispatcher that preserves the legacy script entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import sys
from typing import Any


@dataclass(frozen=True)
class Command:
    module: str
    lock_reason: str | None = None
    description: str = ""


COMMANDS: dict[str, Command] = {
    "baseline-all": Command("baseline_all", "baseline-all", "执行全部信源基线与立即增量核验"),
    "crawl": Command("crawl", "crawl", "抓取 NERIS 法规和执法文书"),
    "enhance": Command("enhance", "enhance", "执行修订、案例、文书增强阶段"),
    "repair": Command("repair", "repair", "执行 P0-P2 多源修复流水线"),
    "amac-crawl": Command("amac_crawl", "amac-crawl", "抓取 AMAC 补充来源"),
    "run-sources": Command(
        "source_crawl",
        "run-sources",
        "运行工作簿注册的公开信源",
    ),
    "import-wechat": Command(
        "import_wechat",
        "import-wechat",
        "导入基小律 JSON + HTML 导出包",
    ),
    "build-digest": Command("build_digest", "build-digest", "生成信源基线与变化摘要"),
    "neris-attachments": Command(
        "neris_attachments",
        "neris-attachments",
        "发现或下载 NERIS 独立附件",
    ),
    "prefetch-revision-evidence": Command(
        "prefetch_revision_evidence",
        "prefetch-revision-evidence",
        "预取官方修订关系证据",
    ),
    "normalize-laws": Command("normalize_laws", "normalize-laws", "清洗 NERIS 法规"),
    "download-assets": Command("download_assets", "download-assets", "下载清洗发现的资产"),
    "export-markdown-laws": Command(
        "export_markdown_laws",
        "export-markdown-laws",
        "导出 NERIS Markdown",
    ),
    "build-catalog": Command("build_catalog", "build-catalog", "生成统一目录"),
    "normalize-catalog": Command(
        "normalize_catalog",
        "normalize-catalog",
        "清洗统一目录 canonical JSON",
    ),
    "export-markdown-catalog": Command(
        "export_markdown_catalog",
        "export-markdown-catalog",
        "导出统一目录 Markdown",
    ),
    "build-canonical-relations": Command(
        "build_canonical_relations",
        "build-canonical-relations",
        "生成 canonical 关系图",
    ),
    "export-relation-viewer": Command(
        "relation_viewer",
        "export-relation-viewer",
        "导出 canonical 关系图静态查看器",
    ),
    "coverage-gaps": Command(
        "coverage_gaps",
        "coverage-gaps",
        "生成覆盖缺口报告",
    ),
    "validate-normalized": Command(
        "validate_normalized",
        None,
        "校验 NERIS 清洗层",
    ),
    "validate-enhance": Command("validate_enhance", None, "校验增强抓取结果"),
    "validate-catalog": Command("validate_catalog", None, "校验统一目录"),
    "validate-catalog-exports": Command(
        "validate_catalog_exports",
        "validate-catalog-exports",
        "校验统一目录导出覆盖率",
    ),
}


def _usage() -> str:
    rows = "\n".join(
        f"  {name:<28} {command.description}"
        for name, command in sorted(COMMANDS.items())
    )
    return (
        "Usage: csrc-crawler <command> [command options]\n\n"
        "Commands:\n"
        f"{rows}\n\n"
        "Global options such as --output-root, --config, --delay-min, "
        "--max-retries, and --max-download-bytes are passed through to the "
        "target command."
    )


def _call_command(command: Command, args: list[str]) -> int:
    original_argv = sys.argv[:]
    sys.argv = [f"{command.module}.py", *args]
    try:
        module = importlib.import_module(command.module)
        entrypoint: Any = getattr(module, "main")
        if command.lock_reason is None:
            run_with_context: Any | None = getattr(module, "run_with_context", None)
            if run_with_context is not None:
                reason = command.module.replace("_", "-")
                return int(run_with_context(entrypoint, reason))
            return int(entrypoint())
        run_with_output_lock: Any = getattr(module, "run_with_output_lock")
        return int(run_with_output_lock(entrypoint, command.lock_reason))
    finally:
        sys.argv = original_argv


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        sys.stdout.write(_usage() + "\n")
        return 0

    command_name = args.pop(0)
    command = COMMANDS.get(command_name)
    if command is None:
        sys.stderr.write(f"Unknown command: {command_name}\n")
        sys.stderr.write(_usage() + "\n")
        return 2
    return _call_command(command, args)


if __name__ == "__main__":
    sys.exit(main())
