#!/usr/bin/env python3
"""Send a few sample alerts to a running bridge to show dedupe, risk and validation.

Usage: ``make demo`` (with ``make run`` going in another terminal).
Reads WEBHOOK_SECRET from the environment or ./.env. Talks only to the URL you give it
(default http://127.0.0.1:8000), which should be your own local bridge.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx
from dotenv import load_dotenv


def show(title: str, response: httpx.Response) -> None:
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    print(f"\n{title}\n  -> HTTP {response.status_code} {json.dumps(body)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    load_dotenv()
    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret:
        print("WEBHOOK_SECRET is not set; run `make init` first.", file=sys.stderr)
        return 1

    endpoint = f"{args.url.rstrip('/')}/webhook/tradingview"
    alert_id = f"demo-{int(time.time())}"
    alert = {
        "secret": secret,
        "alert_id": alert_id,
        "symbol": "AAPL",
        "side": "buy",
        "qty": 10,
        "order_type": "market",
    }
    try:
        with httpx.Client(timeout=5.0) as client:
            show("1. A valid market order", client.post(endpoint, json=alert))
            show(
                "2. The same alert_id again (TradingView retry / double click)",
                client.post(endpoint, json=alert),
            )
            show(
                "3. Quantity above max_qty_per_order",
                client.post(endpoint, json={**alert, "alert_id": alert_id + "-big", "qty": 100000}),
            )
            show(
                "4. Malformed: qty sent as a string",
                client.post(endpoint, json={**alert, "alert_id": alert_id + "-bad", "qty": "10"}),
            )
            show(
                "5. Wrong secret",
                client.post(
                    endpoint, json={**alert, "alert_id": alert_id + "-x", "secret": "nope"}
                ),
            )
            show("6. Health", client.get(f"{args.url.rstrip('/')}/health"))
    except httpx.ConnectError:
        print(f"\nCould not reach {args.url}. Is `make run` going?", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
