from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def test_settings_build_database_url():
    settings = get_settings()
    assert settings.sqlalchemy_database_url.startswith("sqlite:///")
    assert settings.resolved_raw_data_dir.name == "raw"


def test_bot_response_mode_validation(monkeypatch):
    monkeypatch.setenv("BOT_RESPONSE_MODE", "invalid")

    with pytest.raises(ValidationError):
        Settings()


def test_feishu_enabled_requires_required_config(monkeypatch):
    monkeypatch.setenv("FEISHU_ENABLED", "true")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "token")

    with pytest.raises(ValidationError):
        Settings()


def test_wecom_enabled_requires_required_config(monkeypatch):
    monkeypatch.setenv("WECOM_ENABLED", "true")
    monkeypatch.setenv("WECOM_CORP_ID", "corp")
    monkeypatch.setenv("WECOM_AGENT_ID", "100001")
    monkeypatch.setenv("WECOM_SECRET", "secret")
    monkeypatch.setenv("WECOM_CALLBACK_TOKEN", "token")
    monkeypatch.setenv("WECOM_ENCODING_AES_KEY", "")

    with pytest.raises(ValidationError):
        Settings()


def test_non_local_rejects_default_secrets(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_API_KEY", "change-me")
    monkeypatch.setenv("INTERNAL_TOKEN", "change-me")

    with pytest.raises(ValidationError):
        Settings()


def test_non_local_rejects_permission_mode_none(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_API_KEY", "prod-api-key")
    monkeypatch.setenv("INTERNAL_TOKEN", "prod-internal-token")
    monkeypatch.setenv("PERMISSION_MODE", "none")

    with pytest.raises(ValidationError):
        Settings()


def test_explicit_local_provider_stays_local(monkeypatch):
    monkeypatch.setenv("VECTOR_STORE_PROVIDER", "local")
    monkeypatch.setenv("MILVUS_URI", "http://localhost:19530")

    settings = Settings()

    assert settings.vector_store_provider == "local"


def test_internal_gateway_provider_configuration(monkeypatch):
    monkeypatch.setenv("MODEL_SERVING_MODE", "internal_gateway")
    monkeypatch.setenv("LLM_PROVIDER", "vLLM")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "TEI")

    settings = Settings()

    assert settings.model_serving_mode == "internal_gateway"
    assert settings.llm_provider == "vllm"
    assert settings.embedding_provider == "tei"


def test_invalid_model_serving_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "unsupported-provider")

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FAILED_UPLOAD_RETENTION_HOURS", "-1"),
        ("FAILED_UPLOAD_CLEANUP_BATCH_SIZE", "0"),
    ],
)
def test_failed_upload_retention_configuration_is_validated(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_production_internal_gateway_requires_vllm_and_tei(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_API_KEY", "prod-api-key")
    monkeypatch.setenv("INTERNAL_TOKEN", "prod-internal-token")
    monkeypatch.setenv("PERMISSION_MODE", "plugin")
    monkeypatch.setenv("MODEL_SERVING_MODE", "internal_gateway")
    monkeypatch.setenv("LLM_PROVIDER", "vllm")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "tei")
    monkeypatch.setenv("LLM_API_BASE", "http://vllm.internal/v1")
    monkeypatch.setenv("EMBEDDING_API_BASE", "http://tei.internal/v1")
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-key")
    monkeypatch.setenv("LLM_MODEL", "Qwen/Qwen3-14B")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")

    settings = Settings()

    assert settings.model_serving_mode == "internal_gateway"
