# Milvus 生产集成 + 监控指标体系 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 生产级 Milvus 向量存储 + Prometheus 监控指标，覆盖 HTTP/业务/系统三层可观测性。

**Architecture:** 配置层新增 milvus provider 选项，vector_store.py 增加 milvus 分支（复用现有 MilvusClient）；新建 metrics.py 集中管理 Prometheus 指标，在 middleware/llm/embedding/retrieval/vector_store 等关键路径埋点，health 路由新增 /metrics 端点。

**Tech Stack:** pymilvus + prometheus-client + docker-compose (Milvus + etcd + MinIO + Prometheus + Grafana)

---

## Part A: Milvus 生产集成

### Task 1: 依赖配置

**Files:**
- Modify: `requirements.txt`
- Modify: `.env.example`

- [ ] **Step 1: 锁定 pymilvus 版本**

编辑 `requirements.txt`，确认 pymilvus 行存在：

```
pymilvus>=2.5,<3.0
```

无需新增依赖，pymilvus 已在 requirements.txt 中。

- [ ] **Step 2: .env.example 新增 milvus 配置**

编辑 `.env.example`，在现有 Zilliz 配置块后追加：

```env
# 本地 Milvus（无认证，无需填 MILVUS_TOKEN）
MILVUS_URI=http://localhost:19530
MILVUS_TOKEN=
MILVUS_COLLECTION=oa_rag_chunks
```

- [ ] **Step 3: 同步更新实际 .env**

编辑 `e:\xm\rag-knowledge-system-main\.env`，在 `VECTOR_STORE_PROVIDER=local` 行后追加同样的 milvus 配置块。

- [ ] **Step 4: 提交**

```bash
git add requirements.txt .env.example .env
git commit -m "feat: add milvus configuration entries"
```

---

### Task 2: Config 层 — 新增 milvus 字段 + 修复 provider 切换逻辑

**Files:**
- Modify: `app/core/config.py:72-81, 246-247`

- [ ] **Step 1: 新增 milvus 配置字段**

在 `zilliz_collection` 定义后（约第 81 行后）追加：

```python
    milvus_uri: str | None = Field(
        default="http://localhost:19530",
        validation_alias=AliasChoices("MILVUS_URI", "milvus_uri"),
    )
    milvus_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MILVUS_TOKEN", "milvus_token"),
    )
    milvus_collection: str = Field(
        default="oa_rag_chunks",
        validation_alias=AliasChoices("MILVUS_COLLECTION", "milvus_collection"),
    )
```

- [ ] **Step 2: 修复 provider 自动切换逻辑**

将第 246-247 行：

```python
        if self.vector_store_provider == "local" and self.zilliz_uri and self.zilliz_token:
            self.vector_store_provider = "zilliz"
```

替换为（支持 milvus/zilliz 显式配置 + 自动检测）：

```python
        # 归一化 vector_store_provider，支持 milvus / zilliz / local
        self.vector_store_provider = self.vector_store_provider.lower()
        if self.vector_store_provider == "local":
            if self.zilliz_uri and self.zilliz_token:
                self.vector_store_provider = "zilliz"
            elif self.milvus_uri:
                self.vector_store_provider = "milvus"
```

- [ ] **Step 3: 在 validate_required_config 末尾追加 milvus 字段归一化**

在 `validate_required_config` 方法中（约第 206 行附近），zilliz 归一化代码后追加：

```python
        self.milvus_uri = self._normalize_text(self.milvus_uri)
        self.milvus_token = self._normalize_text(self.milvus_token)
```

- [ ] **Step 4: 添加 is_external_service_configured 的 milvus 项**

编辑 `is_external_service_configured` 方法中的 `mapping` dict，在 `"zilliz"` 项后追加：

```python
            "milvus": bool(self.milvus_uri),
```

- [ ] **Step 5: 验证**

```bash
cd e:\xm\rag-knowledge-system-main && python -c "from app.core.config import get_settings; s=get_settings(); print(f'provider={s.vector_store_provider}'); print('OK')"
```

- [ ] **Step 6: 提交**

```bash
git add app/core/config.py
git commit -m "feat: add milvus config fields and auto-detect logic"
```

---

### Task 3: VectorStoreClient — 支持 milvus provider

**Files:**
- Modify: `app/integrations/vector_store.py:31-90, 285-286, 69-84`

- [ ] **Step 1: 修改 _provider() 方法**

将第 285-286 行：

```python
    def _provider(self) -> str:
        return self.settings.vector_store_provider.lower()
```

替换为：

```python
    def _provider(self) -> str:
        provider = self.settings.vector_store_provider.lower()
        if provider in ("milvus", "zilliz", "local"):
            return provider
        return "local"
```

- [ ] **Step 2: 修改路由逻辑 — upsert/search/delete**

将三处 `if self._provider() == "zilliz"` 改为 `if self._provider() in ("milvus", "zilliz")`：

**upsert_embeddings** (第 43 行)：
```python
        if self._provider() in ("milvus", "zilliz"):
            return self._upsert_milvus(embeddings=embeddings, metadatas=metadatas)
```

**search** (第 54 行)：
```python
        if self._provider() in ("milvus", "zilliz"):
            return self._search_milvus(query_embedding=query_embedding, top_k=top_k, filters=filters)
```

**delete_embeddings** (第 65 行)：
```python
        if self._provider() in ("milvus", "zilliz"):
            return self._delete_milvus(chunk_ids=chunk_ids)
```

- [ ] **Step 3: 重命名方法 + 适配 milvus**

将 `_upsert_zilliz_placeholder` 重命名为 `_upsert_milvus`，修改第 157 行方法名：

```python
    def _upsert_milvus(
```

方法内部，将 `UriAndToken` 获取逻辑改为按 provider 区分：

```python
        if self._provider() == "milvus":
            uri = self.settings.milvus_uri
            token = self.settings.milvus_token or ""
            collection = self.settings.milvus_collection
        else:
            uri = self.settings.zilliz_uri
            token = self.settings.zilliz_token
            collection = self.settings.zilliz_collection

        if not uri:
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message=f"{self._provider()} provider uri is not configured",
                status_code=422,
            )
```

然后创建 client：
```python
        client = MilvusClient(uri=uri, token=token or "")
        self._ensure_collection(client=client, data_type_cls=DataType, collection=collection)
```

同样修改 `_search_zilliz_placeholder` → `_search_milvus`（第 224 行方法名）和 `_delete_zilliz` → `_delete_milvus`（第 196 行方法名），内部同样按 provider 获取 uri/token/collection。

- [ ] **Step 4: 修改 probe() 支持 milvus**

将第 69-84 行的 `probe()` 方法替换为：

```python
    def probe(self) -> bool:
        provider = self._provider()
        if provider == "local":
            return True
        if provider == "milvus":
            if not self.settings.milvus_uri:
                return False
            try:
                from pymilvus import MilvusClient
            except ImportError:
                return False
            try:
                client = MilvusClient(uri=self.settings.milvus_uri, token=self.settings.milvus_token or "")
                client.has_collection(collection_name=self.settings.milvus_collection)
                return True
            except Exception:
                logger.exception("Milvus probe failed")
                return False
        # zilliz
        if not self.settings.zilliz_uri or not self.settings.zilliz_token:
            return False
        try:
            from pymilvus import MilvusClient
        except ImportError:
            return False
        try:
            client = MilvusClient(uri=self.settings.zilliz_uri, token=self.settings.zilliz_token)
            client.has_collection(collection_name=self.settings.zilliz_collection)
            return True
        except Exception:
            logger.exception("Zilliz probe failed")
            return False
```

- [ ] **Step 5: 修改 _ensure_collection 接收 collection 参数 + 区分索引类型**

找到 `_ensure_collection` 方法（约第 288 行），修改签名：

```python
    def _ensure_collection(self, *, client, data_type_cls, collection: str) -> None:
        if client.has_collection(collection_name=collection):
            return
```

方法内部，索引类型按 provider 区分（替换 `index_type="AUTOINDEX"`）：

```python
        index_type = "IVF_FLAT" if self._provider() == "milvus" else "AUTOINDEX"
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type=index_type,
            metric_type="COSINE",
            params={"nlist": 1024} if index_type == "IVF_FLAT" else {},
        )
```

所有 `self.settings.zilliz_collection` 引用改为使用传入的 `collection` 参数。

- [ ] **Step 6: 验证**

```bash
cd e:\xm\rag-knowledge-system-main && python -c "from app.integrations.vector_store import VectorStoreClient; print('OK')"
```

- [ ] **Step 7: 提交**

```bash
git add app/integrations/vector_store.py
git commit -m "feat: add milvus provider support to VectorStoreClient"
```

---

### Task 4: Health Service — Milvus 健康检查

**Files:**
- Modify: `app/services/health.py`

- [ ] **Step 1: 修改健康检查，区分 milvus/zilliz/local**

找到 `HealthService` 中向量存储相关的健康检查代码，将 `zilliz` 相关逻辑改为根据 `vector_store_provider` 区分：

```python
        provider = settings.vector_store_provider
        if provider == "local":
            health["vector_store"] = "local"
        elif provider == "milvus":
            health["vector_store"] = "up" if client.probe() else "down"
        elif provider == "zilliz":
            health["vector_store"] = "up" if client.probe() else "down"
        else:
            health["vector_store"] = "unknown"
```

- [ ] **Step 2: 验证健康检查**

启动应用后测试：
```bash
curl -s http://127.0.0.1:18080/api/v1/health | python -m json.tool
```

预期 `vector_store` 字段为 `"local"`（Milvus 未启动时）或 `"up"`（Milvus 已启动时）。

- [ ] **Step 3: 提交**

```bash
git add app/services/health.py
git commit -m "feat: add milvus health check support"
```

---

### Task 5: docker-compose.yml

**Files:**
- Create: `docker-compose.yml`

- [ ] **Step 1: 创建 docker-compose.yml**

新建项目根目录 `docker-compose.yml`：

```yaml
version: "3.8"

services:
  etcd:
    image: quay.io/coreos/etcd:v3.5.18
    environment:
      - ETCD_AUTO_COMPACTION_MODE=revision
      - ETCD_AUTO_COMPACTION_RETENTION=1000
      - ETCD_QUOTA_BACKEND_BYTES=4294967296
      - ETCD_SNAPSHOT_COUNT=50000
    volumes:
      - ./data/etcd:/etcd
    command: etcd -advertise-client-urls=http://127.0.0.1:2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd
    healthcheck:
      test: ["CMD", "etcdctl", "endpoint", "health"]
      interval: 30s
      timeout: 20s
      retries: 3

  minio:
    image: minio/minio:RELEASE.2024-12-18T13-15-44Z
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    volumes:
      - ./data/minio:/minio_data
    command: minio server /minio_data --console-address ":9001"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 30s
      timeout: 20s
      retries: 3

  milvus:
    image: milvusdb/milvus:v2.5.12
    container_name: milvus-standalone
    command: ["milvus", "run", "standalone"]
    security_opt:
      - seccomp:unconfined
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000
      MINIO_ACCESS_KEY_ID: minioadmin
      MINIO_SECRET_ACCESS_KEY: minioadmin
    volumes:
      - ./data/milvus:/var/lib/milvus
    ports:
      - "19530:19530"
      - "9091:9091"
    depends_on:
      etcd:
        condition: service_healthy
      minio:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9091/healthz"]
      interval: 30s
      timeout: 20s
      retries: 3

  prometheus:
    image: prom/prometheus:v2.55.0
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - ./data/prometheus:/prometheus
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.path=/prometheus"

  grafana:
    image: grafana/grafana:11.3.0
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=admin
    volumes:
      - ./data/grafana:/var/lib/grafana
      - ./grafana-dashboards:/etc/grafana/provisioning/dashboards
```

- [ ] **Step 2: 创建 prometheus.yml**

新建 `prometheus.yml`：

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: "knowledge-base"
    metrics_path: "/api/v1/metrics"
    static_configs:
      - targets: ["host.docker.internal:18080"]
```

- [ ] **Step 3: 提交**

```bash
git add docker-compose.yml prometheus.yml
git commit -m "feat: add docker-compose with Milvus + Prometheus + Grafana"
```

---

## Part B: 监控指标体系

### Task 6: 核心 metrics 模块

**Files:**
- Create: `app/core/metrics.py`

- [ ] **Step 1: 创建 metrics.py**

新建 `app/core/metrics.py`：

```python
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, generate_latest, REGISTRY


# ===== HTTP 层 =====
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=REGISTRY,
)

# ===== LLM 层 =====
llm_requests_total = Counter(
    "llm_requests_total",
    "Total LLM API requests",
    ["model", "status"],
    registry=REGISTRY,
)

llm_request_duration_seconds = Histogram(
    "llm_request_duration_seconds",
    "LLM API request duration",
    ["model"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0],
    registry=REGISTRY,
)

llm_tokens_total = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed",
    ["model", "type"],
    registry=REGISTRY,
)

# ===== Embedding 层 =====
embedding_requests_total = Counter(
    "embedding_requests_total",
    "Total embedding API requests",
    ["model", "status"],
    registry=REGISTRY,
)

embedding_request_duration_seconds = Histogram(
    "embedding_request_duration_seconds",
    "Embedding API request duration",
    ["model"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=REGISTRY,
)

# ===== 检索层 =====
retrieval_duration_seconds = Histogram(
    "retrieval_duration_seconds",
    "Retrieval operation duration",
    ["strategy"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
    registry=REGISTRY,
)

# ===== 文档上传 =====
documents_uploaded_total = Counter(
    "documents_uploaded_total",
    "Total documents uploaded",
    ["source_type", "status"],
    registry=REGISTRY,
)

# ===== 问答 =====
qa_questions_total = Counter(
    "qa_questions_total",
    "Total QA questions asked",
    registry=REGISTRY,
)

# ===== 向量存储 =====
vector_store_operations_total = Counter(
    "vector_store_operations_total",
    "Total vector store operations",
    ["provider", "operation"],
    registry=REGISTRY,
)

vector_store_operation_duration_seconds = Histogram(
    "vector_store_operation_duration_seconds",
    "Vector store operation duration",
    ["provider", "operation"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
    registry=REGISTRY,
)

# ===== 系统层 =====
db_pool_available = Gauge(
    "db_pool_available",
    "Database connection pool available connections",
    registry=REGISTRY,
)

service_health = Gauge(
    "service_health",
    "External service health (1=up, 0=down)",
    ["service"],
    registry=REGISTRY,
)


def get_metrics_response() -> bytes:
    """返回 Prometheus 格式的指标数据"""
    return generate_latest(REGISTRY)
```

- [ ] **Step 2: 验证**

```bash
pip install prometheus-client && cd e:\xm\rag-knowledge-system-main && python -c "from app.core.metrics import get_metrics_response; print('OK')"
```

- [ ] **Step 3: 提交**

```bash
git add app/core/metrics.py requirements.txt
git commit -m "feat: add Prometheus metrics module"
```

---

### Task 7: HTTP 层埋点

**Files:**
- Modify: `app/core/middleware.py`
- Modify: `app/main.py`

- [ ] **Step 1: 在 middleware.py 中新增 MetricsMiddleware**

在 `app/core/middleware.py` 文件末尾追加：

```python
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.metrics import http_requests_total, http_request_duration_seconds


class MetricsMiddleware(BaseHTTPMiddleware):
    _skip_paths = {"/api/v1/health", "/api/v1/metrics", "/docs", "/openapi.json", "/favicon.ico"}

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        method = request.method

        if path not in self._skip_paths:
            start = time.perf_counter()
            response = await call_next(request)
            elapsed = time.perf_counter() - start
            http_requests_total.labels(method=method, path=path, status=str(response.status_code)).inc()
            http_request_duration_seconds.labels(method=method, path=path).observe(elapsed)
            return response

        return await call_next(request)
```

- [ ] **Step 2: 在 main.py 注册 MetricsMiddleware**

编辑 `app/main.py`，在 `from app.core.middleware import TraceIdMiddleware` 后追加 import：

```python
from app.core.middleware import MetricsMiddleware, TraceIdMiddleware
```

在 `app.add_middleware(TraceIdMiddleware)` 后追加：

```python
    app.add_middleware(MetricsMiddleware)
```

- [ ] **Step 3: 验证**

```bash
cd e:\xm\rag-knowledge-system-main && python -c "from app.main import app; print('OK')"
```

- [ ] **Step 4: 提交**

```bash
git add app/core/middleware.py app/main.py
git commit -m "feat: add HTTP metrics middleware"
```

---

### Task 8: 业务层埋点 — LLM + Embedding

**Files:**
- Modify: `app/integrations/llm.py`
- Modify: `app/integrations/embedding.py`

- [ ] **Step 1: LLM 调用埋点**

编辑 `app/integrations/llm.py`，在文件顶部 import 区追加：

```python
from app.core.metrics import llm_requests_total, llm_request_duration_seconds, llm_tokens_total
```

在 `generate_answer()` 方法中，`started = perf_counter()` 行之后、`data = post_json_with_retries(...)` 调用之后，找 `return LlmResult(...)` 语句**之前**，追加埋点代码：

```python
        model_name = data.get("model") or self.settings.llm_model or "unknown"
        llm_requests_total.labels(model=model_name, status="success").inc()
        llm_request_duration_seconds.labels(model=model_name).observe(latency_ms / 1000.0)
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        if prompt_tokens:
            llm_tokens_total.labels(model=model_name, type="prompt").inc(prompt_tokens)
        if completion_tokens:
            llm_tokens_total.labels(model=model_name, type="completion").inc(completion_tokens)
```

在 `raise AppError(...)` 语句前（即失败路径）追加：

```python
        model_name = self.settings.llm_model or "unknown"
        llm_requests_total.labels(model=model_name, status="failed").inc()
```

- [ ] **Step 2: Embedding 调用埋点**

编辑 `app/integrations/embedding.py`，在顶部 import 区追加：

```python
from app.core.metrics import embedding_requests_total, embedding_request_duration_seconds
```

在 `_embed_via_http()` 方法的 `elapsed_ms` 计算后（约第 137 行）、`return all_results` 前追加：

```python
        model_name = model_name or "unknown"
        embedding_requests_total.labels(model=model_name, status="success").inc(len(all_results))
        embedding_request_duration_seconds.labels(model=model_name).observe(elapsed_ms / 1000.0)
```

在 `raise AppError(...)` 前（失败路径）追加：

```python
        model_name = self.settings.embedding_model or "unknown"
        embedding_requests_total.labels(model=model_name, status="failed").inc(len(texts) if texts else 0)
```

- [ ] **Step 3: 提交**

```bash
git add app/integrations/llm.py app/integrations/embedding.py
git commit -m "feat: add LLM and Embedding metrics instrumentation"
```

---

### Task 9: 业务层埋点 — 检索 + 向量存储 + 文档上传 + 问答

**Files:**
- Modify: `app/services/retrieval.py`
- Modify: `app/integrations/vector_store.py`
- Modify: `app/api/documents.py`
- Modify: `app/api/qa_conversation.py`

- [ ] **Step 1: 检索埋点**

编辑 `app/services/retrieval.py`，在 import 区追加：

```python
from app.core.metrics import retrieval_duration_seconds
```

在 `search()` 方法返回前，找到耗时计算后的位置，追加：

```python
        retrieval_duration_seconds.labels(strategy=strategy).observe(total_latency / 1000.0)
```

（`strategy` 变量名需根据实际代码调整，如 `search_request.strategy` 或实际的策略名）

- [ ] **Step 2: 向量存储操作埋点**

编辑 `app/integrations/vector_store.py`，在 import 区追加：

```python
from app.core.metrics import vector_store_operations_total, vector_store_operation_duration_seconds
```

在 `_upsert_milvus` / `_upsert_local` / `_search_milvus` / `_search_local` 等方法中，操作前后包裹：

```python
        import time
        provider = self._provider()
        t0 = time.perf_counter()
        # ... 原有 upsert/search 逻辑 ...
        vector_store_operations_total.labels(provider=provider, operation="upsert").inc()
        vector_store_operation_duration_seconds.labels(provider=provider, operation="upsert").observe(time.perf_counter() - t0)
```

（为每个 upsert/search/delete 方法分别加埋点，operation 标签对应 `"upsert"`/`"search"`/`"delete"`）

- [ ] **Step 3: 文档上传埋点**

编辑 `app/api/documents.py`，在 import 区追加：

```python
from app.core.metrics import documents_uploaded_total
```

在 `upload_document()` 函数返回前追加：

```python
    documents_uploaded_total.labels(source_type=source_type, status="success").inc()
```

在异常处理（如有）中追加失败计数。

- [ ] **Step 4: 问答埋点**

编辑 `app/api/qa_conversation.py`，在 import 区追加：

```python
from app.core.metrics import qa_questions_total
```

在 `send_message()` 函数返回前追加：

```python
    qa_questions_total.inc()
```

- [ ] **Step 5: 提交**

```bash
git add app/services/retrieval.py app/integrations/vector_store.py app/api/documents.py app/api/qa_conversation.py
git commit -m "feat: add retrieval, vector store, document, qa metrics"
```

---

### Task 10: /metrics 端点 + 系统层指标

**Files:**
- Modify: `app/api/health.py`
- Modify: `app/core/metrics.py` (更新 service_health)

- [ ] **Step 1: 新增 /metrics 端点**

编辑 `app/api/health.py`，在文件末尾追加：

```python
from fastapi.responses import Response as FastAPIResponse
from app.core.metrics import get_metrics_response, service_health as service_health_gauge
from app.integrations.vector_store import VectorStoreClient
from app.integrations.embedding import EmbeddingProvider
from app.integrations.llm import LlmProvider


@router.get("/metrics")
def metrics(
    settings: Settings = Depends(get_settings_dep),
    session: Session = Depends(get_session),
):
    # 更新 service_health gauge
    try:
        from sqlalchemy import text
        session.execute(text("SELECT 1"))
        service_health_gauge.labels(service="postgres").set(1)
    except Exception:
        service_health_gauge.labels(service="postgres").set(0)

    try:
        import redis
        r = redis.Redis.from_url(settings.resolved_redis_url)
        r.ping()
        service_health_gauge.labels(service="redis").set(1)
    except Exception:
        service_health_gauge.labels(service="redis").set(0)

    try:
        vs_client = VectorStoreClient(settings)
        vs_ok = vs_client.probe()
        service_health_gauge.labels(service="vector_store").set(1 if vs_ok else 0)
    except Exception:
        service_health_gauge.labels(service="vector_store").set(0)

    return FastAPIResponse(content=get_metrics_response(), media_type="text/plain; version=0.0.4")
```

- [ ] **Step 2: 验证 /metrics 端点**

```bash
curl -s http://127.0.0.1:18080/api/v1/metrics | head -20
```

预期看到 `# HELP` / `# TYPE` 开头的 Prometheus 格式输出。

- [ ] **Step 3: 提交**

```bash
git add app/api/health.py
git commit -m "feat: add GET /metrics endpoint with system health gauges"
```

---

### Task 11: Grafana Dashboard 模板

**Files:**
- Create: `grafana-dashboards/knowledge-base.json`

- [ ] **Step 1: 创建 Grafana Dashboard JSON**

新建 `grafana-dashboards/knowledge-base.json`，内容为以下 JSON（精简版，含 4 个关键面板）：

```json
{
  "title": "Knowledge Base Core",
  "uid": "kb-core",
  "panels": [
    {
      "title": "HTTP Request Rate",
      "targets": [{"expr": "rate(http_requests_total[1m])"}],
      "type": "graph"
    },
    {
      "title": "LLM Latency (P99)",
      "targets": [{"expr": "histogram_quantile(0.99, rate(llm_request_duration_seconds_bucket[5m]))"}],
      "type": "graph"
    },
    {
      "title": "LLM Token Usage",
      "targets": [{"expr": "rate(llm_tokens_total[5m])"}],
      "type": "graph"
    },
    {
      "title": "Service Health",
      "targets": [{"expr": "service_health"}],
      "type": "stat"
    }
  ]
}
```

（此处为精简版，实际完整 dashboard 可后续在 Grafana UI 中编辑导出）

- [ ] **Step 2: 创建 Grafana provisioning 配置**

新建 `grafana-dashboards/dashboard.yml`：

```yaml
apiVersion: 1
providers:
  - name: "default"
    folder: ""
    type: file
    options:
      path: /etc/grafana/provisioning/dashboards
```

- [ ] **Step 3: 提交**

```bash
git add grafana-dashboards/
git commit -m "feat: add Grafana dashboard template"
```

---

## Part C: 集成验证

### Task 12: 端到端验证

- [ ] **Step 1: 启动 Milvus（可选，如果有 Docker）**

```bash
docker compose up -d etcd minio milvus
# 等待约 30 秒
docker compose ps  # 确认 milvus healthy
```

- [ ] **Step 2: 修改 .env 切换 provider**

```env
VECTOR_STORE_PROVIDER=milvus
MILVUS_URI=http://localhost:19530
```

- [ ] **Step 3: 重启应用并验证健康检查**

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 18080
```

```bash
# 验证健康检查
curl -s http://127.0.0.1:18080/api/v1/health | python -m json.tool
# 预期 vector_store 字段为 "up"（Milvus 启动时）或 "down"（未启动时）
```

- [ ] **Step 4: 验证 /metrics 端点**

```bash
curl -s http://127.0.0.1:18080/api/v1/metrics | grep -E "^http_|^llm_|^embedding_|^retrieval|^documents_|^qa_|^vector_store_|^service_health"
# 预期看到所有指标类型
```

- [ ] **Step 5: 触发业务埋点**

上传一个文档，发一条问答，再查 /metrics 确认计数增加：

```bash
# 查看指标变化
curl -s http://127.0.0.1:18080/api/v1/metrics | grep -E "^(documents_uploaded_total|qa_questions_total|llm_requests_total)"
```

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "test: verify Milvus integration and metrics instrumentation"
```

---

## 自检清单

| 检查项 | 状态 |
|--------|:--:|
| Config 层支持 milvus provider | ✅ Task 2 |
| vector_store.py 三方法路由到 milvus | ✅ Task 3 |
| probe/health 支持 milvus | ✅ Task 3, 4 |
| docker-compose 一键部署 | ✅ Task 5 |
| metrics.py 定义 12 个指标 | ✅ Task 6 |
| HTTP 埋点 | ✅ Task 7 |
| LLM + Embedding 埋点 | ✅ Task 8 |
| 检索 + 向量 + 文档 + 问答埋点 | ✅ Task 9 |
| /metrics 端点 + 系统健康 gauge | ✅ Task 10 |
| Grafana dashboard | ✅ Task 11 |
| 端到端验证 | ✅ Task 12 |
| 无 TBD / TODO | ✅ |
