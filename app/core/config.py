from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORAGE_ROOT = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "knowledge-base-core"
    app_env: str = "local"
    app_host: str = "127.0.0.1"
    app_port: int = 18080
    app_api_prefix: str = "/api/v1"
    scenario_profile: str = "campus"
    log_level: str = "INFO"
    max_upload_size_mb: int = 50
    app_api_key: str = "change-me"
    internal_token: str = "change-me"
    provider_timeout_seconds: float = 30.0
    provider_retry_count: int = 1
    health_probe_external_services: bool = False
    metrics_auth_enabled: bool = False
    allow_provider_fallbacks: bool = True
    # Model serving is deliberately separated from the RAG API process.
    # Production uses internal OpenAI-compatible vLLM and TEI gateways.
    model_serving_mode: str = "local"
    llm_provider: str = "auto"
    embedding_provider: str = "auto"
    rate_limit_enabled: bool = False
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60
    rate_limit_key_prefix: str = "oa-rag:rate-limit"
    rate_limit_fail_open: bool = False
    rate_limit_trust_x_forwarded_for: bool = False
    ingestion_mode: str = "sync"
    failed_upload_retention_hours: int = 72
    failed_upload_cleanup_batch_size: int = 200
    evaluation_execution_mode: str = "sync"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    cors_allow_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    enable_folder_source: bool = False
    folder_source_allowed_roots: str = ""
    admin_manual_user_creation_enabled: bool = False
    admin_auth_enabled: bool = False
    admin_auth_user_headers: str = "X-Authenticated-User,X-User-Id"
    admin_auth_allowed_roles: str = "platform_admin"
    retrieval_authenticated_identity_required: bool = False
    auth_session_enabled: bool = False
    auth_session_secret: str | None = None
    auth_session_ttl_seconds: int = 3600
    auth_session_issuer: str = "knowledge-base-core"
    trusted_identity_header_enabled: bool = False
    trusted_identity_user_headers: str = "X-Authenticated-User,X-User-Id"
    trusted_identity_external_source_headers: str = "X-Identity-Source,X-Auth-Issuer"
    trusted_identity_external_id_headers: str = "X-Identity-External-Id,X-Auth-Subject"
    evaluation_retrieval_user_id: str = "evaluation-service"
    campus_admin_roles: str = "campus_admin,school_admin"
    upload_requires_auth: bool = False
    document_review_enabled: bool = False
    document_upload_allowed_roles: str = ""

    database_url: str | None = None
    postgres_host: str | None = None
    postgres_port: int = 5432
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800

    redis_url: str | None = Field(default=None, validation_alias=AliasChoices("REDIS_URL", "redis_url"))
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None

    storage_root: Path = Field(default_factory=lambda: DEFAULT_STORAGE_ROOT)
    raw_data_dir: Path | None = None
    processed_data_dir: Path | None = None
    sample_data_dir: Path | None = None

    zilliz_uri: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ZILLIZ_URI", "ZILLIZ_ENDPOINT", "zilliz_uri"),
    )
    zilliz_token: str | None = Field(default=None, validation_alias=AliasChoices("ZILLIZ_TOKEN", "zilliz_token"))
    zilliz_collection: str = Field(
        default="oa_rag_chunks",
        validation_alias=AliasChoices("ZILLIZ_COLLECTION", "ZILLIZ_COLLECTION_NAME", "zilliz_collection"),
    )
    vector_store_provider: str = "local"

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

    embedding_api_base: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EMBEDDING_API_BASE", "EMBEDDING_BASE_URL", "embedding_api_base"),
    )
    embedding_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EMBEDDING_API_KEY", "embedding_api_key"),
    )
    embedding_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EMBEDDING_MODEL", "embedding_model"),
    )
    embedding_vector_size: int = Field(
        default=4096,
        validation_alias=AliasChoices("EMBEDDING_VECTOR_SIZE", "EMBEDDING_DIM", "embedding_vector_size"),
        description=(
            "Embedding 输出向量维度,需与所选模型一致:Qwen3-Embedding-8B=4096,"
            "0.6B=1024,4B=2560。修改维度必须重建 Milvus Collection 并全量重索引。"
        ),
    )
    # ------------------------------------------------------------------
    # 本地嵌入模型配置（生产级降级方案，替代 SHA256 确定性哈希）
    # ------------------------------------------------------------------
    embedding_local_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "EMBEDDING_LOCAL_MODEL",
            "embedding_local_model",
        ),
        description=(
            "本地 sentence-transformers 模型名称，作为 HTTP API 不可用时的降级方案。"
            "推荐选型：中文场景 'BAAI/bge-small-zh-v1.5'（512维/24MB），"
            "英文场景 'all-MiniLM-L6-v2'（384维/80MB），"
            "多语言 'intfloat/multilingual-e5-small'（384维/118MB）。"
            "设为 None 则跳过本地模型降级，直接使用确定性哈希（仅限开发/测试）。"
        ),
    )
    embedding_local_device: str = Field(
        default="cpu",
        validation_alias=AliasChoices(
            "EMBEDDING_LOCAL_DEVICE",
            "embedding_local_device",
        ),
        description="本地嵌入模型运行设备：'cpu' 或 'cuda'。CPU 模式适用性最广。",
    )
    # 生产安全开关：设为 True 才允许在本地语义模型也失败时降级到 SHA256 哈希
    embedding_allow_hash_fallback: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "EMBEDDING_ALLOW_HASH_FALLBACK",
            "embedding_allow_hash_fallback",
        ),
        description="仅限开发环境使用。生产环境务必保持 False。",
    )

    llm_api_base: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_BASE", "LLM_BASE_URL", "llm_api_base"),
    )
    llm_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "llm_api_key"),
    )
    llm_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_MODEL", "llm_model"),
    )
    llm_max_tokens: int = Field(
        default=1200,
        validation_alias=AliasChoices("LLM_MAX_TOKENS", "llm_max_tokens"),
    )
    llm_temperature: float = Field(
        default=0.1,
        validation_alias=AliasChoices("LLM_TEMPERATURE", "llm_temperature"),
    )
    # 思考模式:auto=复杂多跳问题自动开启(默认),off=全程关闭,on=全程开启
    # 普通 RAG 问答务必保持关闭,否则 max_tokens 会被思考过程耗尽
    llm_thinking_mode: str = "auto"
    answer_judge_enabled: bool = False
    answer_judge_model: str | None = None
    answer_judge_max_tokens: int = 320
    answer_judge_min_faithfulness: float = 0.8
    answer_judge_min_relevance: float = 0.75
    agent_query_rewrite_mode: str = "auto"
    agent_query_rewrite_max_tokens: int = 96
    retrieval_strategy: str = "hybrid"
    rrf_k: float = 60.0
    evidence_threshold_dense: float = 0.35
    evidence_threshold_hybrid: float = 0.55

    # ---------------- 图增强检索（第一版：仅公开校园制度与办事指南）----------------
    # 总开关。关闭时问答链路行为与改造前完全一致，不启用任何图检索。
    graph_enabled: bool = False
    # 存储实现：memory | neo4j。默认 memory，可在无 Neo4j 环境完成隔离验证。
    graph_store_provider: str = "memory"
    neo4j_uri: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NEO4J_URI", "neo4j_uri"),
    )
    neo4j_user: str | None = Field(
        default="neo4j",
        validation_alias=AliasChoices("NEO4J_USER", "neo4j_user"),
    )
    neo4j_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NEO4J_PASSWORD", "neo4j_password"),
    )
    neo4j_database: str = Field(
        default="neo4j",
        validation_alias=AliasChoices("NEO4J_DATABASE", "neo4j_database"),
    )
    # 图检索是否允许进入在线问答链路。构图可独立开启，便于先建图后检索。
    graph_retrieval_enabled: bool = False
    # 单次图扩展返回的关系上限
    graph_expand_limit: int = 50
    # 图关系问题的必要关系类型；缺任一类型时判定证据不完整并澄清。
    graph_required_relations: str = "handled_by,requires_material"
    # 抽取器名称与提示模板版本，写入批次以便复核
    graph_extractor_name: str = "llm-constrained-v1"
    graph_prompt_template_version: str = "campus-procedure-v1"

    dify_base_url: str = "http://127.0.0.1:8000"
    dify_app_key: str | None = None
    dify_retrieval_user_id: str = "dify-external"

    bot_response_mode: str = "qa"
    bot_top_k: int = 8
    bot_dedup_ttl_seconds: int = 300
    feishu_enabled: bool = False
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_verification_token: str | None = None
    feishu_encrypt_key: str | None = None
    feishu_base_url: str = "https://open.feishu.cn"
    wecom_enabled: bool = False
    wecom_corp_id: str | None = None
    wecom_agent_id: str | None = None
    wecom_secret: str | None = None
    wecom_callback_token: str | None = None
    wecom_encoding_aes_key: str | None = None
    wecom_base_url: str = "https://qyapi.weixin.qq.com"

    rerank_enabled: bool = False
    rerank_provider: str = "tei"
    rerank_api_base: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RERANK_API_BASE", "rerank_api_base"),
    )
    rerank_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RERANK_API_KEY", "rerank_api_key"),
    )
    rerank_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RERANK_MODEL", "rerank_model"),
    )
    # 扩召回倍数:Rerank 前从向量库取 top_k × factor 个候选。默认 3;配置 5 时 top_k=5 → 召回 25
    rerank_recall_factor: int = Field(
        default=3,
        validation_alias=AliasChoices("RERANK_RECALL_FACTOR", "rerank_recall_factor"),
    )

    # 权限配置
    permission_mode: str = "none"  # none=无权限检查，plugin=使用插件
    permission_plugin: str = "app.permissions.noop.NoOpPermissionChecker"  # 插件类路径

    # 图片识别配置
    image_recognition_provider: str = Field(
        default="paddle_ocr",
        validation_alias=AliasChoices("IMAGE_RECOGNITION_PROVIDER", "image_recognition_provider"),
    )
    paddle_ocr_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PADDLE_OCR_TOKEN", "paddle_ocr_token"),
    )
    multimodal_api_base: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MULTIMODAL_API_BASE", "multimodal_api_base"),
    )
    multimodal_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MULTIMODAL_API_KEY", "multimodal_api_key"),
    )
    multimodal_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MULTIMODAL_MODEL", "multimodal_model"),
    )
    image_recognition_timeout: float = Field(
        default=30.0,
        validation_alias=AliasChoices("IMAGE_RECOGNITION_TIMEOUT", "image_recognition_timeout"),
    )
    image_recognition_batch_size: int = Field(
        default=5,
        validation_alias=AliasChoices("IMAGE_RECOGNITION_BATCH_SIZE", "image_recognition_batch_size"),
    )
    paddle_ocr_api_base: str | None = Field(
        default="http://127.0.0.1:8080/v1/ocr",
        validation_alias=AliasChoices("PADDLE_OCR_API_BASE", "paddle_ocr_api_base"),
    )
    image_vlm_confidence_threshold: float = Field(
        default=0.85,
        validation_alias=AliasChoices(
            "IMAGE_VLM_CONFIDENCE_THRESHOLD",
            "image_vlm_confidence_threshold",
        ),
    )
    image_cache_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("IMAGE_CACHE_ENABLED", "image_cache_enabled"),
    )

    @model_validator(mode="after")
    def validate_required_config(self) -> "Settings":
        self.app_env = self._normalize_text(self.app_env, default="local") or "local"
        self.scenario_profile = self._normalize_text(self.scenario_profile, default="campus") or "campus"
        if self.scenario_profile not in {"campus", "enterprise", "generic"}:
            raise ValueError("SCENARIO_PROFILE must be campus, enterprise, or generic.")
        self.model_serving_mode = (
            self._normalize_text(self.model_serving_mode, default="local") or "local"
        ).lower()
        if self.model_serving_mode not in {"local", "internal_gateway", "external_api"}:
            raise ValueError("MODEL_SERVING_MODE must be local, internal_gateway, or external_api.")
        self.llm_provider = (self._normalize_text(self.llm_provider, default="auto") or "auto").lower()
        if self.llm_provider not in {"auto", "vllm", "ollama", "openai_compatible"}:
            raise ValueError("LLM_PROVIDER must be auto, vllm, ollama, or openai_compatible.")
        self.embedding_provider = (
            self._normalize_text(self.embedding_provider, default="auto") or "auto"
        ).lower()
        if self.embedding_provider not in {"auto", "tei", "openai_compatible", "local"}:
            raise ValueError(
                "EMBEDDING_PROVIDER must be auto, tei, openai_compatible, or local."
            )
        self.campus_admin_roles = self._normalize_text(
            self.campus_admin_roles,
            default="campus_admin,school_admin",
        ) or "campus_admin,school_admin"
        self.zilliz_collection = self._normalize_text(
            self.zilliz_collection,
            default="oa_rag_chunks",
        )
        self.vector_store_provider = self._normalize_text(
            self.vector_store_provider,
            default="local",
        )
        self.llm_api_base = self._normalize_text(self.llm_api_base)
        self.llm_api_key = self._normalize_text(self.llm_api_key)
        self.llm_model = self._normalize_text(self.llm_model)
        self.answer_judge_model = self._normalize_text(self.answer_judge_model)
        self.embedding_api_base = self._normalize_text(self.embedding_api_base)
        self.embedding_api_key = self._normalize_text(self.embedding_api_key)
        self.embedding_model = self._normalize_text(self.embedding_model)
        self.zilliz_uri = self._normalize_text(self.zilliz_uri)
        self.zilliz_token = self._normalize_text(self.zilliz_token)
        self.milvus_uri = self._normalize_text(self.milvus_uri)
        self.milvus_token = self._normalize_text(self.milvus_token)

        if self.app_env != "local" and self.model_serving_mode == "internal_gateway":
            if self.llm_provider != "vllm" or self.embedding_provider != "tei":
                raise ValueError(
                    "internal_gateway production requires LLM_PROVIDER=vllm and EMBEDDING_PROVIDER=tei."
                )
            if not (self.llm_api_base and self.embedding_api_base):
                raise ValueError(
                    "internal_gateway production requires LLM_API_BASE and EMBEDDING_API_BASE."
                )

        if not self.database_url:
            missing = [
                name
                for name, value in (
                    ("POSTGRES_HOST", self.postgres_host),
                    ("POSTGRES_DB", self.postgres_db),
                    ("POSTGRES_USER", self.postgres_user),
                    ("POSTGRES_PASSWORD", self.postgres_password),
                )
                if not value
            ]
            if missing:
                joined = ", ".join(missing)
                raise ValueError(
                    f"Missing database settings: {joined}. "
                    "Provide DATABASE_URL or the PostgreSQL settings."
                )

        if self.max_upload_size_mb <= 0:
            raise ValueError("MAX_UPLOAD_SIZE_MB must be greater than 0.")

        if self.provider_timeout_seconds <= 0:
            raise ValueError("PROVIDER_TIMEOUT_SECONDS must be greater than 0.")

        if self.provider_retry_count < 0:
            raise ValueError("PROVIDER_RETRY_COUNT must be greater than or equal to 0.")

        if self.failed_upload_retention_hours < 0:
            raise ValueError("FAILED_UPLOAD_RETENTION_HOURS must be greater than or equal to 0.")
        if self.failed_upload_cleanup_batch_size <= 0:
            raise ValueError("FAILED_UPLOAD_CLEANUP_BATCH_SIZE must be greater than 0.")

        if self.rate_limit_requests <= 0:
            raise ValueError("RATE_LIMIT_REQUESTS must be greater than 0.")
        if self.rate_limit_window_seconds <= 0:
            raise ValueError("RATE_LIMIT_WINDOW_SECONDS must be greater than 0.")
        self.rate_limit_key_prefix = self._normalize_text(
            self.rate_limit_key_prefix,
            default="oa-rag:rate-limit",
        ) or "oa-rag:rate-limit"

        self.evaluation_execution_mode = self._normalize_text(
            self.evaluation_execution_mode,
            default="sync",
        ) or "sync"
        if self.evaluation_execution_mode not in {"sync", "background", "celery"}:
            raise ValueError("EVALUATION_EXECUTION_MODE must be sync, background, or celery.")
        self.celery_broker_url = self._normalize_text(self.celery_broker_url)
        self.celery_result_backend = self._normalize_text(self.celery_result_backend)
        if self.evaluation_execution_mode == "celery" and not self.celery_broker_url:
            raise ValueError("CELERY_BROKER_URL is required when EVALUATION_EXECUTION_MODE=celery.")

        if self.db_pool_size <= 0:
            raise ValueError("DB_POOL_SIZE must be greater than 0.")
        if self.db_max_overflow < 0:
            raise ValueError("DB_MAX_OVERFLOW must be greater than or equal to 0.")
        if self.db_pool_timeout_seconds <= 0:
            raise ValueError("DB_POOL_TIMEOUT_SECONDS must be greater than 0.")
        if self.db_pool_recycle_seconds <= 0:
            raise ValueError("DB_POOL_RECYCLE_SECONDS must be greater than 0.")

        self.auth_session_secret = self._normalize_text(self.auth_session_secret)
        self.auth_session_issuer = self._normalize_text(
            self.auth_session_issuer,
            default="knowledge-base-core",
        ) or "knowledge-base-core"
        if self.auth_session_ttl_seconds <= 0:
            raise ValueError("AUTH_SESSION_TTL_SECONDS must be greater than 0.")
        if self.auth_session_enabled and not self.auth_session_secret:
            raise ValueError("AUTH_SESSION_SECRET is required when AUTH_SESSION_ENABLED=true.")

        # vector_store_provider 必须显式指定，避免把 local 静默切到外部向量库
        self.vector_store_provider = self.vector_store_provider.lower()
        if self.vector_store_provider not in {"local", "milvus", "zilliz"}:
            self.vector_store_provider = "local"

        self.rerank_api_base = self._normalize_text(self.rerank_api_base)
        self.rerank_api_key = self._normalize_text(self.rerank_api_key)
        self.rerank_model = self._normalize_text(self.rerank_model)
        self.rerank_provider = (
            self._normalize_text(self.rerank_provider, default="tei") or "tei"
        ).lower()
        if self.rerank_provider not in {"tei", "openai_compatible"}:
            raise ValueError("RERANK_PROVIDER must be tei or openai_compatible.")
        if self.rerank_enabled and not self.rerank_api_base:
            raise ValueError("RERANK_API_BASE is required when RERANK_ENABLED=true.")
        if self.rerank_enabled and not self.rerank_model:
            raise ValueError("RERANK_MODEL is required when RERANK_ENABLED=true.")
        if self.app_env != "local" and self.rerank_enabled and not self.rerank_api_key:
            raise ValueError("RERANK_API_KEY is required in non-local environments when RERANK_ENABLED=true.")
        if self.rerank_recall_factor <= 0:
            raise ValueError("RERANK_RECALL_FACTOR must be greater than 0.")
        self.agent_query_rewrite_mode = self._normalize_text(
            self.agent_query_rewrite_mode,
            default="auto",
        ) or "auto"
        if self.agent_query_rewrite_mode not in {"auto", "llm", "heuristic"}:
            raise ValueError("AGENT_QUERY_REWRITE_MODE must be auto, llm, or heuristic.")
        if self.agent_query_rewrite_max_tokens <= 0:
            raise ValueError("AGENT_QUERY_REWRITE_MAX_TOKENS must be greater than 0.")
        self.llm_thinking_mode = (
            self._normalize_text(self.llm_thinking_mode, default="auto") or "auto"
        ).lower()
        if self.llm_thinking_mode not in {"auto", "off", "on"}:
            raise ValueError("LLM_THINKING_MODE must be auto, off, or on.")
        self.retrieval_strategy = (
            self._normalize_text(self.retrieval_strategy, default="hybrid") or "hybrid"
        ).lower()
        if self.retrieval_strategy != "hybrid":
            raise ValueError("RETRIEVAL_STRATEGY must be hybrid in the production retrieval pipeline.")
        if self.answer_judge_max_tokens <= 0:
            raise ValueError("ANSWER_JUDGE_MAX_TOKENS must be greater than 0.")
        for name, value in (
            ("EVIDENCE_THRESHOLD_DENSE", self.evidence_threshold_dense),
            ("EVIDENCE_THRESHOLD_HYBRID", self.evidence_threshold_hybrid),
            ("ANSWER_JUDGE_MIN_FAITHFULNESS", self.answer_judge_min_faithfulness),
            ("ANSWER_JUDGE_MIN_RELEVANCE", self.answer_judge_min_relevance),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1.")
        if self.rrf_k < 1.0:
            raise ValueError("RRF_K must be >= 1.0.")
        self.evaluation_retrieval_user_id = self._normalize_text(
            self.evaluation_retrieval_user_id,
            default="evaluation-service",
        ) or "evaluation-service"
        self.dify_retrieval_user_id = self._normalize_text(
            self.dify_retrieval_user_id,
            default="dify-external",
        ) or "dify-external"
        self.bot_response_mode = self._normalize_text(self.bot_response_mode, default="qa") or "qa"
        if self.bot_response_mode not in {"qa", "search"}:
            raise ValueError("BOT_RESPONSE_MODE must be either 'qa' or 'search'.")
        if self.bot_top_k <= 0:
            raise ValueError("BOT_TOP_K must be greater than 0.")
        if self.bot_dedup_ttl_seconds <= 0:
            raise ValueError("BOT_DEDUP_TTL_SECONDS must be greater than 0.")
        self.feishu_app_id = self._normalize_text(self.feishu_app_id)
        self.feishu_app_secret = self._normalize_text(self.feishu_app_secret)
        self.feishu_verification_token = self._normalize_text(self.feishu_verification_token)
        self.feishu_encrypt_key = self._normalize_text(self.feishu_encrypt_key)
        self.feishu_base_url = self._normalize_text(self.feishu_base_url, default="https://open.feishu.cn") or "https://open.feishu.cn"
        self.wecom_corp_id = self._normalize_text(self.wecom_corp_id)
        self.wecom_agent_id = self._normalize_text(self.wecom_agent_id)
        self.wecom_secret = self._normalize_text(self.wecom_secret)
        self.wecom_callback_token = self._normalize_text(self.wecom_callback_token)
        self.wecom_encoding_aes_key = self._normalize_text(self.wecom_encoding_aes_key)
        self.wecom_base_url = self._normalize_text(self.wecom_base_url, default="https://qyapi.weixin.qq.com") or "https://qyapi.weixin.qq.com"
        self.permission_mode = self._normalize_text(self.permission_mode, default="none") or "none"
        if self.feishu_enabled and not (self.feishu_app_id and self.feishu_app_secret and self.feishu_verification_token):
            raise ValueError("FEISHU_APP_ID, FEISHU_APP_SECRET and FEISHU_VERIFICATION_TOKEN are required when FEISHU_ENABLED=true.")
        if self.wecom_enabled and not (
            self.wecom_corp_id and self.wecom_agent_id and self.wecom_secret and self.wecom_callback_token and self.wecom_encoding_aes_key
        ):
            raise ValueError("WECOM_CORP_ID, WECOM_AGENT_ID, WECOM_SECRET, WECOM_CALLBACK_TOKEN and WECOM_ENCODING_AES_KEY are required when WECOM_ENABLED=true.")

        if self.app_env != "local":
            if self.app_api_key == "change-me" or self.internal_token == "change-me":
                raise ValueError("APP_API_KEY and INTERNAL_TOKEN must not use default placeholder values outside local env.")
            if self.permission_mode == "none":
                raise ValueError("PERMISSION_MODE=none is only allowed in local env.")
            if self.embedding_allow_hash_fallback:
                raise ValueError("EMBEDDING_ALLOW_HASH_FALLBACK=true is only allowed in local env.")

        # 图片识别配置验证
        self.image_recognition_provider = self._normalize_text(
            self.image_recognition_provider,
            default="paddle_ocr",
        ) or "paddle_ocr"
        
        # 根据提供者类型验证必需的配置
        if self.image_recognition_provider == "paddle_ocr":
            if not self.paddle_ocr_token:
                # PaddleOCR token是可选的，但如果没有配置，将无法使用
                pass
        elif self.image_recognition_provider == "multimodal":
            if not (self.multimodal_api_base and self.multimodal_api_key and self.multimodal_model):
                # 多模态提供者需要完整的配置
                pass
        
        # 规范化配置值
        self.paddle_ocr_token = self._normalize_text(self.paddle_ocr_token)
        self.multimodal_api_base = self._normalize_text(self.multimodal_api_base)
        self.multimodal_api_key = self._normalize_text(self.multimodal_api_key)
        self.multimodal_model = self._normalize_text(self.multimodal_model)
        self.paddle_ocr_api_base = self._normalize_text(self.paddle_ocr_api_base)
        
        # 验证超时和批量大小
        if self.image_recognition_timeout <= 0:
            raise ValueError("IMAGE_RECOGNITION_TIMEOUT must be greater than 0.")
        if self.image_recognition_batch_size <= 0:
            raise ValueError("IMAGE_RECOGNITION_BATCH_SIZE must be greater than 0.")
        if not 0.0 <= self.image_vlm_confidence_threshold <= 1.0:
            raise ValueError("IMAGE_VLM_CONFIDENCE_THRESHOLD must be between 0 and 1.")

        return self

    @staticmethod
    def _normalize_text(value: str | None, default: str | None = None) -> str | None:
        if value is None:
            return default
        normalized = value.strip()
        if normalized == "":
            return default
        return normalized

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        assert self.postgres_host
        assert self.postgres_db
        assert self.postgres_user
        assert self.postgres_password
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def resolved_raw_data_dir(self) -> Path:
        return self.raw_data_dir or self.storage_root / "raw"

    @property
    def resolved_processed_data_dir(self) -> Path:
        return self.processed_data_dir or self.storage_root / "processed"

    @property
    def resolved_sample_data_dir(self) -> Path:
        return self.sample_data_dir or self.storage_root / "samples"

    @property
    def resolved_redis_url(self) -> str:
        if self.redis_url:
            return self.redis_url
        password_part = f":{self.redis_password}@" if self.redis_password else ""
        return (
            f"redis://{password_part}{self.redis_host}:{self.redis_port}/{self.redis_db}"
        )

    def ensure_storage_dirs(self) -> None:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.resolved_raw_data_dir.mkdir(parents=True, exist_ok=True)
        self.resolved_processed_data_dir.mkdir(parents=True, exist_ok=True)
        self.resolved_sample_data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def resolved_folder_source_allowed_roots(self) -> list[Path]:
        roots = [
            value.strip()
            for value in self.folder_source_allowed_roots.split(",")
            if value.strip()
        ]
        return [Path(root).expanduser().resolve() for root in roots]

    def is_external_service_configured(self, service_name: str) -> bool:
        mapping = {
            "zilliz": bool(self.zilliz_uri and self.zilliz_token and self.zilliz_collection),
            "milvus": bool(self.milvus_uri),
            "embedding_provider": bool(
                self.embedding_api_base and self.embedding_api_key and self.embedding_model
            ),
            "llm_provider": bool(self.llm_api_base and self.llm_api_key and self.llm_model),
            "image_recognition": bool(
                (self.image_recognition_provider == "paddle_ocr" and self.paddle_ocr_api_base and self.paddle_ocr_token)
                or (self.image_recognition_provider == "multimodal" and self.multimodal_api_base and self.multimodal_api_key and self.multimodal_model)
            ),
        }
        return mapping.get(service_name, False)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
