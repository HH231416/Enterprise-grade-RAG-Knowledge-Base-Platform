# 外部服务联调运行手册

`docker-compose.integration.yml` 提供 PostgreSQL、Redis、Milvus（含 etcd 和 MinIO）、OpenSearch、FastAPI API 和 Celery Worker 的单节点真实服务栈。它用于验证网络、鉴权、数据持久化、Hybrid 检索与异步评测，不等同于高可用生产集群。

## 1. 前置条件

- Docker Desktop 已启动，建议分配至少 8 GB 内存和 4 个 CPU。
- Windows + WSL2 使用 OpenSearch 时，若容器启动失败，执行 `wsl -d docker-desktop sysctl -w vm.max_map_count=262144`。
- 有可用的 OpenAI 兼容 Embedding 与 LLM 服务。Embedding 维度必须与 `EMBEDDING_VECTOR_SIZE` 一致；切换维度前必须删除并重建 Milvus collection。

## 2. 启动

```powershell
Copy-Item .env.integration.example .env.integration
# 编辑 .env.integration，填入真实 Embedding/LLM 配置和随机密码/Token。
docker compose --env-file .env.integration -f docker-compose.integration.yml up -d --build
docker compose --env-file .env.integration -f docker-compose.integration.yml ps
```

检查容器日志：

```powershell
docker compose --env-file .env.integration -f docker-compose.integration.yml logs -f api celery-worker
```

如果从零开始清理本地联调数据：

```powershell
docker compose --env-file .env.integration -f docker-compose.integration.yml down -v
```

该命令会删除联调 PostgreSQL、Redis、Milvus、OpenSearch 的卷，不能在需要保留数据时执行。

## 3. 验证链路

```powershell
Invoke-RestMethod http://127.0.0.1:18080/api/v1/health
Invoke-RestMethod http://127.0.0.1:9200/_cluster/health
$env:OA_RAG_API_URL = 'http://127.0.0.1:18080'
$env:INTERNAL_TOKEN = (Get-Content .env.integration | Select-String '^INTERNAL_TOKEN=').ToString().Split('=', 2)[1]
python scripts\external_stack_smoke.py
```

`external_stack_smoke.py` 不使用 TestClient 或 SQLite，会经过已启动 API：

1. 检查 PostgreSQL、Redis、Milvus Health Probe；
2. 上传一个 DOCX，验证 PostgreSQL 元数据、真实 Embedding 与 Milvus 写入；
3. 调用 `debug-search` 的 `hybrid` 模式，要求同时存在 `dense_hits` 与 `sparse_hits`，后者来自 OpenSearch；
4. 调用受控 Agent，验证引用式回答；
5. 创建评测 Run，检查 `pending -> completed`，以此验证 Redis Broker、Celery Worker 与临时 `eval_<run_uuid>` 向量集合；
6. 删除评测 Run 和测试文档，清理临时数据。

脚本生成的 JSON 证据写入 `qa-outputs/external-stack/<时间戳>/smoke-result.json`。

## 4. 联调通过标准

| 组件 | 通过证据 |
|---|---|
| PostgreSQL | `/health` 的 `postgres=up`，上传的 `Document/DocumentChunk` 可被检索，API 重启后仍存在 |
| Redis | `/health` 的 `redis=up`，评测 Run 初始为 `pending`，被 Worker 消费 |
| Milvus | `/health.probes.zilliz=up`，Hybrid 调试请求有 `dense_hits`，API 重启后仍能召回 |
| OpenSearch | `/_cluster/health` 可用，Hybrid 调试请求有非空 `sparse_hits` |
| Celery | Worker 日志执行 `knowledge_base.execute_evaluation_run`，Run 最终为 `completed` |

## 5. 已知边界

- 该 Compose 使用单节点 Milvus 和 OpenSearch；OpenSearch 索引默认 1 个副本，单节点可能为 `yellow`，不影响联调。预发/生产应使用多节点、TLS、服务账号、快照和容量告警。
- 评测服务当前执行隔离 Dense 检索；它验证 Celery 和 Milvus 临时集合，但不将 Hybrid 评测结果作为结论。OpenSearch 由第 3 步 Hybrid `debug-search` 独立验证。
- `APP_ENV=local` 是为了允许本地联调默认权限模式。预发环境必须设置非默认 API Key/内部 Token，并接入真实身份与 `PermissionChecker` 插件。
