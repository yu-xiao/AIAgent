from __future__ import annotations

from ai_agent import cli
from ai_agent.config import SecuritySettings, Settings


def test_serve_validates_and_restricts_proxy_headers(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        security=SecuritySettings(trusted_proxy_ips=("127.0.0.1",)),
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: captured.update(kwargs))

    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "9000"]) == 0
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == ["127.0.0.1/32"]


def test_serve_rejects_invalid_production_boundary_before_start(monkeypatch) -> None:
    settings = Settings(_env_file=None, environment="production")
    started = False

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: started)

    assert cli.main(["serve"]) == 1
    assert started is False
