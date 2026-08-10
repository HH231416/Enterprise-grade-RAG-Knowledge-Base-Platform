from __future__ import annotations

from dataclasses import dataclass, field


def _json_safe(value):
    """将任意值递归转换为 JSON 可序列化形式（兜底转字符串）。

    解析器元数据可能包含非 JSON 类型（如 bytes、枚举），
    持久化 parsed_blocks 快照时统一转换，避免落库失败。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {key: _json_safe(v) for key, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


@dataclass(slots=True)
class ParsedBlock:
    text: str
    chunk_type: str = "text"
    section_title: str | None = None
    page_no: int | None = None
    sheet_name: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为 JSON 可存储的字典（待审核阶段持久化快照）。"""
        return {
            "text": self.text,
            "chunk_type": self.chunk_type,
            "section_title": self.section_title,
            "page_no": self.page_no,
            "sheet_name": self.sheet_name,
            "row_start": self.row_start,
            "row_end": self.row_end,
            "metadata": _json_safe(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> ParsedBlock:
        """从 to_dict() 的结果还原 ParsedBlock。"""
        return cls(
            text=data.get("text", ""),
            chunk_type=data.get("chunk_type", "text"),
            section_title=data.get("section_title"),
            page_no=data.get("page_no"),
            sheet_name=data.get("sheet_name"),
            row_start=data.get("row_start"),
            row_end=data.get("row_end"),
            metadata=data.get("metadata") or {},
        )


@dataclass(slots=True)
class ChunkingOptions:
    strategy: str = "fixed"
    max_chars: int = 1200
    overlap_chars: int = 150
    table_rows_per_chunk: int = 20
    parent_max_chars: int = 3000
    child_max_chars: int = 600
    min_chunk_sentences: int = 3
    max_chunk_sentences: int = 20
    similarity_threshold: float = 0.5
    merge_window: int = 3


@dataclass(slots=True)
class ChunkPayload:
    chunk_index: int
    chunk_text: str
    chunk_type: str
    section_title: str | None
    page_no: int | None
    sheet_name: str | None
    row_start: int | None
    row_end: int | None
    token_count: int
    char_count: int
    metadata_json: dict
    parent_chunk_uuid: str | None = None
    chunk_group_uuid: str | None = None
    chunk_level: str | None = None
    context_text: str | None = None
