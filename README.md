# CSRC 法规库爬虫

从中国证监会证券期货法规数据库（NERIS）抓取法规、执法文书、修订关系和案例引用，并使用中国证券投资基金业协会（AMAC）官网补充来源，最终生成适合检索、RAG、知识图谱和归档的 JSON / Markdown 数据。

数据来源：

- [证监会证券期货法规数据库（NERIS）](https://neris.csrc.gov.cn/falvfagui/)
- [中国证券投资基金业协会（AMAC）](https://www.amac.org.cn/)

## 功能

- 抓取 NERIS 法规列表、结构化正文和元数据。
- 抓取 NERIS 执法文书正文、当事人、处罚信息和法律依据。
- 根据官方 `changeLaw` 证据生成法规修订族和 `supersedes` 关系。
- 建立“法规 / 条文 → 执法文书”的案例索引。
- 发现并下载 NERIS 独立附件及正文内嵌图片、附件。
- 抓取 AMAC 政策法规、页面正文和附件，补充 NERIS 未收录内容。
- 合并 NERIS 与 AMAC 来源，生成来源无关的统一法规目录。
- 清洗 HTML、提取表格和附件，导出纯文本及 Markdown。
- 使用 checkpoint 断点续传，并提供多层数据校验脚本。

## 运行要求

- Python 3.10+
- 可访问 NERIS 和 AMAC 官网的网络环境
- 足够的本地磁盘空间；全量正文和附件会占用较多空间

安装依赖：

```bash
git clone <repository-url>
cd csrc-law-crawler

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Windows PowerShell 激活虚拟环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

## 配置输出目录

默认输出目录在 [config.py](config.py) 中：

```python
OUTPUT_DIR = Path("/mnt/d/FUND_COMPLIANCE/CSRC")
```

首次运行前请将它改为本机可写目录，例如：

```python
OUTPUT_DIR = Path("/data/csrc-law")
```

所有脚本共享该目录。不要让两个爬虫实例同时写入同一个输出目录。

## 5 分钟试跑

先抓取少量法规，确认网络、解析和输出路径正常：

```bash
# 抓取 5 条法规；会写入 checkpoint，之后可以继续运行
python crawl.py --types regulation --limit 5

# 生成清洗后的 JSON
python normalize_laws.py --limit 5 --force

# 导出 Markdown
python export_markdown_laws.py --limit 5 --force

# 校验清洗结果
python validate_normalized.py --sample 5
```

主要结果位于：

```text
OUTPUT_DIR/
├── laws/                  # NERIS 原始法规 JSON
├── normalized/laws/       # 清洗后的法规 JSON
└── markdown/laws/         # 可直接阅读或导入 RAG 的 Markdown
```

## 使用方式

### 方案一：只构建 NERIS 法规库

适合需要法规正文、修订关系、案例和执法文书的用户。

```bash
# 1. 抓取全部法规正文，并查询独立附件列表
python crawl.py --types regulation

# 2. 预取官方修订证据，支持中断后继续
python prefetch_revision_evidence.py --workers 2

# 3. 从官方证据重建修订关系和关联法规
python enhance.py --pass 2 --rebuild-relations

# 4. 建立法规/条文到执法文书的案例索引
python enhance.py --pass 3

# 5. 下载案例实际引用的执法文书正文
python enhance.py --pass 4

# 6. 下载 NERIS 独立附件
python neris_attachments.py --workers 2
```

如果确实需要官网全部执法文书，而不只是案例索引引用的文书：

```bash
python enhance.py --pass 4 --all-writs
```

### 方案二：生成适合检索 / RAG 的数据

在“方案一”的法规抓取完成后运行：

```bash
# 1. 清洗 HTML、提取表格、发现正文内嵌资产
python normalize_laws.py --force

# 2. 下载清洗阶段发现的图片和附件
python download_assets.py

# 3. 校验清洗及资产引用
python validate_normalized.py --sample 10

# 4. 导出 Markdown
python export_markdown_laws.py --force --clean
```

推荐的数据入口：

- RAG / 全文检索：`normalized/laws/* -> full_text_plain`
- 保留表格的检索：`normalized/laws/* -> full_text_markdown`
- 人工阅读或 Markdown 索引：`markdown/laws/current/`
- 原始数据追溯：`laws/reg_*.json`

原始 `laws/` 不会被清洗脚本覆盖。

### 方案三：构建 NERIS + AMAC 统一法规目录

先完成 NERIS 法规抓取，再运行多来源流水线：

```bash
# P0：可信修订关系、NERIS 独立附件和覆盖缺口
# P1：AMAC 页面及附件补充
# P2：来源匹配、统一实体、清洗、Markdown 和校验
python repair.py --phase all --delay-min 1.8 --delay-max 3.6
```

`repair.py` 不包含首次 NERIS 法规正文抓取。空目录开始时，必须先运行：

```bash
python crawl.py --types regulation
```

也可以分阶段执行：

```bash
python repair.py --phase p0 --delay-min 1.8 --delay-max 3.6
python repair.py --phase p1 --delay-min 1.8 --delay-max 3.6
python repair.py --phase p2
```

统一目录结果：

```text
OUTPUT_DIR/catalog/
├── laws/                  # 来源无关的统一法规实体
├── normalized/laws/       # 统一目录清洗结果
├── markdown/laws/         # 统一目录 Markdown
├── manifest.json
└── review_queue.json      # 歧义匹配或效力状态待复核项
```

## 常用命令

| 任务 | 命令 |
| --- | --- |
| 抓取法规 | `python crawl.py --types regulation` |
| 抓取官网全部文书 | `python crawl.py --types writ` |
| 限量调试 | `python crawl.py --types regulation --limit 10` |
| 跳过附件列表查询 | `python crawl.py --types regulation --skip-attachments` |
| 运行全部增强阶段 | `python enhance.py --pass all` |
| 重建修订关系 | `python enhance.py --pass 2 --rebuild-relations` |
| 只抓案例引用文书 | `python enhance.py --pass 4` |
| 抓全部执法文书 | `python enhance.py --pass 4 --all-writs` |
| 抓取 AMAC 补充来源 | `python amac_crawl.py` |
| 生成统一目录 | `python build_catalog.py` |
| 清洗统一目录 | `python normalize_catalog.py --force --clean` |
| 导出统一目录 Markdown | `python export_markdown_catalog.py --force --clean` |

查看任意入口的完整参数：

```bash
python crawl.py --help
python enhance.py --help
python repair.py --help
```

## 校验

运行单元测试和语法检查：

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

校验不同数据层：

```bash
# 抽样回源校验修订、案例和文书
python validate_enhance.py --sample 2 --pass all

# 校验 NERIS 清洗、资产和 Markdown 前置数据
python validate_normalized.py --sample 10

# 校验多来源匹配和统一目录引用
python validate_catalog.py

# 校验统一目录 normalized / Markdown 覆盖率
python validate_catalog_exports.py
```

后两个校验脚本会直接执行校验，不提供 `--help` 参数。

## 运行安全与注意事项

1. 温和访问源站

   默认单请求间隔为 1.8–3.6 秒，每 40 次请求额外暂停 8–15 秒。全量任务耗时较长是正常现象。不要并行启动多个全量实例。

2. 不要组合 `--rebuild-relations` 和 `--limit`

   `--rebuild-relations` 会清理旧修订引用并覆盖 `relations/revisions.json`。如果同时限制为前 N 条，只会留下局部关系图。`repair.py --phase p0 --law-limit N` 具有同样风险，应仅在独立测试输出目录使用。

3. 谨慎使用 `--clean`

   `normalize_catalog.py --clean`、`export_markdown_catalog.py --clean` 和 `export_markdown_laws.py --clean` 会清空对应派生目录后重建。它们不会删除原始 `laws/`，但不应与其他写入任务并行运行。

4. 限量资产下载会生成限量清单

   `download_assets.py --limit-laws` 或 `--limit-assets` 会使用本轮扫描结果重写全局资产 manifest。调试时建议使用独立输出目录，完成后再运行一次不带 limit 的命令。

5. 附件失败不一定是本地错误

   部分 NERIS 附件接口会返回 HTTP 200 但正文为空；相关项会记录为失败并进入缺口清单。

6. 数据不是法律意见

   本项目保存官方来源和抓取时间，仍应在正式使用前回到官方页面核验时效性、完整性和效力状态。

## 输出目录

```text
OUTPUT_DIR/
├── laws/
│   └── reg_{secFutrsLawId}.json
├── writs/
│   └── writ_{lawWritId}.json
├── relations/
│   ├── revisions.json
│   ├── revision_evidence_cache/
│   ├── related_laws.json
│   ├── cases.json
│   ├── coverage_gaps.json
│   ├── source_matches.json
│   └── catalog_relations.json
├── sources/
│   ├── amac/
│   └── amac_manifest.json
├── normalized/
│   ├── laws/
│   └── manifest.json
├── assets/
│   ├── laws/
│   ├── neris_attachments/
│   ├── amac/
│   ├── assets_manifest.json
│   └── assets_failures.json
├── markdown/
│   ├── laws/current/
│   ├── laws/other/
│   └── manifest.json
├── catalog/
│   ├── laws/
│   ├── normalized/laws/
│   ├── markdown/laws/
│   ├── manifest.json
│   └── review_queue.json
├── checkpoint.json
└── manifest.json
```

## 数据模型

### 法规

`laws/reg_{id}.json` 保存：

- `metadata`：名称、文号、发布单位、发布日期、生效日期、效力状态等。
- `entries`：章节、条文和子项。
- `full_text`：按法规结构拼接的原始全文。
- `source`：列表摘要、详情页 URL 和抓取时间。
- `source_attachments`：NERIS 独立附件。
- `revision_ref`：指向法规修订族。

### 执法文书

`writs/writ_{id}.json` 保存：

- `metadata`：文书名、发文机关、日期、类型和原文链接。
- `body`：文书正文。
- `legal_basis`：引用法规及条文。
- `parties`：当事人、角色、违法类型和处罚金额。

### 修订与案例关系

- `relations/revisions.json`：官方修订族、版本节点和 `supersedes` 边。
- `relations/related_laws.json`：NERIS 返回的关联法规。
- `relations/cases.json`：法规级和条文级案例，以及需要抓取的文书 ID。
- `relations/catalog_relations.json`：统一目录中的发布、附件等跨实体关系。

### 清洗法规

`normalized/laws/reg_{id}.json` 主要增加：

- `full_text_plain`：去除 HTML 后的检索文本。
- `full_text_markdown`：保留表格和资产占位的 Markdown。
- `tables`：结构化表格及 Markdown 表格。
- `assets`：图片、附件 URL、本地路径、哈希和下载状态。

## 当前数据快照

最近校验日期：2026-06-24。该表是一次本地全量运行结果，不代表源站数据永久不变。

| 数据 | 数量 |
| --- | ---: |
| NERIS 法规 | 3422 |
| NERIS 官网执法文书列表 | 3249 |
| 已抓取案例引用文书 | 781 |
| NERIS 独立附件记录 | 1313 |
| AMAC 页面及附件来源记录 | 664 |
| 统一法规实体 | 3852 |
| 统一目录 Markdown | 3852 |
| 修订族 | 3311 |
| 有官方证据的修订边 | 1086 |
| 关联法规边 | 946 |

默认 Pass 4 只抓取 `relations/cases.json` 引用的文书，因此本地文书数通常少于官网文书总数。

## 代码入口

| 文件 | 作用 |
| --- | --- |
| [crawl.py](crawl.py) | NERIS 法规和文书基础抓取 |
| [enhance.py](enhance.py) | 修订、案例、文书增强阶段调度 |
| [repair.py](repair.py) | P0–P2 多来源流水线调度 |
| [client.py](client.py) | HTTP 限速、重试和 WAF 检测 |
| [parser.py](parser.py) | NERIS 法规 JSON 解析 |
| [writ_parser.py](writ_parser.py) | 执法文书 HTML 解析 |
| [normalize_laws.py](normalize_laws.py) | 法规 HTML 清洗、表格和资产抽取 |
| [download_assets.py](download_assets.py) | 正文内嵌资产下载 |
| [neris_attachments.py](neris_attachments.py) | NERIS 独立附件发现和下载 |
| [amac_crawl.py](amac_crawl.py) | AMAC 来源和附件抓取 |
| [build_catalog.py](build_catalog.py) | 多来源匹配和统一法规实体生成 |
| [normalize_catalog.py](normalize_catalog.py) | 统一目录内容清洗 |
| [export_markdown_laws.py](export_markdown_laws.py) | NERIS 法规 Markdown 导出 |
| [export_markdown_catalog.py](export_markdown_catalog.py) | 统一目录 Markdown 导出 |
| [coverage_gaps.py](coverage_gaps.py) | 正文、附件和系列覆盖缺口检测 |

## 已知限制

- NERIS 原始正文包含源站 HTML；检索和 RAG 应优先使用 normalized 层。
- 修订方向根据官方修订组内的版本号顺序推导；版本号缺失或相同不会生成方向边。
- 自动来源匹配主要依据规范化标题、文号和发布日期，歧义项会进入 `catalog/review_queue.json`。
- PDF、DOCX 等附件的自动抽取受文件格式、扫描质量和源站文件完整性影响。
- `manifest.json` 主要覆盖基础法规；执法文书范围以 `relations/cases.json` 和 `writs/` 为准。
