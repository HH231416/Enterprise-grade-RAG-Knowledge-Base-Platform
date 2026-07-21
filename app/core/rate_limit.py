"""Redis-backed fixed-window rate limiting for public HTTP endpoints."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import Settings


_INCREMENT_WITH_TTL = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


class RateLimitUnavailable(RuntimeError):
    """Raised when the limiter is enabled but its Redis backend is unavailable."""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RedisFixedWindowRateLimiter:
    """A Redis Lua-script limiter safe for multiple API replicas."""

    def __init__(self, settings: Settings, redis_client: Redis | None = None) -> None:
        self.settings = settings
        self._redis = redis_client

    def check(self, client_key: str) -> RateLimitDecision:
        if not self.settings.rate_limit_enabled:
            return RateLimitDecision(
                allowed=True,
                remaining=self.settings.rate_limit_requests,
                retry_after_seconds=0,
            )

        window = self.settings.rate_limit_window_seconds
        window_id = int(time.time() // window)
        key = f"{self.settings.rate_limit_key_prefix}:{window_id}:{client_key}"
        try:
            result = self._client().eval(_INCREMENT_WITH_TTL, 1, key, window)
            count, ttl = int(result[0]), int(result[1])
        except (RedisError, OSError, TypeError, ValueError, IndexError) as exc:
            if self.settings.rate_limit_fail_open:
                return RateLimitDecision(
                    allowed=True,
                    remaining=self.settings.rate_limit_requests,
                    retry_after_seconds=0,
                )
            raise RateLimitUnavailable("Redis rate limiter is unavailable") from exc

        return RateLimitDecision(
            allowed=count <= self.settings.rate_limit_requests,
            remaining=max(self.settings.rate_limit_requests - count, 0),
            retry_after_seconds=max(ttl, 1),
        )

    def _client(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(
                self.settings.resolved_redis_url,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
        return self._redis
