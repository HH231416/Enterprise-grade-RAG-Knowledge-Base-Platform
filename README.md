企业级 RAG（检索增强生成）知识库管理平台。将企业的文档自动消化为可对话的知识库，支持 IM 机器人、Dify 集成、HTTP API 等多种接入方式。

1. 启动后端服务
python -m uvicorn app.main:app --host 127.0.0.1 --port 18080 --reload

2. 启动前端服务
cd frontend
npm run dev


[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green.svg)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/react-19-blue.svg)](https://react.dev/)

---

## 📖 学习路径

如果是第一次接触这个项目，建议按以下顺序阅读代码：

### 第一阶段：理解架构（30 分钟）

| 顺序 | 文件 | 看懂什么 |
|------|------|----------|
| 1 | `app/main.py` | 应用入口：FastAPI 如何组装中间件、路由、生命周期 |
| 2 | `app/api/router.py` | 所有 API 路由注册，一眼看清系统有哪些功能模块 |
| 3 | `app/core/config.py` | 配置系统：Pydantic Settings 如何管理 60+ 配置项 |
| 4 | `app/db/base.py` + `app/db/session.py` | 数据库基础：SQLAlchemy ORM 基类和会话管理 |

> 学完这阶段，你能回答：**系统有哪些模块？配置怎么管？请求怎么进来？**

### 第二阶段：核心链路（60 分钟）

| 顺序 | 文件 | 看懂什么 |
|------|------|----------|
| 5 | `app/api/documents.py` | 文档上传 API：接收文件 → 解析 → 分块 → 向量化 |
| 6 | `app/ingestion/parser_registry.py` | 解析器注册：根据文件后缀自动选择对应解析器 |
| 7 | `app/ingestion/chunking.py` | 切分引擎：6 种策略如何将文本切为检索块 |
| 8 | `app/integrations/embedding.py` | 向量化：文本 → Embedding API → 向量 |
| 9 | `app/integrations/vector_store.py` | 向量存储：向量如何存入 Milvus/本地并检索 |
| 10 | `app/services/retrieval.py` | 检索服务：稠密/混合检索的完整流程 |
| 11 | `app/integrations/llm.py` | LLM 调用：将检索结果+问题发给大模型生成答案 |
| 12 | `app/services/qa.py` | 问答服务：串联 检索→判断→生成 的完整链路 |

> 学完这阶段，你能回答：**一份文档上传后经历了什么？一个问题如何得到回答？**

### 第三阶段：扩展功能（按需阅读）

| 功能 | 入口文件 | 关键文件 |
|------|----------|----------|
| 飞书机器人 | `app/api/feishu.py` | `app/integrations/feishu_client.py`, `app/integrations/bot_crypto.py` |
| 企微机器人 | `app/api/wecom.py` | `app/integrations/wecom_client.py` |
| Dify 集成 | `app/api/dify.py` | 查看如何作为外部知识库被 Dify 调用 |
| 图片识别 | `app/integrations/image_recognition.py` | PaddleOCR + 多模态双 provider |
| 质量评测 | `app/services/evaluation.py` | 数据集管理 + Hit@k/MRR 指标计算 |
| 多轮对话 | `app/api/qa_conversation.py` | 对话持久化 + 历史上下文注入 |
| 权限系统 | `app/permissions/loader.py` | 可插拔权限检查器接口 |
| 检索测试 | `app/api/retrieval.py` | 调试用检索接口 |
| 切分预览 | `app/api/chunking.py` | 预览不同策略的切分效果 |
| 监控指标 | `app/core/metrics.py` | Prometheus 指标定义 |

### 第四阶段：数据模型

| 文件 | 说明 |
|------|------|
| `app/models/` | 所有数据表定义（15 个模型） |
| `migrations/versions/` | 数据库迁移历史（12+ 个迁移文件） |
| `app/db/types.py` | 跨数据库兼容类型（UUID/JSON/PK） |

### 第五阶段：前端（可选）

| 文件 | 说明 |
|------|------|
| `frontend/src/App.tsx` | 路由定义 |
| `frontend/src/pages/DocumentsPage.tsx` | 文档管理页 |
| `frontend/src/pages/QaChatPage.tsx` | 多轮问答页 |
| `frontend/src/pages/RetrievalPage.tsx` | 检索测试页 |
| `frontend/src/api.ts` | 所有后端 API 调用函数 |

---

## ✨ 功能清单

### 📄 文档处理
- **10+ 格式支持**：PDF、Word、Excel、PowerPoint、Markdown、HTML、CSV、邮件（EML）、聊天记录、图片（8 种格式）、纯文本；PDF/Word/Excel/HTML/PPTX 可提取内嵌图片
- **图片识别**：PaddleOCR 文字识别 + 多模态大模型描述（千问 VL），支持表格检测
- **PDF 深度解析**：自动提取 PDF 中嵌入的图片并识别

### ✂️ 智能切分
- **6 种切分策略**：固定长度、按结构、表格感知、父子分层、语义、聊天记录
- **策略预览**：上传前可预览不同策略的切分效果
- **灵活调参**：每种策略支持独立参数（块大小、重叠量、句数阈值等）

### 🔍 检索模式
- **稠密检索（dense）**：纯向量语义检索
- **混合检索（hybrid）**：BM25 关键词 + 向量语义双路召回，alpha 加权融合
- **Rerank 重排序**：搜索结果二次排序，提升准确率
- **评测工作台**：构建评测集，量化 Hit@1/3/5、MRR 等指标

### 💬 智能问答
- **多轮对话**：上下文持久化，支持连续追问
- **引用溯源**：每个回答附来源片段，可追溯到原文档
- **证据判断**：自动检测检索结果质量，证据不足时明确告知而非编造
- **多模型兼容**：OpenAI 兼容协议，支持千问/DeepSeek/GPT 等任意模型

### 🔌 第三方对接
- **Dify**：作为外部知识库接入，支持多知识库组合
- **飞书机器人**：消息加解密、事件回调、@ 触发
- **企业微信机器人**：XML 解析、AES 加解密、主动回复
- **通用 HTTP API**：任意平台可调用

### 🛡️ 企业特性
- **知识库分类**：`source_module` 多知识库隔离
- **可插拔权限**：实现 `PermissionChecker` 接口即可对接企业权限
- **监控指标**：Prometheus `/metrics` 端点，15 个业务指标
- **生产级向量库**：支持本地 Milvus / Zilliz Cloud / 内存（开发用）
- **全链路追踪**：Trace ID 贯穿请求，便于排错

---

## 🚀 快速开始

### 前置要求

- Python 3.11+
- PostgreSQL 14+（必需）
- Redis 6+（必需）
- Milvus 2.5+ 或 Zilliz Cloud（生产必需，开发可用内存模式）
- Node.js 18+（前端）

### 1. 环境准备

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
```

### 2. 配置 `.env`

```env
# 数据库（必需）
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=oa_rag_poc
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password

# Redis（必需）
REDIS_HOST=127.0.0.1
REDIS_PORT=6379

# 向量存储（三选一）
VECTOR_STORE_PROVIDER=local          # 开发：内存模式（重启丢失）
# VECTOR_STORE_PROVIDER=milvus       # 生产：本地 Milvus
# MILVUS_URI=http://localhost:19530
# VECTOR_STORE_PROVIDER=zilliz       # 云：Zilliz Cloud
# ZILLIZ_URI=https://xxx.zillizcloud.com
# ZILLIZ_TOKEN=your_token

# LLM（问答必需）
LLM_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=sk-your_key
LLM_MODEL=qwen-plus

# Embedding（检索必需）
EMBEDDING_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=sk-your_key
EMBEDDING_MODEL=text-embedding-v3
EMBEDDING_VECTOR_SIZE=1024

# 图片识别（可选，默认 PaddleOCR）
IMAGE_RECOGNITION_PROVIDER=multimodal
MULTIMODAL_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
MULTIMODAL_API_KEY=sk-your_key
MULTIMODAL_MODEL=qwen-vl-plus
```

### 3. 初始化数据库

```bash
alembic upgrade head
```

### 4. 启动

```bash
# 终端 1：后端
python -m uvicorn app.main:app --host 127.0.0.1 --port 18080 --reload

# 终端 2：前端
cd frontend && npm install && npm run dev
```

| 地址 | 说明 |
|------|------|
| http://localhost:5173 | 前端管理台 |
| http://localhost:18080/docs | Swagger API 文档 |
| http://localhost:18080/api/v1/health | 健康检查 |
| http://localhost:18080/api/v1/metrics | Prometheus 指标 |

### 5. Docker 部署（可选）

```bash
# 启动 Milvus + Prometheus + Grafana
docker compose up -d

# Grafana 监控大盘
# http://localhost:3000 (admin/admin)
```

---

## 🏗️ 技术架构

```
┌─────────────────────────────────────────────────────┐
│                    前端 (React 19 + Vite)             │
│         文档管理 · 智能问答 · 检索测试 · 评测工作台      │
└─────────────────┬───────────────────────────────────┘
                  │ HTTP REST API
┌─────────────────▼───────────────────────────────────┐
│                   后端 (FastAPI)                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐            │
│  │ 文档摄取  │ │  检索服务  │ │  问答服务  │            │
│  │10+解析器  │ │稠密/混合  │ │多轮/单轮  │            │
│  │6种切分策略│ │Rerank    │ │引用落地  │            │
│  └──────────┘ └──────────┘ └──────────┘            │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐            │
│  │ IM 机器人 │ │  权限系统  │ │  评测引擎  │            │
│  │飞书/企微  │ │可插拔接口 │ │Hit@k/MRR │            │
│  └──────────┘ └──────────┘ └──────────┘            │
└─────────────────┬───────────────────────────────────┘
                  │
    ┌─────────────┼─────────────┐
    ▼             ▼             ▼
┌───────┐  ┌──────────┐  ┌──────────┐
│PostgreSQL│ │ Milvus    │  │  Redis   │
│ 元数据   │  │ 向量存储  │  │ 缓存/会话│
│ 对话记录 │  │ 语义检索  │  │ 去重    │
└───────┘  └──────────┘  └──────────┘
```

---

## 📡 API 概览

所有接口文档：http://localhost:18080/docs

| 模块 | 端点 | 说明 |
|------|------|------|
| 文档管理 | `POST /api/v1/documents/upload` | 上传文档 |
| 文档管理 | `GET /api/v1/documents` | 文档列表 |
| 检索 | `POST /api/v1/retrieval/search` | 知识检索 |
| 检索 | `POST /api/v1/retrieval/debug-search` | 调试检索 |
| 问答 | `POST /api/v1/qa/answer` | 单次问答 |
| 受控 Agent | `POST /api/v1/qa/agent-answer` | 证据门控、一次改写重试与执行轨迹 |
| 多轮对话 | `POST /api/v1/qa/conversations` | 创建对话 |
| 多轮对话 | `POST /api/v1/qa/conversations/{id}/messages` | 发送消息 |
| Dify | `POST /api/v1/dify/retrieval` | Dify 外部知识库 |
| 飞书 | `POST /api/v1/feishu/events` | 飞书事件回调 |
| 企微 | `POST /api/v1/wecom/callback` | 企微消息回调 |
| 评测 | `POST /api/v1/evaluation/runs` | 创建评测运行 |
| 切分 | `POST /api/v1/chunking/preview` | 切分预览 |
| 系统 | `GET /api/v1/health` | 健康检查 |
| 监控 | `GET /api/v1/metrics` | Prometheus 指标 |

---

## 📂 项目结构

```
rag-knowledge-system/
├── app/
│   ├── api/              # 19 个路由模块
│   │   ├── documents.py      # 文档 CRUD + 上传
│   │   ├── retrieval.py      # 检索 + 调试检索
│   │   ├── qa.py             # 单次问答
│   │   ├── qa_conversation.py # 多轮对话
│   │   ├── dify.py           # Dify 集成
│   │   ├── feishu.py         # 飞书机器人
│   │   ├── wecom.py          # 企微机器人
│   │   ├── evaluation.py     # 评测管理
│   │   ├── chunking.py       # 切分配置
│   │   ├── health.py         # 健康检查 + 指标
│   │   └── ...
│   ├── core/             # 配置 / 错误 / 中间件 / 日志 / 指标
│   ├── db/               # SQLAlchemy 基类 / 会话 / 类型
│   ├── ingestion/        # 文档解析器 + 切分引擎
│   │   ├── parsers/          # 10 种解析器
│   │   └── chunking.py       # 6 种切分策略
│   ├── integrations/     # 外部服务集成
│   │   ├── llm.py            # LLM 调用（OpenAI 兼容）
│   │   ├── embedding.py      # Embedding 向量化
│   │   ├── vector_store.py   # Milvus/Zilliz/本地 向量存储
│   │   ├── image_recognition.py # PaddleOCR + 多模态
│   │   ├── rerank.py         # 重排序
│   │   ├── feishu_client.py  # 飞书 SDK
│   │   └── wecom_client.py   # 企微 SDK
│   ├── models/           # 15 个 SQLAlchemy 数据模型
│   ├── permissions/      # 可插拔权限检查器
│   ├── repositories/     # 数据访问层
│   ├── retrieval/        # 混合检索 / BM25 / 评分融合
│   ├── schemas/          # Pydantic 请求/响应模型
│   └── services/         # 业务逻辑层（18 个服务）
├── frontend/             # React 19 + Vite + TypeScript
│   └── src/
│       ├── pages/            # 7 个页面
│       ├── components/       # 通用组件
│       └── api.ts            # 后端 API 调用
├── migrations/           # Alembic 数据库迁移（14 个文件）
├── tests/                # 27 个测试文件
├── scripts/              # 运维脚本
├── docs/                 # 设计文档 / 接入指南 / 接口契约
├── grafana-dashboards/   # Grafana 监控大盘
├── docker-compose.yml    # Milvus + Prometheus + Grafana
├── prometheus.yml        # Prometheus 采集配置
├── requirements.txt
└── .env.example
```

第一阶段：架构总览（5 个文件，30 分钟）

app/main.py           → 入口：FastAPI 组装中间件、路由、生命周期
app/api/router.py     → 一眼看清所有功能模块
app/core/config.py    → 60+ 配置项如何管理
app/db/base.py        → ORM 基类
app/db/session.py     → 数据库会话
第二阶段：核心链路（8 个文件，60 分钟）

app/api/documents.py            → 文档上传入口
app/ingestion/parser_registry.py → 10 种解析器注册
app/ingestion/chunking.py       → 6 种切分策略
app/integrations/embedding.py   → 文本 → 向量
app/integrations/vector_store.py → 向量存入 Milvus
app/services/retrieval.py       → 检索完整流程
app/integrations/llm.py         → LLM 生成答案
app/services/qa.py              → 串联检索→生成
第三阶段：扩展功能（按需）
想看 IM 机器人→ app/api/feishu.py，想看评测→ app/services/evaluation.py，想看监控→ app/core/metrics.py。

第四阶段：数据层
app/models/ 全部 15 个模型 + migrations/versions/ 迁移历史。















---

## 🔧 配置参考

企业部署下的 OpenSearch、Milvus/Zilliz、Celery/Redis、阈值校准和连接池配置见 [企业化升级指南](docs/enterprise_upgrade_guide.md)。

完整配置项见 `.env.example`，以下为关键配置：

| 分类 | 变量 | 默认值 | 说明 |
|------|------|--------|------|
| 数据库 | `DATABASE_URL` | — | PostgreSQL 连接串（或分别配 HOST/DB/USER/PASSWORD） |
| 向量库 | `VECTOR_STORE_PROVIDER` | `local` | `local` / `milvus` / `zilliz` |
| 向量库 | `MILVUS_URI` | `localhost:19530` | 本地 Milvus 地址 |
| LLM | `LLM_API_BASE` | — | LLM API 地址（OpenAI 兼容） |
| LLM | `LLM_MODEL` | — | 模型名称，如 `qwen-plus` |
| Embedding | `EMBEDDING_API_BASE` | — | Embedding API 地址 |
| Embedding | `EMBEDDING_MODEL` | — | 如 `text-embedding-v3` |
| 图片 | `IMAGE_RECOGNITION_PROVIDER` | `paddle_ocr` | `paddle_ocr` / `multimodal` |
| 图片 | `MULTIMODAL_MODEL` | — | 如 `qwen-vl-plus` |
| 权限 | `PERMISSION_MODE` | `none` | `none` / `plugin` |
| Dify | `DIFY_APP_KEY` | — | Dify 接入密钥 |
| 摄入 | `INGESTION_MODE` | `sync` | `sync` / `async` |

---

## 🧪 测试

```bash
# 全量测试
pytest tests/ -x -q

# 核心链路测试
pytest tests/test_retrieval_qa.py -v

# 切分策略测试
pytest tests/test_chunking_strategies.py -v

# 图片识别测试
pytest tests/test_image_recognition.py -v
```

---

## 🤝 致谢

感谢 [linux.do](https://linux.do) 社区的支持！
