#!/usr/bin/env python3
"""Extract readable text from downloaded source assets."""

from __future__ import annotations

import io
import re
from pathlib import Path

from parser import repair_known_neris_mojibake


TEXT_ASSET_SUFFIXES = {".pdf", ".docx", ".txt"}


def _clean_extracted_text(text: str) -> str:
    text = repair_known_neris_mojibake(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _decode_text_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_asset_text_bytes(data: bytes, suffix: str) -> str:
    suffix = suffix.lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            return _clean_extracted_text(
                "\n\n".join((page.extract_text() or "") for page in reader.pages)
            )
        if suffix == ".docx":
            from docx import Document

            document = Document(io.BytesIO(data))
            return _clean_extracted_text(
                "\n".join(paragraph.text for paragraph in document.paragraphs)
            )
        if suffix == ".txt":
            return _clean_extracted_text(_decode_text_bytes(data))
    except Exception:
        return ""
    return ""


def extract_local_asset_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    suffix = path.suffix.lower()
    if suffix not in TEXT_ASSET_SUFFIXES:
        return ""
    return extract_asset_text_bytes(path.read_bytes(), suffix)
