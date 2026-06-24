# CSRC 法规库爬虫

从 [证监会证券期货法规数据库（NERIS）](https://neris.csrc.gov.cn/falvfagui/) 抓取法规正文、执法文书正文和关系数据，并以[中国证券投资基金业协会官网](https://www.amac.org.cn/index/)作为补充官方来源，供合规检索、RAG、关系推理或后续入库使用。

默认输出目录：`/mnt/d/FUND_COMPLIANCE/CSRC`，可在 [config.py](/home/anjie/projects/csrc-law-crawler/config.py:6) 修改。

## 当前数据快照

最近校验时间：2026-06-24。

| 项目 | 当前结果 |
| --- | ---: |
| 官网法规列表 `lawType=1` | 3422 |
| 本地法规文件 `laws/reg_*.json` | 3422 |
| 官网执法文书列表 `lawType=2` | 3249 |
| 本地执法文书文件 `writs/writ_*.json` | 781 |
| 修订族 `relations/revisions.json` | 3311 |
| 修订版本节点 | 4411 |
| 无本地正文的历史修订节点 | 989 |
| 有官方证据的修订边 | 1086 |
| 关联法规边 | 946 |
| 案例引用唯一文书 ID | 781 |
| NERIS 独立附件记录 | 1313 |
| NERIS 独立附件已下载 / 源站空文件 | 1033 / 280 |
| AMAC 页面原始文件 | 489 |
| AMAC 页面及附件来源记录 | 664 |
| AMAC 已下载附件 | 175 |
| 统一法规实体 `catalog/laws` | 3852 |
| 统一目录 normalized `catalog/normalized/laws` | 3852 |
| 统一目录 Markdown `catalog/markdown/laws` | 3852 |
| AMAC 新增于 NERIS 的来源记录 | 429 |
| 清洗派生法规 `normalized/laws` | 3422 |
| 清洗抽取表格 | 1276 |
| 清洗发现图片/附件资产 | 1587 |
| 已下载资产 | 1159 |
| 下载失败资产 | 428 |
| 覆盖缺口 `relations/coverage_gaps.json` | 463 |
| Markdown 法规文件 `markdown/laws` | 3422 |
| Markdown 现行有效 `markdown/laws/current` | 3380 |
| Markdown 其他状态 `markdown/laws/other` | 42 |

说明：本地执法文书默认只抓取 `cases.json` 引用到的文书，不是官网全量 3249 份。需要全量时运行 `python enhance.py --pass 4 --all-writs`。

## 修复与数据质量

本轮已完成以下修复：

- `revisions.json` 不再按 `csrc_number` 合并。修订族只能来自 NERIS `changeLaw.evltList` 证据，边包含来源、证据和置信度。
- NERIS 独立附件通过 `findLocalFile` / `downloadLocal` 补抓，不再只依赖正文内嵌链接。
- AMAC 原始记录独立写入 `sources/amac/`，不会伪装成 NERIS `reg_*` 文件。
- 新增来源匹配层和统一法规实体层：`relations/source_matches.json`、`catalog/laws/`。
- 新增 `coverage_gaps.json`，区分源站缺失、未查询附件、未下载、下载失败和解析失败。
- 3422 个法规文件的 `metadata.pub_org` 已从 `source.list_summary.pub_org` 回填，当前缺失数为 0。
- 文书详情页解析已改为标准库 `HTMLParser`，不再用正则跨 HTML 表格抓取 metadata。
- 18 份被污染的文书元数据已重跑，当前 `writ_type` 污染、超长 `writ_type`、缺失 `dspt_date`、空正文均为 0。
- 已增加法规清洗派生层：原始 `laws/` 不动，清洗结果写入 `normalized/laws/`。
- 已增加图片/附件资产下载层：资产写入 `assets/laws/{law_id}/`，并生成全局 manifest 和失败清单。
- 已增加 Markdown 导出层：`normalized/laws/` 可导出到 `markdown/laws/current` 和 `markdown/laws/other`，资产占位会替换为本地相对路径或源 URL。
- Markdown 文件名采用 `title - fileno - effective_date.md`，自动清理文件名非法字符、控制 UTF-8 字节长度，并在重名时追加短 ID。

仍需注意：

- 原始 `laws/` 仍保留官网 API 原始 HTML，这是刻意保真的 source layer；检索/RAG 应优先使用 `normalized/laws/`。
- `normalized/laws/` 的 `full_text_plain` 与 `full_text_markdown` 当前无 HTML-like 标签残留，并已把表格转成结构化 `tables[]` 和 Markdown。
- 1313 个 NERIS 独立附件中已下载 1033 个；其余 280 个由下载接口返回 HTTP 200 空内容，属于源站附件对象不可用。
- 正文内嵌资产共 274 个，已下载 126 个、失败 148 个；失败主要是官网 `rdqsHeader/file/...` 返回空响应，详见 `assets/assets_failures.json`。
- `coverage_gaps.json` 当前记录 26 个源站缺正文、428 个下载失败和 9 个待人工复核的系列编号缺口。
- AMAC 补充层共提供 664 条页面/附件来源记录，匹配结果为：NERIS 未收录 429、同文 219、正文更完整的补充副本 15、歧义 1。
- 统一目录 3852 个实体均已生成 normalized JSON 和 Markdown；其中 10 个模板、XBRL 或旧格式附件无法自动抽取正文，Markdown 会明确标记为 `metadata_only` 并保留官方来源及本地附件链接。
- 25 份本地文书没有 `legal_basis`。抽检显示这通常来自官网详情页本身没有结构化处理依据，或旧文书页面结构较弱。
- `manifest.json` 当前只覆盖法规文件；文书以 `relations/cases.json` 的 `writ_ids` 和 `writs/` 文件为准。

## 安装

```bash
cd /home/anjie/projects/csrc-law-crawler
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

PDF和DOCX附件正文分别使用 `pypdf`、`python-docx` 抽取。

## P0-P2 修复流程

一键执行：

```bash
python repair.py --phase all
```

分阶段执行：

```bash
# P0：可信修订关系、NERIS独立附件、覆盖缺口
python prefetch_revision_evidence.py --workers 2
python enhance.py --pass 2 --rebuild-relations --skip-related-laws
python neris_attachments.py --workers 2
python normalize_laws.py --force
python download_assets.py --manifest-only
python coverage_gaps.py

# P1：AMAC政策法规基线 + 登备动态等全站补充检索
python amac_crawl.py

# P2：来源匹配和统一法规实体
python build_catalog.py
python validate_catalog.py
python normalize_catalog.py --force --clean
python export_markdown_catalog.py --force --clean
python validate_catalog_exports.py
```

修订关系重建会清除旧 `revision_ref`。旧版 `revisions.json` 不允许被增量复用，必须显式使用 `--rebuild-relations`。
`prefetch_revision_evidence.py` 会把每条 `changeLaw` 响应写入
`relations/revision_evidence_cache/`，中断后可继续，建图时优先复用缓存。

## 推荐运行顺序

```bash
# 1. 全量法规正文，支持 checkpoint 断点续传
python crawl.py --types regulation

# 2. 首次升级时从官网重建可信修订链
python enhance.py --pass 2 --rebuild-relations

# 3. 法规/条文 -> 执法案例索引
python enhance.py --pass 3

# 4. 抓取案例引用的执法文书正文
python enhance.py --pass 4

# 可选：抓全量执法文书
python enhance.py --pass 4 --all-writs
```

一次性跑增强阶段：

```bash
python enhance.py --pass all
```

常用参数：

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `crawl.py` | `--types regulation \| writ \| all` | 抓取法规、文书或全部 |
| `crawl.py` | `--limit N` | 仅抓前 N 条，便于调试 |
| `enhance.py` | `--pass 2 \| 3 \| 4 \| all` | 运行增强阶段 |
| `enhance.py` | `--limit N` | Pass 2/3 仅处理前 N 条法规 |
| `enhance.py` | `--all-writs` | Pass 4 抓取官网文书列表全量 |
| `enhance.py` | `--writ-pages N` | Pass 4 全量模式最多扫描 N 页 |
| `enhance.py` | `--force` | Pass 4 强制重抓文书详情 |
| `enhance.py` | `--no-patch-revision-ref` | Pass 2 不回写法规文件的 `revision_ref` |
| `enhance.py` | `--rebuild-relations` | 丢弃旧修订图和旧 `revision_ref` 后重建 |
| `enhance.py` | `--skip-related-laws` | 仅重建修订图，不刷新关联法规 |
| `enhance.py` | `--skip-law-level-cases` | Pass 3 跳过法规级案例 |

限速策略在 [config.py](/home/anjie/projects/csrc-law-crawler/config.py:11)：每次请求间隔 1.8-3.6 秒，每 40 次请求额外休息 8-15 秒。不要并行跑多个实例，容易触发 WAF 或 5xx。

## 输出目录结构

```text
OUTPUT_DIR/
├── laws/
│   └── reg_{secFutrsLawId}.json
├── writs/
│   └── writ_{lawWritId}.json
├── relations/
│   ├── revisions.json
│   ├── related_laws.json
│   ├── cases.json
│   ├── coverage_gaps.json
│   ├── source_matches.json
│   └── catalog_relations.json
├── sources/
│   ├── amac/
│   │   └── amac_{url_hash}.json
│   └── amac_manifest.json
├── catalog/
│   ├── laws/
│   │   └── law_{canonical_id}.json
│   ├── normalized/
│   │   ├── laws/
│   │   │   └── law_{canonical_id}.json
│   │   └── manifest.json
│   ├── markdown/
│   │   ├── laws/
│   │   │   ├── current/
│   │   │   └── other/
│   │   └── manifest.json
│   ├── manifest.json
│   └── review_queue.json
├── normalized/
│   ├── laws/
│   │   └── reg_{secFutrsLawId}.json
│   └── manifest.json
├── assets/
│   ├── laws/
│   │   └── {secFutrsLawId}/
│   │       ├── image_*.png
│   │       └── asset_manifest.json
│   ├── assets_manifest.json
│   ├── assets_failures.json
│   ├── neris_attachments/
│   └── amac/
├── markdown/
│   ├── laws/
│   │   ├── current/                # status = 现行有效
│   │   │   └── {title} - {fileno} - {effective_date}.md
│   │   └── other/
│   │       └── {title} - {fileno} - {effective_date}.md
│   └── manifest.json
├── manifest.json
├── checkpoint.json
└── crawl.log / enhance.log
```

实体 ID：

| 类型 | 官网字段 | 本地文件 |
| --- | --- | --- |
| 法规 | `secFutrsLawId` | `laws/reg_{id}.json` |
| 执法文书 | `lawWritId` | `writs/writ_{id}.json` |
| 条文 | `secFutrsLawEntryId` | 法规 JSON 的 `entries[]` / `items[]` |

## 数据结构

### 法规文件

`laws/reg_{id}.json`：

```json
{
  "metadata": {
    "id": "0fc431a2a10b47909beef058f6ac3335",
    "number": "1001",
    "name": "中华人民共和国证券法",
    "fileno": "主席令第37号",
    "pub_org": "全国人民代表大会常务委员会",
    "pub_date": "2019-12-27",
    "effective_date": "2020-02-29",
    "ineffective_date": null,
    "status_code": "1",
    "status": "现行有效",
    "version": "20191230",
    "body_ago": "前言或导语",
    "body_aft": ""
  },
  "entries": [
    {
      "entry_id": "…",
      "code": "0000.0000.0000.0001",
      "class_code": "…",
      "title": "第一章 总则",
      "text": "…",
      "items": [
        {
          "entry_id": "…",
          "code": "…",
          "title": "",
          "text": "…"
        }
      ]
    }
  ],
  "full_text": "按 metadata/body/entries 拼接的全文",
  "entry_class_code": "4",
  "source": {
    "list_summary": {
      "fileno": "主席令第37号",
      "pub_org": "全国人民代表大会常务委员会",
      "pub_date_ms": 1577376000000
    },
    "crawled_at": "…",
    "detail_url": "https://neris.csrc.gov.cn/falvfagui/rdqsHeader/mainbody?navbarId=1&secFutrsLawId=…"
  },
  "revision_ref": {
    "family_id": "neris:…",
    "relations_file": "relations/revisions.json"
  }
}
```

### 执法文书文件

`writs/writ_{id}.json`：

```json
{
  "metadata": {
    "id": "4fc2d518f9de4f0482dfb2df6e28024a",
    "name": "中国证券监督管理委员会福建监管局行政处罚决定书〔2026〕21号（亚太所、田梦珺、任海春）",
    "fileno": "",
    "issue_org": "中国证券监督管理委员会福建监管局",
    "dspt_date": "2026-06-02",
    "dspt_date_ms": null,
    "writ_type": "行政处罚",
    "original_link": "http://www.csrc.gov.cn/…"
  },
  "body": "一、案情简介\n…",
  "legal_basis": [
    {
      "law_id": "0fc431a2a10b47909beef058f6ac3335",
      "entry_id": "85d0334ade4a45e598c548539dbf3c7f",
      "law_name": "中华人民共和国证券法",
      "entry_title": "第一百六十三条"
    }
  ],
  "parties": [
    {
      "party_type": "组织机构",
      "name": "…",
      "role": "会计师事务所及其从业人员",
      "violation_type": "审计程序缺陷",
      "penalty_amount": "264.15万元"
    }
  ],
  "list_summary": null,
  "source": {
    "crawled_at": "…",
    "detail_url": "https://neris.csrc.gov.cn/falvfagui/rdqsHeader/lawWritInfo?navbarId=1&lawWritId=…",
    "list_api": "rdqsHeader/informationController?lawType=2",
    "detail_type": "html"
  }
}
```

### 修订关系

`relations/revisions.json`：

```json
{
  "updated_at": "…",
  "schema_version": 2,
  "families": {
    "neris:…": {
      "family_id": "neris:…",
      "versions": [
        {
          "id": "0fc431a2a10b47909beef058f6ac3335",
          "csrc_number": "1001",
          "version": "20191230",
          "label": "中华人民共和国证券法",
          "name": "中华人民共和国证券法",
          "local_file": "laws/reg_0fc431a2a10b47909beef058f6ac3335.json"
        }
      ],
      "edges": [
        {
          "from": "新版 secFutrsLawId",
          "to": "旧版 secFutrsLawId",
          "relation": "supersedes",
          "source": "neris.changeLaw",
          "confidence": 0.95
        }
      ]
    }
  },
  "by_law_id": {
    "0fc431a2a10b47909beef058f6ac3335": "1001"
  }
}
```

### 关联法规

`relations/related_laws.json`：

```json
{
  "updated_at": "…",
  "items": {
    "源法规 secFutrsLawId": [
      {
        "to_law_id": "目标法规 secFutrsLawId",
        "name": "…",
        "fileno": "…",
        "relation_type": "…",
        "raw": {}
      }
    ]
  }
}
```

### 案例索引

`relations/cases.json`：

```json
{
  "updated_at": "…",
  "writ_ids": ["4fc2d518f9de4f0482dfb2df6e28024a"],
  "by_law": {
    "0fc431a2a10b47909beef058f6ac3335": {
      "entry_counts": {
        "secFutrsLawEntryId": 3
      },
      "law_level": [
        {
          "law_writ_id": "4fc2d518f9de4f0482dfb2df6e28024a",
          "name": "…",
          "fileno": "…",
          "issue_org": "…",
          "dspt_date_ms": 1780329600000,
          "link_addr": "http://www.csrc.gov.cn/…",
          "local_file": "writs/writ_4fc2d518f9de4f0482dfb2df6e28024a.json",
          "detail_url": "https://neris.csrc.gov.cn/falvfagui/rdqsHeader/lawWritInfo?navbarId=1&lawWritId=…"
        }
      ],
      "by_entry": {
        "secFutrsLawEntryId": []
      }
    }
  }
}
```

## 清洗与资产管线

原始 `laws/` 不覆盖。清洗和资产下载是派生层，可重复运行：

```bash
# 1. 生成 normalized/laws
python normalize_laws.py --force

# 2. 下载 normalized 中发现的图片/附件
python download_assets.py --force

# 3. 校验清洗和资产状态
python validate_normalized.py --sample 10

# 4. 导出 Markdown
python export_markdown_laws.py --force --clean
```

调试参数：

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `normalize_laws.py` | `--limit N` | 仅清洗前 N 个法规 |
| `normalize_laws.py` | `--force` | 覆盖已有派生文件 |
| `download_assets.py` | `--limit-laws N` | 仅扫描前 N 个 normalized 法规 |
| `download_assets.py` | `--limit-assets N` | 最多下载/检查 N 个资产 |
| `download_assets.py` | `--force` | 已有本地资产也重新下载 |
| `validate_normalized.py` | `--sample N` | 每类问题展示 N 个样本 |
| `export_markdown_laws.py` | `--limit N` | 仅导出前 N 个法规 |
| `export_markdown_laws.py` | `--force` | 覆盖已有 Markdown 文件 |
| `export_markdown_laws.py` | `--clean` | 导出前清空旧的 `markdown/laws` |

`normalized/laws/reg_{id}.json` 主要新增字段：

```json
{
  "source_file": "laws/reg_xxx.json",
  "normalized_at": "…",
  "body_ago": {
    "raw_html": "…",
    "plain": "…",
    "markdown": "…",
    "tables": [],
    "assets": []
  },
  "entries": [
    {
      "entry_id": "…",
      "title": "…",
      "text_raw_html": "…",
      "text_plain": "…",
      "text_markdown": "…",
      "tables": ["table_0001"],
      "assets": ["image_xxx"],
      "items": []
    }
  ],
  "full_text_plain": "供检索/RAG 使用的纯文本",
  "full_text_markdown": "保留表格和资产占位的 Markdown",
  "tables": [
    {
      "table_id": "table_0001",
      "rows": [["列1", "列2"]],
      "markdown": "| 列1 | 列2 |"
    }
  ],
  "assets": [
    {
      "asset_id": "image_xxx",
      "kind": "image",
      "source_url": "https://neris.csrc.gov.cn/falvfagui/rdqsHeader/file/...",
      "local_file": "assets/laws/{law_id}/image_xxx.png",
      "content_type": "image/png",
      "sha256": "…",
      "download_status": "ok"
    }
  ]
}
```

## 校验

项目内置抽样回源校验：

```bash
python validate_enhance.py --sample 2 --pass all
```

最近一次结果：

```text
pass2 进度: 3422/3422
pass3 进度: 3422/3422
pass4 进度: 781
抽样校验未发现不一致
```

本地结构校验结果：

```text
missing_pub_org 0
revision version_ids 4411
revision by_law_id 4411
bad revision membership 0
writ_type_contaminated 0
missing writ dspt_date 0
empty writ body 0
```

清洗派生层校验结果：

```text
raw_laws 3422
normalized_laws 3422
tables 1276
assets 274
assets_ok 125
assets_pending 0
assets_failed 149
html_in_plain 0
html_in_markdown 0
missing_full_text 0
missing_local_files 0
```

Markdown 导出结果：

```text
markdown_files 3422
manifest_count 3422
current_count 3380
other_count 42
asset_placeholders 0
literal_backslash_n 0
top_level_md 0
```

## 代码入口

| 文件 | 作用 |
| --- | --- |
| [crawl.py](/home/anjie/projects/csrc-law-crawler/crawl.py:1) | Pass 1：列表和详情正文抓取 |
| [enhance.py](/home/anjie/projects/csrc-law-crawler/enhance.py:1) | Pass 2/3/4 调度入口 |
| [parser.py](/home/anjie/projects/csrc-law-crawler/parser.py:1) | 法规 JSON 解析和全文拼接 |
| [writ_parser.py](/home/anjie/projects/csrc-law-crawler/writ_parser.py:1) | 执法文书详情页 HTML 解析 |
| [revisions_graph.py](/home/anjie/projects/csrc-law-crawler/revisions_graph.py:1) | 修订族合并和 supersedes 边生成 |
| [pass2_relations.py](/home/anjie/projects/csrc-law-crawler/pass2_relations.py:1) | 修订关系和关联法规 |
| [pass3_cases.py](/home/anjie/projects/csrc-law-crawler/pass3_cases.py:1) | 法规/条文到执法文书案例索引 |
| [pass4_writs.py](/home/anjie/projects/csrc-law-crawler/pass4_writs.py:1) | 执法文书详情补抓 |
| [validate_enhance.py](/home/anjie/projects/csrc-law-crawler/validate_enhance.py:1) | 抽样回源校验 |
| [normalize_laws.py](/home/anjie/projects/csrc-law-crawler/normalize_laws.py:1) | 法规 HTML 清洗、表格抽取、资产发现 |
| [download_assets.py](/home/anjie/projects/csrc-law-crawler/download_assets.py:1) | 图片/附件下载与资产 manifest 回写 |
| [validate_normalized.py](/home/anjie/projects/csrc-law-crawler/validate_normalized.py:1) | 清洗派生层和资产状态校验 |
| [export_markdown_laws.py](/home/anjie/projects/csrc-law-crawler/export_markdown_laws.py:1) | 将 normalized 法规导出为 Markdown |
| [neris_attachments.py](/home/anjie/projects/csrc-law-crawler/neris_attachments.py:1) | NERIS 独立附件发现和下载 |
| [coverage_gaps.py](/home/anjie/projects/csrc-law-crawler/coverage_gaps.py:1) | 正文、附件和系列缺口检测 |
| [amac_crawl.py](/home/anjie/projects/csrc-law-crawler/amac_crawl.py:1) | AMAC政策法规和全站补充采集 |
| [build_catalog.py](/home/anjie/projects/csrc-law-crawler/build_catalog.py:1) | 多来源匹配和统一法规实体生成 |
| [normalize_catalog.py](/home/anjie/projects/csrc-law-crawler/normalize_catalog.py:1) | 统一法规实体 normalized 派生层 |
| [export_markdown_catalog.py](/home/anjie/projects/csrc-law-crawler/export_markdown_catalog.py:1) | 统一法规目录 Markdown 导出 |
| [repair.py](/home/anjie/projects/csrc-law-crawler/repair.py:1) | P0-P2修复调度入口 |
| [validate_catalog.py](/home/anjie/projects/csrc-law-crawler/validate_catalog.py:1) | 多来源匹配和目录引用校验 |
| [validate_catalog_exports.py](/home/anjie/projects/csrc-law-crawler/validate_catalog_exports.py:1) | 统一目录 normalized/Markdown 覆盖校验 |
| [prefetch_revision_evidence.py](/home/anjie/projects/csrc-law-crawler/prefetch_revision_evidence.py:1) | 可断点续跑的 NERIS 修订证据缓存 |
