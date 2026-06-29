#!/usr/bin/env python3
"""Extract readable text from downloaded source assets."""

from __future__ import annotations

import io
import re
from pathlib import Path

from parser import repair_known_neris_mojibake


TEXT_ASSET_SUFFIXES = {".pdf", ".docx", ".txt"}
PAGE_NUMBER_LINE_RE = re.compile(r"\d{1,4}")


def _clean_extracted_text(text: str) -> str:
    text = repair_known_neris_mojibake(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    lines = _strip_sequential_page_numbers(lines)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _strip_sequential_page_numbers(lines: list[str]) -> list[str]:
    candidates = [
        (index, int(line))
        for index, line in enumerate(lines)
        if PAGE_NUMBER_LINE_RE.fullmatch(line)
    ]
    if not candidates:
        return lines

    remove_indexes: set[int] = set()
    run: list[tuple[int, int]] = []
    for candidate in candidates:
        if run and candidate[1] == run[-1][1] + 1:
            run.append(candidate)
        else:
            _collect_page_number_run(run, remove_indexes)
            run = [candidate]
    _collect_page_number_run(run, remove_indexes)

    if not remove_indexes:
        return lines
    return [line for index, line in enumerate(lines) if index not in remove_indexes]


def _collect_page_number_run(
    run: list[tuple[int, int]],
    remove_indexes: set[int],
) -> None:
    if not run:
        return
    first_index, first_value = run[0]
    if first_value > 2:
        return
    if len(run) >= 3 or (len(run) >= 2 and first_index == 0):
        remove_indexes.update(index for index, _value in run)


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
