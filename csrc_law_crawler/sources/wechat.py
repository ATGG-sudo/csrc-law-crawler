"""Manual importer for a JSON + HTML wechat-article-exporter bundle."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from runtime import utc_now_iso
from storage import append_jsonl, load_json, output_dir, save_json

from .evidence import record_fingerprints
from .registry import load_registry


JSON_FILE_NAME = "微信公众号文章.json"
COMMENT_MARKER = "<!-- 评论数据 -->"
METRIC_FIELDS = {"readNum", "oldLikeNum", "shareNum", "likeNum", "commentNum"}


def _article_id(fakeid: str, aid: str) -> str:
    return hashlib.sha256(f"{fakeid}:{aid}".encode("utf-8")).hexdigest()


def _batch_id(input_dir: Path, articles: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    paths = [input_dir / JSON_FILE_NAME]
    paths.extend(input_dir / str(item["aid"]) / "index.html" for item in articles)
    for path in sorted(paths):
        relative = path.relative_to(input_dir).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _bundle_file_hashes(input_dir: Path, articles: list[dict[str, Any]]) -> list[dict[str, str]]:
    paths = [input_dir / JSON_FILE_NAME]
    paths.extend(input_dir / str(item["aid"]) / "index.html" for item in articles)
    return [
        {
            "file": path.relative_to(input_dir).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(paths)
    ]


def _has_json_comments(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, str)):
        return bool(value)
    return True


def _html_comment_text(html: str) -> str:
    if COMMENT_MARKER not in html:
        return ""
    tail = html.partition(COMMENT_MARKER)[2]
    soup = BeautifulSoup(tail, "html.parser")
    for node in soup.select("script,style,noscript"):
        node.decompose()
    return soup.get_text(" ", strip=True)


def _sanitize_article_html(html: str) -> tuple[str, str, list[str]]:
    before_comments = html.partition(COMMENT_MARKER)[0]
    soup = BeautifulSoup(before_comments, "html.parser")
    for node in soup.select("script,style,noscript,iframe,object,embed,form"):
        node.decompose()
    content = soup.select_one("#js_content") or soup.find("article") or soup.body or soup
    links: list[str] = []
    for anchor in content.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if urlsplit(href).scheme in {"http", "https"} and href not in links:
            links.append(href)
    return str(content), content.get_text("\n", strip=True), links


def _official_links(links: list[str], registry: dict[str, Any]) -> list[str]:
    hosts = {
        (urlsplit(endpoint["url"]).hostname or "").lower() for endpoint in registry["endpoints"]
    }
    return [link for link in links if (urlsplit(link).hostname or "").lower() in hosts]


def _published_at(article: dict[str, Any]) -> str | None:
    raw = article.get("update_time") or article.get("create_time")
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _archive_bundle(input_dir: Path, archive_dir: Path, articles: list[dict[str, Any]]) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(input_dir / JSON_FILE_NAME, archive_dir / JSON_FILE_NAME)
    for article in articles:
        aid = str(article["aid"])
        source = input_dir / aid
        target = archive_dir / aid
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)


def import_wechat_bundle(
    input_dir: Path,
    *,
    root: Path | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output_root = root or output_dir()
    source_registry = registry or load_registry()
    config = source_registry.get("wechat", {}).get("wechat_jixiaolv") or {}
    json_path = input_dir / JSON_FILE_NAME
    if not json_path.is_file():
        raise FileNotFoundError(f"missing {JSON_FILE_NAME}: {json_path}")
    articles = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(articles, list) or not articles:
        raise ValueError("wechat JSON must contain a non-empty article array")

    fakeids: set[str] = set()
    warnings: list[str] = []
    for article in articles:
        if not isinstance(article, dict):
            raise ValueError("wechat article must be an object")
        aid = str(article.get("aid") or "").strip()
        fakeid = str(article.get("fakeid") or "").strip()
        if not aid or not fakeid or not str(article.get("content") or "").strip():
            raise ValueError("each wechat article requires aid, fakeid, and exported content")
        fakeids.add(fakeid)
        html_path = input_dir / aid / "index.html"
        if not html_path.is_file():
            raise FileNotFoundError(f"missing HTML export: {html_path}")
        if _has_json_comments(article.get("comments")):
            raise ValueError(f"comments must be disabled in JSON export: aid={aid}")
        html = html_path.read_text(encoding="utf-8")
        if _html_comment_text(html):
            raise ValueError(f"comments must be disabled in HTML export: aid={aid}")
        account_name = str(article.get("_accountName") or "").strip()
        if account_name and account_name != config.get("account_name"):
            warnings.append(f"aid={aid} account name is {account_name!r}")
    if len(fakeids) != 1:
        raise ValueError("wechat import contains multiple fakeids")
    detected_fakeid = next(iter(fakeids))
    expected_fakeid = str(config.get("expected_fakeid") or "").strip()
    if not expected_fakeid:
        report = {
            "schema_version": 1,
            "status": "incomplete",
            "reason": "expected_fakeid_not_configured",
            "detected_fakeid": detected_fakeid,
            "article_count": len(articles),
            "generated_at": utc_now_iso(),
        }
        save_json(output_root / "reports" / "wechat_imports" / "fakeid_candidate.json", report)
        return report
    if detected_fakeid != expected_fakeid:
        raise ValueError(
            f"wechat fakeid mismatch: expected {expected_fakeid}, got {detected_fakeid}"
        )

    batch_id = _batch_id(input_dir, articles)
    archive_dir = output_root / "raw" / "wechat" / "imports" / batch_id
    _archive_bundle(input_dir, archive_dir, articles)
    changes_path = output_root / "work" / "changes" / f"wechat_{batch_id}.jsonl"
    official_link_items: list[dict[str, Any]] = []
    written = 0
    for article in articles:
        aid = str(article["aid"])
        html_bytes = (input_dir / aid / "index.html").read_bytes()
        html = html_bytes.decode("utf-8")
        safe_html, html_text, links = _sanitize_article_html(html)
        plain_text = str(article.get("content") or "").strip() or html_text
        record_id = _article_id(detected_fakeid, aid)
        metadata = {
            "name": article.get("title"),
            "publisher": config.get("account_name"),
            "author": article.get("author_name"),
            "digest": article.get("digest"),
            "pub_date": _published_at(article),
            "document_type": "wechat_clue",
        }
        raw_article = {
            key: value
            for key, value in article.items()
            if key not in METRIC_FIELDS and key != "comments"
        }
        response_bytes = (
            json.dumps(raw_article, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\0"
            + html_bytes
        )
        fingerprints = record_fingerprints(
            metadata=metadata,
            plain_text=plain_text,
            assets=[],
            response_bytes=response_bytes,
        )
        raw_html = archive_dir / aid / "index.html"
        raw_json = archive_dir / JSON_FILE_NAME
        record = {
            "schema_version": 1,
            "source_record_id": record_id,
            "source_system": "wechat",
            "ingest_status": "complete",
            "material_lane": "clue",
            "metadata": metadata,
            "content": {"plain_text": plain_text, "html": safe_html},
            "source": {
                "endpoint_id": "wechat_jixiaolv",
                "page_url": article.get("link"),
                "raw_file": str(raw_html.relative_to(output_root)),
                "raw_json": str(raw_json.relative_to(output_root)),
                "fakeid": detected_fakeid,
                "aid": aid,
                "batch_id": batch_id,
                "official_links": _official_links(links, source_registry),
                "fetched_at": utc_now_iso(),
            },
            "discovery_evidence": [
                {
                    "endpoint_id": "wechat_jixiaolv",
                    "batch_id": batch_id,
                    "aid": aid,
                }
            ],
            "assets": [],
            "attachment_documents": [],
            "fingerprints": fingerprints,
        }
        record_path = output_root / "raw" / "sources" / "records" / "wechat" / f"{record_id}.json"
        previous = load_json(record_path, None)
        save_json(record_path, record)
        written += 1
        if previous:
            old = previous.get("fingerprints") or {}
            if old.get("content_sha256") != fingerprints["content_sha256"]:
                change_type = "content_changed"
            elif old.get("metadata_sha256") != fingerprints["metadata_sha256"]:
                change_type = "metadata_changed"
            else:
                change_type = None
            if change_type:
                append_jsonl(
                    changes_path,
                    {
                        "schema_version": 1,
                        "endpoint_id": "wechat_jixiaolv",
                        "source_record_id": record_id,
                        "change_type": change_type,
                        "detected_at": utc_now_iso(),
                    },
                )
        for link in record["source"]["official_links"]:
            official_link_items.append(
                {
                    "source_record_id": record_id,
                    "article_title": metadata["name"],
                    "official_url": link,
                }
            )

    manifest = {
        "schema_version": 1,
        "batch_id": batch_id,
        "exporter_commit": config.get("exporter_commit"),
        "account_name": config.get("account_name"),
        "fakeid": detected_fakeid,
        "article_count": len(articles),
        "json_file": JSON_FILE_NAME,
        "html_dir_pattern": "${aid}",
        "comments_expected": False,
        "files": _bundle_file_hashes(input_dir, articles),
        "imported_at": utc_now_iso(),
    }
    save_json(archive_dir / "import_manifest.json", manifest)
    report = {
        **manifest,
        "status": "complete",
        "written": written,
        "warnings": warnings,
        "official_link_count": len(official_link_items),
    }
    reports_dir = output_root / "reports" / "wechat_imports"
    save_json(reports_dir / f"{batch_id}.json", report)
    save_json(
        reports_dir / f"{batch_id}_official_links.json",
        {"schema_version": 1, "items": official_link_items},
    )
    return report


__all__ = ["JSON_FILE_NAME", "import_wechat_bundle"]
