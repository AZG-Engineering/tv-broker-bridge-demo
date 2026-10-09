"""Process configuration, read from environment variables.

Fails closed: missing, short, placeholder or duplicated secrets, an unknown
broker name, or a non-paper Alpaca URL all raise ``ConfigError`` before the
server binds a port.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from tv_broker_bridge.broker.alpaca import (
    PAPER_BASE_URL,
    AlpacaPaperBroker,
    validate_paper_base_url,
)
from tv_broker_bridge.broker.base import Broker
from tv_broker_bridge.broker.sim import SimBroker, SimConfig
from tv_broker_bridge.errors import ConfigError

BrokerName = Literal["sim", "alpaca_paper"]
MIN_SECRET_LENGTH = 16
PLACEHOLDER_PREFIX = "replace-with"


def _secret(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required (see .env.example; `make init` generates one)")
    if value.lower().startswith(PLACEHOLDER_PREFIX):
        raise ConfigError(f"{name} is still the placeholder from .env.example; set a real value")
    if len(value) < MIN_SECRET_LENGTH:
        raise ConfigError(f"{name} must be at least {MIN_SECRET_LENGTH} characters")
    return value


def _number(env: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not value >= minimum:  # also rejects NaN
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _probability(env: Mapping[str, str], name: str) -> float:
    value = _number(env, name, 0.0)
    if value > 1.0:
        raise ConfigError(f"{name} must be between 0 and 1")
    return value


@dataclass(frozen=True)
class Settings:
    webhook_secret: str = field(repr=False)
    admin_secret: str = field(repr=False)
    broker: BrokerName = "sim"
    alpaca_key_id: str | None = field(default=None, repr=False)
    alpaca_secret_key: str | None = field(default=None, repr=False)
    alpaca_base_url: str = PAPER_BASE_URL
    risk_config_path: str = "config/risk.yaml"
    db_path: str = "data/bridge.db"
    heartbeat_path: str = "data/heartbeat.json"
    log_level: str = "INFO"
    log_file: str | None = None
    reconcile_interval_s: float = 30.0
    heartbeat_interval_s: float = 5.0
    broker_timeout_s: float = 2.0
    submit_attempts: int = 3
    submit_backoff_s: float = 0.2
    unknown_grace_s: float = 120.0
    max_body_bytes: int = 16_384
    sim: SimConfig = field(default_factory=SimConfig)

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, require_secrets: bool = True) -> Settings:
        """Build settings from ``env``.

        ``require_secrets=False`` is for CLI commands (kill, status, ...) that never
        authenticate an inbound request.
        """
        webhook = admin = ""
        if require_secrets:
            webhook = _secret(env, "WEBHOOK_SECRET")
            admin = _secret(env, "ADMIN_SECRET")
            if webhook == admin:
                raise ConfigError("ADMIN_SECRET must differ from WEBHOOK_SECRET")

        broker = env.get("BROKER", "sim").strip().lower()
        if broker not in ("sim", "alpaca_paper"):
            raise ConfigError(f"BROKER must be 'sim' or 'alpaca_paper', got {broker!r}")

        base_url = env.get("ALPACA_BASE_URL", PAPER_BASE_URL).strip() or PAPER_BASE_URL
        key_id = env.get("ALPACA_API_KEY_ID", "").strip() or None
        secret_key = env.get("ALPACA_API_SECRET_KEY", "").strip() or None
        if broker == "alpaca_paper":
            validate_paper_base_url(base_url)  # refuse live/odd URLs before anything starts
            if not key_id or not secret_key:
                raise ConfigError(
                    "BROKER=alpaca_paper requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY"
                )

        seed_raw = env.get("SIM_SEED", "").strip()
        try:
            seed = int(seed_raw) if seed_raw else None
        except ValueError:
            raise ConfigError("SIM_SEED must be an integer") from None
        try:
            sim_price = Decimal(env.get("SIM_DEFAULT_PRICE", "100.00"))
        except InvalidOperation:
            raise ConfigError("SIM_DEFAULT_PRICE must be a decimal number") from None
        if not sim_price.is_finite() or sim_price <= 0:
            raise ConfigError("SIM_DEFAULT_PRICE must be > 0")

        return cls(
            webhook_secret=webhook,
            admin_secret=admin,
            broker=cast(BrokerName, broker),
            alpaca_key_id=key_id,
            alpaca_secret_key=secret_key,
            alpaca_base_url=base_url,
            risk_config_path=env.get("RISK_CONFIG_PATH", "config/risk.yaml"),
            db_path=env.get("DB_PATH", "data/bridge.db"),
            heartbeat_path=env.get("HEARTBEAT_PATH", "data/heartbeat.json"),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            log_file=env.get("LOG_FILE") or None,
            reconcile_interval_s=_number(env, "RECONCILE_INTERVAL_S", 30.0, minimum=0.1),
            heartbeat_interval_s=_number(env, "HEARTBEAT_INTERVAL_S", 5.0, minimum=0.1),
            broker_timeout_s=_number(env, "BROKER_TIMEOUT_S", 2.0, minimum=0.1),
            submit_attempts=int(_number(env, "SUBMIT_ATTEMPTS", 3, minimum=1)),
            submit_backoff_s=_number(env, "SUBMIT_BACKOFF_S", 0.2),
            unknown_grace_s=_number(env, "UNKNOWN_GRACE_S", 120.0),
            sim=SimConfig(
                latency_s=_number(env, "SIM_LATENCY_MS", 0.0) / 1000.0,
                default_price=sim_price,
                partial_fill_probability=_probability(env, "SIM_PARTIAL_FILL_PROB"),
                reject_probability=_probability(env, "SIM_REJECT_PROB"),
                seed=seed,
            ),
        )


def create_broker(settings: Settings) -> Broker:
    """Instantiate the configured broker adapter."""
    if settings.broker == "alpaca_paper":
        return AlpacaPaperBroker(
            key_id=settings.alpaca_key_id,
            secret_key=settings.alpaca_secret_key,
            base_url=settings.alpaca_base_url,
            timeout_s=settings.broker_timeout_s,
        )
    return SimBroker(settings.sim)
