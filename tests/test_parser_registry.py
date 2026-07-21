from __future__ import annotations

from pathlib import Path

import pytest

from app.ingestion.parser_registry import ParserRegistry
from app.ingestion.parsers.docx_parser import DocxParser
from app.ingestion.parsers.text_parser import TextParser


def test_default_registry_contains_all_upload_extensions():
    registry = ParserRegistry()

    assert set(registry.supported_file_types()) == {
        "bmp",
        "chat.jsonl",
        "csv",
        "docx",
        "eml",
        "gif",
        "htm",
        "html",
        "jpeg",
        "jpg",
        "jsonl",
        "markdown",
        "md",
        "pdf",
        "png",
        "text",
        "tif",
        "tiff",
        "txt",
        "webp",
        "xlsx",
    }


def test_registry_normalizes_extension_and_builds_parser():
    registry = ParserRegistry()

    assert isinstance(registry.get_parser(".DOCX"), DocxParser)
    assert isinstance(registry.get_parser("TXT"), TextParser)


def test_registry_parse_delegates_to_selected_parser(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_text("enterprise knowledge", encoding="utf-8")

    blocks = ParserRegistry().parse("txt", path)

    assert [block.text for block in blocks] == ["enterprise knowledge"]


def test_registry_reports_supported_types_for_unknown_extension():
    with pytest.raises(ValueError, match="supported types"):
        ParserRegistry().get_parser("exe")
