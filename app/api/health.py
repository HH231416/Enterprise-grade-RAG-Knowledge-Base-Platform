from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_settings_dep, get_trace_id
from app.core.config import Settings
from app.core.metrics import get_metrics_response, service_health
from app.core.metrics import db_pool_available
from app.db.runtime import get_db_pool_status
from app.core.responses import success_response
from app.schemas.health import HealthStatus
from app.services.health import HealthService


router = APIRouter(tags=["system"])


@router.get("/health", response_model=dict)
def health_check(
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
):
    data: HealthStatus = HealthService(settings).check(session)
    # Update service_health gauges
    service_health.labels(service="postgres").set(1 if data.postgres == "up" else 0)
    service_health.labels(service="redis").set(1 if data.redis == "up" else 0)
    if data.database_pool and "available" in data.database_pool:
        db_pool_available.set(data.database_pool["available"])
    return success_response(data.model_dump(), trace_id)


@router.get("/metrics")
def metrics(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    if settings.metrics_auth_enabled and not hmac.compare_digest(internal_token or "", settings.internal_token):
        raise HTTPException(status_code=403, detail="metrics access denied")
    # Refresh system health gauges on each scrape
    try:
        session.execute(text("SELECT 1"))
        service_health.labels(service="postgres").set(1)
        pool_status = get_db_pool_status()
        if "available" in pool_status:
            db_pool_available.set(pool_status["available"])
    except Exception:
        service_health.labels(service="postgres").set(0)

    try:
        from redis import Redis
        Redis.from_url(
            settings.resolved_redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        ).ping()
        service_health.labels(service="redis").set(1)
    except Exception:
        service_health.labels(service="redis").set(0)

    return Response(content=get_metrics_response(), media_type="text/plain; version=0.0.4")
