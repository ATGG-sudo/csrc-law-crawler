#!/usr/bin/env python3
"""Export a static local HTML viewer for the canonical relation graph."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from build_canonical_relations import canonical_graph_path
from runtime import log_event
from storage import (
    load_json,
    output_path,
    relative_to_output,
    reports_dir,
    run_with_output_lock,
    save_json,
    source_matches_path,
    utc_now_iso,
)

VIEWER_SUBDIR = "relation_viewer"
HTML_DATA_PLACEHOLDER = "__RELATION_VIEWER_DATA__"


def relation_viewer_dir() -> Path:
    return reports_dir() / VIEWER_SUBDIR


def relation_viewer_index_path() -> Path:
    return relation_viewer_dir() / "index.html"


def relation_viewer_payload_path() -> Path:
    return relation_viewer_dir() / "payload.json"


def _node_kind(node_id: Any, node: dict[str, Any]) -> str:
    node_type = str(node.get("type") or "")
    text_id = str(node_id or "")
    if node_type == "writ" or text_id.startswith("writ:"):
        return "writ"
    if node_type == "law_stub" or text_id.startswith("neris:"):
        return "stub"
    if node_type == "law" or text_id.startswith("law_"):
        return "law"
    return "other"


def _load_canonical_doc(node: dict[str, Any]) -> dict[str, Any]:
    local_file = str(node.get("local_file") or "")
    if not local_file:
        return {}
    path = output_path(local_file)
    if not path.exists() or path.suffix.lower() != ".json":
        return {}
    return load_json(path, {})


def _compact_node(node: dict[str, Any]) -> dict[str, Any]:
    node_id = str(node.get("id") or "")
    kind = _node_kind(node_id, node)
    doc = _load_canonical_doc(node) if kind == "law" else {}
    metadata = doc.get("metadata") or {}
    effectiveness = doc.get("effectiveness") or {}
    sources = doc.get("sources") or []
    title = node.get("title") or doc.get("title") or node_id
    source_system = node.get("source_system")
    source_record_id = node.get("source_record_id")
    raw_file: str | None = None
    raw_exists: bool | None = None
    if kind == "stub" and source_system == "neris" and source_record_id:
        raw_path = output_path(Path("raw") / "neris" / "laws" / f"reg_{source_record_id}.json")
        raw_file = relative_to_output(raw_path)
        raw_exists = raw_path.exists()
    nameless = kind == "stub" and (not node.get("title") or node.get("title") == node_id)
    item = {
        "id": node_id,
        "kind": kind,
        "type": node.get("type"),
        "title": title,
        "nameless": nameless,
        "document_type": node.get("document_type") or doc.get("document_type"),
        "status": node.get("status") or doc.get("status"),
        "effectiveness": effectiveness.get("status"),
        "effectiveness_basis": effectiveness.get("basis"),
        "fileno": metadata.get("fileno"),
        "pub_org": metadata.get("pub_org"),
        "pub_date": metadata.get("pub_date"),
        "effective_date": metadata.get("effective_date"),
        "source_system": source_system,
        "source_record_id": source_record_id,
        "version": node.get("version"),
        "source_count": len(sources),
        "local_file": node.get("local_file"),
    }
    if raw_file is not None:
        item["raw_file"] = raw_file
        item["raw_exists"] = raw_exists
    return item


def _compact_edge(edge: dict[str, Any]) -> dict[str, Any]:
    evidence = edge.get("evidence") or {}
    return {
        "from": str(edge.get("from") or ""),
        "to": str(edge.get("to") or ""),
        "relation": edge.get("relation"),
        "source": edge.get("source"),
        "confidence": edge.get("confidence"),
        "rule_id": edge.get("rule_id") or evidence.get("rule_id"),
    }


def _ranked_counts(
    edges: list[dict[str, Any]],
    nodes_by_id: dict[str, dict[str, Any]],
    *,
    relation: str,
    endpoint: str,
    kind: str | None = None,
    limit: int,
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for edge in edges:
        if edge.get("relation") != relation:
            continue
        node_id = str(edge.get(endpoint) or "")
        node = nodes_by_id.get(node_id)
        if not node:
            continue
        if kind and node.get("kind") != kind:
            continue
        counts[node_id] += 1
    return [
        {
            "id": node_id,
            "count": count,
            "title": nodes_by_id.get(node_id, {}).get("title") or node_id,
            "kind": nodes_by_id.get(node_id, {}).get("kind"),
        }
        for node_id, count in counts.most_common(limit)
    ]


def _stub_rank(
    edges: list[dict[str, Any]],
    nodes_by_id: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    relation_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for edge in edges:
        relation = str(edge.get("relation") or "")
        for endpoint in ("from", "to"):
            node_id = str(edge.get(endpoint) or "")
            node = nodes_by_id.get(node_id)
            if node and node.get("kind") == "stub":
                counts[node_id] += 1
                relation_counts[node_id][relation] += 1
    result = []
    for node_id, count in counts.most_common(limit):
        node = nodes_by_id.get(node_id, {})
        result.append(
            {
                "id": node_id,
                "count": count,
                "title": node.get("title") or node_id,
                "relations": dict(relation_counts[node_id]),
            }
        )
    return result


def build_viewer_payload(*, rank_limit: int = 30) -> dict[str, Any]:
    graph_path = canonical_graph_path()
    graph = load_json(graph_path, {})
    if not graph.get("nodes") or not graph.get("edges"):
        raise FileNotFoundError(
            f"关系图为空或不存在，请先运行 build_canonical_relations.py: {graph_path}"
        )

    nodes = [_compact_node(node) for node in graph.get("nodes") or []]
    edges = [_compact_edge(edge) for edge in graph.get("edges") or []]
    nodes_by_id = {str(node["id"]): node for node in nodes if node.get("id")}
    node_kind_counts = Counter(str(node.get("kind") or "other") for node in nodes)
    relation_counts = Counter(str(edge.get("relation") or "unknown") for edge in edges)
    edge_source_counts = Counter(str(edge.get("source") or "unknown") for edge in edges)
    stub_nodes = [node for node in nodes if node.get("kind") == "stub"]
    source_map = load_json(source_matches_path(), {})

    return {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "source_files": {
            "graph": relative_to_output(graph_path),
            "source_map": relative_to_output(source_matches_path()),
        },
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "node_kinds": dict(sorted(node_kind_counts.items())),
            "relations": dict(sorted(relation_counts.items())),
            "edge_sources": dict(edge_source_counts.most_common()),
            "source_map_entries": len(source_map.get("by_source") or {}),
            "nameless_stub_nodes": sum(1 for node in stub_nodes if node.get("nameless")),
            "raw_missing_stub_nodes": sum(
                1 for node in stub_nodes if node.get("raw_exists") is False
            ),
        },
        "rankings": {
            "high_cited_laws": _ranked_counts(
                edges,
                nodes_by_id,
                relation="cited_by_case",
                endpoint="from",
                kind="law",
                limit=rank_limit,
            ),
            "publishers": _ranked_counts(
                edges,
                nodes_by_id,
                relation="publishes",
                endpoint="from",
                kind="law",
                limit=rank_limit,
            ),
            "stub_nodes": _stub_rank(edges, nodes_by_id, limit=rank_limit),
        },
        "nodes": nodes,
        "edges": edges,
    }


def _json_for_html(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace(
        "</",
        "<\\/",
    )


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CSRC 制度关系图查看器</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2933;
      --muted: #697386;
      --line: #d8dee8;
      --law: #1f7a5c;
      --stub: #7c8797;
      --writ: #a45f13;
      --accent: #2563eb;
      --danger: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
    }
    header {
      min-height: 64px;
      padding: 14px 20px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: 0;
    }
    .subtitle { color: var(--muted); font-size: 12px; margin-top: 3px; }
    .shell {
      display: grid;
      grid-template-columns: 360px minmax(520px, 1fr) 360px;
      gap: 12px;
      padding: 12px;
      height: calc(100vh - 65px);
      min-height: 680px;
    }
    aside, main {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      min-width: 0;
    }
    .panel-header {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .panel-body { padding: 12px 14px; overflow: auto; height: calc(100% - 48px); }
    input, select {
      width: 100%;
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      color: var(--text);
      background: #fff;
      font: inherit;
    }
    .controls {
      display: grid;
      grid-template-columns: 1fr 104px;
      gap: 8px;
      margin-bottom: 10px;
    }
    .wide-control { margin-bottom: 10px; }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0; }
    button.chip {
      min-height: 30px;
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 999px;
      padding: 5px 10px;
      cursor: pointer;
      color: var(--text);
    }
    button.chip.active {
      border-color: var(--accent);
      color: var(--accent);
      background: #eff6ff;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(110px, 1fr));
      gap: 8px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      background: #fff;
      min-height: 64px;
    }
    .metric strong { display: block; font-size: 20px; line-height: 1.2; }
    .metric span { color: var(--muted); font-size: 12px; }
    #graph {
      height: calc(100% - 89px);
      position: relative;
      overflow: hidden;
      background:
        linear-gradient(#eef1f5 1px, transparent 1px),
        linear-gradient(90deg, #eef1f5 1px, transparent 1px);
      background-size: 28px 28px;
    }
    svg { position: absolute; inset: 0; width: 100%; height: 100%; }
    .node {
      position: absolute;
      transform: translate(-50%, -50%);
      width: 168px;
      min-height: 52px;
      padding: 8px 10px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #fff;
      box-shadow: 0 4px 16px rgba(31, 41, 51, 0.10);
      cursor: pointer;
    }
    .node.law { border-top: 4px solid var(--law); }
    .node.stub { border-top: 4px dashed var(--stub); background: #f9fafb; }
    .node.writ { border-top: 4px solid var(--writ); }
    .node.selected { outline: 3px solid rgba(37, 99, 235, 0.24); }
    .node-title {
      font-size: 12px;
      font-weight: 700;
      line-height: 1.25;
      max-height: 45px;
      overflow: hidden;
    }
    .node-meta {
      margin-top: 5px;
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .list { display: grid; gap: 8px; }
    .item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      cursor: pointer;
      background: #fff;
    }
    .item:hover { border-color: var(--accent); }
    .item-title { font-weight: 700; line-height: 1.35; }
    .item-meta { margin-top: 4px; color: var(--muted); font-size: 12px; }
    .section-title {
      margin: 16px 0 8px;
      font-weight: 800;
      font-size: 13px;
      color: #344054;
    }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid var(--line); padding: 7px 4px; text-align: left; }
    th { color: var(--muted); font-weight: 700; }
    .detail-grid {
      display: grid;
      grid-template-columns: 96px 1fr;
      gap: 8px;
      align-items: start;
    }
    .label { color: var(--muted); }
    .value { word-break: break-word; }
    .legend { display: flex; gap: 10px; color: var(--muted); font-size: 12px; }
    .dot { width: 10px; height: 10px; display: inline-block; border-radius: 50%; margin-right: 4px; }
    .dot.law { background: var(--law); }
    .dot.stub { background: var(--stub); }
    .dot.writ { background: var(--writ); }
    @media (max-width: 1180px) {
      .shell { grid-template-columns: 320px 1fr; height: auto; }
      .right { grid-column: 1 / -1; height: 520px; }
      main { min-height: 620px; }
    }
    @media (max-width: 760px) {
      header { align-items: flex-start; flex-direction: column; }
      .shell { display: block; padding: 8px; }
      aside, main { margin-bottom: 8px; height: 620px; }
      .metrics { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>CSRC 制度关系图查看器</h1>
      <div class="subtitle" id="generated"></div>
    </div>
    <div class="legend">
      <span><i class="dot law"></i>制度</span>
      <span><i class="dot stub"></i>stub</span>
      <span><i class="dot writ"></i>文书</span>
    </div>
  </header>
  <div class="shell">
    <aside>
      <div class="panel-header">检索与排行</div>
      <div class="panel-body">
        <div class="controls">
          <input id="search" placeholder="搜索制度名、文号、ID">
          <select id="depth">
            <option value="1">1 层</option>
            <option value="2">2 层</option>
          </select>
        </div>
        <select id="stubFilter" class="wide-control" title="stub 过滤">
          <option value="hide_unnamed">隐藏无名 stub</option>
          <option value="show_all">显示全部 stub</option>
          <option value="hide_all">隐藏全部 stub</option>
        </select>
        <div class="chips" id="relationChips"></div>
        <div class="section-title">搜索结果</div>
        <div class="list" id="results"></div>
        <div class="section-title">高引用制度</div>
        <div class="list" id="highCited"></div>
        <div class="section-title">stub 节点排行</div>
        <div class="list" id="stubRank"></div>
      </div>
    </aside>
    <main>
      <div class="metrics" id="metrics"></div>
      <div id="graph"></div>
    </main>
    <aside class="right">
      <div class="panel-header">节点详情</div>
      <div class="panel-body">
        <div id="details"></div>
        <div class="section-title">当前子图边</div>
        <div id="edgeTable"></div>
        <div class="section-title">发布关系入口</div>
        <div class="list" id="publishers"></div>
      </div>
    </aside>
  </div>
  <script id="viewer-data" type="application/json">__RELATION_VIEWER_DATA__</script>
  <script>
    const data = JSON.parse(document.getElementById('viewer-data').textContent);
    const nodes = new Map(data.nodes.map(node => [node.id, node]));
    const edges = data.edges;
    const adjacency = new Map();
    for (const edge of edges) {
      if (!adjacency.has(edge.from)) adjacency.set(edge.from, []);
      if (!adjacency.has(edge.to)) adjacency.set(edge.to, []);
      adjacency.get(edge.from).push(edge);
      adjacency.get(edge.to).push(edge);
    }
    const state = {
      selected: data.rankings.high_cited_laws[0]?.id || data.nodes[0]?.id,
      relation: 'all',
      stubMode: 'hide_unnamed',
      depth: 1,
      query: ''
    };
    const relationLabels = {
      all: '全部',
      supersedes: '修订替代',
      publishes: '发布附件',
      related_to: '关联法规',
      cited_by_case: '案例引用'
    };
    const kindLabels = { law: '制度', stub: 'stub', writ: '文书', other: '其他' };
    const relationColors = {
      supersedes: '#b42318',
      publishes: '#1f7a5c',
      related_to: '#2563eb',
      cited_by_case: '#a45f13'
    };
    function esc(value) {
      return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      })[c]);
    }
    function displayTitle(id) {
      const node = nodes.get(id);
      return node?.title || id;
    }
    function nodeMeta(node) {
      return [
        kindLabels[node.kind] || node.kind,
        node.fileno,
        node.status || node.effectiveness,
        node.kind === 'stub' && node.raw_exists === false ? 'raw 缺失' : ''
      ]
        .filter(Boolean).join(' · ');
    }
    function nodeVisible(node) {
      if (!node) return false;
      if (node.kind !== 'stub') return true;
      if (state.stubMode === 'show_all') return true;
      if (state.stubMode === 'hide_all') return false;
      return !node.nameless;
    }
    function edgeAllowed(edge) {
      return state.relation === 'all' || edge.relation === state.relation;
    }
    function edgeVisible(edge) {
      return edgeAllowed(edge) && nodeVisible(nodes.get(edge.from)) && nodeVisible(nodes.get(edge.to));
    }
    function firstVisibleNodeId() {
      const ranked = data.rankings.high_cited_laws.find(item => nodeVisible(nodes.get(item.id)));
      return ranked?.id || data.nodes.find(nodeVisible)?.id;
    }
    function ensureSelectedVisible() {
      if (!nodeVisible(nodes.get(state.selected))) state.selected = firstVisibleNodeId();
    }
    function neighborhood(rootId) {
      if (!rootId || !nodeVisible(nodes.get(rootId))) return { nodeIds: [], subEdges: [] };
      const seen = new Set([rootId]);
      let frontier = [rootId];
      const subEdgeMap = new Map();
      for (let level = 0; level < state.depth; level += 1) {
        const next = [];
        for (const nodeId of frontier) {
          for (const edge of adjacency.get(nodeId) || []) {
            if (!edgeVisible(edge)) continue;
            const other = edge.from === nodeId ? edge.to : edge.from;
            if (!nodeVisible(nodes.get(other))) continue;
            subEdgeMap.set(`${edge.from}|${edge.to}|${edge.relation}|${edge.source}`, edge);
            if (!seen.has(other) && seen.size < 160) {
              seen.add(other);
              next.push(other);
            }
          }
        }
        frontier = next;
      }
      return { nodeIds: Array.from(seen), subEdges: Array.from(subEdgeMap.values()).slice(0, 260) };
    }
    function layout(nodeIds) {
      const graph = document.getElementById('graph');
      const rect = graph.getBoundingClientRect();
      const width = Math.max(rect.width, 640);
      const height = Math.max(rect.height, 520);
      const cx = width / 2;
      const cy = height / 2;
      const positions = new Map([[state.selected, { x: cx, y: cy }]]);
      const rest = nodeIds.filter(id => id !== state.selected);
      const direct = [];
      const second = [];
      const selectedEdges = new Set((adjacency.get(state.selected) || []).filter(edgeVisible).flatMap(e => [e.from, e.to]));
      for (const id of rest) (selectedEdges.has(id) ? direct : second).push(id);
      const placeRing = (ids, radius, offset) => {
        const count = Math.max(ids.length, 1);
        ids.forEach((id, index) => {
          const angle = offset + (Math.PI * 2 * index / count);
          positions.set(id, {
            x: cx + Math.cos(angle) * radius,
            y: cy + Math.sin(angle) * radius
          });
        });
      };
      placeRing(direct, Math.min(width, height) * 0.28, -Math.PI / 2);
      placeRing(second, Math.min(width, height) * 0.42, -Math.PI / 2 + 0.18);
      return positions;
    }
    function renderMetrics() {
      const counts = data.counts;
      const visibleNodes = data.nodes.filter(nodeVisible);
      const visibleIds = new Set(visibleNodes.map(node => node.id));
      const visibleKinds = visibleNodes.reduce((acc, node) => {
        acc[node.kind] = (acc[node.kind] || 0) + 1;
        return acc;
      }, {});
      const visibleEdges = edges.filter(edge =>
        edgeAllowed(edge) && visibleIds.has(edge.from) && visibleIds.has(edge.to)
      ).length;
      const cards = [
        ['可见节点', visibleNodes.length],
        ['可见边', visibleEdges],
        ['制度', visibleKinds.law || 0],
        ['stub', `${visibleKinds.stub || 0}/${counts.node_kinds.stub || 0}`]
      ];
      document.getElementById('metrics').innerHTML = cards.map(([label, value]) =>
        `<div class="metric"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`
      ).join('');
      document.getElementById('generated').textContent =
        `生成时间 ${data.generated_at} · ${data.source_files.graph}`;
    }
    function renderRelationChips() {
      const relations = ['all', 'supersedes', 'publishes', 'related_to', 'cited_by_case'];
      document.getElementById('relationChips').innerHTML = relations.map(rel =>
        `<button class="chip ${state.relation === rel ? 'active' : ''}" data-rel="${rel}">${relationLabels[rel] || rel}</button>`
      ).join('');
      document.querySelectorAll('[data-rel]').forEach(button => {
        button.addEventListener('click', () => {
          state.relation = button.dataset.rel;
          render();
        });
      });
    }
    function renderList(id, items, options = {}) {
      const element = document.getElementById(id);
      const visibleItems = items.filter(item => nodeVisible(nodes.get(item.id) || item));
      element.innerHTML = visibleItems.slice(0, options.limit || 30).map(item => {
        const node = nodes.get(item.id) || item;
        const meta = item.count ? `${item.count} 条关系 · ${nodeMeta(node)}` : nodeMeta(node);
        return `<div class="item" data-node="${esc(item.id)}">
          <div class="item-title">${esc(node.title || item.title || item.id)}</div>
          <div class="item-meta">${esc(meta)}</div>
        </div>`;
      }).join('') || '<div class="item-meta">无结果</div>';
      element.querySelectorAll('[data-node]').forEach(item => {
        item.addEventListener('click', () => {
          state.selected = item.dataset.node;
          render();
        });
      });
    }
    function renderSearch() {
      const query = state.query.trim().toLowerCase();
      if (!query) {
        renderList('results', data.rankings.high_cited_laws.slice(0, 8), { limit: 8 });
        return;
      }
      const terms = query.split(/\\s+/).filter(Boolean);
      const matches = data.nodes.filter(node => nodeVisible(node)).filter(node => {
        const haystack = [node.title, node.id, node.fileno, node.pub_org, node.status, node.source_record_id]
          .filter(Boolean).join(' ').toLowerCase();
        return terms.every(term => haystack.includes(term));
      }).slice(0, 80).map(node => ({ id: node.id }));
      renderList('results', matches, { limit: 80 });
    }
    function renderDetails() {
      const node = nodes.get(state.selected);
      if (!node) return;
      const rows = [
        ['标题', node.title],
        ['节点类型', kindLabels[node.kind] || node.kind],
        ['文号', node.fileno],
        ['状态', node.status || node.effectiveness],
        ['文件类型', node.document_type],
        ['发布机构', node.pub_org],
        ['发布日期', node.pub_date],
        ['施行日期', node.effective_date],
        ['来源数', node.source_count],
        ['源系统', node.source_system],
        ['源记录', node.source_record_id],
        ['raw 状态', node.raw_exists === true ? '已抓取' : node.raw_exists === false ? '未抓取' : undefined],
        ['raw 文件', node.raw_file],
        ['本地文件', node.local_file],
        ['ID', node.id]
      ];
      document.getElementById('details').innerHTML =
        `<div class="detail-grid">${rows.map(([k, v]) =>
          `<div class="label">${esc(k)}</div><div class="value">${esc(v || '-')}</div>`
        ).join('')}</div>`;
    }
    function renderGraph() {
      const graph = document.getElementById('graph');
      const { nodeIds, subEdges } = neighborhood(state.selected);
      const positions = layout(nodeIds);
      const lines = subEdges.map(edge => {
        const a = positions.get(edge.from);
        const b = positions.get(edge.to);
        if (!a || !b) return '';
        const color = relationColors[edge.relation] || '#7c8797';
        return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${color}" stroke-width="1.6" stroke-opacity="0.72" />`;
      }).join('');
      const nodeHtml = nodeIds.map(id => {
        const node = nodes.get(id);
        const pos = positions.get(id);
        return `<div class="node ${esc(node.kind)} ${id === state.selected ? 'selected' : ''}"
          style="left:${pos.x}px;top:${pos.y}px" data-node="${esc(id)}">
          <div class="node-title">${esc(node.title || id)}</div>
          <div class="node-meta">${esc(nodeMeta(node))}</div>
        </div>`;
      }).join('');
      graph.innerHTML = `<svg aria-hidden="true">${lines}</svg>${nodeHtml}`;
      graph.querySelectorAll('[data-node]').forEach(item => {
        item.addEventListener('click', () => {
          state.selected = item.dataset.node;
          render();
        });
      });
      renderEdgeTable(subEdges);
    }
    function renderEdgeTable(subEdges) {
      const rows = subEdges.slice(0, 120).map(edge =>
        `<tr><td>${esc(relationLabels[edge.relation] || edge.relation)}</td><td>${esc(displayTitle(edge.from))}</td><td>${esc(displayTitle(edge.to))}</td><td>${esc(edge.source)}</td></tr>`
      ).join('');
      document.getElementById('edgeTable').innerHTML =
        `<table><thead><tr><th>关系</th><th>from</th><th>to</th><th>来源</th></tr></thead><tbody>${rows}</tbody></table>`;
    }
    function render() {
      ensureSelectedVisible();
      renderMetrics();
      renderRelationChips();
      renderSearch();
      renderList('highCited', data.rankings.high_cited_laws, { limit: 10 });
      renderList('stubRank', data.rankings.stub_nodes, { limit: 10 });
      renderList('publishers', data.rankings.publishers, { limit: 10 });
      renderDetails();
      renderGraph();
    }
    document.getElementById('search').addEventListener('input', event => {
      state.query = event.target.value;
      renderSearch();
    });
    document.getElementById('depth').addEventListener('change', event => {
      state.depth = Number(event.target.value);
      render();
    });
    document.getElementById('stubFilter').addEventListener('change', event => {
      state.stubMode = event.target.value;
      render();
    });
    window.addEventListener('resize', () => renderGraph());
    render();
  </script>
</body>
</html>
"""


def export_relation_viewer(
    *,
    write_payload: bool = True,
    rank_limit: int = 30,
) -> dict[str, Any]:
    payload = build_viewer_payload(rank_limit=rank_limit)
    out_dir = relation_viewer_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    html = HTML_TEMPLATE.replace(HTML_DATA_PLACEHOLDER, _json_for_html(payload))
    relation_viewer_index_path().write_text(html, encoding="utf-8")
    if write_payload:
        save_json(relation_viewer_payload_path(), payload)
    return {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "viewer_file": relative_to_output(relation_viewer_index_path()),
        "payload_file": (
            relative_to_output(relation_viewer_payload_path()) if write_payload else None
        ),
        "nodes": payload["counts"]["nodes"],
        "edges": payload["counts"]["edges"],
        "relations": payload["counts"]["relations"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 canonical 关系图静态查看器")
    parser.add_argument(
        "--no-payload",
        action="store_true",
        help="只写 index.html，不额外写 payload.json",
    )
    parser.add_argument("--rank-limit", type=int, default=30)
    args = parser.parse_args()
    try:
        manifest = export_relation_viewer(
            write_payload=not args.no_payload,
            rank_limit=args.rank_limit,
        )
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1
    log_event(
        "cli_result",
        message=(
            f"完成: nodes={manifest['nodes']} edges={manifest['edges']} "
            f"-> {manifest['viewer_file']}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "export-relation-viewer"))
