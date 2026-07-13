#!/usr/bin/env python3
"""Extract readable text from downloaded source assets."""

from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

from parser import repair_known_neris_mojibake


TEXT_ASSET_SUFFIXES = {".doc", ".docx", ".pdf", ".txt", ".xlsx"}
PAGE_NUMBER_LINE_RE = re.compile(r"\d{1,4}")
logging.getLogger("pypdf").setLevel(logging.ERROR)


class AssetTextExtractionTimeout(TimeoutError):
    """Raised when a caller-imposed extraction deadline expires."""


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


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(element: ET.Element) -> str:
    return "".join(node.text or "" for node in element.iter() if _xml_local_name(node.tag) == "t")


def _extract_xlsx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as workbook:
        names = set(workbook.namelist())
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared_strings = [
                _xml_text(item) for item in root.iter() if _xml_local_name(item.tag) == "si"
            ]

        rows: list[str] = []
        sheets = sorted(
            name for name in names if name.startswith("xl/worksheets/") and name.endswith(".xml")
        )
        for sheet in sheets:
            root = ET.fromstring(workbook.read(sheet))
            for row in root.iter():
                if _xml_local_name(row.tag) != "row":
                    continue
                values: list[str] = []
                for cell in row:
                    if _xml_local_name(cell.tag) != "c":
                        continue
                    cell_type = cell.attrib.get("t")
                    if cell_type == "inlineStr":
                        value = _xml_text(cell)
                    else:
                        value = next(
                            (
                                node.text or ""
                                for node in cell.iter()
                                if _xml_local_name(node.tag) == "v"
                            ),
                            "",
                        )
                        if cell_type == "s" and value.isdigit():
                            index = int(value)
                            value = shared_strings[index] if index < len(shared_strings) else ""
                    if value.strip():
                        values.append(value.strip())
                if values:
                    rows.append(" ".join(values))
        return _clean_extracted_text("\n".join(rows))


def _extract_doc_with_command(path: Path, command: str) -> str:
    executable = shutil.which(command)
    if not executable:
        return ""
    try:
        result = subprocess.run(
            [executable, str(path)],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except AssetTextExtractionTimeout:
        raise
    except Exception:
        return ""
    if result.returncode != 0 or not result.stdout:
        return ""
    return _clean_extracted_text(_decode_text_bytes(result.stdout))


def _extract_doc_with_soffice(path: Path) -> str:
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if not executable:
        return ""
    with tempfile.TemporaryDirectory() as temp:
        out_dir = Path(temp)
        try:
            result = subprocess.run(
                [
                    executable,
                    "--headless",
                    "--convert-to",
                    "txt:Text",
                    "--outdir",
                    str(out_dir),
                    str(path),
                ],
                capture_output=True,
                check=False,
                timeout=120,
            )
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        text_files = sorted(out_dir.glob("*.txt"))
        if not text_files:
            return ""
        return _clean_extracted_text(_decode_text_bytes(text_files[0].read_bytes()))


def _extract_doc_text(path: Path) -> str:
    for command in ("antiword", "catdoc"):
        text = _extract_doc_with_command(path, command)
        if text:
            return text
    return _extract_doc_with_soffice(path)


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
        if suffix == ".xlsx":
            return _extract_xlsx_text(data)
        if suffix == ".doc":
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "asset.doc"
                path.write_bytes(data)
                return _extract_doc_text(path)
    except Exception:
        return ""
    return ""


def extract_local_asset_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    suffix = path.suffix.lower()
    if suffix not in TEXT_ASSET_SUFFIXES:
        return ""
    if suffix == ".doc":
        return _extract_doc_text(path)
    return extract_asset_text_bytes(path.read_bytes(), suffix)
