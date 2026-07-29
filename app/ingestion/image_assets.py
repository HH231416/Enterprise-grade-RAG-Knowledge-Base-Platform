from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ImageAsset:
    """An extracted image plus the document location it came from."""

    content: bytes
    source_name: str
    mime_type: str = "image/jpeg"
    page_no: int | None = None
    sheet_name: str | None = None
    metadata: dict = field(default_factory=dict)

