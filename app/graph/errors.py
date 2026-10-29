"""图模块异常。

区分「可降级的可用性故障」与「不可降级的完整性/授权失败」。
授权与来源完整性失败不得通过降级绕过。
"""
from __future__ import annotations

from app.core.errors import AppError, ErrorCode


class GraphError(AppError):
    """图模块基础异常。"""

    def __init__(self, message: str, *, code: ErrorCode = ErrorCode.INTERNAL_ERROR, status_code: int = 500):
        super().__init__(code=code, message=message, status_code=status_code)


class GraphUnavailableError(GraphError):
    """图存储不可用。可降级到纯文档检索。"""

    def __init__(self, message: str = "graph store unavailable") -> None:
        super().__init__(message=message, code=ErrorCode.RETRIEVAL_FAILED, status_code=503)


class GraphSourceChangedError(GraphError):
    """构建/批准期间来源快照已变化。不可降级，必须重新构建。"""

    def __init__(self, message: str = "document source snapshot changed, rebuild required") -> None:
        super().__init__(message=message, code=ErrorCode.DOCUMENT_DUPLICATED, status_code=409)


class GraphPublishFailedError(GraphError):
    """发布写入失败。数据库审批状态与在线生效状态必须可区分。"""

    def __init__(self, message: str = "graph publish failed") -> None:
        super().__init__(message=message, code=ErrorCode.INTERNAL_ERROR, status_code=500)


class GraphAdminRequiredError(GraphError):
    """缺少可信管理员身份。

    挂载 require_admin_actor 不足以认为授权完成：该依赖在配置关闭时返回空身份，
    此时图审批与发布必须拒绝继续。
    """

    def __init__(self, message: str = "trusted admin identity is required for graph approval") -> None:
        super().__init__(message=message, code=ErrorCode.PERMISSION_DENIED, status_code=403)


class GraphScopeError(GraphError):
    """资料不属于公开范围，禁止进入图构建、发布或在线召回。"""

    def __init__(self, message: str = "document is not in public graph scope") -> None:
        super().__init__(message=message, code=ErrorCode.PERMISSION_DENIED, status_code=403)
