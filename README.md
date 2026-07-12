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
- 支持 AMAC 专项抓取：自律管理、行业研究栏目，并可只下载页面显式链接的 PDF。
- 合并 NERIS 与 AMAC 来源，生成来源无关的统一法规目录。
- 按来源和标题证据归一化效力状态，并推断正式版替代试行版。
- 为分类、匹配、效力、关系和人工核验规则输出稳定 `rule_id` 和规则 manifest。
- 清洗 HTML、提取表格和附件，导出纯文本及 Markdown。
- 使用 checkpoint 断点续传，并提供多层数据校验脚本。

## 工程结构

项目保留原有脚本入口，便于直接运行既有流水线；同时提供 `csrc_law_crawler/` 包作为新的稳定 import surface，后续模块迁移优先放入包命名空间。当前主要包入口包括：

```text
csrc_law_crawler.core       # Settings、RunContext、FileStore、HTTP policy、models
csrc_law_crawler.sources    # NERIS / AMAC source adapter exports
csrc_law_crawler.processing # catalog / relation processing helpers
csrc_law_crawler.export     # Markdown export helpers
```

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

默认输出目录为当前工作目录下的 `csrc-output/`。生产或全量运行不要改源码里的路径；请显式使用环境变量、配置文件或全局 CLI 参数指定输出目录：

```bash
export CSRC_OUTPUT_ROOT=/data/csrc-law
```

PowerShell：

```powershell
$env:CSRC_OUTPUT_ROOT = "D:\FUND_COMPLIANCE\CSRC"
```

也可以写一个 JSON 配置文件，并通过 `CSRC_CONFIG_FILE` 或 `--config` 指定：

```json
{
  "output_root": "/data/csrc-law",
  "max_download_bytes": 104857600,
  "amac_verify_tls": true,
  "delay_min": 1.8,
  "delay_max": 3.6,
  "max_retries": 5,
  "retry_backoff_base": 5.0,
  "workers": 1
}
```

也可以对单次运行使用全局 CLI 参数，写入型脚本都会识别并在正式解析脚本参数前剥离：

```bash
python crawl.py --output-root /data/csrc-law --types regulation --limit 5
python repair.py --config ./csrc_crawler_config.json --phase p2
python download_assets.py --max-download-bytes 104857600
python amac_crawl.py --delay-min 0.25 --delay-max 0.7 --max-retries 5
```

可选安全配置：

```bash
# 单个附件/二进制响应的最大读取字节数，默认 100 MiB
export CSRC_MAX_DOWNLOAD_BYTES=104857600

# 默认校验 AMAC HTTPS 证书；仅在排查证书环境问题时临时关闭
export CSRC_AMAC_VERIFY_TLS=true

# 请求节奏与重试策略也可以由环境变量、配置文件或全局 CLI 参数覆盖
export CSRC_DELAY_MIN=1.8
export CSRC_DELAY_MAX=3.6
export CSRC_MAX_RETRIES=5
export CSRC_RETRY_BACKOFF_BASE=5.0
export CSRC_WORKERS=1
```

所有写入型脚本会获取输出目录锁。不要并行启动多个全量实例；如果误启动，后启动的进程会直接失败而不是和已有进程交错写入。

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
├── raw/neris/laws/        # NERIS 来源层法规
├── work/normalized_neris/ # NERIS 中间清洗 JSON
└── work/markdown_neris/   # NERIS 中间 Markdown
```

写入型脚本还会为每次运行生成：

```text
OUTPUT_DIR/reports/runs/{run_id}/
├── run_manifest.json
├── events.jsonl
├── failures.jsonl
└── metrics.json
```

## 使用方式

### 方案一：只构建 NERIS 法规库

适合需要法规正文、修订关系、案例和执法文书的用户。

```bash
# 1. 抓取全部法规正文，并查询独立附件列表
python crawl.py --types regulation

# 2. 预取官方修订证据，支持中断后继续
python prefetch_revision_evidence.py --workers 1

# 3. 从官方证据重建修订关系和关联法规
python enhance.py --pass 2 --rebuild-relations

# 4. 建立法规/条文到执法文书的案例索引
python enhance.py --pass 3

# 5. 下载案例实际引用的执法文书正文
python enhance.py --pass 4

# 6. 下载 NERIS 独立附件
python neris_attachments.py --workers 1
```

`crawl.py` 使用同一套 `PipelineStep` / `StepResult` 编排。`--types all` 会按“法规、执法文书”生成两个阶段；默认任一类型抓取出现局部失败就停止，并写入 `reports/crawl_step_results.json` 和对应 failure report。只有显式加 `--allow-incomplete` 时才会继续后续类型。

如果确实需要官网全部执法文书，而不只是案例索引引用的文书：

```bash
python enhance.py --pass 4 --all-writs
```

`enhance.py` 同样使用统一 `PipelineStep` / `StepResult` 编排。默认任一 pass 返回 `incomplete` 或 `failed` 就停止，并写入 `reports/enhance_step_results.json`；只有显式加 `--allow-incomplete` 时才会继续执行后续 pass。

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

这一方案生成的是 NERIS 中间层，便于单源检查。需要唯一正式消费入口时，继续执行“方案三”的 P2。

正式统一目录的数据入口：

- RAG / 全文检索：`canonical/json/* -> full_text_plain`
- 保留表格的检索：`canonical/json/* -> full_text_markdown`
- 人工阅读：`canonical/markdown/{current,unknown,historical,reference}/`
- 唯一正式关系图：`canonical/relations/graph.json`
- 关系图人工巡检：`reports/relation_viewer/index.html`
- 原始数据追溯：`raw/neris/laws/`、`raw/amac/records/`
- 效力规则说明：[规则说明.md](规则说明.md)

`raw/` 不写入修订引用等派生字段；附件扫描状态独立存放。

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

`repair.py` 使用统一 `PipelineStep` / `StepResult` 编排。默认任一阶段返回 `incomplete` 或 `failed` 就停止后续阶段，并写入 `reports/repair_step_results.json`。只有显式加 `--allow-incomplete` 时才会继续执行下游阶段，最终结果仍会标记为 `incomplete`，用于调试和取证，不建议作为正式发布输入。

正式输出：

```text
OUTPUT_DIR/canonical/
├── json/                  # 唯一 normalized JSON
├── markdown/
│   ├── current/           # 现行有效，含 AMAC 正式制度缺省有效
│   ├── unknown/           # 证据不足，待核验
│   ├── historical/        # 失效、废止、被修改、已被替代
│   └── reference/         # 征求意见稿、动态、说明、模板、辅助材料
├── relations/graph.json   # 唯一正式关系图
├── indexes/source_map.json
└── manifest.json
```

### AMAC 专项抓取

AMAC 抓取默认保持原有政策法规补充行为。需要抓取专项栏目时使用显式参数，仍保持单 worker、随机延迟和 checkpoint 续跑，不做目录枚举或 URL 猜测。

自律管理全量正文和 PDF：

```bash
python -m csrc_law_crawler.cli.main amac-crawl \
  --output-root /mnt/d/amac \
  --only-self-regulatory-management \
  --download-pdf-assets \
  --delay-min 2.5 \
  --delay-max 5.0
```

行业研究全量正文和 PDF：

```bash
python -m csrc_law_crawler.cli.main amac-crawl \
  --output-root /mnt/d/amac \
  --only-industry-research \
  --download-pdf-assets \
  --delay-min 2.5 \
  --delay-max 5.0
```

行业研究目前覆盖 `https://www.amac.org.cn/hyyj/` 下的 `研究报告`、`声音`、`ESG研究` 三个子栏目。记录写入 `raw/amac/records/`，PDF 写入 `raw/assets/amac/<source_record_id>/`。PDF 文件名使用 `<清洗后的PDF标题> - <发布日期>.pdf`；如果标题来自列表文本，不使用源站 `P020...pdf` 编号作为可读文件名。记录元数据保留 `source_category/source_section` 主分类，并在同一文件被多个栏目发现时写入 `source_categories/source_sections`。

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
| 抓取 AMAC 自律管理 PDF | `python -m csrc_law_crawler.cli.main amac-crawl --output-root /mnt/d/amac --only-self-regulatory-management --download-pdf-assets` |
| 抓取 AMAC 行业研究 PDF | `python -m csrc_law_crawler.cli.main amac-crawl --output-root /mnt/d/amac --only-industry-research --download-pdf-assets` |
| 生成统一目录 | `python build_catalog.py` |
| 清洗统一目录 | `python normalize_catalog.py --force --clean` |
| 导出统一目录 Markdown | `python export_markdown_catalog.py --force --clean` |
| 导出关系图查看器 | `python relation_viewer.py` |

查看任意入口的完整参数：

```bash
python crawl.py --help
python enhance.py --help
python amac_crawl.py --help
python repair.py --help
```

安装为包后也可以使用统一入口；旧脚本入口保持兼容：

```bash
csrc-crawler crawl --types regulation
csrc-crawler enhance --pass 2 --rebuild-relations
csrc-crawler repair --phase p2
csrc-crawler validate-catalog-exports
```

## 校验

运行测试和语法检查：

```bash
python -m pytest -q
python -m compileall -q .
```

安装开发依赖后，本地可运行与 CI 一致的质量检查：

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m mypy *.py tests
python -m coverage run -m pytest -q
python -m coverage report
python -m pip_audit -r requirements.txt --progress-spinner off
```

CI 配置位于 `.github/workflows/ci.yml`，会运行 pytest、语法检查、ruff、mypy、coverage 和依赖扫描；依赖扫描当前作为告警项，不阻断普通代码变更。

关键 JSON 产物的 schema 快照位于 `schemas/*.schema.json`，对应的运行时契约在 `models.py`。`validate_catalog.py`、`validate_normalized.py`、`validate_catalog_exports.py` 会在原有业务校验前执行基础结构校验。

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

   写入型脚本会使用输出目录锁保护 `raw/`、`work/`、`canonical/` 和 `reports/`；锁文件位于 `CSRC_OUTPUT_ROOT/.csrc-law-crawler.lock`。

   写入型 CLI 和校验 CLI 运行都会在 `reports/runs/` 下生成 run manifest、事件、失败和指标文件，便于自动化调度或事后排查。

   多阶段流水线还会写出 `reports/crawl_step_results.json`、`reports/repair_step_results.json` 或 `reports/enhance_step_results.json`，其中每个阶段都有 `complete / incomplete / failed` 状态；下游默认拒绝消费非 complete 阶段，除非运行时显式指定 `--allow-incomplete`。

2. 修订关系采用事务式发布

   `--rebuild-relations` 与 `--limit` 的组合会被直接拒绝。任务中存在任一失败时，旧正式图保持不变，checkpoint 标记为 `incomplete`。

3. 谨慎使用 `--clean`

   `normalize_catalog.py --clean` 和 `export_markdown_catalog.py --clean` 会清空对应 canonical 派生目录后重建。它们不会删除 `raw/`，但不应与其他写入任务并行运行。

4. 限量资产下载不会覆盖全局清单

   `download_assets.py --limit-laws` 或 `--limit-assets` 的结果写入 `work/runs/`；只有全量扫描可以更新 `reports/assets_manifest.json`。

5. 附件失败不一定是本地错误

   HTTP 200 空附件、HTML 错误页和超出 `CSRC_MAX_DOWNLOAD_BYTES` 的响应不会落盘为有效附件；达到上限后才记录为源站内容失败。

6. AMAC TLS 策略

   AMAC 请求默认校验 HTTPS 证书。仅在明确知道证书环境异常时使用 `python amac_crawl.py --amac-insecure-tls` 临时关闭；该策略会写入 AMAC manifest。

7. 数据不是法律意见

   本项目保存官方来源和抓取时间，仍应在正式使用前回到官方页面核验时效性、完整性和效力状态。

## 输出目录

```text
OUTPUT_DIR/
├── raw/
│   ├── neris/
│   │   ├── laws/
│   │   ├── writs/
│   │   ├── attachment_index/
│   │   ├── revision_evidence/
│   │   └── manifest.json
│   ├── amac/
│   │   ├── records/
│   │   └── manifest.json
│   └── assets/
│       ├── embedded/
│       ├── neris_attachments/
│       └── amac/
├── canonical/
│   ├── json/
│   ├── markdown/{current,unknown,historical,reference}/
│   ├── relations/graph.json
│   ├── indexes/source_map.json
│   └── manifest.json
├── work/
│   ├── catalog/
│   ├── normalized_neris/
│   ├── relations/
│   ├── checkpoints/
│   └── runs/
└── reports/
    ├── coverage_gaps.json
    ├── assets_manifest.json
    ├── assets_failures.json
    ├── relation_viewer/index.html
    └── review_queue.json
```

## 数据模型

### 法规

`raw/neris/laws/reg_{id}.json` 保存：

- `metadata`：名称、文号、发布单位、发布日期、生效日期、效力状态等。
- `entries`：章节、条文和子项。
- `full_text`：按法规结构拼接的原始全文。
- `source`：列表摘要、详情页 URL 和抓取时间。
- 原始法规中不写入 `revision_ref` 或附件扫描运行状态。
- 独立附件查询结果位于 `raw/neris/attachment_index/{id}.json`。

### 执法文书

`raw/neris/writs/writ_{id}.json` 保存：

- `metadata`：文书名、发文机关、日期、类型和原文链接。
- `body`：文书正文。
- `legal_basis`：引用法规及条文。
- `parties`：当事人、角色、违法类型和处罚金额。

### 正式关系图

`canonical/relations/graph.json` 合并：

- `supersedes`：官方修订关系，以及正式版替代同题名试行版的目录推断关系。
- `related_to`：NERIS 关联法规。
- `cited_by_case`：法规/条文到执法文书。
- `publishes`：公告到正式附件文件。

组件级关系只存在于 `work/relations/`，不作为正式消费入口。

需要人工巡检关系质量时，可以运行 `python relation_viewer.py`，从正式关系图导出本地静态查看器到 `reports/relation_viewer/index.html`。查看器只消费 `canonical/relations/graph.json`、`canonical/json/` 和 `canonical/indexes/source_map.json`，不改变正式关系图。

### 清洗法规

`canonical/json/law_{canonical_id}.json` 主要包含：

- `full_text_plain`：去除 HTML 后的检索文本。
- `full_text_markdown`：保留表格和资产占位的 Markdown。
- `tables`：结构化表格及 Markdown 表格。
- `assets`：图片、附件 URL、本地路径、哈希和下载状态；canonical 层按 `sha256` 合并重复资产，并通过 `source_urls`、`local_files`、`source_records` 保留多来源证据。

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
| Markdown：current / unknown / historical / reference | 3427 / 140 / 49 / 236 |
| 正式关系图节点 / 边 | 5749 / 4994 |
| `supersedes` / `related_to` / `cited_by_case` / `publishes` | 1086 / 777 / 2989 / 142 |

默认 Pass 4 只抓取 `work/relations/cases.json` 引用的文书，因此本地文书数通常少于官网文书总数。

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
| [build_canonical_relations.py](build_canonical_relations.py) | 合并唯一正式关系图 |
| [relation_viewer.py](relation_viewer.py) | 导出关系图本地静态查看器 |
| [migrate_strict_layout.py](migrate_strict_layout.py) | 严格目录迁移和旧派生清理 |
| [coverage_gaps.py](coverage_gaps.py) | 正文、附件和系列覆盖缺口检测 |

## 已知限制

- NERIS 来源正文包含源站 HTML；检索和 RAG 应使用 `canonical/`。
- 修订方向根据官方修订组内的版本号顺序推导；版本号缺失或相同不会生成方向边。
- 试行版替代关系根据标题、发布日期和发布机构推断，正式使用前仍应人工核验。
- 自动来源匹配主要依据规范化标题、文号和发布日期，歧义项会进入 `reports/review_queue.json`。
- PDF、DOCX 等附件的自动抽取受文件格式、扫描质量和源站文件完整性影响。
- 正式消费入口仅为 `canonical/`；`work/` 内容可以随时由 `raw/` 重建。
