# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

# UUID 类型，用于校验和解析路径参数中的文档唯一标识符
from uuid import UUID

# FastAPI 核心组件
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
# FileResponse：直接将服务器文件作为 HTTP 响应流式返回（下载/预览）
from fastapi.responses import FileResponse
# SQLAlchemy 数据库会话类型，用于依赖注入的类型标注
from sqlalchemy.orm import Session

# 依赖注入的可调用对象
from app.api.deps import get_session, get_settings_dep, get_trace_id
# 全局配置类，用于类型标注和依赖注入
from app.core.config import Settings
# Prometheus Counter 指标：统计文档上传成功次数
from app.core.metrics import documents_uploaded_total
# 统一成功响应包装函数，将业务数据封装为标准 JSON 格式（含 trace_id）
from app.core.responses import success_response
# 请求/响应的 Pydantic 数据模型（Schema）
from app.schemas.documents import (
    BatchDocumentOperationRequest,  # 批量操作请求体（含 doc_uuids 列表）
    DocumentReindexResponse,        # 重索引响应体
    DocumentUpdateRequest,          # 更新文档请求体
    DocumentUploadResponse,         # 上传文档响应体
)
# 文档管理服务类：封装列表查询、详情、更新、删除、重索引等通用业务逻辑
from app.services.document_management import DocumentManagementService
# 文档服务类：专门处理文件上传完整流程（保存→解析→分块→向量化入库）
from app.services.documents import DocumentService
# 后台任务运行器：负责将耗时任务提交到异步队列中执行
from app.services.background_jobs import BackgroundJobRunner
# 摄入服务类：处理文档的同步摄入/重索引流程（解析→分块→Embedding→写入向量库）
from app.services.ingestion import IngestionService

# 创建模块化路由实例，所有路径自动加上 /documents 前缀，在 OpenAPI 文档中归入 "documents" 分组
router = APIRouter(prefix="/documents", tags=["documents"])

# ============================================================
# GET /documents — 分页查询文档列表，支持多条件筛选
# ============================================================
@router.get("", response_model=dict)
def list_documents(
    page: int = Query(default=1, ge=1),           # 页码，默认第1页，ge=1 校验必须 ≥1
    page_size: int = Query(default=20, ge=1, le=100),  # 每页条数，默认20，限制范围 1~100
    source_type: str | None = Query(default=None),     # 按来源类型筛选（如 pdf、docx）
    source_module: str | None = Query(default=None),   # 按业务模块筛选（如 hr、finance）
    parse_status: str | None = Query(default=None),    # 按解析状态筛选（pending/completed/failed）
    index_status: str | None = Query(default=None),    # 按向量索引状态筛选
    keyword: str | None = Query(default=None),         # 按关键词模糊搜索文档标题/内容
    trace_id: str = Depends(get_trace_id),             # 依赖注入：获取链路追踪 ID
    session: Session = Depends(get_session),           # 依赖注入：获取数据库会话，请求结束后自动关闭
):
    # 实例化文档管理服务，传入数据库会话，调用列表查询方法
    data = DocumentManagementService(session).list_documents(
        page=page,
        page_size=page_size,
        source_type=source_type,
        source_module=source_module,
        parse_status=parse_status,
        index_status=index_status,
        keyword=keyword,
    )
    # mode="json" 将 UUID→字符串、datetime→ISO 格式，再包装为统一响应结构返回
    return success_response(data.model_dump(mode="json"), trace_id)


# ============================================================
# GET /documents/{doc_uuid} — 获取单个文档的详细信息
# ============================================================
@router.get("/{doc_uuid}", response_model=dict)
def get_document_detail(
    doc_uuid: UUID,                           # 路径参数，FastAPI 自动校验 UUID 格式，非法则返回 422
    trace_id: str = Depends(get_trace_id),    # 依赖注入：链路追踪 ID
    session: Session = Depends(get_session),  # 依赖注入：数据库会话
):
    # 调用服务层根据 UUID 查询文档详情（含元数据、分块信息、文件路径等）
    data = DocumentManagementService(session).get_document_detail(doc_uuid)
    return success_response(data.model_dump(mode="json"), trace_id)


# ============================================================
# PATCH /documents/{doc_uuid} — 部分更新文档元数据
# ============================================================
@router.patch("/{doc_uuid}", response_model=dict)
def update_document(
    doc_uuid: UUID,                           # 路径参数：要更新的文档 UUID
    request: DocumentUpdateRequest,           # 请求体：FastAPI 自动反序列化 JSON 为 Pydantic 模型并校验
    trace_id: str = Depends(get_trace_id),    # 依赖注入：链路追踪 ID
    session: Session = Depends(get_session),  # 依赖注入：数据库会话
):
    # 调用服务层更新文档元数据（如标题、标签、访问级别等）
    data = DocumentManagementService(session).update_document(doc_uuid, request)
    return success_response(data.model_dump(mode="json"), trace_id)


# ============================================================
# POST /documents/batch/reindex — 批量重新向量化索引
# ============================================================
@router.post("/batch/reindex", response_model=dict)
def batch_reindex_documents(
    request: BatchDocumentOperationRequest,   # 请求体：包含 doc_uuids 列表
    trace_id: str = Depends(get_trace_id),    # 依赖注入：链路追踪 ID
    session: Session = Depends(get_session),  # 依赖注入：数据库会话
):
    # 提取 UUID 列表传给服务层批量执行重索引
    data = DocumentManagementService(session).batch_reindex_documents(request.doc_uuids)
    return success_response(data.model_dump(mode="json"), trace_id)


# ============================================================
# GET /documents/{doc_uuid}/download — 下载/预览文档原始文件
# ============================================================
@router.get("/{doc_uuid}/download")  # 没有 response_model，因为返回文件流而非 JSON
def download_document(
    doc_uuid: UUID,                           # 路径参数：要下载的文档 UUID
    session: Session = Depends(get_session),  # 依赖注入：数据库会话（此接口不需要 trace_id）
):
    # 先查询文档详情，获取文件路径和文件名
    data = DocumentManagementService(session).get_document_detail(doc_uuid)
    # 双重校验：数据库记录中的路径是否存在 + 磁盘上文件是否实际存在
    if not data.file_path or not data.file_exists:
        from app.core.errors import AppError, ErrorCode  # 延迟导入，避免模块级循环引用
        # 抛出 404 业务异常，全局异常处理器会将其转换为标准 JSON 错误响应
        raise AppError(
            code=ErrorCode.DOCUMENT_NOT_FOUND,
            message="document file not found",
            status_code=404,
        )
    
    # 根据文件扩展名确定 MIME 类型
    import mimetypes  # 延迟导入标准库
    mime_type, _ = mimetypes.guess_type(data.file_path)  # 根据后缀推断 MIME（如 .pdf→application/pdf）
    if not mime_type:
        mime_type = "application/octet-stream"  # 未知后缀回退为通用二进制流，浏览器触发下载
    
    # 判断是否为图片文件（image/png、image/jpeg 等都以 image/ 开头）
    is_image = mime_type.startswith("image/")
    
    # 返回文件流响应
    return FileResponse(
        path=data.file_path,                                        # 服务器上的文件绝对路径
        filename=data.file_name,                                    # 浏览器下载时使用的文件名
        media_type=mime_type,                                       # MIME 类型
        content_disposition_type="inline" if is_image else "attachment",  # 图片浏览器内显示，其他触发下载
    )


# ============================================================
# POST /documents/upload — 上传文档并触发摄入流程
# ============================================================
@router.post("/upload", response_model=dict)
async def upload_document(  # async：文件上传涉及 I/O，避免阻塞事件循环
    file: UploadFile = File(...),             # 上传的文件，...（Ellipsis）表示必填
    title: str | None = Form(default=None),   # 文档标题，可选，不传则从文件名推断
    source_type: str = Form(...),             # 文档来源类型，必填（如 pdf、docx、txt、url）
    source_module: str = Form(...),           # 业务模块，必填（如 hr、finance），用于分类和权限
    version: str = Form(default="v1"),        # 文档版本号，默认 v1
    access_level: str = Form(default="internal"),  # 访问级别，默认 internal（内部）
    owner_dept: str | None = Form(default=None),   # 所属部门，可选
    owner_role: str | None = Form(default=None),   # 所属角色，可选
    tags: str | None = Form(default=None),         # 标签（逗号分隔字符串），可选
    extra_meta: str | None = Form(default=None),   # 额外 JSON 元数据，可选，用于扩展自定义字段
    trace_id: str = Depends(get_trace_id),         # 依赖注入：链路追踪 ID
    session: Session = Depends(get_session),       # 依赖注入：数据库会话
    settings: Settings = Depends(get_settings_dep),  # 依赖注入：全局配置单例
):
    # 调用文档服务执行完整上传流程：校验→保存文件→创建数据库记录→摄入（同步或异步）
    document, job, chunk_count = await DocumentService(settings, session).upload_document(
        file=file,
        title=title,
        source_type=source_type,
        source_module=source_module,
        version=version,
        access_level=access_level,
        owner_dept=owner_dept,
        owner_role=owner_role,
        tags=tags,
        extra_meta=extra_meta,
    )
    # 构造上传响应模型
    data = DocumentUploadResponse(
        doc_uuid=document.doc_uuid,            # 创建的文档 UUID
        job_uuid=job.job_uuid,                 # 后台任务 UUID（客户端可据此轮询进度）
        status=job.status,                     # 任务当前状态
        chunk_count=chunk_count,               # 分块数量（同步模式有值，异步为 0）
        execution_mode=settings.ingestion_mode, # 执行模式（sync/async）
    )
    # Prometheus 指标埋点：按来源类型和状态标签递增上传成功计数器
    documents_uploaded_total.labels(source_type=source_type, status="success").inc()
    return success_response(data.model_dump(mode="json"), trace_id)


# ============================================================
# POST /documents/{doc_uuid}/reindex — 对单个文档重新执行向量化索引
# ============================================================
@router.post("/{doc_uuid}/reindex", response_model=dict)
def reindex_document(
    doc_uuid: UUID,                              # 路径参数：要重索引的文档 UUID
    trace_id: str = Depends(get_trace_id),       # 依赖注入：链路追踪 ID
    session: Session = Depends(get_session),     # 依赖注入：数据库会话
    settings: Settings = Depends(get_settings_dep),  # 依赖注入：全局配置（用于判断 sync/async）
):
    # 策略分流：根据全局配置的 ingestion_mode 决定同步还是异步执行
    if settings.ingestion_mode == "async":
        # === 异步模式 ===
        service = DocumentManagementService(session)  # 实例化管理服务
        document = service.require_document(doc_uuid)  # 查询文档，不存在则抛 404 异常
        job = service.create_reindex_job(doc_uuid)     # 创建一条 pending 状态的重索引任务记录
        # 将摄入任务提交到后台任务队列（Celery/RQ/线程池），由 Worker 异步消费执行
        BackgroundJobRunner.submit_ingest_job(
            doc_uuid=document.doc_uuid,
            job_uuid=job.job_uuid,
            settings=settings,
        )
        chunk_count = 0  # 异步模式下分块数暂为 0，实际数量在任务完成后更新
    else:
        # === 同步模式 ===
        # 在当前请求线程内完成：重新解析→重新分块→重新生成 Embedding→写入向量数据库
        document, job, chunk_count = IngestionService(session, settings).reprocess_document(doc_uuid)
    # 构造重索引响应模型（同步和异步使用相同结构，客户端通过 execution_mode+chunk_count 区分）
    data = DocumentReindexResponse(
        doc_uuid=document.doc_uuid,
        job_uuid=job.job_uuid,
        status=job.status,
        chunk_count=chunk_count,
        execution_mode=settings.ingestion_mode,
    )
    return success_response(data.model_dump(mode="json"), trace_id)


# ============================================================
# DELETE /documents/{doc_uuid} — 删除单个文档
# ============================================================
@router.delete("/{doc_uuid}", response_model=dict)
def delete_document(
    doc_uuid: UUID,                           # 路径参数：要删除的文档 UUID
    trace_id: str = Depends(get_trace_id),    # 依赖注入：链路追踪 ID
    session: Session = Depends(get_session),  # 依赖注入：数据库会话
):
    # 调用服务层删除文档（含数据库记录、磁盘文件、向量库中的向量数据）
    DocumentManagementService(session).delete_document(doc_uuid)
    # 直接构造简单字典返回删除确认，无需使用 Pydantic 模型
    return success_response({"doc_uuid": str(doc_uuid), "deleted": True}, trace_id)


# ============================================================
# POST /documents/batch/delete — 批量删除文档
# ============================================================
@router.post("/batch/delete", response_model=dict)  # 使用 POST 而非 DELETE，因 DELETE 通常不带请求体
def batch_delete_documents(
    request: BatchDocumentOperationRequest,   # 请求体：包含 doc_uuids 列表
    trace_id: str = Depends(get_trace_id),    # 依赖注入：链路追踪 ID
    session: Session = Depends(get_session),  # 依赖注入：数据库会话
):
    # 提取 UUID 列表传给服务层批量删除
    data = DocumentManagementService(session).batch_delete_documents(request.doc_uuids)
    return success_response(data.model_dump(mode="json"), trace_id)
