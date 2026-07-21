# 企业化升级指南

本项目保留本地默认模式，方便开发和测试；以下配置将截图中的七项限制切换为可部署的企业模式。外部组件需要由运行环境提供，代码不会在没有集群的情况下伪造可用状态。

## 1. 进程内 BM25 -> OpenSearch

设置：

```env
SPARSE_INDEX_PROVIDER=opensearch
OPENSEARCH_URL=https://opensearch.example.com:9200
OPENSEARCH_USERNAME=service_user
OPENSEARCH_PASSWORD=service_password
OPENSEARCH_INDEX=oa_rag_sparse_chunks
```

项目中的 `SparseIndexProvider` 会切换到 OpenSearch：摄取时增量写入/删除 Chunk，查询时执行 `multi_match`，避免多应用实例各自维护内存 BM25。首次切换或全量修复时执行：

```powershell
python scripts\rebuild_sparse_index.py
```

生产运维需负责 OpenSearch 的分片、副本、快照、访问控制和容量告警；当前代码默认创建 1 主分片、1 副本，实际规模应按数据量调整。

## 2. 本地向量内存 -> Milvus/Zilliz

本地 `local` Provider 仅用于开发，服务重启会丢失内存向量。生产需使用已有持久化 Provider：

```env
VECTOR_STORE_PROVIDER=milvus
MILVUS_URI=http://milvus:19530
MILVUS_COLLECTION=oa_rag_chunks
```

或：

```env
VECTOR_STORE_PROVIDER=zilliz
ZILLIZ_URI=https://<cluster>.api.gcp-us-west1.zillizcloud.com
ZILLIZ_TOKEN=<token>
ZILLIZ_COLLECTION=oa_rag_chunks
```

评测仍使用 `eval_<run_uuid>` 独立集合，运行删除或失败时清理临时索引。

## 3. 同步评测/无消息队列 -> Celery + Redis

本地可用 `sync`；应用内后台线程可用 `background`；多实例生产使用 `celery`：

```env
EVALUATION_EXECUTION_MODE=celery
CELERY_BROKER_URL=redis://:password@redis:6379/0
CELERY_RESULT_BACKEND=redis://:password@redis:6379/1
```

启动 Worker：

```powershell
python -m celery -A app.tasks.evaluation_tasks.celery_app worker --loglevel=INFO
```

创建评测 Run 后接口会立即返回 `pending`，Worker 使用独立数据库会话执行向量化、检索和指标计算。任务定义了两次指数退避重试；运行状态仍是最终真相，前端通过查询 Run 详情轮询状态。

## 4. 固定证据阈值 -> 策略化阈值

```env
EVIDENCE_THRESHOLD_DENSE=0.35
EVIDENCE_THRESHOLD_HYBRID=0.55
```

Dense 余弦分数和 Hybrid 融合分数分布不同，不能共用一个硬编码阈值。当前 Agent 会按策略选择阈值，并把实际值写入 `AgentTraceStep.evidence_threshold`。上线前应基于业务 Golden Set 扫描阈值，综合无依据回答率、拒答率和 Hit@K 选择配置。

## 5. 启发式 Query Rewrite -> LLM 结构化改写

```env
AGENT_QUERY_REWRITE_MODE=auto
AGENT_QUERY_REWRITE_MAX_TOKENS=96
```

`auto` 模式下，配置 LLM 后优先调用 JSON 合约的改写接口：`{"query": "..."}`；未配置、超时或返回非法 JSON 时降级回启发式规则。`llm` 模式用于强制使用模型改写，`heuristic` 用于只走本地规则。改写调用会写入 LLM 调用日志，便于分析成本和失败率。

## 6. 连接池与健康检查

```env
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_POOL_TIMEOUT_SECONDS=30
DB_POOL_RECYCLE_SECONDS=1800
HEALTH_PROBE_EXTERNAL_SERVICES=true
```

非 SQLite 数据库启用 `pool_pre_ping`、连接回收和可配置池容量。`GET /api/v1/health` 返回 `database_pool`，`GET /api/v1/metrics` 暴露 `db_pool_available`。Redis 健康探针使用 1 秒连接/读写超时，避免 Redis 宕机拖慢健康检查。

## 7. 企业部署验收顺序

1. PostgreSQL、Redis、Milvus/Zilliz、OpenSearch 可用，并配置服务账号与网络策略。
2. 设置 `VECTOR_STORE_PROVIDER=milvus|zilliz`、`SPARSE_INDEX_PROVIDER=opensearch`、`EVALUATION_EXECUTION_MODE=celery`。
3. 执行 `python scripts\rebuild_sparse_index.py`，再从文档管理任务重建向量索引。
4. 启动 API 与 Celery Worker，检查 `/health`、`/metrics` 与评测 Run 状态流转。
5. 使用业务 Golden Set 校准 Dense/Hybrid 阈值，记录 Hit@K、MRR、拒答正确率、P95 延迟与单问成本。

## 面试口径

可以说：项目将本地开发实现与企业部署 Provider 解耦，默认内存模式用于快速验证；生产切换到 OpenSearch、Milvus/Zilliz、Celery/Redis 后，分别解决多实例稀疏索引不一致、向量重启丢失和长任务阻塞请求线程的问题。

不能说：本地测试模式已经等同于多实例生产集群。外部服务的高可用、备份、容量规划、告警与权限策略仍需要在真实部署环境中完成。
