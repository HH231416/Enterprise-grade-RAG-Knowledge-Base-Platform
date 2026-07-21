# Milvus 生产集成 + 监控指标体系 设计文档

**日期**: 2026-06-16
**状态**: 已确认

---

## 一、Milvus 生产集成

### 1.1 现状

- `vector_store.py` 中本地模式用 `dict` 内存存储，**重启即丢失所有向量**
- 已有的 Zilliz 分支要求在 `config.py:246` 同时设置 `zilliz_uri` 和 `zilliz_token` 才切换
- 本地 Milvus（无认证）不满足此条件，无法使用

### 1.2 目标

- 支持本地部署的 Milvus Standalone（pymilvus 直连，无需 token）
- 保留对 Zilliz Cloud 的兼容（有 token 的场景）
- 通过 docker-compose 一键启动 Milvus + 依赖（etcd、MinIO）

### 1.3 配置变更

`.env` 新增：

```env
VECTOR_STORE_PROVIDER=milvus
MILVUS_URI=http://localhost:19530
MILVUS_TOKEN=
MILVUS_COLLECTION=oa_rag_chunks
```

`config.py` 新增字段：

```python
milvus_uri: str = "http://localhost:19530"
milvus_token: str | None = None
milvus_collection: str = "oa_rag_chunks"
```

Provider 切换逻辑改为：
- `VECTOR_STORE_PROVIDER` 显式设为 `milvus` 时走本地 Milvus
- 显式设为 `zilliz` 或有 token 时走 Zilliz Cloud
- 其他情况 fallback 到 local

### 1.4 vector_store.py 改动

- `_provider()` 返回 `"milvus"` / `"zilliz"` / `"local"`
- `probe()` 中 milvus 模式不要求 token
- `_ensure_collection()` 索引类型：本地 Milvus 用 `IVF_FLAT`（nlist=1024），Zilliz Cloud 保持 `AUTOINDEX`
- 复用现有 `MilvusClient` 调用（upsert/search/delete），本地和云端的 API 一致

### 1.5 部署

新增 `docker-compose.yml`（项目根目录），包含：

| 服务 | 镜像 | 端口 |
|------|------|------|
| milvus | milvusdb/milvus:v2.5.12 | 19530, 9091 |
| etcd | quay.io/coreos/etcd:v3.5.18 | 2379 |
| minio | minio/minio:latest | 9000, 9001 |

---

## 二、监控指标体系

### 2.1 现状

- 仅基础 Python logging + TraceIdMiddleware
- 无任何量化指标（QPS、延迟、错误率、Token 消耗）

### 2.2 目标

- Prometheus 格式指标端点 `GET /api/v1/metrics`
- HTTP 层自动埋点 + 业务层手动埋点
- 提供 Grafana Dashboard JSON 模板

### 2.3 指标清单

**HTTP 层**（中间件自动采集）：

| 指标 | 类型 | 标签 |
|------|------|------|
| `http_requests_total` | Counter | method, path, status |
| `http_request_duration_seconds` | Histogram | method, path |

**业务层**（代码显式埋点）：

| 指标 | 类型 | 标签 | 埋点位置 |
|------|------|------|----------|
| `llm_requests_total` | Counter | model, status | `llm.py:generate_answer()` |
| `llm_request_duration_seconds` | Histogram | model | 同上 |
| `llm_tokens_total` | Counter | model, type(prompt/completion) | 同上 |
| `embedding_requests_total` | Counter | model, status | `embedding.py:embed_texts()` |
| `embedding_request_duration_seconds` | Histogram | model | 同上 |
| `retrieval_duration_seconds` | Histogram | strategy | `retrieval.py:search()` |
| `documents_uploaded_total` | Counter | source_type, status | `documents.py:upload_document()` |
| `qa_questions_total` | Counter | — | `qa_conversation.py:send_message()` |
| `vector_store_operations_total` | Counter | provider, operation | `vector_store.py:upsert/search/delete` |
| `vector_store_operation_duration_seconds` | Histogram | provider, operation | 同上 |

**系统层**（健康检查式采集）：

| 指标 | 类型 | 说明 |
|------|------|------|
| `db_pool_available` | Gauge | 数据库连接池可用数 |
| `service_health` | Gauge | postgres/redis/milvus 连通性 (1/0) |

### 2.4 实现架构

```
prometheus-client (Python 库)
  │
  ├── app/core/metrics.py        ← 所有指标定义 + Registry
  ├── app/core/middleware.py     ← HTTP 请求计数/延迟
  ├── app/integrations/llm.py   ← LLM 调用埋点
  ├── app/integrations/embedding.py ← Embedding 调用埋点
  ├── app/services/retrieval.py ← 检索延迟埋点
  ├── app/integrations/vector_store.py ← Milvus 操作埋点
  ├── app/api/documents.py      ← 文档上传计数
  ├── app/api/qa_conversation.py ← 问答计数
  └── app/api/health.py         ← GET /metrics 端点
```

### 2.5 可视化

- 新增 `grafana-dashboard.json`（项目根目录），导入 Grafana 即可使用
- docker-compose 中可选启动 Prometheus + Grafana

---

## 三、文件变更汇总

| 操作 | 文件 | 说明 |
|------|------|------|
| 新建 | `docker-compose.yml` | Milvus + etcd + MinIO + Prometheus + Grafana |
| 新建 | `app/core/metrics.py` | 所有 Prometheus 指标定义 |
| 新建 | `grafana-dashboard.json` | Grafana 仪表盘模板 |
| 修改 | `app/core/config.py` | 新增 milvus_uri/token/collection 配置 |
| 修改 | `app/integrations/vector_store.py` | milvus provider + 指标埋点 |
| 修改 | `app/core/middleware.py` | HTTP 埋点 |
| 修改 | `app/integrations/llm.py` | LLM 调用埋点 |
| 修改 | `app/integrations/embedding.py` | Embedding 调用埋点 |
| 修改 | `app/services/retrieval.py` | 检索延迟埋点 |
| 修改 | `app/api/documents.py` | 文档上传计数 |
| 修改 | `app/api/qa_conversation.py` | 问答计数 |
| 修改 | `app/api/health.py` | 新增 /metrics 端点 |
| 修改 | `app/api/router.py` | 注册 metrics 路由 |
| 修改 | `requirements.txt` | 新增 prometheus-client |
| 修改 | `.env.example` | 新增 milvus 和 metrics 配置 |

## 四、自检

- [x] 无 TBD/TODO
- [x] Milvus 支持无认证本地部署
- [x] Zilliz Cloud 兼容性保留
- [x] 指标覆盖 HTTP / 业务 / 系统三层
- [x] 埋点位置明确到具体方法
- [x] docker-compose 可一键部署完整环境
