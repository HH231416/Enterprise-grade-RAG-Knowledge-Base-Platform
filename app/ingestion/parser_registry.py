# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

# Callable 泛型：用于定义解析器工厂函数的类型签名
from collections.abc import Callable
from pathlib import Path
from typing import Any

# 文档解析器抽象基类
from app.ingestion.parsers.base import DocumentParser
# 解析后的结构化块类型
from app.ingestion.types import ParsedBlock

# 解析器工厂类型别名：一个可接收任意关键字参数并返回 DocumentParser 实例的可调用对象
ParserFactory = Callable[..., DocumentParser]


class ParserRegistry:
    """解析器注册表：根据文件扩展名解析并实例化对应的文档解析器（策略模式 + 工厂模式）。"""

    def __init__(self) -> None:
        # 内部字典：文件类型 → 解析器工厂函数
        self._factories: dict[str, ParserFactory] = {}
        # 初始化时自动注册所有内置解析器
        self._register_defaults()

    @staticmethod
    def normalize_file_type(file_type: str) -> str:
        """归一化文件类型字符串：去空白、转小写、去掉前导点号。"""
        normalized = file_type.strip().lower().lstrip(".")
        if not normalized:  # 空字符串校验
            raise ValueError("file type must not be empty")
        return normalized

    def register(self, *file_types: str) -> Callable[[ParserFactory], ParserFactory]:
        """装饰器注册方法：将解析器工厂函数与一个或多个文件类型关联。

        用法：@registry.register("pdf", "PDF") 即可将工厂绑定到 pdf 类型。
        返回装饰器函数，支持 @ 语法糖。
        """
        if not file_types:  # 至少需要一个文件类型
            raise ValueError("at least one file type is required")

        def decorator(factory: ParserFactory) -> ParserFactory:
            for file_type in file_types:       # 遍历所有文件类型
                normalized = self.normalize_file_type(file_type)  # 归一化处理
                self._factories[normalized] = factory  # 注册到字典
            return factory  # 返回原工厂（符合装饰器惯例）

        return decorator

    def supported_file_types(self) -> tuple[str, ...]:
        """返回当前支持的所有文件类型列表（排序后的元组）。"""
        return tuple(sorted(self._factories))

    def get_parser(self, file_type: str, **kwargs: Any) -> DocumentParser:
        """根据文件类型获取对应的解析器实例。

        Args:
            file_type: 文件类型/扩展名（如 "pdf"、"docx"）
            **kwargs: 传递给解析器工厂的额外参数

        Returns:
            DocumentParser 实例

        Raises:
            ValueError: 不支持的文件类型
        """
        normalized = self.normalize_file_type(file_type)  # 归一化
        factory = self._factories.get(normalized)          # 查找工厂
        if factory is None:                                # 未注册的类型
            supported = ", ".join(self.supported_file_types())
            raise ValueError(
                f"unsupported file type: {normalized!r}; supported types: {supported}"
            )
        return factory(**kwargs)  # 调用工厂函数，传入参数，返回解析器实例

    def parse(self, file_type: str, file_path: Path, **kwargs: Any) -> list[ParsedBlock]:
        """便捷方法：获取解析器并直接解析文件，返回结构化块列表。

        Args:
            file_type: 文件类型/扩展名
            file_path: 文件路径
            **kwargs: 传递给解析器的额外参数

        Returns:
            ParsedBlock 列表
        """
        parser = self.get_parser(file_type, **kwargs)  # 获取解析器实例
        return parser.parse(Path(file_path))            # 调用解析器的 parse 方法

    def _register_defaults(self) -> None:
        """注册所有内置的文档解析器（PDF、Word、Excel、Markdown 等）。"""
        # 延迟导入各解析器类，避免模块级循环引用
        from app.ingestion.parsers.chat_parser import ChatParser       # JSONL 聊天记录
        from app.ingestion.parsers.csv_parser import CsvParser         # CSV 表格
        from app.ingestion.parsers.docx_parser import DocxParser       # Word 文档
        from app.ingestion.parsers.eml_parser import EmlParser         # 邮件
        from app.ingestion.parsers.html_parser import HtmlParser       # HTML 网页
        from app.ingestion.parsers.image_parser import ImageParser     # 图片（OCR 识别）
        from app.ingestion.parsers.markdown_parser import MarkdownParser  # Markdown
        from app.ingestion.parsers.pdf_parser import PdfParser         # PDF
        from app.ingestion.parsers.text_parser import TextParser       # 纯文本
        from app.ingestion.parsers.xlsx_parser import XlsxParser       # Excel 表格

        # 解析器类与其支持的文件类型映射表
        registrations: tuple[tuple[ParserFactory, tuple[str, ...]], ...] = (
            (PdfParser, ("pdf",)),                                      # PDF
            (DocxParser, ("docx",)),                                    # Word
            (XlsxParser, ("xlsx",)),                                    # Excel
            (TextParser, ("txt", "text")),                              # 纯文本
            (MarkdownParser, ("md", "markdown")),                       # Markdown
            (HtmlParser, ("html", "htm")),                              # HTML
            (CsvParser, ("csv",)),                                      # CSV
            (EmlParser, ("eml",)),                                      # 邮件
            (ChatParser, ("jsonl", "chat.jsonl")),                      # 聊天记录
            (ImageParser, ("jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp")),  # 图片（8 种格式）
        )
        # 遍历注册表，调用 register 装饰器批量注册
        for factory, file_types in registrations:
            self.register(*file_types)(factory)  # register(*types) 返回装饰器，传入 factory 完成注册


# 模块级单例：全局解析器注册表实例
parser_registry = ParserRegistry()
