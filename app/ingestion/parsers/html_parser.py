from __future__ import annotations

import base64
from html.parser import HTMLParser
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.ingestion.image_assets import ImageAsset
from app.ingestion.parsers.common import normalize_text
from app.ingestion.types import ParsedBlock


class _ReadableHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []
        self.image_sources: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "img":
            attributes = dict(attrs)
            self.image_sources.append((attributes.get("src", ""), attributes.get("alt")))
            if attributes.get("alt"):
                self._parts.append(attributes["alt"])
            return
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag in {"p", "br", "div", "section", "article", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in {"p", "div", "section", "article", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    def text(self) -> str:
        return normalize_text(" ".join(self._parts))


class HtmlParser:
    def parse(self, file_path: Path) -> list[ParsedBlock]:
        parser = _ReadableHtmlParser()
        parser.feed(file_path.read_text(encoding="utf-8", errors="ignore"))
        text = parser.text()
        blocks: list[ParsedBlock] = []
        if text:
            blocks.append(ParsedBlock(text=text, metadata={"parser": "html"}))

        assets = self._extract_local_images(parser.image_sources, file_path)
        if assets:
            from app.ingestion.parsers.image_parser import ImageParser

            try:
                blocks.extend(ImageParser().parse_assets(assets))
            except Exception:
                import logging

                logging.getLogger(__name__).exception("HTML image extraction failed: %s", file_path)
        return blocks

    @staticmethod
    def _extract_local_images(
        sources: list[tuple[str, str | None]],
        file_path: Path,
    ) -> list[ImageAsset]:
        assets: list[ImageAsset] = []
        root = file_path.parent.resolve()
        for source, alt in sources:
            if not source:
                continue
            content: bytes | None = None
            mime_type = "image/jpeg"
            if source.startswith("data:"):
                try:
                    header, encoded = source.split(",", 1)
                    mime_type = header[5:].split(";", 1)[0] or mime_type
                    content = base64.b64decode(encoded) if ";base64" in header else unquote(encoded).encode()
                except (ValueError, UnicodeError):
                    continue
            else:
                parsed = urlparse(source)
                # Remote URLs are intentionally not fetched from the ingestion worker.
                if parsed.scheme or parsed.netloc:
                    continue
                candidate = (file_path.parent / parsed.path).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    continue
                if not candidate.is_file():
                    continue
                content = candidate.read_bytes()
                mime_type = mimetypes.guess_type(candidate.name)[0] or mime_type
            if content:
                assets.append(
                    ImageAsset(
                        content=content,
                        source_name=f"{file_path.name}#image-{len(assets) + 1}",
                        mime_type=mime_type,
                        metadata={
                            "source_html": file_path.name,
                            "extraction_method": "html_img",
                            "alt": alt or "",
                        },
                    )
                )
        return assets
