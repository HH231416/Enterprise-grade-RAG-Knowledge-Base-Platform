# Kubernetes 生产部署

此目录部署无状态的 API 与评测 Worker。两类 Pod 均可横向扩展：API 使用三副本、滚动更新、PodDisruptionBudget、HPA 与跨可用区分散；评测使用两个 Celery Worker，从 Redis 消费任务。所有 API 和 Worker 共享同一个 ReadWriteMany 存储卷，使上传文件、解析文件和任务重试不会依赖某个 Pod 的本地磁盘。

不要将根目录的 `docker-compose.yml` 用作生产集群。生产环境必须提供独立高可用或受管的 PostgreSQL、Redis、Milvus、OpenSearch，以及持久化的 RWX 存储。它们的地址通过 `rag-system-secrets` 和 `rag-system-config` 注入，应用 Pod 不保存数据库、向量库或消息队列的本地状态。

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
