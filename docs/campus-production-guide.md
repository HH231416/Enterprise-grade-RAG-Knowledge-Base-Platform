# 校园综合知识库生产部署指南

本项目以校园综合知识库作为验证场景，生产部署时仍按多租户知识库服务治理。学校官网公开制度、办事指南、课程资料、课题组文档和公开资源分别使用 `source_module` 隔离；文档的 `access_level`、`owner_dept`、`owner_role` 用于可见范围控制。

## 部署架构总览（双卡 A100 48GB × 2 + CPU 节点）

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
    │  PostgreSQL (HA) · Redis (Sentinel) · Milvus · OpenSearch │
    └──────────────────────────────────────────────────────────┘
              │
    ┌─────────┼──────────────────────────────────────────────────┐
    │         │            GPU / CPU 推理节点（校园自建）           │
    │         │                                                   │
    │  ┌──────▼──────────────────────────────────────────────┐  │
    │  │  卡1 (A100 48GB) — vLLM (:8000)                      │  │
    │  │  · Qwen3-14B (LLM 问答)              28GB           │  │
    │  │  · Qwen3-VL-8B (图片语义理解，按需)    20GB           │  │
    │  └─────────────────────────────────────────────────────┘  │
    │                                                             │
    │  ┌──────▼──────────────────────────────────────────────┐  │
    │  │  卡2 (A100 48GB) — TEI (:8080)                       │  │
    │  │  · bge-m3 (Embedding 向量化)           8GB           │  │
    │  │  · 剩余 40GB：batch 扩展 + 未来 Rerank               │  │
    │  └─────────────────────────────────────────────────────┘  │
    │                                                             │
    │  ┌──────▼──────────────────────────────────────────────┐  │
    │  │  CPU 节点 — PaddleOCR Docker (:8866)                  │  │
    │  │  · 扫描页面文字提取 + 表格识别（占图片 85%）            │  │
    │  │  · 不占 GPU，与推理服务互不抢占                        │  │
    │  └─────────────────────────────────────────────────────┘  │
    │                                                             │
    │    🚫 全链路数据不出校园内网                                    │
    └─────────────────────────────────────────────────────────────┘
```

**核心设计原则**：全链路不出校。五类模型分层部署——LLM 和视觉理解共享卡1 vLLM 实例，Embedding 独占卡2 TEI 实例，OCR 走 CPU 节点不占 GPU。没有一行数据、一个 token 离开校园内网。

## 模型选型总表

| # | 职能 | 选型 | 部署方式 | 显存 | 调用场景 | 优先级 |
|---|------|------|---------|------|---------|--------|
| 1 | LLM 问答生成 | Qwen3-14B | vLLM，卡1 | 28GB | 100% 在线：检索片段理解、证据门控、拒答、JSON 改写、答案裁判 | ⭐ 必须 |
| 2 | Embedding 向量化 | bge-m3 | TEI，卡2 | 8GB | 100% 在线/离线：查询向量化、文档向量化、语义分块 | ⭐ 必须 |
| 3 | 图片文字提取 | PaddleOCR 本地部署 | Docker，CPU 节点 | 0 | 离线摄入：扫描页面文字提取 + 表格识别（占图片 ~85%） | ⭐ 必须 |
| 4 | 图片语义理解 | Qwen3-VL-8B | vLLM，卡1（与 LLM 同实例） | 20GB | 离线摄入：流程图/架构图/组织图语义描述（占图片 ~15%） | ◐ 后续按需 |
| 5 | Rerank 精排 | bge-reranker-v2-m3 | 可选独立服务 | ~2GB | 在线检索后精排：交叉编码器对粗排候选重排序 | ○ 可选增强 |

## 推荐生产配置

生产环境使用 PostgreSQL、Redis、Milvus/Zilliz、OpenSearch 和 RWX 对象/文件存储。不要使用 `local` 向量库或 `memory` 稀疏索引承载生产流量。

```env
# ── 基础环境 ──
APP_ENV=production
SCENARIO_PROFILE=campus
VECTOR_STORE_PROVIDER=milvus
SPARSE_INDEX_PROVIDER=opensearch
INGESTION_MODE=async
EVALUATION_EXECUTION_MODE=celery

# ── 安全开关 ──
ALLOW_PROVIDER_FALLBACKS=false
EMBEDDING_ALLOW_HASH_FALLBACK=false
PERMISSION_MODE=plugin
PERMISSION_PLUGIN=app.permissions.campus.CampusPermissionChecker
RETRIEVAL_AUTHENTICATED_IDENTITY_REQUIRED=true

# ── LLM：卡1 vLLM Qwen3-14B ──
LLM_API_BASE=http://gpu-node.internal:8000/v1
LLM_API_KEY=vllm
LLM_MODEL=Qwen3-14B
LLM_MAX_TOKENS=1200
LLM_TEMPERATURE=0.1

# ── Embedding：卡2 TEI bge-m3 ──
EMBEDDING_API_BASE=http://gpu-node.internal:8080/v1
EMBEDDING_API_KEY=tei
EMBEDDING_MODEL=bge-m3
EMBEDDING_VECTOR_SIZE=1024

# ── 图片文字提取：CPU 节点 PaddleOCR 本地部署 ──
IMAGE_RECOGNITION_PROVIDER=paddle_ocr
PADDLE_OCR_SERVICE_URL=http://ocr-node.internal:8866

# ── 图片语义理解：卡1 vLLM Qwen3-VL-8B（按需加载） ──
MULTIMODAL_API_BASE=http://gpu-node.internal:8000/v1
MULTIMODAL_API_KEY=vllm
MULTIMODAL_MODEL=Qwen3-VL-8B
```

### AI 推理服务选型说明

| 组件 | 选型 | 理由 |
|------|------|------|
| **LLM 推理** | vLLM + Qwen3-14B | PagedAttention 显存管理高效，同家族 8B→14B 零迁移成本，vLLM 适配最成熟 |
| **Embedding 推理** | TEI + bge-m3 | HuggingFace 官方引擎，Flash Attention 批量编码，原生 `/v1/embeddings`，中英文检索均衡 |
| **图片文字提取** | PaddleOCR 本地部署 | CPU 运行不占 GPU，单页 ~1 秒，覆盖 85% 扫描文字和表格场景 |
| **图片语义理解** | Qwen3-VL-8B | 与 LLM 同 vLLM 实例，离线摄入时按需调用，覆盖 15% 图表/流程图场景 |
| **Rerank 精排** | bge-reranker-v2-m3（预留） | 交叉编码精排增强排序质量，卡2 剩余 40GB 已为其预留空间，评测验证收益后上线 |

**为什么 LLM 和视觉模型同卡部署？** Qwen3-VL-8B 只在离线摄入时调用——批量导入安排在低峰期，不对在线问答造成 GPU 抢占。两个模型在同一 vLLM 实例中通过 `model` 参数区分路由，运维复杂度不增加。

**为什么 Embedding 独占卡2？** Embedding 承载 100% 流量——每次检索和文档摄入都经过。独占保证高并发时 batch 推理不受 LLM 的 KV cache 影响。卡2 剩余 40GB 留给未来 Rerank 和 batch 扩展。

**为什么 OCR 不用 GPU？** 文字提取是确定性计算不是推理，CPU 性能已足够。把 GPU 显存留给 LLM 和 Embedding 两个真正需要并行计算的服务。

**为什么不是 Ollama？** Ollama 适合单机开发验证。生产场景 vLLM 的吞吐和 TEI 的嵌入优化显著优于 Ollama 统一方案。"专业工具做专业事"——LLM 自回归解码和 Embedding 批量编码的负载特征完全不同，分开部署可各自针对性调优。

**为什么不是公有云 API？** 学校制度文件、人事规定、财务数据通过互联网出口传输到公有云是合规红线。全链路不出校意味着没有一行数据、一个 token 离开校园内网。

身份认证由学校统一身份平台或网关完成。网关必须剥离客户端伪造的身份头，再注入 `X-Authenticated-User` 或配置的外部身份头；服务端会从本地身份表加载角色和学院，而不会信任请求体中的角色字段。`CampusPermissionChecker` 对未知访问级别默认拒绝，公开资料才允许匿名访问。

## 数据入库约定

| 资料 | `source_module` | `source_type` | 建议访问级别 |
|---|---|---|---|
| 校级制度/办事指南 | `administration` | `rule_doc` / `procedure` | `public` 或 `internal` |
| 官网通知 | `campus_notice` | `notice` | `public` |
| 课程资料 | `teaching` | `course` | `internal` / `role` |
| 课题组论文与实验记录 | `research` | `paper` / `experiment` | `department` / `role` |
| 群聊知识沉淀 | `im_knowledge` | `chat` | `department` / `private` |
| 图书馆和开放课程 | `library` | `public_resource` | `public` |

只采集学校官网、信息公开网、政府公开政策和自有/已获授权的课题组资料。禁止导入成绩、学号、联系方式、需要登录才能取得的页面以及未授权的聊天记录。

## 性能验收

索引规模按"文档数 + 知识片段数"双口径记录。一份长制度切分后会产生多个向量，因此"十万级文档"不能替代向量数量描述。先用校园真实资料验证正确性，再用公开语料和扩容数据验证规模。

```powershell
python scripts/campus_retrieval_benchmark.py .\benchmarks\campus_queries.jsonl `
  --base-url http://127.0.0.1:18080/api/v1 `
  --concurrency 32 --repeat 10 --strategy hybrid
```

脚本输出客户端和服务端检索的 p50/p95/p99。检索延迟与大模型生成延迟必须分开报告。

## GPU 推理节点运维

### 卡1 — vLLM 启动（LLM + 视觉双模型）

```bash
# 启动 vLLM，同时加载 Qwen3-14B 和 Qwen3-VL-8B
# 视觉模型按需调用，通过 model 参数路由
CUDA_VISIBLE_DEVICES=0 python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-14B \
  --host 0.0.0.0 --port 8000 \
  --gpu-memory-utilization 0.95 \
  --max-model-len 8192 \
  --tensor-parallel-size 1 \
  --enable-lora
```

### 卡2 — TEI 启动

```bash
# Docker 方式（推荐）
docker run -d --gpus '"device=1"' \
  -p 8080:80 \
  -e MODEL_ID=BAAI/bge-m3 \
  ghcr.io/huggingface/text-embeddings-inference:latest
```

### CPU 节点 — PaddleOCR 本地部署

```bash
# 替代公有云 API (paddleocr.aistudio-app.com)
docker run -d \
  -p 8866:8866 \
  paddlepaddle/paddleocr:latest
```

应用侧通过 `PADDLE_OCR_API_BASE` 指向内网 OCR 适配层（该适配层将 `/v1/ocr/jobs` 和状态轮询转换为 PaddleOCR 服务接口），不会调用 `paddleocr.aistudio-app.com`。OCR 置信度低于阈值或文本命中流程图/架构图/图表特征时，再调用内网 vLLM-VL；图片内容 SHA-256 结果使用进程级 LRU 缓存，重复图片不重复请求。

### 模型切换流程（蓝绿部署）

1. 在 GPU 节点新端口启动新模型（如 vLLM 在 `:8001` 跑 Qwen3.5-35B-A3B）
2. 修改 RAG 应用的 `LLM_API_BASE` 指向新端口
3. 在评测环境跑回归评测——验证 Hit@1、MRR、拒答率
4. 评测通过后切换生产配置——Pod 滚动更新，无需重启
5. 旧模型端口保留 7 天作为回滚窗口——有问题一键切回
6. 确认稳定后下线旧模型、释放显存

整个过程 RAG 应用代码不改一行——Provider 抽象使模型切换对业务层透明。

### 模型演进路径

| 时机 | 变更 | 触发条件 |
|------|------|---------|
| 当前 | Qwen3-14B + bge-m3 + PaddleOCR | 生产基线 |
| 短期 | 按需加载 Qwen3-VL-8B | 图表/架构图的 VLM 质量需要提升时 |
| 中期 | 评估 Rerank (bge-reranker-v2-m3) | Hit@1 出现瓶颈、评测验证收益 |
| 中长期 | 评估 Qwen3.5-35B-A3B (MoE) | vLLM MoE 支持成熟 + Golden Set 对比验证 |

## Kubernetes

已有 `deploy/k8s` 清单包含 API 三副本、Celery Worker、迁移 Job、HPA、PDB 和 RWX 存储。部署前替换镜像、域名、数据库/Redis/Milvus/OpenSearch 地址及身份插件，不要直接使用示例 Secret。生产 Ingress 还应配置 WAF、请求体限制和网关限流。

注意：GPU 推理节点（vLLM + TEI）和 CPU 节点（PaddleOCR）独立于 K8s 集群部署。应用 Pod 通过网络调用：

- `http://gpu-node.internal:8000/v1` — LLM + 视觉（卡1 vLLM）
- `http://gpu-node.internal:8080/v1` — Embedding（卡2 TEI）
- `http://ocr-node.internal:8866` — 文字提取（CPU PaddleOCR）
