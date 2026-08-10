# Kubernetes 生产部署

## 学生查询链路

学生请求固定走 `Hybrid + Rerank`：同一个 Milvus Collection 中的 `vector` 稠密字段和由内置 BM25 Function 生成的 `sparse_vector` 字段各取候选，按 `HYBRID_FUSION_ALPHA` 融合后，调用校园 AI 平台独立部署的 TEI `BAAI/bge-reranker-v2-m3` 服务精排，再交给校园内网 vLLM 生成带引用回答。RAG API 与 Worker 不加载重排模型权重。

部署前必须由校园 AI 平台提供 `tei-reranker.ai-platform.svc.cluster.local/rerank` 服务，并在 `rag-system-secrets` 填入其服务令牌。API 和 Worker 通过 `RERANK_ENABLED=true`、`RERANK_PROVIDER=tei` 共同启用该链路；Rerank 服务不可用时，系统保留 Hybrid 融合排序并记录异常，保证学生查询可用。

## 失败文件保留与清理

摄取失败时，Worker 将文档标记为 `failed`，但保留共享存储 `/data/raw` 内的原始文件；默认保留时间由 `FAILED_UPLOAD_RETENTION_HOURS=72` 控制。保留窗口内可使用同一 `doc_uuid` 重新摄取，成功后会清除失败保留标记。

`rag-failed-upload-cleanup` CronJob 每小时向 Celery 投递一次清理任务。实际删除操作由已挂载 RWX 存储卷的 Worker 执行，且只允许删除 `/data/raw` 下已过期的失败文件；删除后文档状态变更为 `failed_expired`。已过期文件不能恢复，需要重新上传。修改保留周期后，更新 ConfigMap 并滚动 Worker。

此目录部署无状态的 API 与评测 Worker。两类 Pod 均可横向扩展：API 使用三副本、滚动更新、PodDisruptionBudget、HPA 与跨可用区分散；评测使用两个 Celery Worker，从 Redis 消费任务。所有 API 和 Worker 共享同一个 ReadWriteMany 存储卷，使上传文件、解析文件和任务重试不会依赖某个 Pod 的本地磁盘。

不要将根目录的 `docker-compose.yml` 用作生产集群。生产环境必须提供独立高可用或受管的 PostgreSQL、Redis、Milvus，以及持久化的 RWX 存储。它们的地址通过 `rag-system-secrets` 和 `rag-system-config` 注入，应用 Pod 不保存数据库、向量库或消息队列的本地状态。

模型服务也不随 RAG API 镜像部署。生产链路固定为：`RAG API -> 校园内网 vLLM（Qwen）`，以及 `RAG API -> 校园内网 TEI（Embedding）`。在 `secret.example.yaml` 中将 `LLM_API_BASE` 指向 vLLM 的 `/v1` 地址，将 `EMBEDDING_API_BASE` 指向 TEI 的 OpenAI 兼容 `/v1` 地址；两者均通过内网 Service 和服务令牌访问。vLLM/TEI 应由学校 AI 平台单独进行 GPU 调度、扩缩容和模型版本管理。

## 部署顺序

1. 替换镜像地址、域名、`configmap.yaml` 中的平台服务地址，并由密钥管理系统生成 `rag-system-secrets`。不要提交或直接使用 `secret.example.yaml` 的占位值。
2. 使用集群实际的 RWX StorageClass 创建 `rag-shared-storage`，或绑定已经存在的同名 PVC。
3. 先执行迁移，确保新旧 API 不会并发争抢数据库 DDL：

```powershell
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/secret.yaml
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/migration-job.yaml
kubectl wait --for=condition=complete job/rag-db-migration -n rag-system --timeout=300s
```

4. 部署运行负载：

```powershell
kubectl apply -k deploy/k8s
kubectl rollout status deployment/rag-api -n rag-system
kubectl rollout status deployment/rag-evaluation-worker -n rag-system
```

生产 Ingress 应额外启用组织的 WAF、请求体大小限制和网关级限流；应用内部的 Redis 限流仍保留，作为跨副本一致的第二道保护。Prometheus 应通过集群内 Service 抓取 `/api/v1/metrics`，并携带 `rag-system-secrets` 中 `INTERNAL_TOKEN` 对应的 `X-Internal-Token` 请求头。生产清单启用了该端点校验，不要将指标端点直接公开到公网。就绪探针只验证应用和关键数据连接，不执行外部模型深度探测，避免第三方模型的瞬时波动触发错误摘流。
