from __future__ import annotations

import pytest
from pydantic import SecretStr

from ai_agent.config import Environment, PermissionSystemSettings, Settings
from ai_agent.errors import ConfigurationError


def test_default_development_config_is_ready() -> None:
    settings = Settings(_env_file=None)

    settings.validate_runtime()


def test_production_rejects_insecure_enabled_integration() -> None:
    settings = Settings(
        _env_file=None,
        environment=Environment.PRODUCTION,
        permission_system=PermissionSystemSettings(enabled=True),
    )

    with pytest.raises(ConfigurationError, match="must use HTTPS"):
        settings.validate_runtime()


def test_live_probe_requires_a_token_or_service_credentials() -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(ConfigurationError, match="short-lived"):
        settings.validate_runtime(
            require_permission_system=True,
            require_permission_token=True,
        )


def test_embedded_url_credentials_are_rejected() -> None:
    permission = PermissionSystemSettings(
        mcp_url="https://user:secret@example.test/mcp",
        access_token=SecretStr("temporary"),
    )
    settings = Settings(_env_file=None, permission_system=permission)

    with pytest.raises(ConfigurationError, match="embedded credentials"):
        settings.validate_runtime(require_permission_system=True)
