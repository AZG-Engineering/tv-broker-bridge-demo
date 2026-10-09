"""Configuration fails closed; the CLI refuses to start on bad config."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from tests.conftest import ADMIN_SECRET, WEBHOOK_SECRET
from tv_broker_bridge import cli
from tv_broker_bridge.broker.sim import SimBroker
from tv_broker_bridge.config import Settings, create_broker
from tv_broker_bridge.errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent
BASE = {"WEBHOOK_SECRET": WEBHOOK_SECRET, "ADMIN_SECRET": ADMIN_SECRET}


class TestSettings:
    def test_defaults(self) -> None:
        s = Settings.from_env(BASE)
        assert s.broker == "sim"
        assert s.reconcile_interval_s == 30
        assert s.heartbeat_interval_s == 5
        assert s.submit_attempts == 3
        assert s.sim.latency_s == 0
        assert s.sim.partial_fill_probability == 0
        assert isinstance(create_broker(s), SimBroker)

    @pytest.mark.parametrize(
        ("env", "message"),
        [
            ({"ADMIN_SECRET": ADMIN_SECRET}, "WEBHOOK_SECRET is required"),
            ({"WEBHOOK_SECRET": WEBHOOK_SECRET}, "ADMIN_SECRET is required"),
            ({**BASE, "WEBHOOK_SECRET": "   "}, "WEBHOOK_SECRET is required"),
            ({**BASE, "WEBHOOK_SECRET": "short"}, "at least 16"),
            ({**BASE, "ADMIN_SECRET": "x" * 15}, "at least 16"),
            ({**BASE, "WEBHOOK_SECRET": "replace-with-a-long-random-string"}, "placeholder"),
            ({**BASE, "ADMIN_SECRET": "Replace-With-Something-Else-Entirely"}, "placeholder"),
            ({**BASE, "ADMIN_SECRET": WEBHOOK_SECRET}, "must differ"),
            ({**BASE, "BROKER": "robinhood"}, "BROKER must be"),
            ({**BASE, "BROKER": "live"}, "BROKER must be"),
            ({**BASE, "BROKER": "alpaca_paper"}, "ALPACA_API_KEY_ID"),
            ({**BASE, "RECONCILE_INTERVAL_S": "fast"}, "must be a number"),
            ({**BASE, "RECONCILE_INTERVAL_S": "0"}, ">= 0.1"),
            ({**BASE, "HEARTBEAT_INTERVAL_S": "nan"}, ">= 0.1"),
            ({**BASE, "SIM_PARTIAL_FILL_PROB": "1.5"}, "between 0 and 1"),
            ({**BASE, "SIM_REJECT_PROB": "-0.1"}, ">= 0"),
            ({**BASE, "SIM_SEED": "abc"}, "SIM_SEED"),
            ({**BASE, "SIM_DEFAULT_PRICE": "free"}, "SIM_DEFAULT_PRICE"),
            ({**BASE, "SIM_DEFAULT_PRICE": "0"}, "SIM_DEFAULT_PRICE"),
        ],
    )
    def test_bad_configuration_is_refused(self, env: dict[str, str], message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            Settings.from_env(env)

    def test_sim_options_are_read(self) -> None:
        s = Settings.from_env(
            {
                **BASE,
                "SIM_LATENCY_MS": "250",
                "SIM_PARTIAL_FILL_PROB": "0.3",
                "SIM_REJECT_PROB": "0.1",
                "SIM_SEED": "42",
                "SIM_DEFAULT_PRICE": "123.45",
                "SUBMIT_ATTEMPTS": "5",
            }
        )
        assert s.sim.latency_s == 0.25
        assert s.sim.partial_fill_probability == 0.3
        assert s.sim.reject_probability == 0.1
        assert s.sim.seed == 42
        assert str(s.sim.default_price) == "123.45"
        assert s.submit_attempts == 5

    def test_secrets_are_not_required_for_operator_commands(self) -> None:
        s = Settings.from_env({}, require_secrets=False)
        assert s.webhook_secret == ""

    def test_repr_hides_secrets(self) -> None:
        text = repr(Settings.from_env(BASE))
        assert WEBHOOK_SECRET not in text
        assert ADMIN_SECRET not in text


@pytest.fixture
def serve_env(tmp_path: Path) -> dict[str, str]:
    risk = tmp_path / "risk.yaml"
    shutil.copyfile(ROOT / "config" / "risk.example.yaml", risk)
    return {
        **BASE,
        "RISK_CONFIG_PATH": str(risk),
        "DB_PATH": str(tmp_path / "bridge.db"),
        "HEARTBEAT_PATH": str(tmp_path / "hb.json"),
    }


class TestServe:
    def test_serve_builds_the_app_and_binds_to_localhost_by_default(
        self, serve_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))

        assert cli.main(["serve", "--port", "9001"], env=serve_env) == 0

        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 9001
        assert captured["log_config"] is None  # JSON logs only; uvicorn does not add its own
        assert captured["access_log"] is False
        captured["app"].state.runtime.close()

    def test_host_must_be_chosen_explicitly_to_listen_more_widely(
        self, serve_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))
        cli.main(["serve", "--host", "0.0.0.0"], env=serve_env)  # noqa: S104
        assert captured["host"] == "0.0.0.0"  # noqa: S104
        captured["app"].state.runtime.close()

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda e: e.pop("WEBHOOK_SECRET"), "WEBHOOK_SECRET is required"),
            (lambda e: e.update(ADMIN_SECRET=e["WEBHOOK_SECRET"]), "must differ"),
            (lambda e: e.update(RISK_CONFIG_PATH="/nonexistent/risk.yaml"), "risk.example.yaml"),
            (lambda e: e.update(BROKER="alpaca_paper"), "ALPACA_API_KEY_ID"),
        ],
    )
    def test_serve_refuses_to_start_on_bad_config(
        self,
        serve_env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        mutate: Any,
        message: str,
    ) -> None:
        def must_not_run(*_a: Any, **_kw: Any) -> None:
            raise AssertionError("the server must not start")

        monkeypatch.setattr(uvicorn, "run", must_not_run)
        mutate(serve_env)
        assert cli.main(["serve"], env=serve_env) == cli.EXIT_CONFIG
        assert message in capsys.readouterr().err

    def test_serve_refuses_an_invalid_risk_file(
        self, serve_env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("max_qty_per_order: 0\n")
        serve_env["RISK_CONFIG_PATH"] = str(bad)
        assert cli.main(["serve"], env=serve_env) == cli.EXIT_CONFIG
        assert "invalid risk config" in capsys.readouterr().err


class TestInitScript:
    def run(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, our own script
            [sys.executable, str(ROOT / "scripts" / "init_env.py"), "--root", str(root)],
            capture_output=True,
            text=True,
            check=False,
        )

    def prepare(self, tmp_path: Path) -> Path:
        (tmp_path / "config").mkdir()
        shutil.copyfile(ROOT / ".env.example", tmp_path / ".env.example")
        shutil.copyfile(
            ROOT / "config" / "risk.example.yaml", tmp_path / "config" / "risk.example.yaml"
        )
        return tmp_path

    def test_creates_working_config_with_random_secrets(self, tmp_path: Path) -> None:
        root = self.prepare(tmp_path)
        result = self.run(root)
        assert result.returncode == 0, result.stderr
        env_text = (root / ".env").read_text()
        values = dict(
            line.split("=", 1) for line in env_text.splitlines() if "=" in line and line[0] != "#"
        )
        # The generated file passes the same validation the server applies...
        settings = Settings.from_env(values)
        assert settings.webhook_secret != settings.admin_secret
        assert "replace-with" not in env_text
        assert (root / ".env").stat().st_mode & 0o077 == 0  # owner-only permissions
        assert (root / "config" / "risk.yaml").exists()

    def test_never_overwrites_existing_files(self, tmp_path: Path) -> None:
        root = self.prepare(tmp_path)
        (root / ".env").write_text("WEBHOOK_SECRET=mine\n")
        (root / "config" / "risk.yaml").write_text("mine: true\n")
        result = self.run(root)
        assert result.returncode == 0
        assert (root / ".env").read_text() == "WEBHOOK_SECRET=mine\n"
        assert (root / "config" / "risk.yaml").read_text() == "mine: true\n"
        assert "kept existing" in result.stdout

    def test_each_run_generates_different_secrets(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        self.run(self.prepare(a))
        self.run(self.prepare(b))
        assert (a / ".env").read_text() != (b / ".env").read_text()
