#!/usr/bin/env python3
"""Read-only full-corpus audit for private-fund compliance records.

The script reads an existing CSRC output root and writes only audit artifacts to
the requested output directory. It never modifies raw, canonical, or report data
under the source root.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import random
import re
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


TITLE_PATTERNS = {
    "explicit_private": re.compile(r"私募|非公开募集"),
    "venture_equity_fund": re.compile(r"创业投资基金|股权投资基金|产业投资基金"),
    "registration_filing": re.compile(
        r"基金管理人.{0,8}(登记|备案)|基金.{0,6}备案|管理人登记"
    ),
    "private_asset_management": re.compile(r"私募资产管理|资产管理业务|资产管理计划"),
    "umbrella_fund_law": re.compile(r"证券投资基金法|基金募集"),
}

# Scope exclusions are intentionally narrow.  These titles contain words such as
# “私募” or “合格投资者”, but belong to public offerings, private bonds, or the
# institutional private-product/ABS platform rather than private-fund compliance.
SCOPE_EXCLUDE_RE = re.compile(
    r"向不特定合格投资者公开发行|中小企业私募债|"
    r"机构间私募产品报价与服务系统(?!.*私募基金)"
)

ONE_OFF_RE = re.compile(
    r"纪律处分|行政处罚|监管措施|自律措施|注销.*登记|暂停.*业务|失联|异常经营|"
    r"限期提交|主动联系|风险提示|通报|决定书"
)
DATE_RE = re.compile(r"^(20\d{2})[-年](\d{1,2})[-月](\d{1,2})")
ALLOWED_EFFECTIVENESS = {"current", "pending", "historical", "unknown", "not_applicable"}
OFFICIAL_DOMAINS = (
    "csrc.gov.cn",
    "amac.org.cn",
    "neris.csrc.gov.cn",
    "gov.cn",
    "chinaclear.cn",
)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def normalize_text(value: str | None) -> str:
    return "".join(
        ch
        for ch in str(value or "")
        if unicodedata.category(ch)[0] in {"L", "N"}
    ).lower()


def normalize_title(value: str | None) -> str:
    text = str(value or "")
    text = re.sub(r"^[【\[]?第?\d+号令[】\]]?", "", text)
    text = re.sub(r"\s*[-—]\s*\d{4}[-年]\d{1,2}[-月]\d{1,2}日?$", "", text)
    return normalize_text(text)


def title_hits(title: str) -> list[str]:
    if SCOPE_EXCLUDE_RE.search(title):
        return []
    return [name for name, pattern in TITLE_PATTERNS.items() if pattern.search(title)]


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    match = DATE_RE.match(str(value).strip())
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def official_url(url: str | None) -> bool:
    if not url:
        return False
    host = urlparse(url).hostname or ""
    return any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS)


def issue(
    record_id: str,
    title: str,
    code: str,
    severity: str,
    evidence: str,
    confidence: float = 1.0,
) -> dict[str, Any]:
    return {
        "id": record_id,
        "title": title,
        "code": code,
        "severity": severity,
        "confidence": confidence,
        "evidence": evidence,
    }


def shingle_coverage(source: str, target: str, width: int = 20, step: int = 20) -> float | None:
    source_norm = normalize_text(source)
    target_norm = normalize_text(target)
    if not source_norm:
        return None
    if len(source_norm) < width:
        return float(source_norm in target_norm)
    chunks = [source_norm[i : i + width] for i in range(0, len(source_norm) - width + 1, step)]
    return sum(chunk in target_norm for chunk in chunks) / len(chunks)


def load_corpus(root: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    all_items: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    for path in sorted((root / "canonical" / "json").glob("*.json")):
        item = read_json(path)
        item["_path"] = str(path)
        all_items[item["id"]] = item
        hits = title_hits(item.get("title") or "")
        if hits:
            item["_scope_hits"] = hits
            candidates.append(item)
    return all_items, candidates


def load_graph(root: Path) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    graph = read_json(root / "canonical" / "relations" / "graph.json")
    edges = graph.get("edges") or []
    incident: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        incident[edge.get("from")].append(edge)
        incident[edge.get("to")].append(edge)
    return edges, incident


def local_audit(root: Path, out: Path, as_of: date) -> None:
    all_items, candidates = load_corpus(root)
    edges, incident = load_graph(root)
    candidate_ids = {item["id"] for item in candidates}
    findings: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    url_rows: dict[str, dict[str, Any]] = {}
    title_groups: dict[str, list[str]] = defaultdict(list)
    source_url_groups: dict[str, list[str]] = defaultdict(list)

    for item in candidates:
        record_id = item["id"]
        title = item.get("title") or ""
        metadata = item.get("metadata") or {}
        material = item.get("material_classification") or {}
        effectiveness = item.get("effectiveness") or {}
        status = effectiveness.get("status")
        lane = material.get("lane")
        category = material.get("category")
        full_text = item.get("full_text_plain") or ""
        content_status = item.get("content_status")
        sources = item.get("sources") or []
        assets = item.get("assets") or []
        source_systems = sorted({source.get("system") for source in sources if source.get("system")})
        page_urls = sorted(
            {source.get("page_url") for source in sources if source.get("page_url")}
        )
        official_page_urls = [url for url in page_urls if official_url(url)]
        pub_date = parse_date(metadata.get("pub_date"))
        effective_date = parse_date(metadata.get("effective_date"))
        ineffective_date = parse_date(metadata.get("ineffective_date"))

        title_groups[normalize_title(title)].append(record_id)
        for url in page_urls:
            source_url_groups[url].append(record_id)
            row = url_rows.setdefault(
                url,
                {
                    "url": url,
                    "kind": "source_page",
                    "record_ids": [],
                    "titles": [],
                    "official": official_url(url),
                },
            )
            row["record_ids"].append(record_id)
            row["titles"].append(title)

        if status not in ALLOWED_EFFECTIVENESS:
            findings.append(issue(record_id, title, "invalid_effectiveness", "high", f"status={status!r}"))
        if metadata.get("pub_date") and not pub_date:
            findings.append(issue(record_id, title, "malformed_pub_date", "high", str(metadata.get("pub_date"))))
        if metadata.get("effective_date") and not effective_date:
            findings.append(issue(record_id, title, "malformed_effective_date", "high", str(metadata.get("effective_date"))))
        if status == "pending" and (not effective_date or effective_date <= as_of):
            findings.append(issue(record_id, title, "pending_date_conflict", "critical", f"effective_date={metadata.get('effective_date')} as_of={as_of}"))
        if status == "current" and effective_date and effective_date > as_of:
            findings.append(issue(record_id, title, "current_before_effective", "critical", f"effective_date={effective_date} as_of={as_of}"))
        if status == "historical" and ineffective_date and ineffective_date > as_of:
            findings.append(issue(record_id, title, "historical_before_ineffective", "critical", f"ineffective_date={ineffective_date} as_of={as_of}"))
        if lane == "rule" and status == "unknown":
            findings.append(issue(record_id, title, "rule_effectiveness_unknown", "high", "rule lane has unknown effectiveness"))
        if content_status == "full_text" and not full_text.strip():
            findings.append(issue(record_id, title, "empty_full_text", "critical", "content_status=full_text but full_text_plain is empty"))
        if lane == "rule" and len(normalize_text(full_text)) < 200:
            findings.append(issue(record_id, title, "short_rule_text", "high", f"normalized_text_chars={len(normalize_text(full_text))}"))
        if lane == "reference" and status != "not_applicable":
            findings.append(issue(record_id, title, "reference_effectiveness_conflict", "medium", f"reference lane status={status}"))
        if ONE_OFF_RE.search(title) and lane == "rule":
            findings.append(issue(record_id, title, "one_off_material_in_rule_lane", "high", f"category={category}"))
        if not official_page_urls:
            findings.append(issue(record_id, title, "missing_official_page_url", "high" if lane == "rule" else "medium", f"page_urls={page_urls}"))

        failed_assets = 0
        missing_asset_files = 0
        sha_mismatch = 0
        pdf_assets = 0
        for asset in assets:
            status_value = asset.get("download_status")
            local_file = asset.get("local_file")
            if status_value not in {"ok", "complete"}:
                failed_assets += 1
            if local_file:
                local_path = root / local_file
                if not local_path.exists():
                    missing_asset_files += 1
                elif asset.get("sha256"):
                    actual = sha256_file(local_path)
                    if actual != asset.get("sha256"):
                        sha_mismatch += 1
                if local_path.suffix.lower() == ".pdf":
                    pdf_assets += 1
            for url in asset.get("source_urls") or [asset.get("source_url")]:
                if not url:
                    continue
                row = url_rows.setdefault(
                    url,
                    {
                        "url": url,
                        "kind": "asset",
                        "record_ids": [],
                        "titles": [],
                        "official": official_url(url),
                    },
                )
                row["record_ids"].append(record_id)
                row["titles"].append(title)

        if failed_assets:
            findings.append(issue(record_id, title, "asset_download_failed", "high", f"failed_assets={failed_assets}/{len(assets)}"))
        if missing_asset_files:
            findings.append(issue(record_id, title, "asset_file_missing", "critical", f"missing_files={missing_asset_files}/{len(assets)}"))
        if sha_mismatch:
            findings.append(issue(record_id, title, "asset_sha_mismatch", "critical", f"sha_mismatch={sha_mismatch}/{len(assets)}"))

        for source in sources:
            if source.get("system") != "neris":
                continue
            local_file = source.get("local_file")
            if not local_file:
                continue
            raw_path = root / local_file
            if not raw_path.exists():
                continue
            raw = read_json(raw_path)
            ms = (((raw.get("source") or {}).get("list_summary") or {}).get("pub_date_ms"))
            if ms:
                utc_date = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
                if pub_date and pub_date != utc_date:
                    findings.append(issue(record_id, title, "neris_pub_date_timezone_mismatch", "high", f"canonical={pub_date} source_utc={utc_date}"))

        relations = incident.get(record_id) or []
        inventory.append(
            {
                "id": record_id,
                "title": title,
                "scope_hits": "|".join(item["_scope_hits"]),
                "document_type": item.get("document_type"),
                "lane": lane,
                "category": category,
                "effectiveness": status,
                "pub_date": metadata.get("pub_date"),
                "effective_date": metadata.get("effective_date"),
                "ineffective_date": metadata.get("ineffective_date"),
                "content_status": content_status,
                "text_chars": len(full_text),
                "source_systems": "|".join(source_systems),
                "page_url_count": len(page_urls),
                "official_page_url_count": len(official_page_urls),
                "asset_count": len(assets),
                "pdf_asset_count": pdf_assets,
                "failed_asset_count": failed_assets,
                "relation_count": len(relations),
                "superseded_by_count": len(item.get("superseded_by") or []),
                "canonical_path": item["_path"],
            }
        )

    same_instrument_pairs = {
        frozenset({edge.get("from"), edge.get("to")})
        for edge in edges
        if edge.get("relation") in {"same_instrument", "supersedes", "amends"}
    }
    duplicate_groups: list[dict[str, Any]] = []
    for normalized, ids in title_groups.items():
        if not normalized or len(ids) < 2:
            continue
        related_pairs = sum(
            frozenset({left, right}) in same_instrument_pairs
            for index, left in enumerate(ids)
            for right in ids[index + 1 :]
        )
        duplicate_groups.append(
            {"normalized_title": normalized, "ids": ids, "count": len(ids), "related_pairs": related_pairs}
        )
        if related_pairs == 0:
            for record_id in ids:
                title = all_items[record_id].get("title") or ""
                findings.append(issue(record_id, title, "unlinked_exact_title_duplicate", "high", f"group_size={len(ids)} ids={ids}"))

    urls = []
    for row in url_rows.values():
        row["record_ids"] = sorted(set(row["record_ids"]))
        row["titles"] = sorted(set(row["titles"]))
        urls.append(row)

    issue_by_id = Counter(row["id"] for row in findings)
    for row in inventory:
        row["issue_count"] = issue_by_id[row["id"]]

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(root),
        "as_of": as_of.isoformat(),
        "all_canonical_records": len(all_items),
        "private_compliance_candidates": len(candidates),
        "candidate_scope_hits": Counter(hit for item in candidates for hit in item["_scope_hits"]),
        "lane_counts": Counter(row["lane"] for row in inventory),
        "effectiveness_counts": Counter(row["effectiveness"] for row in inventory),
        "content_status_counts": Counter(row["content_status"] for row in inventory),
        "source_system_counts": Counter(
            source.get("system")
            for item in candidates
            for source in (item.get("sources") or [])
            if source.get("system")
        ),
        "finding_counts": Counter(row["code"] for row in findings),
        "severity_counts": Counter(row["severity"] for row in findings),
        "affected_records": len(issue_by_id),
        "duplicate_groups": len(duplicate_groups),
        "unique_urls": len(urls),
        "unique_source_page_urls": sum(row["kind"] == "source_page" for row in urls),
        "unique_asset_urls": sum(row["kind"] == "asset" for row in urls),
    }
    summary = json.loads(json.dumps(summary, ensure_ascii=False))

    write_json(out / "local_summary.json", summary)
    write_jsonl(out / "candidate_inventory.jsonl", inventory)
    write_csv(out / "candidate_inventory.csv", inventory)
    write_jsonl(out / "local_findings.jsonl", findings)
    write_csv(out / "local_findings.csv", findings)
    write_json(out / "duplicate_groups.json", duplicate_groups)
    write_jsonl(out / "urls.jsonl", urls)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


_thread_local = threading.local()


def session() -> requests.Session:
    value = getattr(_thread_local, "session", None)
    if value is None:
        value = requests.Session()
        value.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; CSRC-law-crawler-quality-audit/1.0)",
                "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
            }
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=1, pool_connections=4, pool_maxsize=4)
        value.mount("https://", adapter)
        value.mount("http://", adapter)
        _thread_local.session = value
    return value


def extract_live_page(url: str, max_bytes: int = 3_000_000) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"url": url, "checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        host = urlparse(url).hostname or ""
        # The official AMAC policy subdomain currently presents a certificate
        # chain that the runtime cannot validate.  Continue the content check
        # with verification disabled, but record that fact as audit evidence.
        verify_tls = host != "fg.amac.org.cn"
        result["tls_verification_bypassed"] = not verify_tls
        response_context = session().get(
            url,
            timeout=(10, 25),
            stream=True,
            allow_redirects=True,
            verify=verify_tls,
        )
        with response_context as response:
            result.update(
                {
                    "status_code": response.status_code,
                    "final_url": response.url,
                    "content_type": response.headers.get("content-type"),
                    "content_length": response.headers.get("content-length"),
                }
            )
            chunks = []
            size = 0
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
            body = b"".join(chunks)
            result["bytes_read"] = len(body)
            result["truncated"] = size >= max_bytes
            content_type = (response.headers.get("content-type") or "").lower()
            if "html" in content_type or body.lstrip().startswith(b"<"):
                encoding = response.encoding
                if not encoding or encoding.lower() in {"iso-8859-1", "ascii"}:
                    # response.apparent_encoding reads response.content, which is
                    # unavailable after a streamed body has been consumed.
                    encoding = "utf-8"
                html = body.decode(encoding, errors="replace")
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                headings = [
                    node.get_text(" ", strip=True)
                    for node in soup.select("h1, h2, h3, .title, .bt, .content-title")
                    if node.get_text(" ", strip=True)
                ]
                if soup.title and soup.title.get_text(" ", strip=True):
                    headings.insert(0, soup.title.get_text(" ", strip=True))
                text = soup.get_text("\n", strip=True)
                attachments = []
                for link in soup.select("a[href]"):
                    href = urljoin(response.url, link.get("href"))
                    path = urlparse(href).path.lower()
                    if re.search(r"\.(pdf|docx?|xlsx?|xls|zip|rar|txt|png|jpe?g)$", path) or "download" in path or "/files/" in path:
                        attachments.append(href)
                labeled_dates = []
                for pattern in (
                    r"(?:发文日期|发布日期|日期|时间)\s*[:：]?\s*(20\d{2})[-年]\s*(\d{1,2})[-月]\s*(\d{1,2})日?",
                    r"(?:实施日期|施行日期)\s*[:：]?\s*(20\d{2})[-年]\s*(\d{1,2})[-月]\s*(\d{1,2})日?",
                ):
                    for match in re.finditer(pattern, text[:30000]):
                        try:
                            labeled_dates.append(date(*map(int, match.groups())).isoformat())
                        except ValueError:
                            pass
                result.update(
                    {
                        "headings": headings[:20],
                        "text_chars": len(text),
                        "text_normalized": normalize_text(text)[:400000],
                        "labeled_dates": sorted(set(labeled_dates)),
                        "attachment_urls": sorted(set(attachments)),
                    }
                )
            elif "pdf" in content_type or body.startswith(b"%PDF"):
                result["is_pdf"] = True
                result["pdf_signature_ok"] = body.startswith(b"%PDF")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    time.sleep(random.uniform(0.08, 0.2))
    return result


def fetch_urls(out: Path, workers: int) -> None:
    rows = [json.loads(line) for line in (out / "urls.jsonl").read_text("utf-8").splitlines()]
    source_pages = [row for row in rows if row.get("kind") == "source_page" and row.get("official")]
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(extract_live_page, row["url"]): row for row in source_pages}
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            result["record_ids"] = futures[future]["record_ids"]
            result["titles"] = futures[future]["titles"]
            results.append(result)
            if index % 50 == 0:
                print(f"fetched {index}/{len(source_pages)}", flush=True)
    results.sort(key=lambda row: row["url"])
    write_jsonl(out / "url_results.jsonl", results)
    summary = {
        "checked": len(results),
        "reachable_2xx": sum(200 <= (row.get("status_code") or 0) < 300 for row in results),
        "redirect_or_other": sum(300 <= (row.get("status_code") or 0) < 400 for row in results),
        "http_error": sum((row.get("status_code") or 0) >= 400 for row in results),
        "request_error": sum(bool(row.get("error")) for row in results),
        "html_pages": sum("headings" in row for row in results),
        "pdf_pages": sum(bool(row.get("is_pdf")) for row in results),
    }
    write_json(out / "url_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def inspect_pdf(path: Path, expected_sha: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"local_file": str(path)}
    try:
        result["size_bytes"] = path.stat().st_size
        result["sha256"] = sha256_file(path)
        result["sha_match"] = not expected_sha or result["sha256"] == expected_sha
        reader = PdfReader(str(path), strict=False)
        result["pages"] = len(reader.pages)
        text_parts = []
        for page in reader.pages:
            try:
                text_parts.append(page.extract_text() or "")
            except Exception:
                text_parts.append("")
        text = "\n".join(text_parts)
        result["text_chars"] = len(normalize_text(text))
        result["text_sample"] = normalize_text(text)[:500]
        result["scan_likely"] = result["pages"] > 0 and result["text_chars"] < 30
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def pdf_audit(root: Path, out: Path, workers: int) -> None:
    _, candidates = load_corpus(root)
    path_meta: dict[Path, dict[str, Any]] = {}
    for item in candidates:
        for asset in item.get("assets") or []:
            local_file = asset.get("local_file")
            if not local_file:
                continue
            path = root / local_file
            if path.suffix.lower() != ".pdf" or not path.exists():
                continue
            meta = path_meta.setdefault(
                path,
                {"expected_sha": asset.get("sha256"), "record_ids": [], "titles": []},
            )
            meta["record_ids"].append(item["id"])
            meta["titles"].append(item.get("title") or "")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(inspect_pdf, path, meta.get("expected_sha")): (path, meta)
            for path, meta in path_meta.items()
        }
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            _, meta = futures[future]
            result["record_ids"] = sorted(set(meta["record_ids"]))
            result["titles"] = sorted(set(meta["titles"]))
            results.append(result)
            if index % 50 == 0:
                print(f"inspected PDFs {index}/{len(path_meta)}", flush=True)
    results.sort(key=lambda row: row["local_file"])
    write_jsonl(out / "pdf_results.jsonl", results)
    summary = {
        "pdf_files": len(results),
        "valid": sum(not row.get("error") for row in results),
        "invalid": sum(bool(row.get("error")) for row in results),
        "scan_likely": sum(bool(row.get("scan_likely")) for row in results),
        "sha_mismatch": sum(row.get("sha_match") is False for row in results),
        "pages": sum(row.get("pages") or 0 for row in results),
    }
    write_json(out / "pdf_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def reconcile(root: Path, out: Path) -> None:
    all_items, candidates = load_corpus(root)
    candidate_by_id = {item["id"]: item for item in candidates}
    url_results = [json.loads(line) for line in (out / "url_results.jsonl").read_text("utf-8").splitlines()]
    pdf_results = [json.loads(line) for line in (out / "pdf_results.jsonl").read_text("utf-8").splitlines()]
    findings = [json.loads(line) for line in (out / "local_findings.jsonl").read_text("utf-8").splitlines()]
    live_by_url = {row["url"]: row for row in url_results}
    pdf_by_record: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pdf_results:
        for record_id in row.get("record_ids") or []:
            pdf_by_record[record_id].append(row)

    comparison_rows = []
    for item in candidates:
        record_id = item["id"]
        title = item.get("title") or ""
        title_norm = normalize_title(title)
        metadata = item.get("metadata") or {}
        full_text = item.get("full_text_plain") or ""
        sources = item.get("sources") or []
        live_pages = [live_by_url[source.get("page_url")] for source in sources if source.get("page_url") in live_by_url]
        reachable = [row for row in live_pages if 200 <= (row.get("status_code") or 0) < 300]
        if live_pages and not reachable:
            findings.append(issue(record_id, title, "official_source_unreachable", "high", "; ".join(f"{row.get('status_code')} {row.get('error') or row.get('url')}" for row in live_pages)))

        best_title_score = None
        best_content_coverage = None
        official_dates: set[str] = set()
        live_attachment_count = 0
        for row in reachable:
            headings = row.get("headings") or []
            body_norm = row.get("text_normalized") or ""
            scores = []
            for heading in headings:
                heading_norm = normalize_title(heading)
                if not heading_norm or not title_norm:
                    continue
                common = sum(ch in heading_norm for ch in set(title_norm)) / max(1, len(set(title_norm)))
                exact = float(title_norm in heading_norm or heading_norm in title_norm)
                scores.append(max(common, exact))
            if title_norm and body_norm and title_norm in body_norm:
                scores.append(1.0)
            if scores:
                best_title_score = max(best_title_score or 0, max(scores))
            if full_text and body_norm:
                coverage = shingle_coverage(full_text, body_norm)
                if coverage is not None:
                    best_content_coverage = max(best_content_coverage or 0, coverage)
            official_dates.update(row.get("labeled_dates") or [])
            live_attachment_count = max(live_attachment_count, len(row.get("attachment_urls") or []))

        if reachable and best_title_score is not None and best_title_score < 0.65:
            findings.append(issue(record_id, title, "live_title_mismatch", "high", f"best_title_score={best_title_score:.3f}"))
        pub_date = parse_date(metadata.get("pub_date"))
        if pub_date and official_dates and pub_date.isoformat() not in official_dates:
            findings.append(issue(record_id, title, "live_pub_date_mismatch", "high", f"canonical={pub_date} labeled_dates={sorted(official_dates)}", 0.85))
        lane = (item.get("material_classification") or {}).get("lane")
        if lane == "rule" and reachable and best_content_coverage is not None and best_content_coverage < 0.5:
            findings.append(issue(record_id, title, "live_content_low_coverage", "high", f"canonical_to_live_coverage={best_content_coverage:.3f}", 0.8))

        pdfs = pdf_by_record.get(record_id) or []
        scan_pdfs = sum(bool(row.get("scan_likely")) for row in pdfs)
        if item.get("content_status") == "metadata_only" and scan_pdfs:
            findings.append(issue(record_id, title, "scan_pdf_without_searchable_text", "high" if lane == "rule" else "medium", f"scan_pdf_count={scan_pdfs}"))
        if any(row.get("error") for row in pdfs):
            findings.append(issue(record_id, title, "invalid_local_pdf", "critical", f"invalid_pdf_count={sum(bool(row.get('error')) for row in pdfs)}"))

        comparison_rows.append(
            {
                "id": record_id,
                "title": title,
                "live_pages": len(live_pages),
                "reachable_pages": len(reachable),
                "best_title_score": None if best_title_score is None else round(best_title_score, 4),
                "best_content_coverage": None if best_content_coverage is None else round(best_content_coverage, 4),
                "official_labeled_dates": "|".join(sorted(official_dates)),
                "live_attachment_count": live_attachment_count,
                "local_asset_count": len(item.get("assets") or []),
                "pdf_count": len(pdfs),
                "scan_pdf_count": scan_pdfs,
            }
        )

    unique_findings = []
    seen = set()
    for row in findings:
        key = (row["id"], row["code"], row["evidence"])
        if key not in seen:
            seen.add(key)
            unique_findings.append(row)
    write_jsonl(out / "all_findings.jsonl", unique_findings)
    write_csv(out / "all_findings.csv", unique_findings)
    write_jsonl(out / "live_comparisons.jsonl", comparison_rows)
    write_csv(out / "live_comparisons.csv", comparison_rows)
    summary = {
        "candidate_records": len(candidates),
        "finding_rows": len(unique_findings),
        "affected_records": len({row["id"] for row in unique_findings}),
        "severity_counts": Counter(row["severity"] for row in unique_findings),
        "finding_counts": Counter(row["code"] for row in unique_findings),
        "fully_reachable_records": sum(row["live_pages"] and row["live_pages"] == row["reachable_pages"] for row in comparison_rows),
        "records_with_live_pages": sum(bool(row["live_pages"]) for row in comparison_rows),
        "records_with_scan_pdfs": sum(bool(row["scan_pdf_count"]) for row in comparison_rows),
    }
    write_json(out / "reconciled_summary.json", json.loads(json.dumps(summary, ensure_ascii=False)))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["local", "fetch", "pdf", "reconcile"])
    parser.add_argument("--root", type=Path, default=Path("/mnt/d/FUND_COMPLIANCE/CSRC"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 7, 16))
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.mode == "local":
        local_audit(args.root, args.out, args.as_of)
    elif args.mode == "fetch":
        fetch_urls(args.out, args.workers)
    elif args.mode == "pdf":
        pdf_audit(args.root, args.out, args.workers)
    else:
        reconcile(args.root, args.out)


if __name__ == "__main__":
    main()
