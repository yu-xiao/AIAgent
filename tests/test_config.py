from __future__ import annotations

import pytest
from pydantic import SecretStr

from ai_agent.config import (
    Environment,
    ModelSettings,
    OidcSettings,
    PermissionSystemSettings,
    PlatformSettings,
    SecuritySettings,
    Settings,
)
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


def test_enabled_platform_requires_oidc_and_model_configuration() -> None:
    settings = Settings(
        _env_file=None,
        platform=PlatformSettings(enabled=True),
        model=ModelSettings(enabled=False),
    )
    with pytest.raises(ConfigurationError, match="OIDC must be enabled"):
        settings.validate_runtime()

    settings.oidc = OidcSettings(enabled=True)
    with pytest.raises(ConfigurationError, match="model provider must be enabled"):
        settings.validate_runtime()


def test_production_requires_explicit_http_boundary() -> None:
    settings = Settings(_env_file=None, environment=Environment.PRODUCTION)

    with pytest.raises(ConfigurationError, match="allowed host"):
        settings.validate_runtime()

    settings.security = SecuritySettings(allowed_hosts=("agent.example.test",))
    with pytest.raises(ConfigurationError, match="trusted proxy"):
        settings.validate_runtime()


def test_security_settings_normalize_and_validate_values() -> None:
    settings = SecuritySettings(
        allowed_hosts=(" Agent.Example.Test. ", "agent.example.test", "*.example.test"),
        trusted_proxy_ips=("172.20.0.12", "172.20.0.0/16"),
    )

    assert settings.allowed_hosts == ("agent.example.test", "*.example.test")
    assert settings.trusted_proxy_ips == ("172.20.0.12/32", "172.20.0.0/16")

    with pytest.raises(ValueError, match="wildcard"):
        SecuritySettings(allowed_hosts=("agent.*.example.test",))
    with pytest.raises(ValueError, match="Trusted proxy IPs"):
        SecuritySettings(trusted_proxy_ips=("not-an-ip",))
