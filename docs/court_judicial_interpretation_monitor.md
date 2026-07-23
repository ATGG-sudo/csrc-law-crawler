# 最高法“权威发布—司法解释”栏目监测

该监测层只负责栏目发现、官方页面取证和人工复核队列。监测记录使用独立的
`court_judicial_interpretation_monitor` source system 和 `material_lane=clue`，
不会进入 canonical，也不会因栏目页面出现、消失或修改而改变法律效力。

## 运行

```bash
export CSRC_OUTPUT_ROOT=/mnt/d/FUND_COMPLIANCE/CSRC
export CSRC_COURT_MONITOR_DELAY_MIN=2
export CSRC_COURT_MONITOR_DELAY_MAX=5

csrc-court-monitor --baseline
csrc-court-monitor --daily
csrc-court-monitor --weekly
```

`--baseline` 抓取全部详情但不把历史库存报告成新增；`--daily` 完整枚举栏目，
只请求新增或列表元数据变化项的详情；`--weekly` 额外对全部详情执行条件 GET。
退出码 0 表示完整，2 表示可重试的不完整，1 表示配置或致命错误。

主要产物：

- `work/source_runs/<run_id>/manifest.json`
- `work/changes/<run_id>.jsonl`
- `reports/court_judicial_interpretation_monitor/inventory.json`
- `reports/court_judicial_interpretation_monitor/review_queue.json`
- `reports/court_judicial_interpretation_monitor/review_queue.md`
- `reports/digests/<run_id>.json/.md/.html`

人工确认需要入库后，应在受控最高法文档配置中补充精确标题、文号、正文边界、
条文数和断言，再运行受控抓取以及标准 canonical 重建链。复合页面不得在监测
阶段自动拆分。

## 调度模板

`deploy/systemd/` 提供用户级 service 和两个 timer 模板，但仓库不会自动安装。
等价 cron 示例：

```cron
CRON_TZ=Asia/Shanghai
CSRC_OUTPUT_ROOT=/mnt/d/FUND_COMPLIANCE/CSRC
CSRC_COURT_MONITOR_DELAY_MIN=2
CSRC_COURT_MONITOR_DELAY_MAX=5
30 20 * * 1-6 cd /home/anjie/projects/csrc-law-crawler && .venv/bin/python court_monitor.py --daily
30 20 * * 0 cd /home/anjie/projects/csrc-law-crawler && .venv/bin/python court_monitor.py --weekly
```
