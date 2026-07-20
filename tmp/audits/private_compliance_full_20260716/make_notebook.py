#!/usr/bin/env python3
"""Generate and execute the reproducible audit notebook."""

from __future__ import annotations

import argparse
from pathlib import Path

import nbformat as nbf
from nbclient import NotebookClient


def md(text: str):
    return nbf.v4.new_markdown_cell(text.strip())


def code(text: str):
    return nbf.v4.new_code_cell(text.strip())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-dir", type=Path, required=True)
    args = parser.parse_args()
    audit_dir = args.audit_dir.resolve()
    notebook_path = audit_dir / "private_compliance_full_audit.ipynb"

    nb = nbf.v4.new_notebook()
    nb["metadata"]["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    nb["metadata"]["language_info"] = {"name": "python", "version": "3.12"}
    nb["cells"] = [
        md(
            """
# 私募合规语料全量官网复核

快照日期：2026-07-16。数据真源为 `D:\\FUND_COMPLIANCE\\CSRC`；本笔记本仅读取审计产物，不修改代码、raw、canonical 或 reports。
"""
        ),
        md(
            """
## tl;dr

- 私募合规毛池 862 条，剔除 37 条词面误命中后，最终全集 825 条（规则 146，参考/执法 679）。
- 1,008/1,008 个现有官方来源可访问；协会官网独立清单的 598 条私募候选中，597 条与本地 URL 和标准化标题完全对应，1 条规则页漏入库。
- 291/291 个本地 PDF 可解析且无哈希漂移；358/358 个在线 PDF 链接具有有效 PDF 签名。
- 主要风险不是普遍标题错配，而是效力/时效错误、同 URL 重复入库、扫描件不可检索和 5 个空附件端点。
"""
        ),
        code(
            f"""
from pathlib import Path
import csv, json
from collections import Counter
from IPython.display import Markdown, display

AUDIT_DIR = Path({str(audit_dir)!r})
OUT = AUDIT_DIR / "output"

def read_csv(name):
    with (OUT / name).open(encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))

def read_json(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))

def read_jsonl(name):
    return [json.loads(line) for line in (OUT / name).read_text(encoding="utf-8").splitlines()]

inventory = read_csv("candidate_inventory.csv")
summary = read_json("adjudicated_summary.json")
issues = read_csv("adjudicated_issue_families.csv")
positives = read_csv("positive_checks.csv")
cases = read_csv("adjudicated_cases.csv")
url_results = read_jsonl("url_results.jsonl")
pdf_results = read_jsonl("pdf_results.jsonl")
live_amac = read_csv("live_amac_comparison.csv")

assert len(inventory) == 825
assert len(url_results) == 1008 and all(200 <= row.get("status_code", 0) < 300 for row in url_results)
assert len(pdf_results) == 291 and all(not row.get("error") for row in pdf_results)
assert sum(row["match_basis"] == "none" for row in live_amac) == 1
print("审计产物与关键不变量校验通过。")
"""
        ),
        md(
            """
## Context & Methods

目标是复核私募基金合规语料的标题—正文—原始链接—分类—时效—附件关系。方法包括：

1. 扫描 7,008 个 canonical JSON，按私募基金、登记备案、私募资管、股权/创投基金和基金法等口径构造毛池。
2. 人工排除“向不特定合格投资者公开发行”、中小企业私募债和机构间私募产品/ABS 平台等 37 条词面误命中。
3. 隔离执行协会官网全分页抓取（2,525/2,525，失败 0），按相同口径取得 598 条官网私募候选并与 D 盘对账。
4. 访问全部 1,008 个官方来源 URL，检查状态、标题、日期、正文可比性和附件链接。
5. 检查 291 个本地 PDF 的结构、页数、文本层和 SHA；检查 358 个在线 PDF 签名及 18 个直接附件端点。
6. 对自动告警进行人工降噪：区分确认错误、包装页/PDF 来源不对称和校验限制；跨年度目视检查 8 个扫描 PDF 首页。
"""
        ),
        code(
            """
scope = summary["scope"]
rows = [
    ("全部 canonical", 7008),
    ("关键词毛池", scope["gross_keyword_candidates"]),
    ("排除误命中", scope["scope_exclusions"]),
    ("最终私募合规全集", scope["final_private_compliance_population"]),
    ("规则类", scope["rule_lane"]),
    ("参考/执法类", scope["reference_lane"]),
]
display(Markdown("\\n".join(["|口径|数量|", "|---|---:|"] + [f"|{k}|{v:,}|" for k, v in rows])))
"""
        ),
        md("## Data"),
        code(
            """
domain_counts = Counter(row["url"].split("/")[2] for row in url_results)
effectiveness = Counter(row["effectiveness"] for row in inventory)
content_status = Counter(row["content_status"] for row in inventory)

display(Markdown("### 官方来源域名\\n" + "\\n".join(
    ["|域名|URL 数|", "|---|---:|"] + [f"|{k}|{v:,}|" for k, v in domain_counts.most_common()]
)))
display(Markdown("### 效力状态\\n" + "\\n".join(
    ["|状态|记录数|", "|---|---:|"] + [f"|{k}|{v:,}|" for k, v in effectiveness.most_common()]
)))
display(Markdown("### 内容状态\\n" + "\\n".join(
    ["|状态|记录数|", "|---|---:|"] + [f"|{k}|{v:,}|" for k, v in content_status.most_common()]
)))
"""
        ),
        md("## Results"),
        code(
            """
positive_lines = ["|检查项|结果|通过/总数|说明|", "|---|---|---:|---|"]
for row in positives:
    positive_lines.append(
        f"|{row['check']}|{row['result']}|{int(row['numerator']):,}/{int(row['denominator']):,}|{row['note']}|"
    )
display(Markdown("### 正向校验\\n" + "\\n".join(positive_lines)))
"""
        ),
        code(
            """
issue_lines = ["|优先级|严重度|问题族|影响|证据|", "|---:|---|---|---:|---|"]
for row in issues:
    issue_lines.append(
        f"|P{row['priority']}|{row['severity']}|{row['issue_family']}|{int(row['affected']):,} {row['unit']}|{row['evidence']}|"
    )
display(Markdown("### 人工裁决后的问题族\\n\\n> 各问题族有重叠，影响数不可相加。\\n\\n" + "\\n".join(issue_lines)))
"""
        ),
        code(
            """
critical = [row for row in cases if row["severity"] == "critical"]
case_lines = ["|问题族|ID|标题|本地状态证据|", "|---|---|---|---|"]
for row in critical:
    case_lines.append(f"|{row['issue_family']}|{row['id'] or '官网缺失项'}|{row['title']}|{row['evidence']}|")
display(Markdown("### 关键问题明细\\n" + "\\n".join(case_lines)))
"""
        ),
        code(
            """
scans = [row for row in pdf_results if row.get("scan_likely")]
pdf_stats = [
    ("本地 PDF", len(pdf_results)),
    ("有效 PDF", sum(not row.get("error") for row in pdf_results)),
    ("SHA 不一致", sum(row.get("sha_match") is False for row in pdf_results)),
    ("疑似扫描 PDF", len(scans)),
    ("扫描 PDF 涉及记录", len({rid for row in scans for rid in row.get("record_ids", [])})),
]
display(Markdown("### PDF 与扫描件\\n" + "\\n".join(
    ["|指标|数量|", "|---|---:|"] + [f"|{k}|{v:,}|" for k, v in pdf_stats]
)))
"""
        ),
        md(
            """
## Takeaways

1. 标题与链接整体可信：协会官网独立清单中，已覆盖的 597 条均为精确 URL 和标准化标题匹配；没有发现成批标题错配。
2. 时效/效力是首要风险：NERIS 日期系统性提前一天，2025/2026 新规则与旧规则的 pending/current/historical 链条未正确表达。
3. 内容风险集中在结构性边角：同一官网 URL 被拆成多个 canonical ID、短包装页没有并入 PDF 正文、扫描执法材料缺少 OCR。
4. 附件真伪总体可靠：非空远程附件与本地 SHA 全部一致；5 个 NERIS 端点虽然 HTTP 200，但正文为空，应明确标记不可用并寻找替代官方副本。
5. 修复顺序应为：关键效力链与日期 → 漏页/重复 canonical → 规则/参考分类 → 扫描件 OCR → TLS 与附件可观测性。

局限：本次是 2026-07-16 的官网快照；`fg.amac.org.cn` 的 TLS 证书链在当前运行时无法验证，内容校验采用了明确记录的主机级绕过；自动正文相似度不能直接比较“HTML 包装页”与“PDF 正文”，因此相关低覆盖告警经过人工裁决后才纳入结论。
"""
        ),
    ]

    nbf.write(nb, notebook_path)
    client = NotebookClient(nb, timeout=180, kernel_name="python3", allow_errors=False)
    executed = client.execute(cwd=str(audit_dir))
    nbf.write(executed, notebook_path)
    print(notebook_path)


if __name__ == "__main__":
    main()
