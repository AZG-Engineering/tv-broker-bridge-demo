"""Command-line interface: ``tv-bridge serve | kill | resume | status | check-heartbeat``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import uvicorn
from dotenv import load_dotenv

from tv_broker_bridge import __version__
from tv_broker_bridge.app import create_app
from tv_broker_bridge.clock import utc_now
from tv_broker_bridge.config import Settings, create_broker
from tv_broker_bridge.db import Database
from tv_broker_bridge.errors import BridgeError, ConfigError
from tv_broker_bridge.heartbeat import check_heartbeat, read_heartbeat
from tv_broker_bridge.kill_switch import KillSwitch
from tv_broker_bridge.logging_utils import configure_logging, log_event
from tv_broker_bridge.models import OrderStatus
from tv_broker_bridge.store import Store

EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_CONFIG = 2
EXIT_CANCEL_INCOMPLETE = 3


DISCLAIMER = (
    "Engineering demonstration only. Not investment advice. No strategy is included. "
    "Use paper trading. You are responsible for any use with a live account."
)


def _print_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_serve(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    settings = Settings.from_env(env)
    configure_logging(settings.log_level, settings.log_file)
    app = create_app(settings)  # raises ConfigError (risk file, live URL, ...) before binding
    log_event(
        "serving", host=args.host, port=args.port, broker=settings.broker, version=__version__
    )
    # One process only: SimBroker state and the reconcile/heartbeat threads live in-process.
    uvicorn.run(app, host=args.host, port=args.port, log_config=None, access_log=False)
    return EXIT_OK


def _open_store(settings: Settings) -> tuple[Database, Store]:
    db = Database(settings.db_path)
    return db, Store(db)


def cmd_kill(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    settings = Settings.from_env(env, require_secrets=False)
    configure_logging(settings.log_level, settings.log_file)
    db, store = _open_store(settings)
    broker = create_broker(settings) if settings.broker != "sim" else None
    try:
        kill = KillSwitch(store, broker, utc_now)
        outcome = kill.engage(args.reason, "cli", cancel=broker is not None)
    finally:
        if broker is not None:
            broker.close()
        db.close()
    note = None
    if broker is None:
        note = (
            "SimBroker lives inside the server process, so this command cannot cancel its "
            "orders directly. The flag is set; the running server cancels them within one "
            "heartbeat interval."
        )
    _print_json(
        {
            "status": "halted",
            "cancel_complete": outcome.cancel_complete,
            "canceled_order_ids": outcome.canceled_order_ids,
            "error": outcome.error,
            "note": note,
        }
    )
    if broker is None or outcome.cancel_complete:
        return EXIT_OK
    return EXIT_CANCEL_INCOMPLETE


def cmd_resume(_: argparse.Namespace, env: Mapping[str, str]) -> int:
    settings = Settings.from_env(env, require_secrets=False)
    configure_logging(settings.log_level, settings.log_file)
    db, store = _open_store(settings)
    try:
        released = KillSwitch(store, None, utc_now).release("cli")
    finally:
        db.close()
    _print_json({"status": "resumed" if released else "was_not_halted"})
    return EXIT_OK


def cmd_status(_: argparse.Namespace, env: Mapping[str, str]) -> int:
    settings = Settings.from_env(env, require_secrets=False)
    db, store = _open_store(settings)
    try:
        kill = KillSwitch(store, None, utc_now).state()
        working = store.list_orders([s for s in OrderStatus if s.is_working])
        payload = {
            "kill_switch": vars(kill),
            "positions": {k: str(v) for k, v in store.list_positions().items()},
            "working_orders": [
                {
                    "client_order_id": o.client_order_id,
                    "symbol": o.symbol,
                    "side": o.side.value,
                    "qty": str(o.qty),
                    "filled_qty": str(o.filled_qty),
                    "status": o.status.value,
                }
                for o in working
            ],
            "heartbeat": read_heartbeat(settings.heartbeat_path),
        }
    finally:
        db.close()
    _print_json(payload)
    return EXIT_OK


def cmd_check_heartbeat(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    path = args.path or Settings.from_env(env, require_secrets=False).heartbeat_path
    healthy, message = check_heartbeat(path, args.max_age, utc_now())
    print(message)
    return EXIT_OK if healthy else EXIT_UNHEALTHY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tv-bridge",
        description="TradingView-to-broker bridge demo (paper trading only).",
        epilog=DISCLAIMER,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the webhook server")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    kill = sub.add_parser("kill", help="stop accepting alerts and cancel all working orders")
    kill.add_argument("--reason", default="cli kill switch")
    kill.set_defaults(func=cmd_kill)

    resume = sub.add_parser("resume", help="release the kill switch")
    resume.set_defaults(func=cmd_resume)

    status = sub.add_parser("status", help="show kill switch, positions and working orders")
    status.set_defaults(func=cmd_status)

    hb = sub.add_parser(
        "check-heartbeat", help="exit 0 if the heartbeat file is fresh, 1 otherwise"
    )
    hb.add_argument("--max-age", type=float, default=30.0, help="seconds (default 30)")
    hb.add_argument("--path", default=None, help="heartbeat file (default: $HEARTBEAT_PATH)")
    hb.set_defaults(func=cmd_check_heartbeat)
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if env is None:
        load_dotenv()  # reads ./.env if present; real environment variables still win
        env = os.environ
    try:
        return int(args.func(args, env))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNHEALTHY


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
