# 校园综合知识库生产部署指南

> **口径来源**：模型部署为**四卡 H20 目标方案**（业务基线取自优化版：约 2 万份原始文档、约 110 万知识片段、日均 300+ 查询）。这是**选定的目标部署设计**，不代表已实测上线；其中数值为容量推演，不是压测结果。
>
> **必须说清的边界**：① 双副本 ≠ 双卡张量并行；② 业务后端通过内网接口访问模型服务，不在 API 进程内加载权重；③ Embedding 与 Rerank 共卡，存在资源配额与并发竞争，不宣称完全隔离；④ 图关系抽取复用生成模型服务（后台限并发、优先低峰），属**设计安排**。

本项目以校园综合知识库作为验证场景，生产部署时仍按多租户知识库服务治理。学校官网公开制度、办事指南、课程资料、课题组文档和公开资源分别使用 `source_module` 隔离；文档的 `access_level`、`owner_dept`、`owner_role` 用于可见范围控制。

## 部署架构总览（校园内网 AI 平台 + CPU 节点）

```text
                     校园管理台 / 飞书 / 企微 / HTTP 客户端
                                    │
                          统一认证网关 (CAS/OAuth)
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
     FastAPI API (3副本)    Celery Worker (2副本)    Prometheus + Grafana
              │                     │
    ┌─────────┼─────────────────────┼──────────────────────┐
    │         校园内网基础设施层                              │
    │  PostgreSQL (HA) · Redis (Sentinel) · Milvus · Neo4j    │
    └──────────────────────────────────────────────────────────┘
              │
    ┌─────────┼──────────────────────────────────────────────────┐
    │         校园内网 AI 平台（四卡 H20 96G 目标方案）              │
    │  卡1/卡2：Qwen3-32B FP8 生成（双副本 + 负载均衡）              │
    │  卡3：Qwen3-Embedding-8B（4096 维）+ Qwen3-Reranker-8B      │
    │  卡4：Qwen3-VL-32B FP8（离线复杂图表理解）                   │
    │  CPU 节点：PaddleOCR 文字与表格识别                          │
    │                                                             │
    │  RAG Pod 仅通过受控内网地址调用；不在 API 进程加载模型权重      │
    │  🚫 全链路数据不出校园内网                                  │
    └─────────────────────────────────────────────────────────────┘
```

**核心设计原则**：全链路不出校。问答生成、向量化、精排、图片理解和 OCR 均由校内 AI 平台或 CPU 节点提供服务；RAG Pod 只通过内网服务地址调用，不加载模型权重。没有一行数据、一个 token 离开校园内网。**图增强子系统**使用内网 Neo4j 存储图关系（公开制度与办事指南子集），其关系抽取复用生成模型服务（后台低峰、限并发）。

## 模型选型总表

| # | 职能 | 选型 | 部署方式 | 调用场景 | 优先级 |
|---|------|------|---------|---------|--------|
| 1 | LLM 问答生成 | Qwen3-32B（FP8） | 卡1/卡2 vLLM，**双副本 + LB** | 100% 在线：检索片段理解、证据门控、拒答、JSON 改写、答案裁判；离线图关系抽取复用（后台低峰） | ⭐ 必须 |
| 2 | Embedding 向量化 | Qwen3-Embedding-8B（4096 维，FP8） | 卡3 vLLM embed 端点 | 100% 在线/离线：查询向量化、文档向量化、语义分块 | ⭐ 必须 |
| 3 | 图片文字提取 | 校内 CPU 节点 PaddleOCR | Docker，CPU 节点 | 离线摄入：扫描页面文字提取 + 表格识别（占图片 ~85%） | ⭐ 必须 |
| 4 | 图片语义理解 | Qwen3-VL-32B-Instruct（FP8） | 卡4 vLLM 多模态服务 | 离线摄入：流程图/架构图/组织图语义描述（占图片 ~15%） | ◐ 后续按需 |
| 5 | Rerank 精排 | Qwen3-Reranker-8B（FP8） | 卡3 vLLM `/v1/rerank`（与 Embedding 共卡） | 在线检索后对 Hybrid 候选交叉编码精排 | ⭐ 必须 |

> **显存与共卡**：卡1、卡2 各承载一个 Qwen3-32B FP8 副本（约 32GB/副本）；卡3 同时承载 Qwen3-Embedding-8B 与 Qwen3-Reranker-8B，**需要配额与并发管理**，不宣称两者完全隔离；卡4 承载 Qwen3-VL-32B（约 33GB）。

## 推荐生产配置

生产环境使用 PostgreSQL、Redis、Milvus、Neo4j 和 RWX 对象/文件存储。一个 Milvus Collection 同时承载原文、稠密向量、BM25 稀疏向量和标量元数据；Neo4j 承载公开制度与办事指南子集的图关系（图增强子系统）。

```env
# ── 基础环境 ──
APP_ENV=production
SCENARIO_PROFILE=campus
VECTOR_STORE_PROVIDER=milvus
MILVUS_COLLECTION=oa_rag_chunks_v2
INGESTION_MODE=async
EVALUATION_EXECUTION_MODE=celery

# ── 安全开关 ──
ALLOW_PROVIDER_FALLBACKS=false
EMBEDDING_ALLOW_HASH_FALLBACK=false
PERMISSION_MODE=plugin
PERMISSION_PLUGIN=app.permissions.campus.CampusPermissionChecker
RETRIEVAL_AUTHENTICATED_IDENTITY_REQUIRED=true
TRUSTED_IDENTITY_HEADER_ENABLED=true
TRUSTED_IDENTITY_USER_HEADERS=X-Authenticated-User,X-User-Id
TRUSTED_IDENTITY_EXTERNAL_SOURCE_HEADERS=X-Identity-Source,X-Auth-Issuer
TRUSTED_IDENTITY_EXTERNAL_ID_HEADERS=X-Identity-External-Id,X-Auth-Subject

# ── LLM：卡1/卡2 vLLM Qwen3-32B FP8 双副本（经 LB 暴露）──
LLM_API_BASE=http://qwen32-lb.ai-platform.svc.cluster.local:8000/v1
LLM_API_KEY=vllm
LLM_MODEL=Qwen3-32B
LLM_MAX_TOKENS=1200
LLM_TEMPERATURE=0.1

# ── Embedding：卡3 vLLM Qwen3-Embedding-8B（4096 维）──
EMBEDDING_API_BASE=http://vllm-embedding.ai-platform.svc.cluster.local:8000/v1
EMBEDDING_API_KEY=vllm
EMBEDDING_MODEL=Qwen3-Embedding-8B
EMBEDDING_VECTOR_SIZE=4096

# ── 精排：卡3 vLLM Qwen3-Reranker-8B ──
RERANK_ENABLED=true
RERANK_PROVIDER=openai_compatible
RERANK_API_BASE=http://vllm-reranker.ai-platform.svc.cluster.local:8000/v1/rerank
RERANK_API_KEY=vllm
RERANK_MODEL=Qwen3-Reranker-8B

# ── 图片文字提取：校内 CPU 节点 PaddleOCR ──
IMAGE_RECOGNITION_PROVIDER=paddle_ocr
PADDLE_OCR_API_BASE=http://paddleocr.ai-platform.svc.cluster.local/v1/ocr
PADDLE_OCR_TOKEN=replace-with-paddleocr-service-token

# ── 图片语义理解：卡4 vLLM Qwen3-VL-32B（离线摄入）──
MULTIMODAL_API_BASE=http://vllm-qwen3-vl.ai-platform.svc.cluster.local:8000/v1
MULTIMODAL_API_KEY=vllm
MULTIMODAL_MODEL=Qwen3-VL-32B-Instruct

# ── 图增强子系统：内网 Neo4j（仅公开制度与办事指南子集）──
GRAPH_ENABLED=true
GRAPH_STORE_PROVIDER=neo4j
GRAPH_RETRIEVAL_ENABLED=true
GRAPH_EXPAND_LIMIT=50
NEO4J_URI=bolt://neo4j.ai-platform.svc.cluster.local:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=replace-with-neo4j-service-token
```

### AI 推理服务选型说明

| 组件 | 选型 | 理由 |
|------|------|------|
| **LLM 推理** | vLLM + Qwen3-32B（FP8） | 卡1/卡2 各承载一个副本 + 负载均衡，**双副本=HA（单卡故障可切换）**；聚合吞吐优于单套双卡 TP，代价是单流速度 |
| **Embedding 推理** | vLLM + Qwen3-Embedding-8B（4096 维） | Qwen3-Embedding 在 TEI 支持不成熟；vLLM 的 embed 端点 OpenAI 兼容，代码零改动接入 |
| **图片文字提取** | 校内 CPU 节点 PaddleOCR | CPU 运行不占 GPU，单页约 1 秒，覆盖多数扫描文字和表格场景 |
| **图片语义理解** | 卡4 vLLM + Qwen3-VL-32B-Instruct（FP8） | 单独一张卡，离线批量摄入与在线问答隔离，避免 VL 的 prefill 拖垮在线时延 |
| **Rerank 精排** | vLLM + Qwen3-Reranker-8B（FP8，与 Embedding 共卡） | 处理 Hybrid 候选精排；与 Embedding 共卡需配额管理，按候选数和 P95 单独监控、限流和扩容 |

**为什么 Embedding 与 Rerank 同卡（卡3）？** 两者都是在线检索阶段的小模型（8B），合并到一张卡以节省 GPU。但它们负载模型不同——Embedding 重批量吞吐、Rerank 随候选数增长——所以**必须做资源配额与并发管理，不能宣称二者完全隔离**。生产上分别设置并发、超时、限流和告警；Rerank 异常时保留融合排序并记录降级，不回退为 Dense 单路。

**为什么问答与视觉理解分开部署？** 问答服务承接在线请求，视觉模型只服务离线摄取，两者并发模式与显存曲线不同；分成独立服务后，批量导入安排在低峰期，不干扰在线问答。

**为什么双副本而不是双卡张量并行（TP）？** 两个单卡副本 + 负载均衡是**真 HA**（单卡故障自动切换），聚合吞吐优于单套双卡 TP；代价是单流速度下降。这是 HA 与单流速度的取舍，**两种方案不混写**。

**为什么 OCR 不用 GPU？** 文字提取是确定性计算不是推理，CPU 性能已足够。把 GPU 显存留给真正需要并行计算的模型服务。

**为什么不是 Ollama？** 生产场景需要稳定的并发调度、批处理和 OpenAI 兼容接口，vLLM 的吞吐与 PagedAttention/Continuous Batching 更适合当前链路。专业工具做专业事——LLM 自回归解码和 Embedding 批量编码的负载特征完全不同，分开部署可各自针对性调优。

**为什么不是公有云 API？** 学校制度文件、人事规定、财务数据通过互联网出口传输到公有云是合规红线。全链路不出校意味着没有一行数据、一个 token 离开校园内网。

身份认证由学校统一身份平台或网关完成。网关必须剥离客户端伪造的身份头，再注入 `X-Authenticated-User` 或配置的外部身份头；生产环境启用 `TRUSTED_IDENTITY_HEADER_*` 配置，只接受由受信网关注入的身份。服务端从内部身份表加载角色和学院，不信任请求体中的角色字段。`CampusPermissionChecker` 对未知访问级别默认拒绝，公开资料才允许匿名访问。检索服务将公开、校内、院系、角色、私有五级权限计算成同一份 `doc_uuid` 白名单，同时下推给 Milvus Dense 与 BM25 两路召回；融合前再根据当前 PostgreSQL 元数据做二次权限校验，确保异常索引或元数据滞后不会把越权 Chunk 送入 Rerank 和模型上下文。

## 数据入库约定

| 资料 | `source_module` | `source_type` | 建议访问级别 |
|---|---|---|---|
| 校级制度/办事指南 | `administration` | `rule_doc` / `procedure` | `public` 或 `internal` |
| 官网通知 | `campus_notice` | `notice` | `public` |
| 课程资料 | `teaching` | `course` | `internal` / `role` |
| 课题组论文与实验记录 | `research` | `paper` / `experiment` | `department` / `role` |
| 图书馆和开放课程 | `library` | `public_resource` | `public` |

只采集已获授权的制度文件、办事指南、课程资料、课题组资料和公开政策。禁止导入成绩、学号、联系方式、需要登录才能取得的页面及其他未获授权的资料。

## 性能验收

**规模口径**：生产规模约 **2 万份**原始文档、约 **110 万**知识片段（文档数与片段数双口径记录——一份长制度切分后会产生多个向量，不能用"文档数"替代向量数量描述）。先用校园真实资料验证正确性，再用公开语料和扩容数据验证规模。

**延迟口径**：
- 文档检索及精排链路设计目标 **P95 < 200ms**，统计范围含 Query Embedding → Dense → BM25 → 融合(RRF) → Rerank，**不含生成**；生成延迟单独统计。
- **图增强路径单独安排预算**（锚点读取 → 图关系扩展 → 批次及来源校验 → 证据合并），不与文档检索混算；四卡部署是模型服务的资源基础，总体延迟仍需结合实际模型输入长度、候选数、并发和图查询方式判断，不作为实测结论。

```powershell
python scripts/campus_retrieval_benchmark.py .\benchmarks\campus_queries.jsonl `
  --base-url http://127.0.0.1:18080/api/v1 `
  --concurrency 32 --repeat 10 --strategy hybrid
```

脚本输出客户端和服务端检索的 p50/p95/p99。检索延迟与大模型生成延迟必须分开报告。

## 校内 AI 服务接入

生产 RAG Pod 使用以下内网端点，地址和令牌通过 Secret 注入；应用不管理模型权重、GPU 进程或端口暴露：

| 服务 | 地址配置 | 职责 |
|---|---|---|
| 问答生成 | `LLM_API_BASE` | 基于已授权证据生成答案与引用 |
| 向量化 | `EMBEDDING_API_BASE` | 查询和文档向量化 |
| 精排 | `RERANK_API_BASE` | 对 Hybrid 候选交叉编码重排序 |
| 多模态理解 | `MULTIMODAL_API_BASE` | 离线摄取中的图表语义理解 |
| OCR | `PADDLE_OCR_API_BASE` | 扫描文字与表格识别 |
| 图存储 | `NEO4J_URI` | 公开制度与办事指南子集的图关系（增量） |

OCR 置信度低于阈值或命中流程图、架构图、图表特征时，再调用内网多模态服务；图片内容 SHA-256 使用进程级 LRU 缓存，重复图片不重复请求。

### 模型切换流程（蓝绿部署）

1. 在 GPU 节点新端口启动新版本模型（如 vLLM 在 `:8001` 跑新模型）
2. 修改 RAG 应用的 `LLM_API_BASE` 指向新端口
3. 在评测环境跑回归评测——验证 Hit@1、MRR、拒答率
4. 评测通过后切换生产配置——Pod 滚动更新，无需重启
5. 旧模型端口保留 7 天作为回滚窗口——有问题一键切回
6. 确认稳定后下线旧模型、释放显存

整个过程 RAG 应用代码不改一行——Provider 抽象使模型切换对业务层透明。

## Kubernetes

已有 `deploy/k8s` 清单包含 API 三副本、Celery Worker、迁移 Job、HPA、PDB 和 RWX 存储。部署前替换镜像、域名、数据库/Redis/Milvus 地址及身份插件，不要直接使用示例 Secret。生产 Ingress 还应配置 WAF、请求体限制和网关限流。

注意：校内 AI 平台和 CPU OCR 节点独立于应用 K8s 集群部署。应用 Pod 通过网络调用：

- `http://qwen32-lb.ai-platform.svc.cluster.local:8000/v1` — 问答生成（双副本 + LB）
- `http://vllm-embedding.ai-platform.svc.cluster.local:8000/v1` — Embedding（卡3）
- `http://vllm-reranker.ai-platform.svc.cluster.local:8000/v1/rerank` — Rerank（卡3）
- `http://vllm-qwen3-vl.ai-platform.svc.cluster.local:8000/v1` — 多模态理解（卡4）
- `http://paddleocr.ai-platform.svc.cluster.local/v1/ocr` — OCR（CPU 节点）
- `bolt://neo4j.ai-platform.svc.cluster.local:7687` — 图存储（公开资料图增强子系统）
