# 外部服务联调运行手册

`docker-compose.integration.yml` 提供 PostgreSQL、Redis、Milvus（含 etcd 和 MinIO）、FastAPI API 与 Celery Worker 的单节点真实服务栈。Milvus 的一个 Collection 同时保存 `chunk_text`、稠密向量、BM25 稀疏向量和标量元数据；它用于验证网络、鉴权、持久化、Hybrid 检索与异步评测，不等同于高可用生产集群。

## 1. 前置条件

- Docker Desktop 已启动，建议分配至少 6 GB 内存和 4 个 CPU。
- 有可用的 OpenAI 兼容 Embedding 与 LLM 服务；Embedding 维度必须与 `EMBEDDING_VECTOR_SIZE` 一致。
- 本次架构使用 `MILVUS_COLLECTION=oa_rag_chunks_v2`。旧的仅稠密 Collection 不能原地添加 `sparse_vector` 和 BM25 Function，迁移时必须新建 Collection 并重新摄入文档。

## 2. 启动

```powershell
Copy-Item .env.integration.example .env.integration
# 编辑 .env.integration，填入真实 Embedding/LLM 配置和随机密码/Token。
docker compose --env-file .env.integration -f docker-compose.integration.yml up -d --build
docker compose --env-file .env.integration -f docker-compose.integration.yml ps
```

检查应用日志：

```powershell
docker compose --env-file .env.integration -f docker-compose.integration.yml logs -f api celery-worker
```

从零开始清理联调数据：

```powershell
docker compose --env-file .env.integration -f docker-compose.integration.yml down -v
```

该命令会删除联调 PostgreSQL、Redis 和 Milvus 的卷，不能在需要保留数据时执行。

## 3. 验证链路

```powershell
Invoke-RestMethod http://127.0.0.1:18080/api/v1/health
$env:OA_RAG_API_URL = 'http://127.0.0.1:18080'
$env:INTERNAL_TOKEN = (Get-Content .env.integration | Select-String '^INTERNAL_TOKEN=').ToString().Split('=', 2)[1]
python scripts\external_stack_smoke.py
```

`external_stack_smoke.py` 不使用 TestClient 或 SQLite，会经过已启动 API：

1. 检查 PostgreSQL、Redis、Milvus Health Probe。
2. 上传 DOCX，验证 PostgreSQL 元数据与 Milvus 同 Collection 写入。
3. 调用 `debug-search` 的 `hybrid` 模式，要求同时出现 `dense_hits` 与 `sparse_hits`；两者均来自 Milvus 的不同向量字段。
4. 调用受控 Agent，验证带引用回答。
5. 创建评测 Run，检查 `pending -> completed`，验证 Redis Broker、Celery Worker 和 `eval_<run_uuid>` 临时 Collection。
6. 删除评测 Run 和测试文档，清理临时数据。

脚本生成的 JSON 证据写入 `qa-outputs/external-stack/<时间戳>/smoke-result.json`。

## 4. 联调通过标准

| 组件 | 通过证据 |
|---|---|
| PostgreSQL | `/health` 的 `postgres=up`，上传的 `Document/DocumentChunk` 可被检索，API 重启后仍存在 |
| Redis | `/health` 的 `redis=up`，评测 Run 初始为 `pending`，被 Worker 消费 |
| Milvus | `/health.probes.zilliz=up`，Hybrid 调试请求同时有 `dense_hits` 与 `sparse_hits`，API 重启后仍能召回 |
| Celery | Worker 日志执行 `knowledge_base.execute_evaluation_run`，Run 最终为 `completed` |

## 5. 已知边界

- 当前 Compose 是单节点 Milvus。预发/生产应使用多节点、TLS、服务账号、快照和容量告警。
- Milvus 内置 BM25 的分词、停用词和分析器能力应使用真实校园语料评测；若术语、编号或中文召回退化，先检查 analyzer 配置和 Golden Set 指标，再决定是否调整分词或融合权重。
- `APP_ENV=local` 仅为允许本地联调默认权限模式。预发环境必须设置非默认 API Key/内部 Token，并接入真实身份与 `PermissionChecker` 插件。
