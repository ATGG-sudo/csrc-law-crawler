"""执法文书详情页 HTML 解析（lawWritInfo 服务端渲染，无 JSON 正文接口）。"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any


VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

KNOWN_TAGS = {
    "a",
    "body",
    "br",
    "button",
    "div",
    "em",
    "font",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "head",
    "html",
    "i",
    "img",
    "input",
    "label",
    "li",
    "link",
    "meta",
    "option",
    "p",
    "pre",
    "script",
    "select",
    "span",
    "strong",
    "style",
    "table",
    "tbody",
    "td",
    "textarea",
    "tfoot",
    "th",
    "thead",
    "title",
    "tr",
    "u",
    "ul",
}

BLOCK_TAGS = {"div", "p", "pre", "tr"}


@dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)


class LawWritHTMLParser(HTMLParser):
    """Build a small DOM tree while preserving non-HTML angle-bracket text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in KNOWN_TAGS:
            self.stack[-1].children.append(self.get_starttag_text() or f"<{tag}>")
            return

        node = HtmlNode(tag, {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in KNOWN_TAGS:
            self.stack[-1].children.append(self.get_starttag_text() or f"<{tag}/>")
            return

        node = HtmlNode(tag, {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in KNOWN_TAGS:
            self.stack[-1].children.append(f"</{tag}>")
            return

        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _parse_html(page_html: str) -> HtmlNode:
    parser = LawWritHTMLParser()
    parser.feed(page_html)
    parser.close()
    return parser.root


def _classes(node: HtmlNode) -> set[str]:
    return set((node.attrs.get("class") or "").split())


def _children_by_tag(node: HtmlNode, tag: str) -> list[HtmlNode]:
    return [
        child
        for child in node.children
        if isinstance(child, HtmlNode) and child.tag == tag
    ]


def _walk(node: HtmlNode) -> list[HtmlNode]:
    found: list[HtmlNode] = []
    for child in node.children:
        if isinstance(child, HtmlNode):
            found.append(child)
            found.extend(_walk(child))
    return found


def _find_all(node: HtmlNode, tag: str) -> list[HtmlNode]:
    return [child for child in _walk(node) if child.tag == tag]


def _text_content(node: HtmlNode) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
            continue
        if child.tag == "br":
            parts.append("\n")
            continue
        if child.tag == "img":
            alt = child.attrs.get("alt")
            if alt:
                parts.append(alt)
            continue

        parts.append(_text_content(child))
        if child.tag in BLOCK_TAGS:
            parts.append("\n")
    return "".join(parts)


def _clean_text(raw: str, *, preserve_lines: bool = False) -> str:
    text = html.unescape(raw).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ")
    if not preserve_lines:
        return re.sub(r"\s+", " ", text).strip()

    return re.sub(r"\s+\n", "\n", text).strip()


def _node_text(node: HtmlNode, *, preserve_lines: bool = False) -> str:
    return _clean_text(_text_content(node), preserve_lines=preserve_lines)


def _first_href(node: HtmlNode) -> str:
    for link in _find_all(node, "a"):
        href = link.attrs.get("href")
        if href:
            return href
    return ""


def _call_args(onclick: str, function_name: str) -> list[str]:
    marker = f"{function_name}("
    start = onclick.find(marker)
    if start < 0:
        return []
    start += len(marker)
    end = onclick.find(")", start)
    if end < 0:
        return []
    return re.findall(r"""['"]([^'"]+)['"]""", onclick[start:end])


def _extract_label_value_pairs(root: HtmlNode) -> tuple[dict[str, str], dict[str, str]]:
    """Extract metadata from label/value table cells."""
    pairs: dict[str, str] = {}
    hrefs: dict[str, str] = {}

    for row in _find_all(root, "tr"):
        row_text = _node_text(row)
        if "当事人信息" in row_text:
            break

        cells = _children_by_tag(row, "td")
        index = 0
        while index + 1 < len(cells):
            label_cell = cells[index]
            if "table_bg" not in _classes(label_cell):
                index += 1
                continue

            label = _node_text(label_cell)
            value_cell = cells[index + 1]
            value = _node_text(value_cell)
            if label and label not in pairs:
                pairs[label] = value
            href = _first_href(value_cell)
            if href and label not in hrefs:
                hrefs[label] = href
            index += 2

    return pairs, hrefs


def _extract_parties(root: HtmlNode) -> list[dict[str, str]]:
    rows = _find_all(root, "tr")
    start = None
    for index, row in enumerate(rows):
        if "当事人信息" in _node_text(row):
            start = index + 1
            break
    if start is None:
        return []

    parties: list[dict[str, str]] = []
    for row in rows[start:]:
        cells = _children_by_tag(row, "td")
        if len(cells) < 5:
            continue

        values = [_node_text(cell) for cell in cells[:5]]
        if values[0] in {"当事人类型", "当事人名称"} or values[1] == "当事人名称":
            continue
        if not values[1]:
            continue

        parties.append(
            {
                "party_type": values[0],
                "name": values[1],
                "role": values[2],
                "violation_type": values[3],
                "penalty_amount": values[4],
            }
        )
    return parties


def _extract_legal_basis(root: HtmlNode) -> list[dict[str, str]]:
    laws: dict[str, str] = {}
    for link in _find_all(root, "a"):
        args = _call_args(link.attrs.get("onclick", ""), "lawInfo")
        if args:
            laws[args[0]] = _node_text(link)

    basis: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for link in _find_all(root, "a"):
        args = _call_args(link.attrs.get("onclick", ""), "entryInfo")
        if len(args) < 2:
            continue
        law_id, entry_id = args[0], args[1]
        key = (law_id, entry_id)
        if key in seen:
            continue
        seen.add(key)
        basis.append(
            {
                "law_id": law_id,
                "entry_id": entry_id,
                "law_name": laws.get(law_id, ""),
                "entry_title": _node_text(link),
            }
        )
    return basis


def parse_law_writ_info_html(page_html: str) -> dict[str, Any]:
    """解析 lawWritInfo 详情页。"""
    root = _parse_html(page_html)
    pre_nodes = [
        node
        for node in _find_all(root, "pre")
        if "pre_law" in _classes(node)
    ]
    body = _node_text(pre_nodes[0], preserve_lines=True) if pre_nodes else ""

    fields, hrefs = _extract_label_value_pairs(root)

    return {
        "name": fields.get("文件名称", ""),
        "fileno": fields.get("文号", ""),
        "dspt_date": fields.get("发文日期", ""),
        "writ_type": fields.get("文书类型", ""),
        "issue_org": fields.get("发文单位", ""),
        "original_link": hrefs.get("原文链接") or fields.get("原文链接", ""),
        "body": body,
        "legal_basis": _extract_legal_basis(root),
        "parties": _extract_parties(root),
    }


def merge_writ_document(
    writ_id: str,
    *,
    list_summary: dict[str, Any] | None,
    detail: dict[str, Any],
) -> dict[str, Any]:
    """合并列表摘要与详情页解析结果。"""
    summary = list_summary or {}
    metadata = {
        "id": writ_id,
        "name": detail.get("name") or summary.get("name"),
        "fileno": detail.get("fileno") or summary.get("fileno"),
        "issue_org": detail.get("issue_org") or summary.get("issue_org"),
        "dspt_date": detail.get("dspt_date"),
        "dspt_date_ms": summary.get("dspt_date_ms"),
        "writ_type": detail.get("writ_type"),
        "original_link": detail.get("original_link") or summary.get("link_addr"),
    }
    return {
        "metadata": metadata,
        "body": detail.get("body") or "",
        "legal_basis": detail.get("legal_basis") or [],
        "parties": detail.get("parties") or [],
        "list_summary": summary or None,
    }
