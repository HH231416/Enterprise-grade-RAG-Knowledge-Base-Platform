"""权限检查器所需的文档字段构造。

抽出为共享实现的原因：检索层与历史证据过滤都需要构造同样的权限字典。
两处各自拼装会出现字段缺失（例如漏掉 owner_role）导致权限判断与原有检索不一致。
"""
from __future__ import annotations

from typing import Any


def document_to_permission_dict(document: Any) -> dict:
    """将 Document ORM 对象转换为权限检查器所需的字典。

    字段集合必须与检索层保持一致：`CampusPermissionChecker` 依赖
    access_level / owner_dept / owner_role / created_by / tags 判断可见性，
    缺字段会导致角色级或私有资料被误判为不可见或可见。
    """
    return {
        "doc_uuid": str(document.doc_uuid),
        "source_module": document.source_module,
        "source_type": document.source_type,
        "access_level": document.access_level,
        "tags": document.tags or [],
        "owner_dept": document.owner_dept,
        "owner_role": document.owner_role,
        "created_by": document.created_by,
        "title": document.title,
        "file_ext": document.file_ext,
    }


__all__ = ["document_to_permission_dict"]
