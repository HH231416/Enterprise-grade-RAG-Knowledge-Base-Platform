"""面向校园综合知识库的默认权限策略。

该策略只处理知识库文档可见性，不替代学校统一身份认证（SSO/LDAP）。
生产环境应由网关或身份适配器填充 UserContext，再将本插件配置为权限检查器。
"""
from __future__ import annotations

from typing import Any

from app.core.config import get_settings


def _normalise(value: Any) -> str:
    return str(value or "").strip().lower()


class CampusPermissionChecker:
    """按校园角色、学院和资料访问级别做 fail-closed 过滤。"""

    def __init__(self) -> None:
        settings = get_settings()
        raw_roles = getattr(settings, "campus_admin_roles", "campus_admin,school_admin")
        self.admin_roles = {_normalise(item) for item in str(raw_roles).split(",") if _normalise(item)}

    def can_access_document(
        self,
        document: dict[str, Any],
        user_context: dict[str, Any] | None,
    ) -> bool:
        context = user_context or {}
        roles = {_normalise(role) for role in context.get("roles", [])}
        departments = {_normalise(dept) for dept in context.get("departments", [])}
        user_id = _normalise(context.get("user_id"))

        if bool(context.get("is_super_admin")) or roles.intersection(self.admin_roles):
            return True

        access_level = _normalise(document.get("access_level")) or "internal"
        if access_level in {"public", "open", "公开"}:
            return True
        if not bool(context.get("is_authenticated")):
            return False
        if access_level in {"internal", "campus", "校内"}:
            return True

        owner_dept = _normalise(document.get("owner_dept"))
        owner_role = _normalise(document.get("owner_role"))
        if access_level in {"department", "dept", "院系", "学院"}:
            return bool(owner_dept and owner_dept in departments)
        if access_level in {"role", "身份", "角色"}:
            return bool(owner_role and owner_role in roles)
        if access_level in {"private", "personal", "私有"}:
            return bool(user_id and user_id == _normalise(document.get("created_by")))

        # 未知级别默认拒绝，防止新数据类型意外扩大可见范围。
        return False

    def filter_documents(
        self,
        documents: list[dict[str, Any]],
        user_context: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        return [
            document
            for document in documents
            if self.can_access_document(document, user_context)
        ]
