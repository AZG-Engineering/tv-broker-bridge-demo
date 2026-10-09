"""FastAPI application: webhook receiver, kill-switch endpoint, health."""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from tv_broker_bridge.alerting import AlertSink
from tv_broker_bridge.broker.base import Broker
from tv_broker_bridge.clock import Clock, utc_now
from tv_broker_bridge.config import Settings
from tv_broker_bridge.logging_utils import log_event
from tv_broker_bridge.risk import RiskConfig
from tv_broker_bridge.runtime import Runtime, build_runtime
from tv_broker_bridge.schemas import (
    SECRET_FIELD,
    PayloadError,
    parse_json_object,
    validate_alert,
)

WEBHOOK_PATH = "/webhook/tradingview"
SECRET_HEADER = "x-webhook-secret"  # noqa: S105 - header name, not a credential
ADMIN_HEADER = "x-admin-secret"


def _secrets_match(expected: str, supplied: object) -> bool:
    """Constant-time comparison; anything that is not a non-empty string never matches."""
    if not isinstance(supplied, str) or not supplied:
        return False
    return hmac.compare_digest(expected.encode(), supplied.encode())


def _safe_alert_id(data: Mapping[str, Any]) -> str | None:
    value = data.get("alert_id")
    return value[:64] if isinstance(value, str) else None


def handle_webhook(
    rt: Runtime, body: bytes, header_secret: str | None, client: str | None
) -> JSONResponse:
    """Authenticate, strictly validate, then hand the alert to the pipeline."""
    # 1. Parse. An unparseable body from an unauthenticated caller is reported as 401 (we
    #    reveal nothing about the expected format); from an authenticated caller, as 400.
    try:
        data = parse_json_object(body)
    except PayloadError as exc:
        if _secrets_match(rt.settings.webhook_secret, header_secret):
            log_event(
                "rejected_malformed",
                level=logging.WARNING,
                reason=exc.code,
                details=exc.details,
                bytes=len(body),
                client=client,
            )
            return JSONResponse(
                {"status": "rejected", "error": exc.code, "details": exc.details}, 400
            )
        log_event(
            "auth_failed",
            level=logging.WARNING,
            endpoint="webhook",
            reason="unparseable_body_without_valid_header_secret",
            client=client,
        )
        return JSONResponse({"status": "unauthorized"}, 401)

    # 2. Authenticate (header, else the "secret" field in the body: TradingView cannot set headers).
    body_secret = data.pop(SECRET_FIELD, None)
    supplied = header_secret if header_secret is not None else body_secret
    if not _secrets_match(rt.settings.webhook_secret, supplied):
        log_event(
            "auth_failed",
            level=logging.WARNING,
            endpoint="webhook",
            reason="missing_secret" if supplied is None else "bad_secret",
            client=client,
        )
        return JSONResponse({"status": "unauthorized"}, 401)

    # 3. Strict schema validation.
    try:
        alert = validate_alert(data)
    except PayloadError as exc:
        log_event(
            "rejected_malformed",
            level=logging.WARNING,
            reason=exc.code,
            details=exc.details,
            alert_id=_safe_alert_id(data),
            client=client,
        )
        return JSONResponse({"status": "rejected", "error": exc.code, "details": exc.details}, 400)

    # 4. Pipeline.
    outcome = rt.processor.process(alert)
    return JSONResponse(outcome.body(), outcome.http_status)


def create_app(
    settings: Settings,
    *,
    broker: Broker | None = None,
    risk: RiskConfig | None = None,
    clock: Clock = utc_now,
    alert_sink: AlertSink | None = None,
    sleep: Callable[[float], None] | None = None,
    start_background: bool = True,
) -> FastAPI:
    rt = build_runtime(
        settings, broker=broker, risk=risk, clock=clock, alert_sink=alert_sink, sleep=sleep
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_background:
            rt.start_background()
        log_event(
            "startup",
            broker=rt.broker.name,
            risk_limits={
                "max_qty_per_order": rt.risk.max_qty_per_order,
                "max_position_per_symbol": rt.risk.max_position_per_symbol,
                "max_orders_per_day": rt.risk.max_orders_per_day,
                "allowed_symbols": rt.risk.allowed_symbols,
            },
            kill_switch_engaged=rt.kill.is_engaged(),
        )
        try:
            yield
        finally:
            rt.close()
            log_event("shutdown")

    # No interactive docs / OpenAPI: this may sit behind a public tunnel.
    app = FastAPI(
        title="tv-broker-bridge",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = rt

    @app.post(WEBHOOK_PATH)
    async def webhook(request: Request) -> JSONResponse:
        limit = rt.settings.max_body_bytes
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            log_event(
                "rejected_malformed", level=logging.WARNING, reason="body_too_large", bytes=declared
            )
            return JSONResponse({"status": "rejected", "error": "body_too_large"}, 413)
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                log_event(
                    "rejected_malformed", level=logging.WARNING, reason="body_too_large", bytes=size
                )
                return JSONResponse({"status": "rejected", "error": "body_too_large"}, 413)
            chunks.append(chunk)
        client = request.client.host if request.client else None
        try:
            return await run_in_threadpool(
                handle_webhook, rt, b"".join(chunks), request.headers.get(SECRET_HEADER), client
            )
        except Exception as exc:
            log_event(
                "internal_error",
                level=logging.ERROR,
                endpoint="webhook",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return JSONResponse({"status": "error"}, 500)

    @app.post("/admin/kill")
    async def kill(
        request: Request, x_admin_secret: str | None = Header(default=None)
    ) -> JSONResponse:
        if not _secrets_match(rt.settings.admin_secret, x_admin_secret):
            log_event(
                "auth_failed",
                level=logging.WARNING,
                endpoint="admin_kill",
                reason="missing_secret" if x_admin_secret is None else "bad_secret",
                client=request.client.host if request.client else None,
            )
            return JSONResponse({"status": "unauthorized"}, 401)
        reason = "http kill switch"
        raw = await request.body()
        if raw:
            try:
                parsed = parse_json_object(raw[:2048])
                if isinstance(parsed.get("reason"), str):
                    reason = parsed["reason"][:200]
            except PayloadError:
                pass  # the reason is optional; never let a bad body block a kill
        outcome = await run_in_threadpool(rt.kill.engage, reason, "http")
        return JSONResponse(
            {
                "status": "halted",
                "cancel_complete": outcome.cancel_complete,
                "canceled_order_ids": outcome.canceled_order_ids,
                "error": outcome.error,
            },
            200 if outcome.cancel_complete else 202,
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        body = await run_in_threadpool(rt.health)
        return JSONResponse(body, 503 if body["status"] == "degraded" else 200)

    return app
