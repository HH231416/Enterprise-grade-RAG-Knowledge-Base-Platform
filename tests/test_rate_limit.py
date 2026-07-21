from __future__ import annotations

from app.core.config import get_settings
from app.core.rate_limit import RedisFixedWindowRateLimiter


class FakeRedis:
    def __init__(self) -> None:
        self.calls = 0

    def eval(self, _script, _key_count, _key, _window):
        self.calls += 1
        return [self.calls, 42]


def test_redis_rate_limiter_rejects_after_configured_limit(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "2")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    get_settings.cache_clear()

    limiter = RedisFixedWindowRateLimiter(get_settings(), redis_client=FakeRedis())
    assert limiter.check("client-a").allowed is True
    assert limiter.check("client-a").remaining == 0
    decision = limiter.check("client-a")
    assert decision.allowed is False
    assert decision.retry_after_seconds == 42

    get_settings.cache_clear()
