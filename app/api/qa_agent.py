# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

# 依赖注入：数据库会话、配置、trace_id、可信用户上下文
from app.api.deps import get_session, get_settings_dep, get_trace_id, get_trusted_user_context
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.responses import success_response
# QA Agent 请求 Schema
from app.schemas.qa_agent import AgentAnswerRequest
# QA Agent 服务层（有限状态机 Agent）
from app.services.qa_agent import QaAgentService

# 创建路由实例，前缀 /qa，Swagger 中归入 qa-agent 分组
router = APIRouter(prefix="/qa", tags=["qa-agent"])


# ============================================================
# POST /qa/agent-answer — 基于有限状态机 Agent 的智能问答接口
# ============================================================
@router.post("/agent-answer", response_model=dict)
def agent_answer(
    request: AgentAnswerRequest,                         # 请求体：问题、top_k、过滤条件、检索策略等
    trace_id: str = Depends(get_trace_id),                # 依赖注入：链路追踪 ID
    session: Session = Depends(get_session),              # 依赖注入：数据库会话
    settings: Settings = Depends(get_settings_dep),       # 依赖注入：全局配置
    trusted_user_context=Depends(get_trusted_user_context),  # 依赖注入：可信头中的用户上下文
    x_internal_token: str | None = Header(default=None),  # 内部调用 Token（Header 中传递）
):
    # 内部 Token 校验：若提供了 internal_token 则必须匹配
    if x_internal_token is not None and x_internal_token != settings.internal_token:
        raise AppError(
            code=ErrorCode.AUTHENTICATION_FAILED,
            message="invalid internal token",
            status_code=401,
        )
    # 若有可信用户上下文，则覆盖请求中的 user_context
    if trusted_user_context is not None:
        request = request.model_copy(update={"user_context": trusted_user_context})  # 浅拷贝并更新字段
    # 调用 QA Agent 服务执行有限状态机问答流程
    data = QaAgentService(session).answer(
        request,
        authenticated_identity_required=settings.retrieval_authenticated_identity_required,  # 是否要求认证身份
    )
    return success_response(data.model_dump(mode="json"), trace_id)
