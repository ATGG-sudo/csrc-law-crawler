# CSRC 法规库爬虫项目代码审计、功能评估与架构改造方案

## 审计范围

源码包：`csrc-law-crawler-fix-multisource-law-catalog-b796d0c.zip`

本次审计覆盖：

- 项目结构、入口脚本、依赖、配置与输出目录约定。
- NERIS / AMAC 抓取、解析、增强、归一化、关系图、资产下载、Markdown 导出、校验脚本。
- 单元测试与语法检查。
- 架构风险、代码质量风险、运行安全风险、可观测性缺口。
- 面向模块化、可复用性、可观测性的改造方案。

已执行检查：

```bash
python -m unittest discover -s tests -v   # 29 个用例通过
python -m pytest -q                      # 31 个用例通过
python -m compileall -q .                 # 语法检查通过
```

说明：未对真实源站发起爬取，也未做实时 CVE / 依赖漏洞查询；本报告基于静态源码与本地测试运行结果。

## 项目概览

项目是一个单层脚本式 Python 工程：

- 31 个生产 `.py` 文件，约 7,934 行生产代码。
- 2 个测试文件，约 628 行测试代码。
- 无 `pyproject.toml` / 包目录 / CI 配置；依赖仅在 `requirements.txt` 中声明。
- 主要输出分层为 `raw/`、`work/`、`canonical/`、`reports/`，这是项目当前最清晰的架构资产。

核心数据流：

```text
NERIS / AMAC 源站
  -> 抓取客户端与 API 封装
  -> raw 源文件、附件、manifest、checkpoint
  -> work 中间清洗、修订证据、案例索引
  -> canonical 统一法规实体、normalized JSON、Markdown、正式关系图
  -> reports 校验、覆盖缺口、失败与人工复核队列
```

## 功能评估

| 模块 | 当前成熟度 | 评价 |
| --- | --- | --- |
| NERIS 法规抓取 | 较成熟 | 支持列表分页、详情抓取、断点续传、manifest、附件索引查询。 |
| NERIS 执法文书 | 中等 | 能解析服务端渲染 HTML，提取正文、当事人、法律依据；解析依赖页面结构，需更多 fixture。 |
| 修订关系 Pass 2 | 较成熟 | 使用官方 `changeLaw` 证据，失败时保持正式关系图不被局部覆盖，是较好的事务边界。 |
| 案例索引 Pass 3 | 中等 | 能建立法规 / 条文到执法文书索引；失败只记录不阻断，状态语义比 Pass 2 弱。 |
| 文书下载 Pass 4 | 中等 | 支持案例引用文书与全量文书；失败语义和进度状态需统一。 |
| NERIS 附件 | 中等 | 支持发现、下载、哈希、状态；并发可配置，但无全局限速协调。 |
| AMAC 补充来源 | 有价值但启发式较强 | 多通道发现、附件下载、正文抽取；规则依赖标题关键词和页面结构，需要更强可配置性。 |
| 统一目录与效力归一化 | 有业务价值 | 来源匹配、试行替代、效力规则、review queue 都较实用；但规则散落在脚本中，缺少可审计规则配置。 |
| Markdown / RAG 输出 | 较完整 | 能导出 canonical JSON / Markdown，保留来源、资产和关系引用。 |
| 校验体系 | 有基础 | 多个校验脚本覆盖文件数、资产、关系、导出；缺少统一测试策略、CI、覆盖率和回归数据集。 |
| 可观测性 | 较弱 | 主要依赖 `print`、manifest、checkpoint；缺少结构化日志、指标、统一失败分类和 run 级追踪。 |

总体判断：项目已经具备较完整的“法规数据生产流水线”能力，数据分层和证据保留意识较好；主要问题不在功能缺失，而在脚本式实现已经接近复杂度上限，后续扩源、扩规则、稳定运行和问题定位会越来越困难。

## 主要代码审计发现

### 优点

1. **数据分层清晰**：`raw/` 保存源数据，`work/` 保存中间结果，`canonical/` 作为正式消费入口，`reports/` 保存问题报告。这个边界应保留。
2. **JSON 写入具备基本原子性**：`storage.save_json()` 采用临时文件后替换，`publish_json_bundle()` 对关系图发布做了回滚保护。
3. **关键业务规则已有测试**：修订关系、试行替代、AMAC 缺省有效、正文 reflow、附件合并、空附件重试等都有测试。
4. **数据血缘意识较好**：多数字段保留 `source`、`crawled_at`、`evidence`、`confidence`、`local_file` 等可追溯信息。
5. **限速与断点续传已存在**：对源站访问较克制，并有 checkpoint / manifest 支持恢复。

### 高风险问题

| 风险 | 位置示例 | 影响 | 建议 |
| --- | --- | --- | --- |
| 硬编码绝对输出目录 | `config.py` 中 `OUTPUT_DIR = Path("/mnt/d/FUND_COMPLIANCE/CSRC")` | 部署、测试、多环境运行困难；误写本地路径风险高。 | 引入 `Settings`，支持环境变量、CLI 参数、配置文件；禁止业务模块直接 import 全局路径。 |
| import-time 路径常量 | `download_assets.py`、`amac_crawl.py`、`export_markdown_*`、`build_canonical_relations.py` 等 | 测试 patch 配置时不生效；多实例 / 多输出目录复用困难。 | 将所有路径由 `RunContext` / `FileStore` 动态计算。 |
| AMAC 客户端对 `fg.amac.org.cn` 禁用 TLS 校验 | `amac_crawl.py` 的 `verify = not ...` | 存在中间人篡改官方规则、附件和正文的完整性风险。 | 改为可配置证书策略；默认不禁用；必要时固定 CA bundle，并在 manifest 中记录 TLS 策略。 |
| 无跨进程锁 | 多脚本均可写同一 `OUTPUT_DIR`，README 仅提示不要并发 | `--clean`、manifest、checkpoint、normalized 输出可能相互覆盖。 | 引入 `FileLock` / 运行锁；清理类操作必须拿排他锁。 |
| 全量响应读入内存、无大小上限 | `client.get_binary()`、`download_assets._download()`、`amac_crawl._download_asset()` | 大附件、异常响应、压缩炸弹可能导致内存 / 磁盘耗尽。 | 使用 streaming download、最大字节数、Content-Length 检查、临时文件、哈希边下载边计算。 |
| 大函数承担多重职责 | `build_catalog()`、`build_normalized_law()`、`build_canonical_relations()`、`run_pass2()` | 难测试、难复用、改动容易破坏隐含流程。 | 分拆为 SourceLoader、Matcher、EntityWriter、RelationIngestor、Validator 等服务。 |
| 失败语义不统一 | Pass 2 失败会阻断并保留旧图，Pass 3/4 多为记录后继续 | 自动化运行难判断产物是否可信。 | 所有阶段统一 `StepResult`：`complete / incomplete / failed`，并输出机器可读失败分类。 |
| 可观测性依赖 `print` | 全项目约 130 处 `print()` | 问题定位、汇总、监控、告警困难。 | 结构化日志 + 指标 + run manifest + failure jsonl。 |

### 中风险问题

1. **HTTP 策略重复实现**：`client.py`、`download_assets.py`、`amac_crawl.py` 各自实现 session、暂停、下载、重试或部分重试，策略容易漂移。
2. **数据模型全部是自由 `dict[str, Any]`**：字段拼写、schema 迁移、兼容性主要靠约定，缺少运行时校验。
3. **业务规则散落**：标题规范化、效力状态、试行替代、AMAC 文档分类、资产判断分布在多个脚本中，难以审计规则变更。
4. **测试入口不一致**：README 推荐 `unittest discover`，但 `tests/test_neris_encoding_repair.py` 是 pytest 风格函数；`unittest` 只跑到 29 个，`pytest` 才跑到 31 个。
5. **模块间复用 private helper**：如 `export_markdown_catalog.py` 直接从 `export_markdown_laws.py` import `_assets_section`、`_filename_stem` 等私有函数，说明还缺少共享 exporter 工具层。
6. **规则置信度缺少集中校准**：例如标题 + 日期 ±3 天匹配、AMAC 未标状态默认 current、试行替代 confidence 0.86 等，应集中成可配置规则，并输出更多复核样本。
7. **重复全目录扫描**：多阶段通过 glob 全量读取 JSON，对当前规模可接受，但后续扩源和增量更新会变慢。

## 架构风险评估

### 当前架构形态

当前项目接近“脚本编排 + 文件仓库 + 大函数处理器”：

```text
CLI 脚本
  -> 直接 import config / storage / client / parser
  -> 函数内完成请求、解析、转换、写文件、打印日志、更新 checkpoint
  -> 下游脚本再扫目录读取上游产物
```

这种形态适合原型和一次性批处理，但当需求变为“多来源、可复用、可观测、可回归”时，会出现以下风险：

1. **模块边界不稳定**：脚本既是 CLI 又是库函数，参数、全局路径、输出副作用混在一起。
2. **替换成本高**：更换存储、增加来源、增加任务调度器或接入服务化 API，都需要修改很多文件。
3. **测试成本上升**：没有稳定的 public API，只能测试私有函数或 patch 全局变量。
4. **运行状态不可组合**：checkpoint、manifest、reports 各自存在，但没有统一 run id、stage id、指标口径。
5. **数据质量规则难治理**：规则写在代码里，缺少 rule id、版本、解释、人工复核闭环。

## 目标架构

建议采用“分层模块 + 端口适配器 + 显式流水线上下文”的架构。

```text
csrc_law_crawler/
  core/
    settings.py          # 配置、环境变量、CLI override
    context.py           # RunContext: run_id/output_root/logger/metrics/store
    logging.py           # JSON log + console log
    metrics.py           # counters/timers/failure taxonomy
    http.py              # 通用 HTTP client、retry、rate limit、stream download
    storage.py           # FileStore、atomic write、locks、json/jsonl
    models/              # Law, Writ, Asset, SourceRecord, Relation, Manifest

  sources/
    neris/
      client.py          # NERIS API adapter
      parser.py          # 法规 / 文书解析
      pipeline.py        # crawl laws, crawl writs, attachments
    amac/
      client.py          # AMAC adapter
      discovery.py       # policy/site/xwfb discovery
      parser.py          # 页面与附件解析

  processing/
    normalize/
      html.py            # HTML -> plain/markdown/table/asset refs
      text.py            # PDF/DOCX/TXT text cleanup/reflow
    catalog/
      matching.py        # source matching scoring
      effectiveness.py   # 效力判定 rule engine
      entities.py        # canonical entity builder
    relations/
      revisions.py       # revision family builder
      cases.py           # case index builder
      graph.py           # canonical graph builder

  orchestration/
    pipeline.py          # Stage DAG、resume、preconditions、postconditions
    checkpoint.py        # stage-level checkpoint
    validation.py        # shared validation contracts

  export/
    markdown.py
    jsonl.py

  cli/
    main.py              # 单一 CLI，如 csrc-crawler crawl/repair/validate/export
```

核心设计原则：

- **业务纯函数化**：解析、匹配、效力判断、关系推断尽量不读写文件、不访问网络。
- **副作用集中**：网络在 SourceClient，文件在 FileStore，日志指标在 RunContext。
- **数据模型显式化**：用 dataclass / Pydantic / JSON Schema 管理字段、版本和兼容性。
- **产物可复现**：每个 run 记录配置、代码版本、输入范围、源站策略、指标和失败列表。
- **旧 CLI 兼容**：保留现有脚本作为 thin wrapper，内部调用新模块，逐步迁移。

## 可复用接口建议

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Iterable

@dataclass(frozen=True)
class Settings:
    output_root: Path
    delay_min: float
    delay_max: float
    max_retries: int
    max_download_bytes: int
    user_agent: str

@dataclass(frozen=True)
class RunContext:
    run_id: str
    settings: Settings
    store: "FileStore"
    logger: "StructuredLogger"
    metrics: "MetricsRecorder"

class SourceClient(Protocol):
    source_name: str
    def list_records(self, *, limit: int | None = None) -> Iterable["SourceSummary"]: ...
    def fetch_record(self, record_id: str) -> "SourceRecord": ...
    def fetch_assets(self, record_id: str) -> list["AssetRef"]: ...

class PipelineStep(Protocol):
    name: str
    def run(self, ctx: RunContext) -> "StepResult": ...
    def validate(self, ctx: RunContext) -> "ValidationResult": ...
```

阶段执行统一返回：

```python
@dataclass
class StepResult:
    stage: str
    status: str        # complete / incomplete / failed
    seen: int
    written: int
    skipped: int
    failed: int
    output_files: list[str]
    failure_file: str | None
```

## 可观测性改造方案

### 日志

将 `print()` 替换为结构化日志。建议同时输出：

- 控制台：简洁 human-readable。
- `work/logs/{run_id}.jsonl`：机器可读 JSON lines。

每条日志至少包含：

```json
{
  "ts": "2026-06-30T...Z",
  "level": "INFO",
  "run_id": "20260630_...",
  "stage": "neris.crawl_laws",
  "source": "neris",
  "record_id": "...",
  "event": "record_saved",
  "duration_ms": 1234,
  "attempt": 1
}
```

### 指标

建议统一记录：

| 指标 | 说明 |
| --- | --- |
| `http_requests_total{source,method,status}` | 请求数。 |
| `http_retries_total{source,reason}` | 重试数。 |
| `http_request_duration_ms` | 请求耗时。 |
| `download_bytes_total{source}` | 下载字节数。 |
| `records_seen_total{stage}` | 扫描记录数。 |
| `records_written_total{stage}` | 写入记录数。 |
| `records_failed_total{stage,reason}` | 失败记录数。 |
| `parse_failures_total{parser,reason}` | 解析失败数。 |
| `validation_issues_total{validator,severity}` | 校验问题数。 |
| `assets_pending / assets_ok / assets_failed` | 资产状态。 |

本地 CLI 可先写入 `reports/runs/{run_id}/metrics.json`，后续需要服务化时再接 Prometheus / OpenTelemetry。

### 失败分类

将当前自由文本错误拆成固定枚举：

- `network.timeout`
- `network.blocked`
- `http.status_error`
- `content.empty_response`
- `content.html_error_page`
- `parse.missing_body`
- `parse.schema_mismatch`
- `storage.write_error`
- `validation.incomplete_output`

同时保留原始异常文本到 `debug.error_message`。

### Run manifest

每次执行写入：

```text
reports/runs/{run_id}/
  run_manifest.json
  metrics.json
  events.jsonl
  failures.jsonl
  validation.json
```

`run_manifest.json` 应包含：配置、阶段列表、输入范围、代码版本 / zip hash、开始结束时间、产物路径、是否 clean、是否 partial。

## 分阶段改造路线

### 阶段 0：冻结基线

- 改 README 测试命令为 `pytest`，或将 pytest 风格函数改成 unittest 类。
- 增加 golden fixture：NERIS 法规详情、NERIS 文书 HTML、AMAC 页面、PDF/DOCX/TXT 附件样本。
- 为关键 JSON 输出生成 schema snapshot：raw law、raw writ、normalized law、catalog entity、canonical graph。
- 所有 destructive 操作前加输出目录锁。

### 阶段 1：配置与工程化

- 新增 `pyproject.toml`，改为包结构。
- 引入 `Settings`，支持环境变量与 CLI 参数：`OUTPUT_ROOT`、delay、retry、max download bytes、workers。
- 移除 import-time 路径常量，所有路径由 `FileStore` 计算。
- 保留现有脚本，但脚本只解析参数并调用包内服务。

### 阶段 2：HTTP 与下载统一

- 合并 `HumanLikeClient`、`AmacClient`、`download_assets._download` 的重复逻辑。
- 支持 source-specific policy：base_url、headers、referer、verify_tls、rate_limit、retryable_status、blocked_markers。
- 下载改成 streaming，加入最大大小限制、临时文件、边下载边 hash。
- 所有请求发出 structured event 和 metrics。

### 阶段 3：领域模型与仓库层

- 定义 `SourceRecord`、`LawDocument`、`WritDocument`、`AssetRecord`、`CanonicalLaw`、`RelationEdge`。
- 用 schema version 管理 JSON 兼容性；读旧数据时做 migration / normalization。
- `FileStore` 提供：`save_json_atomic()`、`load_json()`、`append_jsonl()`、`publish_bundle()`、`acquire_lock()`。

### 阶段 4：流水线编排

- 把 `crawl.py`、`enhance.py`、`repair.py` 改造成统一 Stage DAG。
- 每个阶段有 precondition / output / validator / checkpoint。
- 统一阶段失败语义：局部失败可继续，但产物状态必须是 `incomplete`，下游默认拒绝消费 incomplete 产物，除非用户显式 `--allow-incomplete`。

### 阶段 5：规则模块化

- 将标题规范化、文号规范化、AMAC 分类、匹配规则、效力规则、试行替代规则集中到 `processing/catalog/rules/`。
- 每条规则有：`rule_id`、`description`、`confidence`、`evidence_fields`、`tests`。
- review queue 记录命中的 rule id，而不是只记录自然语言 reason。

### 阶段 6：可观测性落地

- 全量替换 `print` 为 logger。
- 每阶段输出 metrics、events、failures、validation summary。
- 关键对象携带 `trace_id` / `record_id`：从抓取、解析、归一化、匹配、导出一路贯通。

### 阶段 7：质量门禁

- 统一使用 `pytest`。
- 增加 ruff / mypy / coverage / dependency scan 到 CI。
- 对解析器增加 fixture 回归测试和属性测试。
- 对源匹配、效力规则、关系图增加小型 golden dataset，防止业务规则回退。

## 优先级建议

### 必须优先处理

1. 配置和输出路径重构，消除全局硬编码和 import-time 路径常量。
2. 加输出目录锁，避免并发写和 `--clean` 误删 / 覆盖。
3. 修复 AMAC TLS 策略，默认不禁用证书校验。
4. 统一 HTTP / 下载实现，加入 streaming 与大小上限。
5. 引入结构化日志和 run manifest，至少覆盖所有 CLI 入口。

### 随后处理

1. 抽出 FileStore、SourceClient、PipelineStep。
2. 拆分 `build_catalog()`、`normalize_laws()`、`build_canonical_relations()`。
3. 建立领域模型和 JSON schema。
4. 统一失败语义，让所有阶段都能明确产物是否可正式消费。
5. 扩充 parser / matching / effectiveness 的 fixture 测试。

## 结论

这个项目已经完成了从“单源爬虫”到“多来源法规数据生产流水线”的关键业务探索，当前最有价值的资产是：数据分层、官方证据保留、关系图构建、canonical 输出和校验脚本。

主要瓶颈是工程结构仍停留在脚本堆叠阶段：配置、网络、存储、规则、编排、日志都和业务函数交织。建议采用渐进式改造，不重写业务逻辑，而是先建立 `Settings + RunContext + FileStore + SourceClient + PipelineStep + structured logging` 六个基础构件，再把现有大函数逐步内聚到可测试的领域模块。这样可以在保持现有产物兼容的前提下，提高模块化、可复用性和可观测性。
