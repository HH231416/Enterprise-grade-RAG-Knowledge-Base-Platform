# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

# UUID 生成器：为每个分块创建全局唯一标识
import uuid
from datetime import datetime

# SQLAlchemy ORM 组件
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column  # Mapped：类型标注，mapped_column：列定义

# 声明式基类
from app.db.base import Base
# 自定义列类型：JsonType（JSON 列）、PrimaryKeyBigInt（自增主键）、UuidType（UUID 列）
from app.db.types import JsonType, PrimaryKeyBigInt, UuidType
# TimestampMixin：自动添加 created_at / updated_at 时间戳字段
from app.models.mixins import TimestampMixin


class DocumentChunk(TimestampMixin, Base):
    """文档分块 ORM 模型，映射到 document_chunks 表。

    每个文档经过解析后被切分为多个分块，每个分块独立向量化后存入向量库，
    通过 chunk_uuid ↔ vector_id 实现数据库记录与向量数据的关联溯源。
    """
    # 数据库表名
    __tablename__ = "document_chunks"
    # 表级约束：多个索引优化查询性能
    __table_args__ = (
        Index("uq_document_chunks_chunk_uuid", "chunk_uuid", unique=True),  # 唯一索引：按 chunk_uuid 快速查找
        Index("idx_document_chunks_doc_uuid", "doc_uuid"),                  # 普通索引：按所属文档查询
        Index("idx_document_chunks_page_no", "page_no"),                    # 普通索引：按页码筛选
        Index("idx_document_chunks_sheet_name", "sheet_name"),              # 普通索引：按工作表名筛选
        Index("idx_document_chunks_chunk_type", "chunk_type"),              # 普通索引：按分块类型筛选
        Index("idx_document_chunks_vector_id", "vector_id"),                # 普通索引：按向量 ID 查找
    )

    # 自增主键（BIGINT 类型）
    id: Mapped[int] = mapped_column(PrimaryKeyBigInt, primary_key=True, autoincrement=True)
    # 分块全局唯一标识，默认自动生成 UUID4
    chunk_uuid: Mapped[uuid.UUID] = mapped_column(UuidType, default=uuid.uuid4, nullable=False)
    # 所属文档 UUID，外键关联 documents 表，级联删除
    doc_uuid: Mapped[uuid.UUID] = mapped_column(
        UuidType,
        ForeignKey("documents.doc_uuid", ondelete="CASCADE"),  # 文档删除时自动删除所有分块
        nullable=False,
    )
    # 分块在文档中的序号（从 0 开始）
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 分块类型：text（文本）、table（表格）、image（图片）、structure（结构）、semantic（语义）
    chunk_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text", server_default="text")
    # 分块所属的章节标题
    section_title: Mapped[str | None] = mapped_column(String(255))
    # 分块所在的页码（PDF/Word 等分页文档）
    page_no: Mapped[int | None] = mapped_column(Integer)
    # 分块所在的工作表名（Excel 文档）
    sheet_name: Mapped[str | None] = mapped_column(String(255))
    # 分块在原始表格中的起始行号（Excel）
    row_start: Mapped[int | None] = mapped_column(Integer)
    # 分块在原始表格中的结束行号（Excel）
    row_end: Mapped[int | None] = mapped_column(Integer)
    # Token 数量估算（用于计费和截断控制）
    token_count: Mapped[int | None] = mapped_column(Integer)
    # 字符数量
    char_count: Mapped[int | None] = mapped_column(Integer)
    # 分块的文本内容（核心字段，用于检索和生成）
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 分块内容的摘要（LLM 生成，可选）
    chunk_summary: Mapped[str | None] = mapped_column(Text)
    # 父分块 UUID（父子分块策略：子分块引用父分块）
    parent_chunk_uuid: Mapped[uuid.UUID | None] = mapped_column(UuidType, nullable=True)
    # 分块组 UUID（结构分块策略：同一结构元素的分块归为一组）
    chunk_group_uuid: Mapped[uuid.UUID | None] = mapped_column(UuidType, nullable=True)
    # 分块层级：parent（父）、child（子），用于父子分块策略
    chunk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 上下文文本（检索时可附加的上下文窗口内容）
    context_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 向量库中的向量 ID，用于关联向量数据和数据库记录
    vector_id: Mapped[str | None] = mapped_column(String(128))
    # Zilliz 中的集合名称（多集合场景下标识向量存储位置）
    zilliz_collection: Mapped[str | None] = mapped_column(String(128))
    # 扩展元数据（JSON 格式），存储任意键值对
    metadata_json: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict, server_default="{}")
    # 软删除时间戳（NULL 表示未删除）
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
