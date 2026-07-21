# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

# 依赖注入：数据库会话、链路追踪 ID
from app.api.deps import get_session, get_trace_id
# 统一成功响应包装函数
from app.core.responses import success_response
# 分块预览的请求 Schema
from app.schemas.chunking import (
    ChunkingDocumentPreviewRequest,  # 对已有文档进行分块预览的请求体
    ChunkingPreviewRequest,          # 对自由文本进行分块预览的请求体
)
# 分块服务层
from app.services.chunking import ChunkingService

# 创建路由实例，路径前缀 /chunking，Swagger 中归入 chunking 分组
router = APIRouter(prefix="/chunking", tags=["chunking"])


# ============================================================
# GET /chunking/strategies — 获取所有可用的分块策略列表
# ============================================================
@router.get("/strategies")
def get_strategies(trace_id: str = Depends(get_trace_id)) -> dict:
    """返回系统支持的所有分块策略名称和描述。"""
    service = ChunkingService()                       # 无需数据库会话
    strategies = service.list_strategies()            # 获取策略列表
    return success_response({"strategies": strategies}, trace_id)


# ============================================================
# POST /chunking/preview — 对自由文本进行分块预览
# ============================================================
@router.post("/preview")
def preview_chunking(request: ChunkingPreviewRequest, trace_id: str = Depends(get_trace_id)) -> dict:
    """输入一段文本和分块策略，返回模拟的分块结果，用于调试和策略选择。"""
    service = ChunkingService()                       # 无需数据库会话
    result = service.preview(
        strategy=request.strategy,                    # 分块策略名称（如 fixed、structure、semantic）
        text=request.text,                            # 待分块的原始文本
        options=request.options,                      # 策略的额外参数（如 chunk_size、overlap）
    )
    return success_response(result, trace_id)


# ============================================================
# POST /chunking/documents/{doc_uuid}/preview — 对已有文档进行分块预览
# ============================================================
@router.post("/documents/{doc_uuid}/preview")
def preview_document_chunking(
    doc_uuid: UUID,                                  # 路径参数：文档 UUID
    request: ChunkingDocumentPreviewRequest,          # 请求体：策略名称和选项
    trace_id: str = Depends(get_trace_id),            # 依赖注入：链路追踪 ID
    session: Session = Depends(get_session),          # 依赖注入：数据库会话
) -> dict:
    """对已上传的文档按指定策略进行分块预览，展示分块边界和内容。"""
    service = ChunkingService(session)                # 传入数据库会话（需读取文档内容）
    result = service.preview_document(
        doc_uuid=doc_uuid,
        strategy=request.strategy,
        options=request.options,
    )
    return success_response(result, trace_id)


# ============================================================
# GET /chunking/documents/{doc_uuid}/preview-text — 获取文档的预览文本
# ============================================================
@router.get("/documents/{doc_uuid}/preview-text")
def get_document_preview_text(
    doc_uuid: UUID,                                  # 路径参数：文档 UUID
    trace_id: str = Depends(get_trace_id),            # 依赖注入：链路追踪 ID
    session: Session = Depends(get_session),          # 依赖注入：数据库会话
) -> dict:
    """获取文档解析后的纯文本内容，用于分块预览前的文本展示。"""
    service = ChunkingService(session)                # 传入数据库会话
    result = service.get_document_preview_text(doc_uuid=doc_uuid)
    return success_response(result, trace_id)
