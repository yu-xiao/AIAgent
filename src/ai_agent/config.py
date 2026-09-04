"""Typed configuration and security validation for the P0 runtime."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_agent.errors import ConfigurationError


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class TokenEndpointAuthMethod(StrEnum):
    NONE = "none"
    CLIENT_SECRET_BASIC = "client_secret_basic"
    CLIENT_SECRET_POST = "client_secret_post"


class OidcSettings(BaseModel):
    enabled: bool = False
    issuer: str = "http://localhost:8081/realms/ai-agent"
    client_id: str = "ai-agent-web"
    client_secret: SecretStr = Field(default_factory=lambda: SecretStr(""))
    token_endpoint_auth_method: TokenEndpointAuthMethod = TokenEndpointAuthMethod.NONE
    redirect_uri: str = "http://localhost:8000/api/v1/auth/callback"
    scope: str = "openid profile email"
    signing_algorithms: tuple[str, ...] = ("RS256",)


class PlatformSettings(BaseModel):
    enabled: bool = False
    database_url: str = "postgresql+asyncpg://ai_agent@localhost:5432/ai_agent"
    redis_url: str = "redis://localhost:6379/0"
    runs_enabled: bool = True
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)
    oauth_transaction_ttl_seconds: int = Field(default=600, ge=60, le=1_800)
    session_cookie_name: str = "ai_agent_session"
    post_login_redirect_uri: str = "http://localhost:8000/docs"


class ModelSettings(BaseModel):
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    request_timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    input_price_per_million_tokens: float = Field(default=0.0, ge=0.0)
    output_price_per_million_tokens: float = Field(default=0.0, ge=0.0)
    system_prompt: str = "You are a concise enterprise assistant."


class RunLimitSettings(BaseModel):
    max_question_characters: int = Field(default=4_000, ge=1, le=100_000)
    max_model_rounds: int = Field(default=6, ge=1, le=50)
    max_tool_calls: int = Field(default=10, ge=0, le=100)
    max_run_seconds: float = Field(default=90.0, ge=1.0, le=900.0)
    max_input_tokens: int = Field(default=16_000, ge=1, le=2_000_000)
    max_output_tokens: int = Field(default=2_048, ge=1, le=200_000)
    max_cost_usd: float = Field(default=1.0, gt=0.0, le=10_000.0)


class PermissionSystemSettings(BaseModel):
    enabled: bool = False
    mcp_url: str = "http://localhost:5071/mcp"
    token_url: str = "http://localhost:5264/connect/token"
    scope: str = "permission-system-mcp"
    authorization_client_id: str = ""
    authorization_client_secret: SecretStr = Field(default_factory=lambda: SecretStr(""))
    token_endpoint_auth_method: TokenEndpointAuthMethod = (
        TokenEndpointAuthMethod.CLIENT_SECRET_BASIC
    )
    redirect_uri: str = "http://localhost:8000/api/v1/connections/permission-system/callback"
    access_token: SecretStr = Field(default_factory=lambda: SecretStr(""))
    service_client_id: str = ""
    service_client_secret: SecretStr = Field(default_factory=lambda: SecretStr(""))
    request_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    expected_tools: tuple[str, ...] = (
        "list_datasets",
        "describe_dataset",
        "query_dataset",
    )

    @property
    def has_probe_token(self) -> bool:
        return bool(self.access_token.get_secret_value())

    @property
    def has_service_credentials(self) -> bool:
        return bool(self.service_client_id and self.service_client_secret.get_secret_value())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="AI_AGENT_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "Enterprise AI Agent"
    environment: Environment = Environment.DEVELOPMENT
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"
    platform: PlatformSettings = Field(default_factory=PlatformSettings)
    model: ModelSettings = Field(default_factory=ModelSettings)
    limits: RunLimitSettings = Field(default_factory=RunLimitSettings)
    oidc: OidcSettings = Field(default_factory=OidcSettings)
    permission_system: PermissionSystemSettings = Field(default_factory=PermissionSystemSettings)

    def validate_runtime(
        self,
        *,
        require_oidc: bool = False,
        require_permission_system: bool = False,
        require_permission_token: bool = False,
    ) -> None:
        """Validate completeness and reject insecure production URLs."""

        oidc_required = require_oidc or self.oidc.enabled
        permission_required = require_permission_system or self.permission_system.enabled

        if oidc_required:
            self._validate_oidc()
        if permission_required:
            self._validate_permission_system(require_permission_token=require_permission_token)
        if self.platform.enabled:
            self._validate_platform()

    def _validate_oidc(self) -> None:
        if not self.oidc.client_id.strip():
            raise ConfigurationError("OIDC client ID is required.")
        if "openid" not in self.oidc.scope.split():
            raise ConfigurationError("OIDC scope must include 'openid'.")
        if not self.oidc.signing_algorithms:
            raise ConfigurationError("OIDC signing algorithm allowlist must not be empty.")
        _validate_http_url(
            self.oidc.issuer,
            "OIDC issuer",
            require_https=self.environment == Environment.PRODUCTION,
        )
        _validate_http_url(
            self.oidc.redirect_uri,
            "OIDC redirect URI",
            require_https=self.environment == Environment.PRODUCTION,
        )
        _validate_client_auth(
            self.oidc.token_endpoint_auth_method,
            self.oidc.client_secret,
            "OIDC",
        )

    def _validate_permission_system(self, *, require_permission_token: bool) -> None:
        permission = self.permission_system
        _validate_http_url(
            permission.mcp_url,
            "PermissionSystem MCP URL",
            require_https=self.environment == Environment.PRODUCTION,
        )
        _validate_http_url(
            permission.token_url,
            "PermissionSystem token URL",
            require_https=self.environment == Environment.PRODUCTION,
        )
        _validate_http_url(
            permission.redirect_uri,
            "PermissionSystem redirect URI",
            require_https=self.environment == Environment.PRODUCTION,
        )
        if not permission.scope.strip():
            raise ConfigurationError("PermissionSystem MCP scope is required.")
        if permission.authorization_client_id:
            _validate_client_auth(
                permission.token_endpoint_auth_method,
                permission.authorization_client_secret,
                "PermissionSystem authorization client",
            )
        if require_permission_token and not (
            permission.has_probe_token or permission.has_service_credentials
        ):
            raise ConfigurationError(
                "Configure a short-lived PermissionSystem access token or service credentials "
                "for the live MCP probe."
            )

    def _validate_platform(self) -> None:
        if not self.oidc.enabled:
            raise ConfigurationError("OIDC must be enabled when the P1 platform is enabled.")
        if not self.model.enabled:
            raise ConfigurationError("A model provider must be enabled for Agent runs.")
        if not self.platform.database_url.startswith("postgresql+asyncpg://"):
            raise ConfigurationError("P1 database URL must use PostgreSQL with asyncpg.")
        if self.environment == Environment.PRODUCTION and "ssl=" not in self.platform.database_url:
            raise ConfigurationError("P1 database URL must configure TLS in production.")
        _validate_redis_url(self.platform.redis_url)
        if self.environment == Environment.PRODUCTION and not self.platform.redis_url.startswith(
            "rediss://"
        ):
            raise ConfigurationError("P1 Redis URL must use rediss:// in production.")
        _validate_http_url(
            self.platform.post_login_redirect_uri,
            "Post-login redirect URI",
            require_https=self.environment == Environment.PRODUCTION,
        )
        _validate_http_url(
            self.model.base_url,
            "Model base URL",
            require_https=self.environment == Environment.PRODUCTION,
        )
        if not self.model.model.strip():
            raise ConfigurationError("Model name is required when the model provider is enabled.")
        if not self.model.api_key.get_secret_value():
            raise ConfigurationError(
                "Model API key is required when the model provider is enabled."
            )
        if (
            self.model.input_price_per_million_tokens <= 0
            or self.model.output_price_per_million_tokens <= 0
        ):
            raise ConfigurationError(
                "Positive model input and output prices are required to enforce the run cost cap."
            )


def _validate_client_auth(
    method: TokenEndpointAuthMethod,
    client_secret: SecretStr,
    label: str,
) -> None:
    if method == TokenEndpointAuthMethod.NONE:
        return
    if not client_secret.get_secret_value():
        raise ConfigurationError(f"{label} client secret is required for {method.value}.")


def _validate_http_url(value: str, label: str, *, require_https: bool) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(f"{label} must be an absolute HTTP or HTTPS URL.")
    if parsed.username or parsed.password:
        raise ConfigurationError(f"{label} must not include embedded credentials.")
    if parsed.fragment:
        raise ConfigurationError(f"{label} must not include a fragment.")
    if require_https and parsed.scheme != "https":
        raise ConfigurationError(f"{label} must use HTTPS in production.")


def _validate_redis_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
        raise ConfigurationError("Redis URL must be an absolute redis:// or rediss:// URL.")
    if parsed.username or parsed.password:
        raise ConfigurationError("Redis URL must not include embedded credentials.")
    if parsed.fragment:
        raise ConfigurationError("Redis URL must not include a fragment.")
