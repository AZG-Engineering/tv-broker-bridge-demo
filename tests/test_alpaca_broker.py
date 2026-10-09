"""AlpacaPaperBroker against mocked HTTP. No test here touches the network."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    ADMIN_SECRET,
    WEBHOOK_SECRET,
    FakeClock,
    ListAlertSink,
    default_risk,
    make_alert,
    make_settings,
)
from tv_broker_bridge.app import create_app
from tv_broker_bridge.broker.alpaca import (
    PAPER_BASE_URL,
    AlpacaPaperBroker,
    validate_paper_base_url,
)
from tv_broker_bridge.cli import EXIT_CONFIG, main
from tv_broker_bridge.config import Settings
from tv_broker_bridge.errors import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerError,
    ConfigError,
    LiveEndpointRefusedError,
    OrderNotCancelableError,
    OrderNotFoundError,
    OrderRejectedError,
)
from tv_broker_bridge.models import OrderRequest, OrderStatus, OrderType, Side

D = Decimal
KEY_ID = "TEST-KEY-ID-NOT-REAL"
SECRET_KEY = "test-secret-key-not-real-0000"

Handler = Callable[[httpx.Request], httpx.Response]


def order_json(**overrides: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "id": "ord-111",
        "client_order_id": "tvb-abc",
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "qty": "10",
        "filled_qty": "0",
        "filled_avg_price": None,
        "limit_price": None,
        "status": "accepted",
    }
    order.update(overrides)
    return order


class Recorder:
    """Transport that records every request and answers with a scripted handler."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = handler or (lambda r: httpx.Response(200, json=order_json()))
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)  # type: ignore[no-any-return]


def make_broker(recorder: Recorder, **kwargs: Any) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(
        key_id=KEY_ID, secret_key=SECRET_KEY, transport=recorder.transport, **kwargs
    )


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def broker(rec: Recorder) -> Iterator[AlpacaPaperBroker]:
    b = make_broker(rec)
    yield b
    b.close()


def request_for(**overrides: Any) -> OrderRequest:
    values: dict[str, Any] = {
        "client_order_id": "tvb-abc",
        "symbol": "AAPL",
        "side": Side.BUY,
        "qty": D(10),
        "order_type": OrderType.MARKET,
    }
    values.update(overrides)
    return OrderRequest(**values)


class TestPaperOnlyGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "https://api.alpaca.markets",  # the LIVE host
            "https://api.alpaca.markets/v2",
            "https://API.ALPACA.MARKETS",
            "http://paper-api.alpaca.markets",  # not TLS
            "https://paper-api.alpaca.markets.evil.example",  # look-alike suffix
            "https://evil.example/paper-api.alpaca.markets",
            "https://evil-paper-api.alpaca.markets",
            "https://paper-api.alpaca.markets@evil.example",  # userinfo trick
            "https://user:pw@paper-api.alpaca.markets",
            "https://paper-api.alpaca.markets\\@evil.example",
            "https://paper-api.alpaca.markets:8443",
            "https://paper-api.alpaca.markets:99999999",
            "https://paper-api.alpaca.markets/other",
            "https://paper-api.alpaca.markets/v2/orders",
            "https://paper-api.alpaca.markets/v2?x=1",
            "https://paper-api.alpaca.markets#frag",
            "https://paper-api.alpaca.markets.",  # trailing dot
            "https://paper-api.alpaca.markets\n",
            " https://paper-api.alpaca.markets",
            "https://p\u0430per-api.alpaca.markets",  # Cyrillic 'a' homoglyph
            "paper-api.alpaca.markets",  # no scheme
            "https://",
            "ftp://paper-api.alpaca.markets",
            "https://localhost:8080",
            "https://127.0.0.1",
            "",
        ],
    )
    def test_anything_but_the_paper_endpoint_is_refused(self, url: str) -> None:
        with pytest.raises(LiveEndpointRefusedError, match=r"paper-api\.alpaca\.markets"):
            validate_paper_base_url(url)

    def test_refusal_happens_at_construction_before_any_request(self) -> None:
        recorder = Recorder()
        with pytest.raises(LiveEndpointRefusedError):
            AlpacaPaperBroker(
                key_id=KEY_ID,
                secret_key=SECRET_KEY,
                base_url="https://api.alpaca.markets",
                transport=recorder.transport,
            )
        assert recorder.requests == []

    def test_the_live_host_is_called_out_by_name(self) -> None:
        with pytest.raises(LiveEndpointRefusedError, match="LIVE"):
            validate_paper_base_url("https://api.alpaca.markets")

    @pytest.mark.parametrize(
        "url",
        [
            "https://paper-api.alpaca.markets",
            "https://paper-api.alpaca.markets/",
            "https://paper-api.alpaca.markets/v2",
            "https://paper-api.alpaca.markets/v2/",
            "https://paper-api.alpaca.markets:443",
            "https://PAPER-API.ALPACA.MARKETS",
        ],
    )
    def test_the_paper_endpoint_is_accepted_and_canonicalised(self, url: str) -> None:
        assert validate_paper_base_url(url) == PAPER_BASE_URL

    def test_settings_refuse_to_start_with_a_live_url(self) -> None:
        env = {
            "WEBHOOK_SECRET": WEBHOOK_SECRET,
            "ADMIN_SECRET": ADMIN_SECRET,
            "BROKER": "alpaca_paper",
            "ALPACA_API_KEY_ID": KEY_ID,
            "ALPACA_API_SECRET_KEY": SECRET_KEY,
            "ALPACA_BASE_URL": "https://api.alpaca.markets",
        }
        with pytest.raises(LiveEndpointRefusedError):
            Settings.from_env(env)

    def test_a_second_check_blocks_any_non_paper_request_that_slips_through(
        self, rec: Recorder
    ) -> None:
        broker = make_broker(rec)
        # Simulate a bug or tampering that points the client elsewhere after construction.
        broker._client.base_url = httpx.URL("https://api.alpaca.markets")
        with pytest.raises(LiveEndpointRefusedError, match="blocked a request"):
            broker.ping()
        assert rec.requests == []  # blocked before anything was sent
        broker.close()

    def test_redirects_are_never_followed(self) -> None:
        recorder = Recorder(
            lambda r: httpx.Response(
                302, headers={"Location": "https://api.alpaca.markets/v2/orders"}
            )
        )
        broker = make_broker(recorder)
        with pytest.raises(BrokerError, match="redirect"):
            broker.submit_order(request_for())
        assert len(recorder.requests) == 1  # the Location was not requested
        assert {r.url.host for r in recorder.requests} == {"paper-api.alpaca.markets"}
        broker.close()


class TestCredentials:
    def test_keys_are_required(self) -> None:
        for key, secret in [(None, SECRET_KEY), (KEY_ID, None), ("", ""), (None, None)]:
            with pytest.raises(ConfigError, match="ALPACA_API_KEY_ID"):
                AlpacaPaperBroker(key_id=key, secret_key=secret)

    def test_from_env_reads_only_the_environment(self) -> None:
        env = {"ALPACA_API_KEY_ID": KEY_ID, "ALPACA_API_SECRET_KEY": SECRET_KEY}
        recorder = Recorder()
        broker = AlpacaPaperBroker.from_env(env, transport=recorder.transport)
        broker.ping()
        assert recorder.last.headers["APCA-API-KEY-ID"] == KEY_ID
        assert recorder.last.headers["APCA-API-SECRET-KEY"] == SECRET_KEY
        broker.close()
        with pytest.raises(ConfigError):
            AlpacaPaperBroker.from_env({})

    def test_from_env_defaults_to_and_enforces_the_paper_url(self) -> None:
        env = {
            "ALPACA_API_KEY_ID": KEY_ID,
            "ALPACA_API_SECRET_KEY": SECRET_KEY,
            "ALPACA_BASE_URL": "https://api.alpaca.markets",
        }
        with pytest.raises(LiveEndpointRefusedError):
            AlpacaPaperBroker.from_env(env)

    def test_keys_never_appear_in_repr_or_errors(self, broker: AlpacaPaperBroker) -> None:
        assert SECRET_KEY not in repr(broker)
        assert KEY_ID not in repr(broker)
        recorder = Recorder(lambda r: httpx.Response(401, json={"message": "unauthorized"}))
        failing = make_broker(recorder)
        with pytest.raises(BrokerAuthError) as excinfo:
            failing.ping()
        assert SECRET_KEY not in str(excinfo.value)
        assert KEY_ID not in str(excinfo.value)
        failing.close()

    def test_settings_do_not_leak_secrets_in_repr(self, tmp_path: Path) -> None:
        settings = make_settings(
            tmp_path,
            BROKER="alpaca_paper",
            ALPACA_API_KEY_ID=KEY_ID,
            ALPACA_API_SECRET_KEY=SECRET_KEY,
        )
        text = repr(settings)
        for secret in (SECRET_KEY, KEY_ID, WEBHOOK_SECRET, ADMIN_SECRET):
            assert secret not in text


class TestRequests:
    def test_market_order_request_shape(self, rec: Recorder, broker: AlpacaPaperBroker) -> None:
        broker.submit_order(request_for())
        request = rec.last
        assert (request.method, request.url.path) == ("POST", "/v2/orders")
        assert request.url.host == "paper-api.alpaca.markets"
        assert request.url.scheme == "https"
        assert rec.body() == {
            "symbol": "AAPL",
            "qty": "10",
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": "tvb-abc",
        }
        assert request.headers["APCA-API-KEY-ID"] == KEY_ID
        assert request.headers["APCA-API-SECRET-KEY"] == SECRET_KEY

    def test_limit_order_includes_the_price_as_a_string(
        self, rec: Recorder, broker: AlpacaPaperBroker
    ) -> None:
        broker.submit_order(
            request_for(side=Side.SELL, order_type=OrderType.LIMIT, limit_price=D("101.25"))
        )
        assert rec.body()["limit_price"] == "101.25"
        assert rec.body()["type"] == "limit"
        assert rec.body()["side"] == "sell"

    def test_order_response_is_parsed(self) -> None:
        recorder = Recorder(
            lambda r: httpx.Response(
                200,
                json=order_json(
                    status="partially_filled",
                    qty="10",
                    filled_qty="4",
                    filled_avg_price="187.5",
                    type="limit",
                    limit_price="190",
                ),
            )
        )
        broker = make_broker(recorder)
        order = broker.submit_order(request_for())
        assert order.broker_order_id == "ord-111"
        assert order.client_order_id == "tvb-abc"
        assert order.status is OrderStatus.PARTIALLY_FILLED
        assert (order.qty, order.filled_qty) == (D(10), D(4))
        assert order.avg_fill_price == D("187.5")
        assert order.limit_price == D(190)
        assert order.order_type is OrderType.LIMIT
        broker.close()

    @pytest.mark.parametrize(
        ("alpaca", "expected"),
        [
            ("new", OrderStatus.ACCEPTED),
            ("accepted", OrderStatus.ACCEPTED),
            ("pending_new", OrderStatus.ACCEPTED),
            ("pending_cancel", OrderStatus.ACCEPTED),
            ("partially_filled", OrderStatus.PARTIALLY_FILLED),
            ("filled", OrderStatus.FILLED),
            ("canceled", OrderStatus.CANCELED),
            ("expired", OrderStatus.CANCELED),
            ("done_for_day", OrderStatus.CANCELED),
            ("replaced", OrderStatus.CANCELED),
            ("rejected", OrderStatus.REJECTED),
        ],
    )
    def test_status_mapping(self, alpaca: str, expected: OrderStatus) -> None:
        recorder = Recorder(lambda r: httpx.Response(200, json=order_json(status=alpaca)))
        broker = make_broker(recorder)
        assert broker.submit_order(request_for()).status is expected
        broker.close()

    def test_unknown_status_is_treated_as_working_and_logged(self, logs: Any) -> None:
        recorder = Recorder(lambda r: httpx.Response(200, json=order_json(status="brand_new")))
        broker = make_broker(recorder)
        assert broker.submit_order(request_for()).status is OrderStatus.ACCEPTED
        assert logs.events("broker_unknown_status")[0]["status"] == "brand_new"
        broker.close()

    def test_non_market_non_limit_types_are_tolerated_for_adopted_orders(self) -> None:
        recorder = Recorder(lambda r: httpx.Response(200, json=order_json(type="stop")))
        broker = make_broker(recorder)
        assert broker.submit_order(request_for()).order_type is OrderType.OTHER
        broker.close()

    def test_get_order_and_lookup_by_client_id(
        self, rec: Recorder, broker: AlpacaPaperBroker
    ) -> None:
        broker.get_order("ord-111")
        assert rec.last.url.path == "/v2/orders/ord-111"
        broker.get_order_by_client_id("tvb-abc")
        assert rec.last.url.path == "/v2/orders:by_client_order_id"
        assert rec.last.url.params["client_order_id"] == "tvb-abc"

    def test_ids_are_path_escaped(self, rec: Recorder, broker: AlpacaPaperBroker) -> None:
        broker.get_order("../account")
        assert rec.last.url.raw_path.decode() == "/v2/orders/..%2Faccount"

    def test_not_found_handling(self) -> None:
        recorder = Recorder(lambda r: httpx.Response(404, json={"message": "order not found"}))
        broker = make_broker(recorder)
        with pytest.raises(OrderNotFoundError):
            broker.get_order("nope")
        assert broker.get_order_by_client_id("nope") is None
        with pytest.raises(OrderNotFoundError):
            broker.cancel_order("nope")
        broker.close()

    def test_list_orders_asks_for_all_statuses(self) -> None:
        recorder = Recorder(
            lambda r: httpx.Response(
                200, json=[order_json(id="a"), order_json(id="b", status="filled")]
            )
        )
        broker = make_broker(recorder)
        orders = broker.list_orders()
        assert [o.broker_order_id for o in orders] == ["a", "b"]
        assert recorder.last.url.params["status"] == "all"
        assert recorder.last.url.params["limit"] == "500"
        broker.close()

    def test_positions_long_and_short(self) -> None:
        recorder = Recorder(
            lambda r: httpx.Response(
                200,
                json=[
                    {"symbol": "AAPL", "qty": "10", "side": "long"},
                    {"symbol": "MSFT", "qty": "-4", "side": "short"},
                    {"symbol": "SPY", "qty": "3", "side": "short"},  # positive qty + side=short
                ],
            )
        )
        broker = make_broker(recorder)
        positions = {p.symbol: p.qty for p in broker.list_positions()}
        assert positions == {"AAPL": D(10), "MSFT": D(-4), "SPY": D(-3)}
        assert recorder.last.url.path == "/v2/positions"
        broker.close()

    def test_ping_uses_the_clock_endpoint_which_carries_no_account_data(
        self, rec: Recorder, broker: AlpacaPaperBroker
    ) -> None:
        broker.ping()
        assert rec.last.url.path == "/v2/clock"

    def test_cancel_endpoints(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/orders":  # DELETE all
                return httpx.Response(
                    207, json=[{"id": "a", "status": 200}, {"id": "b", "status": 200}]
                )
            return httpx.Response(204)

        recorder = Recorder(handler)
        broker = make_broker(recorder)
        broker.cancel_order("a")
        assert (recorder.last.method, recorder.last.url.path) == ("DELETE", "/v2/orders/a")
        assert broker.cancel_all_orders() == ["a", "b"]
        assert (recorder.last.method, recorder.last.url.path) == ("DELETE", "/v2/orders")
        broker.close()

    def test_cancel_all_tolerates_already_closed_orders_but_not_real_failures(self) -> None:
        ok = Recorder(
            lambda r: httpx.Response(
                207, json=[{"id": "a", "status": 200}, {"id": "b", "status": 422}]
            )
        )
        broker = make_broker(ok)
        assert broker.cancel_all_orders() == ["a"]
        broker.close()

        bad = Recorder(
            lambda r: httpx.Response(
                207, json=[{"id": "a", "status": 200}, {"id": "b", "status": 500}]
            )
        )
        broker = make_broker(bad)
        with pytest.raises(BrokerError, match="could not be canceled"):
            broker.cancel_all_orders()
        broker.close()

    def test_cancelling_a_finished_order(self) -> None:
        recorder = Recorder(lambda r: httpx.Response(422, json={"message": "order is filled"}))
        broker = make_broker(recorder)
        with pytest.raises(OrderNotCancelableError):
            broker.cancel_order("a")
        broker.close()


class TestFailureMapping:
    def test_rejects_carry_the_brokers_message(self) -> None:
        recorder = Recorder(
            lambda r: httpx.Response(403, json={"message": "insufficient buying power"})
        )
        broker = make_broker(recorder)
        with pytest.raises(OrderRejectedError, match="insufficient buying power") as excinfo:
            broker.submit_order(request_for())
        assert excinfo.value.reason == "insufficient buying power"
        broker.close()

    def test_unprocessable_order_is_a_reject(self) -> None:
        recorder = Recorder(lambda r: httpx.Response(422, json={"message": "qty must be > 0"}))
        broker = make_broker(recorder)
        with pytest.raises(OrderRejectedError):
            broker.submit_order(request_for())
        broker.close()

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
    def test_server_errors_and_rate_limits_are_connection_errors(self, status: int) -> None:
        recorder = Recorder(lambda r: httpx.Response(status, text="upstream trouble"))
        broker = make_broker(recorder)
        with pytest.raises(BrokerConnectionError, match=str(status)):
            broker.submit_order(request_for())
        broker.close()

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectTimeout("t"),
            httpx.ReadTimeout("t"),
            httpx.ConnectError("refused"),
            httpx.RemoteProtocolError("reset"),
        ],
    )
    def test_network_failures_are_connection_errors(self, exc: Exception) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise exc

        broker = make_broker(Recorder(boom))
        with pytest.raises(BrokerConnectionError):
            broker.get_order("x")
        broker.close()

    def test_bad_credentials_are_an_auth_error_not_a_retryable_one(self) -> None:
        broker = make_broker(Recorder(lambda r: httpx.Response(401, json={"message": "no"})))
        with pytest.raises(BrokerAuthError):
            broker.submit_order(request_for())
        broker.close()

    @pytest.mark.parametrize("payload", ["not json at all", "[]", '{"id": "x"}', "null"])
    def test_malformed_order_payloads_are_treated_as_unknown_outcome(self, payload: str) -> None:
        broker = make_broker(Recorder(lambda r: httpx.Response(200, text=payload)))
        with pytest.raises(BrokerConnectionError, match="malformed"):
            broker.submit_order(request_for())
        broker.close()

    def test_malformed_list_and_positions_payloads(self) -> None:
        broker = make_broker(Recorder(lambda r: httpx.Response(200, json=[{"nope": 1}])))
        with pytest.raises(BrokerConnectionError):
            broker.list_orders()
        with pytest.raises(BrokerConnectionError):
            broker.list_positions()
        broker.close()


class TestIdempotentSubmit:
    def test_duplicate_client_order_id_returns_the_existing_order(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    422, json={"message": "client_order_id must be unique", "code": 40010001}
                )
            return httpx.Response(200, json=order_json(id="ord-existing", status="filled"))

        recorder = Recorder(handler)
        broker = make_broker(recorder)
        order = broker.submit_order(request_for())
        assert order.broker_order_id == "ord-existing"
        assert [r.method for r in recorder.requests] == ["POST", "GET"]
        assert recorder.last.url.path == "/v2/orders:by_client_order_id"
        broker.close()

    def test_duplicate_id_error_without_a_findable_order_is_still_a_reject(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(422, json={"message": "client_order_id must be unique"})
            return httpx.Response(404, json={"message": "not found"})

        broker = make_broker(Recorder(handler))
        with pytest.raises(OrderRejectedError):
            broker.submit_order(request_for())
        broker.close()


class TestThroughTheBridge:
    """The full pipeline over the Alpaca adapter, still with mocked HTTP."""

    def build(self, tmp_path: Path, recorder: Recorder) -> tuple[Any, Any, ListAlertSink]:
        settings = make_settings(tmp_path)
        sink = ListAlertSink()
        broker = make_broker(recorder)
        app = create_app(
            settings,
            broker=broker,
            risk=default_risk(),
            clock=FakeClock(),
            alert_sink=sink,
            sleep=lambda _s: None,
            start_background=False,
        )
        return TestClient(app), app.state.runtime, sink

    def test_an_alert_becomes_one_paper_order_and_a_retry_reuses_its_client_id(
        self, tmp_path: Path
    ) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise httpx.ReadTimeout("slow")  # the first attempt times out
                body = json.loads(request.content)
                return httpx.Response(
                    200,
                    json=order_json(
                        client_order_id=body["client_order_id"], status="filled", filled_qty="10"
                    ),
                )
            return httpx.Response(404, json={"message": "nope"})

        recorder = Recorder(handler)
        client, rt, _sink = self.build(tmp_path, recorder)

        response = client.post("/webhook/tradingview", json=make_alert(secret=WEBHOOK_SECRET))
        assert response.json()["status"] == "accepted"

        posts = [r for r in recorder.requests if r.method == "POST"]
        assert len(posts) == 2
        ids = {json.loads(r.content)["client_order_id"] for r in posts}
        assert len(ids) == 1  # same client_order_id on the retry => broker-side idempotency
        assert rt.store.get_position("AAPL") == D(10)
        assert {r.url.host for r in recorder.requests} == {"paper-api.alpaca.markets"}
        client.close()
        rt.close()

    def test_reconciliation_reads_orders_and_positions_from_the_mock(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/orders" and request.method == "GET":
                return httpx.Response(200, json=[])
            if request.url.path == "/v2/positions":
                return httpx.Response(200, json=[{"symbol": "AAPL", "qty": "25", "side": "long"}])
            return httpx.Response(404, json={"message": "nope"})

        recorder = Recorder(handler)
        client, rt, sink = self.build(tmp_path, recorder)
        report = rt.reconciler.run_once()
        assert [m.kind for m in report.mismatches] == ["position_mismatch"]
        assert sink.kinds == ["position_mismatch"]
        assert rt.store.get_position("AAPL") == D(25)
        client.close()
        rt.close()

    def test_a_live_url_in_the_environment_stops_the_process_from_starting(self) -> None:
        env = {
            "WEBHOOK_SECRET": WEBHOOK_SECRET,
            "ADMIN_SECRET": ADMIN_SECRET,
            "BROKER": "alpaca_paper",
            "ALPACA_API_KEY_ID": KEY_ID,
            "ALPACA_API_SECRET_KEY": SECRET_KEY,
            "ALPACA_BASE_URL": "https://api.alpaca.markets",
        }
        assert main(["serve"], env=env) == EXIT_CONFIG


class TestUnexpectedStatuses:
    @pytest.mark.parametrize(
        "call",
        [
            lambda b: b.ping(),
            lambda b: b.get_order("x"),
            lambda b: b.get_order_by_client_id("x"),
            lambda b: b.list_orders(),
            lambda b: b.list_positions(),
            lambda b: b.cancel_order("x"),
            lambda b: b.cancel_all_orders(),
        ],
    )
    def test_any_unexpected_4xx_is_an_error_not_silently_ignored(self, call: Any) -> None:
        broker = make_broker(Recorder(lambda r: httpx.Response(400, json={"message": "bad"})))
        with pytest.raises(BrokerError):
            call(broker)
        broker.close()

    def test_error_bodies_that_are_not_json_still_produce_a_useful_message(self) -> None:
        broker = make_broker(Recorder(lambda r: httpx.Response(400, text="<html>nope</html>")))
        with pytest.raises(OrderRejectedError, match="HTTP 400"):
            broker.submit_order(request_for())
        broker.close()

    def test_orders_without_a_quantity_parse_as_zero(self) -> None:
        # e.g. notional orders placed outside this bridge
        recorder = Recorder(lambda r: httpx.Response(200, json=order_json(qty=None, filled_qty="")))
        broker = make_broker(recorder)
        order = broker.submit_order(request_for())
        assert order.qty == D(0)
        assert order.filled_qty == D(0)
        broker.close()
