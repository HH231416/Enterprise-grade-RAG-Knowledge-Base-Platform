# Enterprise-grade RAG Knowledge Base Platform

面向企业和校园场景的检索增强生成（RAG）知识库平台。系统将文档解析、切分、索引、检索、重排和生成式问答串联为可管理、可评测、可集成的知识服务，并提供 Web 管理界面、HTTP API、Dify、飞书和企业微信接入能力。

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-19-61DAFB)](https://react.dev/)

## 核心能力

- 文档摄入：支持 PDF、Word、Excel、PowerPoint、Markdown、CSV、纯文本和常见图片格式；PDF、Word、PowerPoint 可提取内嵌图片。
- 智能切分：提供固定长度、结构化、表格感知、父子分层和语义等切分策略，并支持切分预览。
- 多路检索：支持稠密检索、BM25 稀疏检索、混合检索和 Rerank 精排；生产环境由同一 Milvus/Zilliz Collection 承载稠密向量、稀疏向量和标量元数据。
- 可控问答：回答附带来源片段，可配置证据阈值、查询改写和答案质量判定，降低无依据回答风险。
- 运营管理：包含文档管理、知识库查询、任务状态、评测集与评测运行、数据源同步、系统配置和健康检查。
- 企业集成：支持 Dify 外部知识库、飞书和企业微信机器人；提供可插拔权限检查、会话认证、链路追踪、限流与 Prometheus 指标。

## 架构

```text
React + Vite 管理界面 / Dify / 飞书 / 企业微信 / HTTP 客户端
                              |
                         FastAPI API
                              |
      文档解析 -> 切分 -> 向量化 -> 稠密/稀疏检索 -> Rerank -> LLM 回答
                              |
 PostgreSQL / SQLite     Milvus / Zilliz     Redis
```

后端采用 FastAPI、SQLAlchemy 和 Pydantic Settings；前端采用 React、TypeScript 和 Vite。模型服务使用 OpenAI 兼容接口，可对接 vLLM、TEI、Ollama 或云端模型服务。

## 项目结构

```text
app/
  api/             REST API 路由
  core/            配置、日志、中间件、异常处理和指标
  db/              SQLAlchemy 运行时与数据类型
  ingestion/       解析器、文档切分和摄入流程
  integrations/    LLM、Embedding、向量库、重排和 IM 集成
  models/          数据模型
  retrieval/       稀疏检索、混合召回和结果融合
  services/        文档、问答、评测、任务和身份等业务服务
frontend/          React 管理界面
deploy/k8s/        Kubernetes 生产部署清单
docs/              API、部署和外部集成文档
tests/             单元、集成与验收测试
```

## 快速开始

### 1. 准备环境

- Python 3.11+
- Node.js 18+
- PostgreSQL 14+，或仅用于本地演示的 SQLite
- 可选：Redis、Milvus 和模型服务

创建虚拟环境并安装后端依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Linux 或 macOS 请将激活命令替换为 `source .venv/bin/activate`，复制配置文件替换为 `cp .env.example .env`。

### 2. 配置本地开发环境

编辑 `.env`。以下配置可用于本地演示，数据保存在项目的 `data/` 目录，向量和稀疏索引使用进程内实现：

```env
DATABASE_URL=sqlite:///./data/knowledge_base.db
VECTOR_STORE_PROVIDER=local
EMBEDDING_PROVIDER=local
ALLOW_PROVIDER_FALLBACKS=true
```

创建开发数据库表：

```powershell
python -c "from app.db.base import Base; from app.db.runtime import get_engine; import app.models; Base.metadata.create_all(get_engine())"
```

要启用真实语义检索和生成式问答，请在 `.env` 中配置模型服务：

```env
LLM_API_BASE=https://your-llm-endpoint/v1
LLM_API_KEY=your-api-key
LLM_MODEL=your-chat-model

EMBEDDING_API_BASE=https://your-embedding-endpoint/v1
EMBEDDING_API_KEY=your-api-key
EMBEDDING_MODEL=your-embedding-model
EMBEDDING_VECTOR_SIZE=1024
```

生产环境建议使用 PostgreSQL、Milvus 或 Zilliz、Redis，并将 `ALLOW_PROVIDER_FALLBACKS` 设为 `false`。Milvus Collection 应包含稠密字段、BM25 生成的稀疏字段和标量元数据；完整环境变量见 [`.env.example`](.env.example)。

### 3. 启动服务

在项目根目录启动后端：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 18080 --reload
```

新开终端启动前端：

```powershell
cd frontend
npm install
npm run dev
```

可访问以下地址：

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:5173` | Web 管理界面 |
| `http://127.0.0.1:18080/docs` | Swagger API 文档 |
| `http://127.0.0.1:18080/redoc` | ReDoc API 文档 |
| `http://127.0.0.1:18080/api/v1/health` | 健康检查 |
| `http://127.0.0.1:18080/api/v1/metrics` | Prometheus 指标 |

## Docker 与部署

根目录的 `docker-compose.yml` 用于启动本地 Milvus、Prometheus 和 Grafana 等基础组件：

```powershell
docker compose up -d
```

它不包含应用、PostgreSQL 和 Redis。集成环境全栈编排位于 `docker-compose.integration.yml`，需要先按照 [`.env.integration.example`](.env.integration.example) 提供模型服务配置。

生产环境的 Kubernetes 清单位于 [`deploy/k8s/`](deploy/k8s/)，部署要求、模型服务边界和可用性策略见 [`deploy/k8s/README.md`](deploy/k8s/README.md)。生产环境应使用独立或受管的 PostgreSQL、Redis、Milvus 和持久化共享存储，不应将本地 Docker Compose 直接用于生产。

## 主要接口

所有接口均以 `/api/v1` 为前缀，完整请求和响应定义见 [`docs/API.md`](docs/API.md)。

| 模块 | 代表接口 | 说明 |
| --- | --- | --- |
| 文档管理 | `POST /documents/upload` | 上传、解析、切分并建立索引 |
| 检索 | `POST /retrieval/search` | 检索测试 |
| 检索调试 | `POST /retrieval/debug-search` | 返回召回与融合明细 |
| 问答 | `POST /qa/answer` | 基于检索结果生成回答 |
| 可控问答 | `POST /qa/agent-answer` | 证据门控、查询改写与执行轨迹 |
| 多轮对话 | `POST /qa/conversations/{id}/messages` | 在已创建会话中发送消息 |
| 评测 | `POST /evaluation/runs` | 运行 Hit@k、MRR 等检索评测 |
| Dify | `POST /dify/retrieval` | Dify 外部知识库检索接口 |
| 数据源 | `POST /sources/folder/sync` | 同步白名单目录中的文档 |
| 运维 | `GET /health`、`GET /metrics` | 健康状态和监控指标 |

默认使用标准响应结构：

```json
{
  "code": 0,
  "message": "ok",
  "data": {},
  "trace_id": "trc_xxx"
}
```

## 配置要点

| 配置项 | 作用 |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy 数据库连接串；生产建议 PostgreSQL。 |
| `VECTOR_STORE_PROVIDER` | `local`、`milvus` 或 `zilliz`。 |
| `RETRIEVAL_STRATEGY` | `dense` 或 `hybrid`。 |
| `RERANK_ENABLED` | 启用 TEI 或 OpenAI 兼容的重排服务。 |
| `INGESTION_MODE` | `sync`、`async`；异步任务可通过 Jobs API 查询。 |
| `EVALUATION_EXECUTION_MODE` | `sync`、`background` 或 `celery`。 |
| `AUTH_SESSION_ENABLED` | 启用基于会话的认证；启用时必须配置 `AUTH_SESSION_SECRET`。 |
| `RATE_LIMIT_ENABLED` | 启用 Redis 分布式限流。 |

不要提交真实的 API Key、数据库密码、机器人凭据或 `.env` 文件。

## 测试与构建

```powershell
# 后端测试
pytest tests/ -q

# 前端生产构建
cd frontend
npm run build

# 前端代码检查
npm run lint
```

测试默认使用独立的 SQLite 数据库和本地存储目录，不会修改开发环境配置。

## 文档索引

- [运行与开发指南](docs/guides/runtime.md)
- [API 文档](docs/API.md)
- [知识库通用查询契约](docs/contracts/knowledge-api.md)
- [飞书机器人接入](docs/guides/feishu-bot-integration.md)
- [企业微信机器人接入](docs/guides/wecom-bot-integration.md)
- [外部集成运行手册](docs/external_integration_runbook.md)
- [校园生产环境指南](docs/campus-production-guide.md)

## 许可与安全

仓库当前未附带开源许可证。部署前请依据组织的合规要求补充许可证、密钥管理、访问控制、数据分级和审计策略。
