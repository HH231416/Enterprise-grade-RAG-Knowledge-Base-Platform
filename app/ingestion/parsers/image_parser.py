from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import json
import logging
from pathlib import Path
import threading
from typing import List, Optional, Union

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.ingestion.parsers.common import normalize_text
from app.ingestion.image_assets import ImageAsset
from app.ingestion.types import ParsedBlock
from app.integrations.image_recognition import (
    ImageRecognitionProvider,
    MultimodalProvider,
    RecognitionResult,
    create_image_recognition_provider,
)
from app.integrations.image_caption import ImageCaptionProvider

logger = logging.getLogger(__name__)

# 支持的图片格式
SUPPORTED_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp"
}


def _run_async(coro):
    """在同步上下文中运行异步协程"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # 已有运行中的事件循环（如 FastAPI），用 nest_asyncio 或直接在线程中跑
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()
    else:
        return asyncio.run(coro)


class ImageParser:
    """图片解析器"""

    _cache_lock = threading.Lock()
    _recognition_cache: OrderedDict[str, RecognitionResult] = OrderedDict()
    _cache_max_entries = 2048

    def __init__(self, recognition_provider: Optional[ImageRecognitionProvider] = None, caption_provider: Optional[ImageCaptionProvider] = None):
        self.settings = get_settings()
        custom_provider = recognition_provider is not None or caption_provider is not None
        self.recognition_provider = recognition_provider or create_image_recognition_provider(self.settings)
        self.caption_provider = caption_provider or ImageCaptionProvider(self.settings)
        # Mocked providers must not share process-wide cached results between tests.
        self._cache_enabled = self.settings.image_cache_enabled and not custom_provider
        self._disk_cache_enabled = self._cache_enabled
        
        # 根据配置自动选择识别模式
        ocr_configured = self.recognition_provider.is_configured()
        caption_configured = self.caption_provider.is_configured()
        
        if isinstance(self.recognition_provider, MultimodalProvider) and ocr_configured:
            self.recognition_mode = "vision"
        elif ocr_configured and caption_configured:
            self.recognition_mode = "hybrid"
        elif ocr_configured:
            self.recognition_mode = "ocr"
        elif caption_configured:
            self.recognition_mode = "caption"
        else:
            self.recognition_mode = "none"
            logger.warning("Neither OCR nor caption provider is configured")

    def parse(self, file_path: Path) -> list[ParsedBlock]:
        """解析单个图片文件"""
        if not file_path.exists():
            raise AppError(
                code=ErrorCode.DOCUMENT_PARSE_FAILED,
                message=f"Image file not found: {file_path}",
                status_code=400,
            )

        # 检查文件格式
        if file_path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            raise AppError(
                code=ErrorCode.UNSUPPORTED_FILE_TYPE,
                message=f"Unsupported image format: {file_path.suffix}",
                status_code=400,
            )

        mime_type = self._mime_type_for_suffix(file_path.suffix)
        return self.parse_asset(
            ImageAsset(
                content=file_path.read_bytes(),
                source_name=file_path.name,
                mime_type=mime_type,
                page_no=1,
                metadata={"source_file": file_path.name, "file_type": "image"},
            )
        )

    def parse_asset(self, asset: ImageAsset) -> list[ParsedBlock]:
        """Recognize one extracted image while retaining its document location."""
        try:
            result = self._recognize_asset(asset)
            return self._process_recognition_result(result, asset)
        except AppError:
            raise
        except Exception as e:
            logger.exception("Image parsing failed for %s", asset.source_name)
            raise AppError(
                code=ErrorCode.DOCUMENT_PARSE_FAILED,
                message=f"Image parsing failed: {e}",
                status_code=500,
            )

    def parse_assets(self, assets: List[ImageAsset]) -> List[ParsedBlock]:
        """Parse extracted document images with routing and cache support."""
        blocks: list[ParsedBlock] = []
        for asset in assets:
            blocks.extend(self.parse_asset(asset))
        return blocks

    def _recognize_asset(self, asset: ImageAsset) -> RecognitionResult:
        image_hash = hashlib.sha256(asset.content).hexdigest()
        cache_key = self._cache_key(image_hash, asset.mime_type)
        cached = self._get_cached_result(cache_key)
        if cached is not None:
            cached.metadata = {**cached.metadata, "image_hash": image_hash, "cache_hit": True}
            return cached

        if self.recognition_mode == "vision":
            result = _run_async(
                self.recognition_provider.recognize_image(asset.content, mime_type=asset.mime_type)
            )
        elif self.recognition_mode in {"ocr", "hybrid"}:
            ocr_result = _run_async(
                self.recognition_provider.recognize_image(asset.content, mime_type=asset.mime_type)
            )
            result = ocr_result
            if self.recognition_mode == "hybrid" and self._should_request_caption(ocr_result):
                caption_text = _run_async(
                    self.caption_provider.caption(asset.content, mime_type=asset.mime_type)
                )
                result = RecognitionResult(
                    text=f"{ocr_result.text}\n\n[图片描述]\n{caption_text}".strip(),
                    confidence=ocr_result.confidence,
                    provider_name=ocr_result.provider_name,
                    model_name=ocr_result.model_name,
                    processing_time_ms=ocr_result.processing_time_ms,
                    metadata={**ocr_result.metadata, "caption": caption_text, "vlm_used": True},
                )
        elif self.recognition_mode == "caption":
            caption_text = _run_async(
                self.caption_provider.caption(asset.content, mime_type=asset.mime_type)
            )
            result = RecognitionResult(
                text=caption_text,
                confidence=1.0,
                provider_name="multimodal_caption",
                model_name=self.settings.multimodal_model or "unknown",
                metadata={"vlm_used": True},
            )
        else:
            raise AppError(
                code=ErrorCode.IMAGE_RECOGNITION_FAILED,
                message="图片识别服务未配置，请配置内网 OCR 或多模态 API",
                status_code=422,
            )

        result.metadata = {**result.metadata, "image_hash": image_hash, "cache_hit": False}
        self._cache_result(cache_key, result)
        return result

    def _should_request_caption(self, result: RecognitionResult) -> bool:
        return (
            result.confidence < self.settings.image_vlm_confidence_threshold
            or self._looks_like_chart(result)
        )

    @staticmethod
    def _looks_like_chart(result: RecognitionResult | str) -> bool:
        """Route visual documents from OCR layout signals or recognized text."""
        markers = ("流程图", "架构图", "组织图", "柱状图", "折线图", "饼图", "图表", "diagram", "chart", "graph")
        if isinstance(result, RecognitionResult):
            metadata = result.metadata or {}
            if any(metadata.get(key) is True for key in ("chart_detected", "has_chart", "contains_chart")):
                return True
            text = result.text
        else:
            text = result
        normalized = text.lower()
        return any(marker in normalized for marker in markers)

    def _cache_key(self, image_hash: str, mime_type: str) -> str:
        return "|".join(
            (
                "v2",
                image_hash,
                mime_type,
                self.recognition_mode,
                self.settings.image_recognition_provider,
                self.settings.paddle_ocr_api_base or "",
                self.settings.multimodal_api_base or "",
                self.settings.multimodal_model or "",
                str(self.settings.image_vlm_confidence_threshold),
            )
        )

    def _cache_file(self, key: str) -> Path:
        filename = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        return Path(self.settings.storage_root) / ".image_cache" / filename

    def _get_cached_result(self, key: str) -> RecognitionResult | None:
        if not self._cache_enabled:
            return None
        with self._cache_lock:
            result = self._recognition_cache.get(key)
            if result is not None:
                self._recognition_cache.move_to_end(key)
                return RecognitionResult(
                    text=result.text,
                    confidence=result.confidence,
                    provider_name=result.provider_name,
                    model_name=result.model_name,
                    processing_time_ms=result.processing_time_ms,
                    metadata=dict(result.metadata),
                )
            if not self._disk_cache_enabled:
                return None
            try:
                payload = json.loads(self._cache_file(key).read_text(encoding="utf-8"))
                result = RecognitionResult(
                    text=str(payload["text"]),
                    confidence=float(payload.get("confidence", 1.0)),
                    provider_name=str(payload.get("provider_name", "")),
                    model_name=str(payload.get("model_name", "")),
                    processing_time_ms=int(payload.get("processing_time_ms", 0)),
                    metadata=dict(payload.get("metadata") or {}),
                )
            except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                return None
            self._recognition_cache[key] = result
            self._recognition_cache.move_to_end(key)
            return RecognitionResult(
                text=result.text,
                confidence=result.confidence,
                provider_name=result.provider_name,
                model_name=result.model_name,
                processing_time_ms=result.processing_time_ms,
                metadata=dict(result.metadata),
            )

    def _cache_result(self, key: str, result: RecognitionResult) -> None:
        if not self._cache_enabled:
            return
        with self._cache_lock:
            self._recognition_cache[key] = RecognitionResult(
                text=result.text,
                confidence=result.confidence,
                provider_name=result.provider_name,
                model_name=result.model_name,
                processing_time_ms=result.processing_time_ms,
                metadata=dict(result.metadata),
            )
            self._recognition_cache.move_to_end(key)
            while len(self._recognition_cache) > self._cache_max_entries:
                self._recognition_cache.popitem(last=False)
            if not self._disk_cache_enabled:
                return
            try:
                cache_file = self._cache_file(key)
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                temp_file = cache_file.with_suffix(".tmp")
                temp_file.write_text(
                    json.dumps(
                        {
                            "text": result.text,
                            "confidence": result.confidence,
                            "provider_name": result.provider_name,
                            "model_name": result.model_name,
                            "processing_time_ms": result.processing_time_ms,
                            "metadata": result.metadata,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                temp_file.replace(cache_file)
            except (OSError, TypeError, ValueError):
                logger.warning("Unable to persist image recognition cache", exc_info=True)

    def parse_batch(self, images: List[Union[bytes, Path, str]]) -> List[ParsedBlock]:
        """批量解析图片文件"""
        if not images:
            return []

        try:
            # 批量调用识别服务
            results = _run_async(
                self.recognition_provider.recognize_batch(images)
            )

            # 处理所有结果
            all_blocks = []
            for image, result in zip(images, results):
                if isinstance(image, (str, Path)):
                    file_path = Path(image)
                else:
                    file_path = Path("image.jpg")  # 默认文件名
                blocks = self._process_recognition_result(result, file_path)
                all_blocks.extend(blocks)

            return all_blocks

        except Exception as e:
            if isinstance(e, AppError):
                raise
            logger.exception("Batch image parsing failed")
            raise AppError(
                code=ErrorCode.DOCUMENT_PARSE_FAILED,
                message=f"Batch image parsing failed: {e}",
                status_code=500,
            )

    def _process_recognition_result(
        self,
        result: RecognitionResult,
        file_path: Path | ImageAsset,
    ) -> List[ParsedBlock]:
        """处理识别结果，转换为ParsedBlock格式"""
        asset = file_path if isinstance(file_path, ImageAsset) else None
        source_path = Path(asset.source_name) if asset else file_path
        blocks = []

        # 标准化文本
        text = normalize_text(result.text)
        if not text:
            # 如果没有识别到文本，创建一个空块
            text = f"[图片内容识别为空: {source_path.name}]"

        # 检查是否包含表格
        if self._contains_table(text):
            # 如果是表格，尝试解析为表格格式
            table_blocks = self._parse_table_content(text, source_path, result, asset)
            blocks.extend(table_blocks)
        else:
            # 普通文本
            metadata = self._result_metadata(result, source_path, asset)

            blocks.append(
                ParsedBlock(
                    text=text,
                    page_no=(asset.page_no if asset and asset.page_no is not None else 1),
                    sheet_name=asset.sheet_name if asset else None,
                    metadata=metadata,
                )
            )

        return blocks

    def _result_metadata(
        self,
        result: RecognitionResult,
        file_path: Path,
        asset: ImageAsset | None,
    ) -> dict:
        metadata = {
            "parser": "image_recognition",
            "provider": result.provider_name,
            "model": result.model_name,
            "confidence": result.confidence,
            "processing_time_ms": result.processing_time_ms,
            "source_file": file_path.name,
            "file_type": "image",
            **(asset.metadata if asset else {}),
            **result.metadata,
        }
        if asset and asset.page_no is not None:
            metadata["page_no"] = asset.page_no
        if asset and asset.sheet_name:
            metadata["sheet_name"] = asset.sheet_name
        return metadata

    def _contains_table(self, text: str) -> bool:
        """检查文本是否包含表格"""
        lines = text.strip().split('\n')
        if len(lines) < 2:
            return False

        # 检查markdown表格语法
        for line in lines:
            if '|' in line and ('---' in line or '---' in line):
                return True

        # 检查竖线分隔的表格
        pipe_count = sum(1 for line in lines if '|' in line)
        if pipe_count >= 2:
            return True

        return False

    def _parse_table_content(
        self,
        text: str,
        file_path: Path,
        result: RecognitionResult,
        asset: ImageAsset | None = None,
    ) -> List[ParsedBlock]:
        """解析表格内容"""
        blocks = []

        # 尝试解析markdown表格
        table_data = self._parse_markdown_table(text)

        if table_data:
            # 成功解析为表格
            headers, rows = table_data

            metadata = {
                **self._result_metadata(result, file_path, asset),
                "chunk_type": "table",
                "headers": headers,
                "_table_rows": [
                    {
                        "row_number": i + 1,
                        "text": " | ".join(row),
                        "fields": dict(zip(headers, row)),
                        "group_key": [],
                    }
                    for i, row in enumerate(rows)
                ],
            }

            # 创建表格块
            table_text = self._format_table_text(headers, rows)
            blocks.append(
                ParsedBlock(
                    text=table_text,
                    page_no=(asset.page_no if asset and asset.page_no is not None else 1),
                    sheet_name=asset.sheet_name if asset else None,
                    metadata=metadata,
                )
            )
        else:
            # 无法解析为表格，作为普通文本处理
            metadata = self._result_metadata(result, file_path, asset)

            blocks.append(
                ParsedBlock(
                    text=text,
                    page_no=(asset.page_no if asset and asset.page_no is not None else 1),
                    sheet_name=asset.sheet_name if asset else None,
                    metadata=metadata,
                )
            )

        return blocks

    def _parse_markdown_table(self, text: str) -> Optional[tuple[List[str], List[List[str]]]]:
        """解析markdown表格"""
        lines = text.strip().split('\n')

        # 查找表格开始
        table_start = -1
        for i, line in enumerate(lines):
            if '|' in line and i + 1 < len(lines) and '---' in lines[i + 1]:
                table_start = i
                break

        if table_start == -1:
            return None

        # 解析表头
        header_line = lines[table_start]
        headers = [cell.strip() for cell in header_line.split('|') if cell.strip()]

        # 跳过分隔行
        data_start = table_start + 2

        # 解析数据行
        rows = []
        for i in range(data_start, len(lines)):
            line = lines[i].strip()
            if not line or '|' not in line:
                break

            row = [cell.strip() for cell in line.split('|') if cell.strip()]
            if len(row) == len(headers):
                rows.append(row)

        if not rows:
            return None

        return headers, rows

    def _format_table_text(self, headers: List[str], rows: List[List[str]]) -> str:
        """格式化表格为文本"""
        lines = []

        # 表头
        lines.append(" | ".join(headers))
        lines.append(" | ".join(["---"] * len(headers)))

        # 数据行
        for row in rows:
            lines.append(" | ".join(row))

        return "\n".join(lines)

    @staticmethod
    def _mime_type_for_suffix(suffix: str) -> str:
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".tiff": "image/tiff",
            ".tif": "image/tiff",
            ".webp": "image/webp",
        }.get(suffix.lower(), "image/jpeg")
