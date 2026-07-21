# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

"""Prometheus 监控指标定义模块，覆盖 HTTP、LLM、Embedding、检索、向量存储等各层。"""
""" Prometheus 是一个开源的监控告警系统，它专门用于收集、存储和查询时间序列数据（带时间戳的数值指标）。
在 metrics.py 中，你看到的是 Prometheus 的 Python 客户端库（prometheus_client），
它的作用是在应用代码中"埋点"，生成指标数据暴露出去，由 Prometheus 服务端定期抓取。 """

# HTTP 层：QPS + 延迟分布
# LLM 层：调用量 + 延迟 + Token 消耗
# 向量存储层：upsert/search 操作的次数和耗时，按 provider 标签分
# 系统层：数据库连接池、外部服务健康状态
# 最后通过 get_metrics_response() 暴露 /metrics 端点，Prometheus Server 定期抓取，配合 Grafana 可视化，
# 一旦指标超过阈值就触发告警。


# 导入 prometheus_client 库的核心组件
from prometheus_client import Counter, Gauge, Histogram, generate_latest, REGISTRY
# Counter   — 只增不减的计数器（请求数、Token 消耗量等）
# Gauge     — 可增可减的仪表盘（连接池可用数、服务健康状态等）
# Histogram — 直方图，统计耗时分布（自动生成 _bucket、_sum、_count 三个子指标）
# generate_latest — 将所有已注册指标序列化为 Prometheus 文本格式
# REGISTRY  — 默认的 CollectorRegistry，所有指标统一注册到此处


# ============================================================
# ===== HTTP 层指标 =====
# ============================================================
http_requests_total = Counter(
    "http_requests_total",              # 指标名称，Prometheus 抓取时以此标识
    "Total HTTP requests",              # 帮助文本，描述指标含义
    ["method", "path", "status"],       # 标签维度：HTTP 方法 + 路径 + 状态码
    registry=REGISTRY,                  # 注册到默认 Registry，不传则自动注册
)
# 使用示例：http_requests_total.labels(method="GET", path="/documents", status="200").inc()

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",    # 指标名称
    "HTTP request duration in seconds", # 帮助文本
    ["method", "path"],                 # 标签维度：HTTP 方法 + 路径
    # 耗时分桶边界（秒）：从 10ms 到 10s，用于统计 P50/P90/P99 延迟
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=REGISTRY,
)
# 使用示例：http_request_duration_seconds.labels(method="POST", path="/upload").observe(0.35)


# ============================================================
# ===== LLM 大模型层指标 =====
# ============================================================
llm_requests_total = Counter(
    "llm_requests_total",               # 指标名称
    "Total LLM API requests",           # 帮助文本
    ["model", "status"],                # 标签维度：模型名称 + 请求状态（success/failure）
    registry=REGISTRY,
)
# 用于监控各模型的调用量和成功率

llm_request_duration_seconds = Histogram(
    "llm_request_duration_seconds",     # 指标名称
    "LLM API request duration",         # 帮助文本
    ["model"],                          # 标签维度：模型名称
    # LLM 调用较慢，分桶从 0.5s 到 60s
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0],
    registry=REGISTRY,
)
# 用于监控各模型的响应延迟分布

llm_tokens_total = Counter(
    "llm_tokens_total",                 # 指标名称
    "Total LLM tokens consumed",        # 帮助文本
    ["model", "type"],                  # 标签维度：模型名称 + Token 类型（input/output/total）
    registry=REGISTRY,
)
# 用于统计 Token 消耗量，配合成本核算


# ============================================================
# ===== Embedding 嵌入层指标 =====
# ============================================================
embedding_requests_total = Counter(
    "embedding_requests_total",         # 指标名称
    "Total embedding API requests",     # 帮助文本
    ["model", "status"],                # 标签维度：模型名称 + 请求状态
    registry=REGISTRY,
)

embedding_request_duration_seconds = Histogram(
    "embedding_request_duration_seconds", # 指标名称
    "Embedding API request duration",     # 帮助文本
    ["model"],                            # 标签维度：模型名称
    # Embedding 调用相对较快，分桶从 50ms 到 10s
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=REGISTRY,
)


# ============================================================
# ===== 检索层指标 =====
# ============================================================
retrieval_duration_seconds = Histogram(
    "retrieval_duration_seconds",       # 指标名称
    "Retrieval operation duration",     # 帮助文本
    ["strategy"],                       # 标签维度：检索策略（如 hybrid、semantic、keyword）
    # 检索应极快，分桶从 10ms 到 5s
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
    registry=REGISTRY,
)


# ============================================================
# ===== 文档上传指标 =====
# ============================================================
documents_uploaded_total = Counter(
    "documents_uploaded_total",         # 指标名称
    "Total documents uploaded",         # 帮助文本
    ["source_type", "status"],          # 标签维度：来源类型 + 上传状态（success/failure）
    registry=REGISTRY,
)
# 在 app/api/documents.py 的上传接口中使用，按来源类型统计上传量


# ============================================================
# ===== Q&A 问答指标 =====
# ============================================================
qa_questions_total = Counter(
    "qa_questions_total",               # 指标名称
    "Total QA questions asked",         # 帮助文本
    # 无标签维度，仅统计问答总数
    registry=REGISTRY,
)

rate_limit_rejections_total = Counter(
    "rate_limit_rejections_total",
    "Total requests rejected by the application rate limiter",
    ["method", "path"],
    registry=REGISTRY,
)

rate_limit_backend_failures_total = Counter(
    "rate_limit_backend_failures_total",
    "Total Redis rate limiter backend failures",
    registry=REGISTRY,
)


# ============================================================
# ===== 向量存储层指标 =====
# ============================================================
vector_store_operations_total = Counter(
    "vector_store_operations_total",    # 指标名称
    "Total vector store operations",    # 帮助文本
    ["provider", "operation"],          # 标签维度：提供商（zilliz/milvus/local）+ 操作类型（insert/search/delete）
    registry=REGISTRY,
)

vector_store_operation_duration_seconds = Histogram(
    "vector_store_operation_duration_seconds",  # 指标名称
    "Vector store operation duration",          # 帮助文本
    ["provider", "operation"],                  # 标签维度：提供商 + 操作类型
    # 向量操作要求低延迟，分桶从 5ms 到 1s
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
    registry=REGISTRY,
)


# ============================================================
# ===== 系统层指标 =====
# ============================================================
db_pool_available = Gauge(
    "db_pool_available",                # 指标名称
    "Database connection pool available connections",  # 帮助文本
    # Gauge 类型，可增可减，用于反映当前瞬时值
    registry=REGISTRY,
)
# 定期采集数据库连接池中可用连接数，用于容量规划和告警

service_health = Gauge(
    "service_health",                   # 指标名称
    "External service health (1=up, 0=down)",  # 帮助文本
    ["service"],                        # 标签维度：服务名（zilliz/milvus/embedding_provider/llm_provider 等）
    registry=REGISTRY,
)
# 健康检查探针定期写入 1（健康）或 0（不健康），配合 Grafana 告警规则使用


def get_metrics_response() -> bytes:
    """将所有已注册的指标序列化为 Prometheus 文本格式，返回 bytes 供 /metrics 端点使用。"""
    # generate_latest 遍历 REGISTRY 中所有 Collector，生成符合 Prometheus 协议的文本
    return generate_latest(REGISTRY)
