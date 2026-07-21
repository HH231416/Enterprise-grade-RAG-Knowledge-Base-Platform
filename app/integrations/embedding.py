"""Embedding 向量化集成模块：提供多级降级的文本向量化服务。

核心设计：三级降级链（Tiered Fallback Chain）
- Tier 1（生产主力）：通过 HTTP 调用 OpenAI 兼容 API，质量最高
- Tier 2（本地兜底）：使用 sentence-transformers 本地推理，提供真实语义
- Tier 3（开发/测试）：SHA256 确定性哈希伪向量，无语义能力，仅保证可用性

面试点：为什么需要三级降级？
- 外部 API 可能不可用（网络故障、配额耗尽、服务宕机）
- 本地模型提供语义兜底，但需要 GPU/内存资源
- 哈希降级确保开发环境零依赖可运行，方便 CI/CD 和本地调试
"""

# PEP 563：延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

# ---- 标准库 ----
import logging                   # 日志记录
import threading                 # 线程锁，保证本地模型加载的线程安全
from dataclasses import dataclass  # 轻量级数据类
from time import perf_counter    # 高精度计时器（比 time.time() 更精确且单调递增）

# ---- 第三方库 ----
import numpy as np               # 高效处理向量数组（本地模型返回 NumPy 数组）

# ---- 项目内部模块 ----
from app.core.config import Settings                         # 应用配置（依赖注入）
from app.core.errors import AppError, ErrorCode               # 自定义错误和错误码枚举
from app.core.metrics import embedding_requests_total, embedding_request_duration_seconds  # Prometheus 监控指标
from app.integrations.http_client import post_json_with_retries   # 带重试的 HTTP POST 客户端
from app.integrations.http_utils import build_openai_compatible_url  # URL 构建工具（自动处理末尾斜杠等）

# 获取当前模块的 logger 实例
logger = logging.getLogger(__name__)


# ============================================================
# 数据类：封装单次 embedding 操作的结果
# ============================================================
@dataclass(slots=True)  # slots=True：禁用 __dict__，每个实例节省约 1KB 内存
class EmbeddingResult:
    """封装一条文本及其对应的 embedding 向量和元数据。
    
    面试点：为什么用 dataclass 而不是普通 dict？
    - 类型安全：IDE 自动补全 + 静态类型检查
    - 不可变（如果加 frozen=True）或可控可变
    - slots=True 减少内存占用（大批量 embedding 场景下效果显著）
    """
    text: str              # 原始输入文本
    vector: list[float]    # 文本的向量表示（浮点数列表，维度由模型决定）
    model_name: str        # 生成向量所使用的模型名称（如 "text-embedding-3-small"）
    provider_name: str     # 提供者名称（如 "http://api.openai.com" 或 "local:cpu"）
    request_tokens: int    # 本次请求消耗的 token 数量（估算值，用于成本统计）


# ============================================================
# EmbeddingProvider：三级降级的 embedding 服务核心类
# ============================================================
class EmbeddingProvider:
    """健壮的 embedding 生成提供者，实现三级生产级降级链。

    面试点：三级降级链的设计考量
    - 每级独立封装为 `_embed_via_*` 方法，符合单一职责原则
    - 降级由 `embed_texts()` 统一编排，使用 try/except 而非 if/else 链
    - `allow_provider_fallbacks` 开关允许禁止降级（生产环境严格模式）
    - `embedding_allow_hash_fallback` 独立控制 Tier 3，防止生产环境误用哈希

    降级策略：
    - Tier 1（HTTP API）：质量最高，依赖网络和外部服务
    - Tier 2（本地模型）：真实语义能力，CPU/GPU 本地推理
    - Tier 3（哈希）：确定性的伪向量，仅用于开发/测试环境
    """

    # ---- 类属性（所有实例共享） ----

    # 批处理大小：平衡吞吐量和 API 限制（OpenAI embedding API 单次最大 2048 条输入）
    _BATCH_SIZE: int = 32

    # 线程锁：确保本地模型在多线程环境下只加载一次（双重检查锁定模式）
    _model_lock: threading.Lock = threading.Lock()

    # 类级别缓存已加载的本地模型实例
    # 懒加载（Lazy Loading）：首次使用时才加载，避免启动时阻塞
    # 单例（Singleton）：全局只加载一次，节省显存/内存
    _local_model: object | None = None

    # ---- 初始化 ----

    def __init__(self, settings: Settings) -> None:
        """构造函数，通过依赖注入接收应用配置。
        
        面试点：依赖注入（Dependency Injection）的优势
        - 解耦：EmbeddingProvider 不依赖具体的配置来源（环境变量/文件/数据库）
        - 可测试：测试时传入 mock Settings 即可，无需修改环境变量
        - FastAPI 中通过 Depends(get_settings) 自动注入
        """
        self.settings = settings

    # ============================================================
    # 核心入口：三级降级编排
    # ============================================================
    def embed_texts(self, texts: list[str]) -> list[EmbeddingResult]:
        """将文本列表转换为 embedding 向量，严格按照 Tier 1 → 2 → 3 顺序降级。
        
        面试点：降级链 vs 重试机制的区别
        - 重试：同一服务失败后重试（网络抖动）
        - 降级：当前服务不可用时切换到备用服务（服务宕机）
        - 本模块：降级链内置重试（post_json_with_retries），两者组合使用
        
        Args:
            texts: 待向量化的文本列表
        Returns:
            EmbeddingResult 列表，与输入顺序一一对应
        Raises:
            AppError: 所有层级都失败时抛出
        """
        # 边缘情况：空列表直接返回（避免后续空遍历和 API 调用）
        if not texts:
            return []

        # ---- Tier 1：HTTP 远程 API（生产首选） ----
        # 只有同时配置了 API 地址和密钥才尝试
        if self.settings.embedding_api_base and self.settings.embedding_api_key:
            try:
                return self._embed_via_http(texts)
            except AppError:
                # HTTP API 调用失败，检查是否允许降级
                if not self.settings.allow_provider_fallbacks:
                    # 严格模式：不允许降级，直接抛出异常中断流程
                    raise
                # 宽松模式：记录警告日志后继续尝试下一级
                logger.warning(
                    "Tier 1 (HTTP API) failed; attempting Tier 2 (local model) fallback"
                )

        # ---- Tier 2：本地语义模型（生产兜底） ----
        if self.settings.embedding_local_model:
            try:
                return self._embed_via_local_model(texts)
            except Exception:
                # 本地模型也可能失败（模型加载错误、OOM 等）
                if not self.settings.allow_provider_fallbacks:
                    raise
                logger.warning(
                    "Tier 2 (local model) failed; falling back to Tier 3 (hash)"
                )

        # ---- Tier 3：确定性哈希（开发/测试） ----
        # embedding_allow_hash_fallback 是独立开关，防止生产环境误用无意义的哈希向量
        # 同时兼容 allow_provider_fallbacks 全局开关（向后兼容）
        hash_fallback_allowed = (
            self.settings.embedding_allow_hash_fallback
            or self.settings.allow_provider_fallbacks
        )
        if hash_fallback_allowed:
            logger.warning(
                "Using SHA256 deterministic embedding — "
                "NOT suitable for production RAG (no semantic capability)"
            )
            return self._embed_locally(texts)

        # ---- 全部失败 ----
        # 所有层级都不可用，抛出明确错误信息指导用户排查
        raise AppError(
            code=ErrorCode.EMBEDDING_FAILED,
            message=(
                "All embedding providers exhausted: "
                "HTTP API unavailable, no local model configured, "
                "and hash fallback is disabled. "
                "Set EMBEDDING_ALLOW_HASH_FALLBACK=true only for development, "
                "or configure EMBEDDING_LOCAL_MODEL for production fallback."
            ),
            status_code=422,
        )

    # ============================================================
    # 健康检查探针
    # ============================================================
    def probe(self) -> bool:
        """健康检查探针：按优先级顺序探测可用的 embedding 提供者。
        
        面试点：为什么健康检查要逐级探测而不是只测当前可用的？
        - 监控目的：知道是哪一级出了问题（而非只知道"不可用"）
        - 运维价值：Tier 1 不可用时提前告警，避免静默降级
        - 只要有任意一级可用就返回 True，保证服务可用性
        
        Returns:
            True 表示至少有一级可用，False 表示全部不可用
        """
        # ---- 探测 Tier 1（HTTP API） ----
        if self.settings.embedding_api_base and self.settings.embedding_api_key:
            try:
                # 用 probe 文本测试 embed_texts，它会优先走 Tier 1
                self.embed_texts(["probe"])
                logger.info("Embedding probe successful (Tier 1: HTTP API is active)")
                return True
            except AppError:
                # Tier 1 不可用，记录异常详情后继续探测下一级
                logger.exception("Embedding probe failed for Tier 1 (HTTP API)")

        # ---- 探测 Tier 2（本地语义模型） ----
        if self.settings.embedding_local_model:
            try:
                # 显式加载模型并测试编码
                self._get_or_load_local_model()
                self._embed_via_local_model(["probe"])
                logger.info("Embedding probe successful (Tier 2: Local model is active)")
                return True
            except Exception:
                logger.exception("Embedding probe failed for Tier 2 (Local model)")

        # ---- 探测 Tier 3（确定性哈希） ----
        # Tier 1 和 Tier 2 都失败，但哈希降级如果开启，服务仍然"可用"
        # 注意：这里只记录警告，因为哈希模式无语义能力，生产环境不推荐
        logger.warning(
            "Embedding probe: Tier 1 & 2 failed, "
            "assuming Tier 3 (hash) is available as last resort."
        )
        return True

    # ============================================================
    # Tier 2：本地模型 embedding
    # ============================================================
    def _embed_via_local_model(self, texts: list[str]) -> list[EmbeddingResult]:
        """[Tier 2] 使用本地 sentence-transformers 模型生成语义向量。
        
        面试点：为什么 normalize_embeddings=True？
        - 归一化后向量模长为 1，余弦相似度简化为点积运算
        - 点积比余弦相似度计算更快（省去除法步骤）
        - Milvus/Qdrant 等向量数据库默认使用内积（IP）度量，要求向量归一化
        
        Args:
            texts: 待编码的文本列表
        Returns:
            EmbeddingResult 列表
        """
        started = perf_counter()  # 高精度计时起点

        # 获取本地模型实例（懒加载，首次调用时加载模型）
        model = self._get_or_load_local_model()

        # 构建提供者标识字符串（方便日志和监控中追踪来源）
        model_name = self.settings.embedding_local_model
        provider_name = f"local:{self.settings.embedding_local_device}"

        # 调用 sentence-transformers 的 encode 方法批量编码
        embeddings_array: np.ndarray = model.encode(  # type: ignore
            texts,
            batch_size=self._BATCH_SIZE,       # 批处理大小，平衡显存和速度
            show_progress_bar=False,           # 生产环境关闭进度条（污染日志）
            normalize_embeddings=True,          # 归一化：使余弦相似度 = 点积
            convert_to_numpy=True,              # 直接输出 NumPy 数组（高效）
        )

        # 健壮性检查：模型实际输出维度 vs 配置期望维度
        # 如果维度不匹配，向量数据库的 collection 需要重建
        actual_dim = embeddings_array.shape[1]          # shape[1] 是向量维度
        expected_dim = self.settings.embedding_vector_size
        if actual_dim != expected_dim:
            logger.warning(
                "Local model '%s' produces %s-dim vectors, but embedding_vector_size=%s. "
                "Vector store collections may need to be recreated with the correct dimension.",
                model_name,
                actual_dim,
                expected_dim,
            )

        # 将 NumPy 数组转换为 EmbeddingResult 对象列表
        results: list[EmbeddingResult] = []
        for text, vector_array in zip(texts, embeddings_array, strict=True):
            # strict=True：Python 3.10+ 特性，两个可迭代对象长度不一致时抛出 ValueError
            results.append(
                EmbeddingResult(
                    text=text,
                    vector=vector_array.tolist(),  # NumPy 行向量 → Python list
                    model_name=model_name,
                    provider_name=provider_name,
                    request_tokens=len(text.split()),  # 按空格分词估算 token 数
                )
            )

        # 记录耗时日志
        elapsed_ms = int((perf_counter() - started) * 1000)
        logger.info(
            "Generated %s local model embedding vectors in %sms", len(results), elapsed_ms
        )

        # 更新 Prometheus 监控指标
        embed_model = model_name or "local-embedding"
        # Counter 指标：成功编码的向量总数
        embedding_requests_total.labels(model=embed_model, status="success").inc(len(results))
        # Histogram 指标：编码耗时分布
        embedding_request_duration_seconds.labels(model=embed_model).observe(
            elapsed_ms / 1000.0
        )

        return results

    # ============================================================
    # 本地模型懒加载（双重检查锁定）
    # ============================================================
    def _get_or_load_local_model(self) -> object:
        """获取本地模型实例，采用"双重检查锁定"确保线程安全。
        
        面试点：双重检查锁定（Double-Checked Locking）模式
        - 为什么需要两次检查？
          1. 第一次检查（无锁）：快速路径，模型已加载时直接返回，避免锁竞争
          2. 第二次检查（有锁）：防止多个线程同时通过第一次检查后重复加载
        - 如果不做第二次检查：线程 A 和 B 同时通过第一次检查，A 获得锁加载模型，
          B 等待锁释放后也会再次加载（重复加载浪费资源）
        - 为什么用类级别锁（_model_lock）而非实例锁？
          类属性 _local_model 是类级别共享的，所以锁也必须是类级别的
        
        Returns:
            已加载的 sentence-transformers 模型实例
        Raises:
            AppError: 模型名称未配置或 sentence-transformers 未安装
        """
        # 第一次检查（无锁）：快速路径，避免不必要的锁获取
        if self._local_model is not None:
            return self._local_model

        # 获取线程锁，进入临界区
        with self._model_lock:
            # 第二次检查（有锁）：防止在等待锁期间其他线程已加载模型
            if self._local_model is not None:
                return self._local_model

            model_name = self.settings.embedding_local_model
            if not model_name:
                raise AppError(
                    code=ErrorCode.EMBEDDING_FAILED,
                    message="embedding_local_model is not configured",
                    status_code=422,
                )

            logger.info(
                "Loading local embedding model '%s' on '%s' "
                "(this may take a moment on first call)...",
                model_name,
                self.settings.embedding_local_device,
            )

            # 动态导入：sentence-transformers 是可选依赖
            # 面试点：为什么用动态导入（lazy import）？
            # - 不是所有部署都需要本地模型（纯 API 模式不需要安装 sentence-transformers）
            # - 减少启动时的导入开销
            # - 只有真正需要时才触发 ImportError
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise AppError(
                    code=ErrorCode.EMBEDDING_FAILED,
                    message=(
                        "sentence-transformers is required for local embedding. "
                        "Install with: pip install sentence-transformers"
                    ),
                    status_code=422,
                )

            # 实例化模型
            model = SentenceTransformer(
                model_name,
                device=self.settings.embedding_local_device,  # "cpu" / "cuda" / "cuda:0"
                trust_remote_code=True,  # 允许执行 HuggingFace Hub 上的自定义模型代码
            )

            # 缓存到类属性（单例模式）
            self._local_model = model
            logger.info(
                "Local embedding model '%s' loaded (max_seq_length=%s, dim=%s)",
                model_name,
                model.max_seq_length,                    # 模型最大输入长度
                model.get_sentence_embedding_dimension(), # 输出向量维度
            )
            return model

    # ============================================================
    # Tier 3：确定性哈希伪向量（开发/测试）
    # ============================================================
    def _embed_locally(self, texts: list[str]) -> list[EmbeddingResult]:
        """[Tier 3] 使用 SHA256 哈希生成确定性伪 embedding 向量。
        
        面试点：SHA256 哈希向量的特性
        - 确定性：相同输入永远得到相同向量（便于测试验证）
        - 无语义：哈希值不反映文本含义，"猫"和"狗"的向量距离没有意义
        - 均匀分布：SHA256 输出均匀分布，但在语义空间中表现为随机噪声
        - 用途：开发环境测试流程能否跑通，不做语义检索
        
        注意：此方法名 `_embed_locally` 可能产生歧义——它不是"本地模型"，
        而是"本地哈希"。命名是历史遗留问题。
        
        Args:
            texts: 待处理的文本列表
        Returns:
            EmbeddingResult 列表（伪语义向量）
        """
        # 方法内导入：hashlib 是标准库，但仅 Tier 3 使用
        import hashlib

        results: list[EmbeddingResult] = []
        model_name = self.settings.embedding_model or "local-deterministic"
        provider_name = "local"
        vector_size = self.settings.embedding_vector_size  # 期望的向量维度

        for text in texts:
            # 步骤 1：SHA256 哈希 → 32 字节摘要
            digest = hashlib.sha256(text.encode("utf-8")).digest()

            # 步骤 2：将字节转换为 0~255 的整数列表
            raw_values = list(digest)

            # 步骤 3：调整长度以匹配期望维度
            if vector_size > len(raw_values):
                # 维度 > 32：重复哈希字节填满（如 1536 维需要重复 48 次）
                repeats = (vector_size // len(raw_values)) + 1
                raw_values = (raw_values * repeats)[:vector_size]
            else:
                # 维度 ≤ 32：截断到期望维度
                raw_values = raw_values[:vector_size]

            # 步骤 4：归一化到 [0.0, 1.0] 范围
            vector = [round(value / 255.0, 6) for value in raw_values]

            results.append(
                EmbeddingResult(
                    text=text,
                    vector=vector,
                    model_name=model_name,
                    provider_name=provider_name,
                    request_tokens=len(text.split()),
                )
            )

        logger.info("Generated %s deterministic hash vectors (dev-only)", len(results))
        return results

    # ============================================================
    # Tier 1：HTTP 远程 API embedding
    # ============================================================
    def _embed_via_http(self, texts: list[str]) -> list[EmbeddingResult]:
        """[Tier 1] 通过 HTTP 请求 OpenAI 兼容 API 生成 embedding。
        
        面试点：OpenAI Embedding API 的关键设计点
        - 兼容性：使用 `/embeddings` 端点 + Bearer Token 认证，
          兼容 OpenAI、Azure OpenAI、Ollama、vLLM 等所有 OpenAI 兼容服务
        - 批处理：_BATCH_SIZE=32，平衡延迟和吞吐（单次请求太多会超时）
        - 重试：post_json_with_retries 内置指数退避重试
        - encoding_format="float"：明确要求 float 格式，base64 格式需额外解码
        
        Args:
            texts: 待编码的文本列表
        Returns:
            EmbeddingResult 列表
        Raises:
            AppError: API 请求失败或返回数据异常时
        """
        started = perf_counter()

        # 构建健壮的请求 URL（自动处理末尾斜杠等边界情况）
        request_url = build_openai_compatible_url(
            self.settings.embedding_api_base, "/embeddings"
        )

        # 构建请求头（Bearer Token 认证 + JSON 内容类型）
        headers = {
            "Authorization": f"Bearer {self.settings.embedding_api_key}",
            "Content-Type": "application/json",
        }

        all_results: list[EmbeddingResult] = []
        model_name: str | None = None  # 从 API 响应中获取实际模型名
        provider_name = self.settings.embedding_api_base
        batch_size = self._BATCH_SIZE

        # 分批处理：避免单次请求过大
        for offset in range(0, len(texts), batch_size):
            batch = texts[offset : offset + batch_size]

            # 构建 OpenAI 兼容的请求体
            payload = {
                "model": self.settings.embedding_model,  # 如 "text-embedding-3-small"
                "input": batch,                           # 文本列表（API 支持批量输入）
                "encoding_format": "float",               # 返回 float 向量（而非 base64）
            }

            # 发送请求（带自动重试）
            data = post_json_with_retries(
                settings=self.settings,
                url=request_url,
                headers=headers,
                payload=payload,
                # 将底层异常包装为 AppError，统一错误处理
                error_factory=lambda exc: AppError(
                    code=ErrorCode.EMBEDDING_FAILED,
                    message=f"embedding provider request failed: {exc}",
                    status_code=422,
                ),
            )

            # 解析响应数据
            raw_vectors = data.get("data") or []  # OpenAI 格式：{"data": [{"embedding": [...]}, ...]}
            if len(raw_vectors) != len(batch):
                # 防御性编程：返回的向量数量必须与输入文本数量一致
                raise AppError(
                    code=ErrorCode.EMBEDDING_FAILED,
                    message="embedding provider returned unexpected result size",
                    status_code=422,
                )

            # 从第一次成功响应中获取并缓存模型名称
            # 之后所有批次使用相同的模型名（同一请求不可能切换模型）
            if model_name is None:
                model_name = (
                    data.get("model")                  # API 返回的模型名（最准确）
                    or self.settings.embedding_model   # 配置的模型名
                    or "remote-embedding"              # 兜底默认名
                )

            # 将响应数据转换为 EmbeddingResult 对象
            for text, item in zip(batch, raw_vectors, strict=True):
                all_results.append(
                    EmbeddingResult(
                        text=text,
                        vector=item.get("embedding", []),  # 提取 embedding 向量
                        model_name=model_name,
                        provider_name=provider_name,
                        request_tokens=len(text.split()),
                    )
                )

        # 记录总耗时日志
        elapsed_ms = int((perf_counter() - started) * 1000)
        logger.info(
            "Fetched %s embedding vectors via HTTP in %sms", len(all_results), elapsed_ms
        )

        # 更新 Prometheus 监控指标
        embed_model = model_name or self.settings.embedding_model or "unknown"
        # Counter：成功获取的向量总数（按模型分组）
        embedding_requests_total.labels(model=embed_model, status="success").inc(
            len(all_results)
        )
        # Histogram：请求耗时分布（秒为单位）
        embedding_request_duration_seconds.labels(model=embed_model).observe(
            elapsed_ms / 1000.0
        )

        return all_results
