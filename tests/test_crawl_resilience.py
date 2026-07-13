from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

import requests
from requests.exceptions import InvalidSchema

from config import LAW_TYPE_REGULATION
from client import HumanLikeClient
from crawl import crawl_type
from csrc_law_crawler.core.io import save_bytes
from csrc_law_crawler.sources.amac.client import AmacClient
from neris_attachments import update_law_attachments
from storage import attachment_index_path, manifest_path, reg_file_path, save_json


LAW_ID = "law-1"


def _list_page() -> dict[str, object]:
    return {
        "pageUtil": {
            "rowCount": 1,
            "pageList": [
                {
                    "secFutrsLawId": LAW_ID,
                    "secFutrsLawName": "规则一",
                    "fileno": "一号",
                }
            ],
        }
    }


def _law_document() -> dict[str, object]:
    return {
        "metadata": {"id": LAW_ID, "name": "规则一"},
        "entries": [],
        "full_text": "规则一正文",
        "source": {"crawled_at": "2026-07-13T00:00:00+00:00"},
    }


class CrawlResilienceTests(unittest.TestCase):
    def test_existing_document_repairs_checkpoint_and_manifest_without_refetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "storage.OUTPUT_DIR", Path(temp_dir)
        ), patch("crawl.fetch_list_page", return_value=_list_page()), patch(
            "crawl.fetch_law_detail",
            side_effect=AssertionError("existing正文不应重抓"),
        ):
            save_json(reg_file_path(LAW_ID), _law_document())
            checkpoint: dict[str, Any] = {
                "completed_ids": {"regulations": [], "writs": []}
            }

            result = crawl_type(
                object(),  # type: ignore[arg-type]
                LAW_TYPE_REGULATION,
                checkpoint,
                fetch_attachments=False,
            )

            self.assertEqual("complete", result["status"])
            self.assertEqual([LAW_ID], checkpoint["completed_ids"]["regulations"])
            manifest = json.loads(manifest_path().read_text(encoding="utf-8"))
            self.assertEqual([LAW_ID], [item["id"] for item in manifest["items"]])

    def test_attachment_failure_resume_reuses_saved_document(self) -> None:
        detail = {
            "lawlist": {
                "law": {"secFutrsLawId": LAW_ID, "secFutrsLawName": "规则一"},
                "lawEntryVOs": [],
            }
        }
        checkpoint: dict[str, Any] = {
            "completed_ids": {"regulations": [], "writs": []}
        }

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "storage.OUTPUT_DIR", Path(temp_dir)
        ), patch("crawl.fetch_list_page", return_value=_list_page()), patch(
            "crawl.fetch_law_detail", return_value=detail
        ) as fetch_detail, patch(
            "crawl.update_law_attachments", side_effect=RuntimeError("attachment timeout")
        ):
            first = crawl_type(
                object(),  # type: ignore[arg-type]
                LAW_TYPE_REGULATION,
                checkpoint,
            )
            self.assertEqual("incomplete", first["status"])
            self.assertTrue(reg_file_path(LAW_ID).exists())
            self.assertEqual(1, fetch_detail.call_count)

            with patch(
                "crawl.fetch_law_detail",
                side_effect=AssertionError("恢复时不应重复抓正文"),
            ), patch("crawl.update_law_attachments", return_value=[]):
                second = crawl_type(
                    object(),  # type: ignore[arg-type]
                    LAW_TYPE_REGULATION,
                    checkpoint,
                )

            self.assertEqual("complete", second["status"])
            self.assertEqual([LAW_ID], checkpoint["completed_ids"]["regulations"])
            self.assertEqual([], checkpoint["failures"])
            self.assertFalse(
                (Path(temp_dir) / "reports" / "crawl_regulations_failures.json").exists()
            )

    def test_atomic_binary_write_preserves_previous_file_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "asset.pdf"
            path.write_bytes(b"old")

            with patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    save_bytes(path, b"new")

            self.assertEqual(b"old", path.read_bytes())
            self.assertFalse(path.with_suffix(".pdf.tmp").exists())

    def test_corrupt_neris_attachment_is_downloaded_again(self) -> None:
        good_data = b"%PDF-good"

        class Payload:
            data = good_data
            content_type = "application/pdf"
            size_bytes = len(good_data)
            sha256 = hashlib.sha256(good_data).hexdigest()

        class Client:
            calls = 0

            def get_binary_payload(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.calls += 1
                return Payload()

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "storage.OUTPUT_DIR", Path(temp_dir)
        ):
            save_json(reg_file_path(LAW_ID), _law_document())
            local_path = Path(temp_dir) / "raw/assets/neris_attachments/law-1/a1.pdf"
            local_path.parent.mkdir(parents=True)
            local_path.write_bytes(b"%PDF-bad!")
            previous = {
                "attachment_id": "a1",
                "name": "附件",
                "source_url": "https://example.test/a1",
                "local_file": "raw/assets/neris_attachments/law-1/a1.pdf",
                "content_type": "application/pdf",
                "size_bytes": len(good_data),
                "sha256": hashlib.sha256(b"different").hexdigest(),
                "download_status": "ok",
            }
            save_json(
                attachment_index_path(LAW_ID),
                {"attachments": [previous]},
            )
            client = Client()
            with patch(
                "neris_attachments.discover_attachments",
                return_value=[
                    {
                        "attachment_id": "a1",
                        "name": "附件",
                        "source_url": "https://example.test/a1",
                        "local_file": None,
                        "content_type": None,
                        "size_bytes": None,
                        "sha256": None,
                        "download_status": "pending",
                        "raw": {},
                    }
                ],
            ):
                items = update_law_attachments(
                    client,  # type: ignore[arg-type]
                    LAW_ID,
                    download=True,
                )

            self.assertEqual(1, client.calls)
            self.assertEqual("ok", items[0]["download_status"])
            self.assertEqual(good_data, local_path.read_bytes())

    def test_amac_client_does_not_retry_404(self) -> None:
        response = requests.Response()
        response.status_code = 404
        response.url = "https://www.amac.org.cn/missing"

        class Session:
            headers: dict[str, str] = {}
            calls = 0

            def get(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.calls += 1
                return response

        client = AmacClient(delay_min=0, delay_max=0)
        session = Session()
        client.session = session  # type: ignore[assignment]

        with patch("csrc_law_crawler.sources.amac.client.time.sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                client.get(response.url)

        self.assertEqual(1, session.calls)
        sleep.assert_not_called()

    def test_neris_binary_stream_is_not_read_before_size_guard(self) -> None:
        class Response:
            status_code = 200
            headers = {"Content-Type": "application/pdf"}

            @property
            def text(self) -> str:
                raise AssertionError("stream body must not be decoded before guarded read")

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size: int):
                yield b"%PDF-data"

        class Session:
            headers: dict[str, str] = {}

            def get(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return Response()

        client = HumanLikeClient(delay_min=0, delay_max=0, batch_size=0)
        client.session = Session()  # type: ignore[assignment]

        payload = client.get_binary_payload("https://example.test/a.pdf")

        self.assertEqual(b"%PDF-data", payload.data)

    def test_neris_binary_failure_preserves_empty_response_reason(self) -> None:
        class Response:
            status_code = 200
            headers: dict[str, str] = {}

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size: int):
                return iter(())

        class Session:
            headers: dict[str, str] = {}

            def get(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return Response()

        client = HumanLikeClient(delay_min=0, delay_max=0, batch_size=0)
        client.session = Session()  # type: ignore[assignment]

        with patch("client.MAX_RETRIES", 1):
            with self.assertRaisesRegex(RuntimeError, "empty attachment response"):
                client.get_binary_payload("https://example.test/empty.pdf")

    def test_neris_client_does_not_retry_404(self) -> None:
        response = requests.Response()
        response.status_code = 404
        response.url = "https://neris.csrc.gov.cn/missing"
        response._content = b"missing"

        class Session:
            headers: dict[str, str] = {}
            calls = 0

            def get(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.calls += 1
                return response

        client = HumanLikeClient(delay_min=0, delay_max=0, batch_size=0)
        session = Session()
        client.session = session  # type: ignore[assignment]

        with patch("client.time.sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                client.get_html(response.url)

        self.assertEqual(1, session.calls)
        sleep.assert_not_called()

    def test_neris_client_does_not_retry_invalid_url(self) -> None:
        class Session:
            headers: dict[str, str] = {}
            calls = 0

            def get(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.calls += 1
                raise InvalidSchema("unsupported URL")

        client = HumanLikeClient(delay_min=0, delay_max=0, batch_size=0)
        session = Session()
        client.session = session  # type: ignore[assignment]

        with patch("client.time.sleep") as sleep:
            with self.assertRaises(InvalidSchema):
                client.get_binary_payload("file:///tmp/missing.jpg")

        self.assertEqual(1, session.calls)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
