# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from fastapi import APIRouter

# 导入各子模块的路由实例（每个模块内部有自己的 prefix 和 tags）
from app.api.admin import router as admin_router               # 管理后台接口
from app.api.auth import router as auth_router                 # 认证接口
from app.api.chunking import router as chunking_router         # 分块预览与策略查询
from app.api.configs import router as configs_router           # 配置管理接口
from app.api.dify import router as dify_router                 # Dify 平台集成接口
from app.api.documents import router as documents_router       # 文档 CRUD 与上传
from app.api.evaluation import router as evaluation_router     # 评测相关接口
from app.api.feishu import router as feishu_router             # 飞书机器人回调
from app.api.health import router as health_router             # 健康检查接口
from app.api.jobs import router as jobs_router                 # 后台任务状态查询
from app.api.knowledge import router as knowledge_router       # 知识库管理接口
from app.api.qa import router as qa_router                     # 问答接口（基础版）
from app.api.qa_agent import router as qa_agent_router         # 问答 Agent 接口（状态机版）
from app.api.qa_conversation import router as qa_conversation_router  # 多轮对话接口
from app.api.retrieval import router as retrieval_router       # 检索接口
from app.api.wecom import router as wecom_router               # 企业微信机器人回调


# 创建顶级 API 路由聚合器（不设 prefix，由 main.py 中的 app_api_prefix 统一添加）
api_router = APIRouter()
# 将各子模块路由逐一注册到聚合路由中
api_router.include_router(auth_router)            # /auth/*
api_router.include_router(admin_router)           # /admin/*
api_router.include_router(health_router)          # /health/*
api_router.include_router(configs_router)         # /configs/*
api_router.include_router(dify_router)            # /dify/*
api_router.include_router(feishu_router)          # /feishu/*
api_router.include_router(wecom_router)           # /wecom/*
api_router.include_router(documents_router)       # /documents/*
api_router.include_router(jobs_router)            # /jobs/*
api_router.include_router(knowledge_router)       # /knowledge/*
api_router.include_router(retrieval_router)       # /retrieval/*
api_router.include_router(qa_router)              # /qa/*
api_router.include_router(qa_agent_router)        # /qa-agent/*
api_router.include_router(qa_conversation_router) # /qa-conversation/*
api_router.include_router(chunking_router)        # /chunking/*
api_router.include_router(evaluation_router)      # /evaluation/*
