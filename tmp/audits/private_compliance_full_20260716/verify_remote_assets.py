#!/usr/bin/env python3
"""Read-only remote-asset verification for the private-compliance audit."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def download(url: str) -> dict:
    host = urlsplit(url).hostname or ""
    verify = host != "fg.amac.org.cn"
    result = {"url": url, "tls_verification_bypassed": not verify}
    try:
        response = requests.get(url, timeout=(10, 60), verify=verify, allow_redirects=True)
        body = response.content
        result.update(
            {
                "status_code": response.status_code,
                "final_url": response.url,
                "content_type": response.headers.get("content-type") or "",
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "pdf_signature_ok": body.startswith(b"%PDF"),
            }
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--urls", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    inventory = list(csv.DictReader(args.inventory.open(encoding="utf-8-sig")))
    asset_meta: dict[str, list[dict]] = defaultdict(list)
    for row in inventory:
        item = read_json(Path(row["canonical_path"]))
        for asset in item.get("assets") or []:
            urls = asset.get("source_urls") or [asset.get("source_url")]
            for url in urls:
                if not url:
                    continue
                asset_meta[url].append(
                    {
                        "id": item["id"],
                        "title": item.get("title") or "",
                        "expected_sha": asset.get("sha256") or "",
                        "download_status": asset.get("download_status") or "",
                        "local_file": asset.get("local_file") or "",
                    }
                )

    url_rows = [json.loads(line) for line in args.urls.read_text(encoding="utf-8").splitlines()]
    urls = sorted({row["url"] for row in url_rows if row.get("kind") == "asset" and row.get("official")})
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(download, url): url for url in urls}
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            metas = asset_meta.get(row["url"]) or []
            expected = {meta["expected_sha"] for meta in metas if meta["expected_sha"]}
            row.update(
                {
                    "record_ids": "|".join(sorted({meta["id"] for meta in metas})),
                    "titles": "|".join(sorted({meta["title"] for meta in metas})),
                    "local_download_statuses": "|".join(sorted({meta["download_status"] for meta in metas})),
                    "expected_sha_count": len(expected),
                    "remote_sha_matches_expected": bool(expected and row.get("sha256") in expected),
                }
            )
            results.append(row)
    results.sort(key=lambda row: row["url"])
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out / "remote_asset_results.csv", results)
    (args.out / "remote_asset_results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results),
        encoding="utf-8",
    )
    summary = {
        "remote_asset_urls": len(results),
        "reachable_2xx": sum(200 <= (row.get("status_code") or 0) < 300 for row in results),
        "request_errors": sum(bool(row.get("error")) for row in results),
        "remote_sha_matches_expected": sum(bool(row.get("remote_sha_matches_expected")) for row in results),
        "without_expected_sha": sum(not row.get("expected_sha_count") for row in results),
        "pdf_urls": sum(urlsplit(row["url"]).path.lower().endswith(".pdf") for row in results),
        "pdf_signature_failures": sum(
            urlsplit(row["url"]).path.lower().endswith(".pdf") and not row.get("pdf_signature_ok")
            for row in results
        ),
        "local_download_statuses": Counter(
            status
            for row in results
            for status in row.get("local_download_statuses", "").split("|")
            if status
        ),
    }
    (args.out / "remote_asset_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
