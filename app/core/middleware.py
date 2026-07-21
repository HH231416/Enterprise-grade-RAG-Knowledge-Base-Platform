from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import Settings
from app.core.metrics import (
    http_request_duration_seconds,
    http_requests_total,
    rate_limit_backend_failures_total,
    rate_limit_rejections_total,
)
from app.core.rate_limit import RateLimitUnavailable, RedisFixedWindowRateLimiter
from app.core.trace import generate_trace_id, set_trace_id


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or generate_trace_id()
        request.state.trace_id = trace_id
        set_trace_id(trace_id)
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Protect public API routes with a Redis-backed, replica-safe limit."""

    _skip_paths = {"/api/v1/health", "/api/v1/metrics", "/docs", "/openapi.json", "/favicon.ico"}

    def __init__(self, app, *, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings
        self.limiter = RedisFixedWindowRateLimiter(settings)

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.settings.rate_limit_enabled or request.url.path in self._skip_paths:
            return await call_next(request)

        try:
            decision = await run_in_threadpool(self.limiter.check, self._client_key(request))
        except RateLimitUnavailable:
            rate_limit_backend_failures_total.inc()
            return JSONResponse(
                status_code=503,
                content={"code": 50301, "message": "rate limit backend unavailable"},
            )

        if not decision.allowed:
            rate_limit_rejections_total.labels(method=request.method, path=request.url.path).inc()
            return JSONResponse(
                status_code=429,
                content={"code": 42901, "message": "rate limit exceeded"},
                headers={
                    "Retry-After": str(decision.retry_after_seconds),
                    "X-RateLimit-Limit": str(self.settings.rate_limit_requests),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.settings.rate_limit_requests)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        return response

    def _client_key(self, request: Request) -> str:
        if self.settings.rate_limit_trust_x_forwarded_for:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",", 1)[0].strip()
        return request.client.host if request.client else "unknown"


class MetricsMiddleware(BaseHTTPMiddleware):
    _skip_paths = {"/api/v1/health", "/api/v1/metrics", "/docs", "/openapi.json", "/favicon.ico"}

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        method = request.method

        if path not in self._skip_paths:
            start = time.perf_counter()
            response = await call_next(request)
            elapsed = time.perf_counter() - start
            http_requests_total.labels(method=method, path=path, status=str(response.status_code)).inc()
            http_request_duration_seconds.labels(method=method, path=path).observe(elapsed)
            return response

        return await call_next(request)
