"""Typed configuration and security validation for the P0 runtime."""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
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


class CredentialVaultBackend(StrEnum):
    MEMORY = "memory"
    HASHICORP_VAULT = "hashicorp_vault"


class ExecutionMode(StrEnum):
    EMBEDDED = "embedded"
    EXTERNAL_WORKER = "external_worker"


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
    database_password_file: str = ""
    redis_url: str = "redis://localhost:6379/0"
    redis_password_file: str = ""
    runs_enabled: bool = True
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)
    oauth_transaction_ttl_seconds: int = Field(default=600, ge=60, le=1_800)
    session_cookie_name: str = "ai_agent_session"
    post_login_redirect_uri: str = "http://localhost:8000/docs"
    external_connection_redirect_uri: str = (
        "http://localhost:8000/api/v1/connections/{server_code}/callback"
    )


class ModelSettings(BaseModel):
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    api_key_file: str = ""
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
    expected_tools: tuple[str, ...] = Field(
        default=("list_datasets", "describe_dataset", "query_dataset"),
        min_length=1,
        max_length=50,
    )

    @field_validator("expected_tools")
    @classmethod
    def validate_expected_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 200 for item in value):
            raise ValueError("PermissionSystem expected Tool names must be non-empty.")
        if len(set(value)) != len(value):
            raise ValueError("PermissionSystem expected Tool names must be unique.")
        return value

    @property
    def has_probe_token(self) -> bool:
        return bool(self.access_token.get_secret_value())

    @property
    def has_service_credentials(self) -> bool:
        return bool(self.service_client_id and self.service_client_secret.get_secret_value())


class McpGatewaySettings(BaseModel):
    """Deterministic defaults for the P2 MCP Gateway."""

    enabled: bool = False
    catalog_ttl_seconds: int = Field(default=300, ge=1, le=86_400)
    default_timeout_seconds: float = Field(default=10.0, ge=0.1, le=300.0)
    max_response_bytes: int = Field(default=1_000_000, ge=1_024, le=50_000_000)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100_000)
    max_concurrency: int = Field(default=20, ge=1, le=1_000)
    circuit_breaker_threshold: int = Field(default=3, ge=1, le=100)
    circuit_breaker_recovery_seconds: float = Field(default=30.0, ge=1.0, le=3_600.0)
    allow_local_addresses: bool = True
    allow_private_addresses: bool = False
    allowed_hosts: tuple[str, ...] = ()
    allowed_ips: tuple[str, ...] = ()

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_allowed_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            host = item.strip().lower().rstrip(".")
            if not host or any(character.isspace() for character in host):
                raise ValueError(
                    "MCP allowed hosts must be non-empty host names without whitespace."
                )
            if host not in normalized:
                normalized.append(host)
        return tuple(normalized)

    @field_validator("allowed_ips")
    @classmethod
    def normalize_allowed_ips(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            try:
                network = ipaddress.ip_network(item.strip(), strict=False)
            except ValueError as exc:
                raise ValueError(
                    "MCP allowed IPs must be valid IP addresses or CIDR networks."
                ) from exc
            canonical = str(network)
            if canonical not in normalized:
                normalized.append(canonical)
        return tuple(normalized)


class CredentialSettings(BaseModel):
    backend: CredentialVaultBackend = CredentialVaultBackend.MEMORY
    address: str = ""
    mount_path: str = "secret"
    path_prefix: str = "ai-agent/credentials"
    token_file: str = ""
    namespace: str = ""
    request_timeout_seconds: float = Field(default=5.0, ge=0.5, le=60.0)


class QuotaSettings(BaseModel):
    enabled: bool = False
    max_concurrent_runs_per_user: int = Field(default=2, ge=1, le=100)
    max_concurrent_runs_per_organization: int = Field(default=20, ge=1, le=10_000)
    max_runs_per_user_per_minute: int = Field(default=10, ge=1, le=10_000)
    max_daily_tokens_per_user: int = Field(default=1_000_000, ge=1, le=2_000_000_000)
    max_daily_tokens_per_organization: int = Field(default=20_000_000, ge=1, le=2_000_000_000)
    max_daily_cost_usd_per_user: float = Field(default=10.0, gt=0.0, le=1_000_000.0)
    max_daily_cost_usd_per_organization: float = Field(default=100.0, gt=0.0, le=10_000_000.0)


class GovernanceSettings(BaseModel):
    enabled: bool = False
    max_request_body_bytes: int = Field(default=1_048_576, ge=1_024, le=50_000_000)
    shutdown_grace_seconds: float = Field(default=30.0, ge=0.0, le=300.0)
    stale_run_after_seconds: int = Field(default=300, ge=90, le=86_400)
    audit_retention_days: int = Field(default=365, ge=30, le=3_650)
    audit_integrity_key_file: str = ""
    audit_integrity_key_id: str = "v1"
    max_concurrent_runs: int = Field(default=20, ge=1, le=10_000)
    quota: QuotaSettings = Field(default_factory=QuotaSettings)


class ExecutionSettings(BaseModel):
    mode: ExecutionMode = ExecutionMode.EMBEDDED
    lease_seconds: int = Field(default=45, ge=15, le=600)
    heartbeat_seconds: int = Field(default=10, ge=1, le=120)
    worker_stale_seconds: int = Field(default=30, ge=5, le=600)
    poll_interval_seconds: float = Field(default=1.0, ge=0.1, le=30.0)
    retry_delay_seconds: float = Field(default=2.0, ge=0.1, le=300.0)
    retry_max_delay_seconds: float = Field(default=60.0, ge=0.1, le=3_600.0)
    retry_jitter_ratio: float = Field(default=0.2, ge=0.0, le=0.5)
    max_attempts: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def validate_lease_timing(self) -> ExecutionSettings:
        if self.heartbeat_seconds * 2 >= self.lease_seconds:
            raise ValueError("Execution lease must exceed twice the heartbeat interval.")
        if self.heartbeat_seconds * 2 >= self.worker_stale_seconds:
            raise ValueError("Worker stale interval must exceed twice the heartbeat interval.")
        if self.retry_max_delay_seconds < self.retry_delay_seconds:
            raise ValueError("Execution retry maximum delay must not be less than its base delay.")
        return self


class ObservabilitySettings(BaseModel):
    tracing_enabled: bool = False
    service_name: str = "enterprise-ai-agent"
    otlp_endpoint: str = ""
    trace_sample_ratio: float = Field(default=0.1, ge=0.0, le=1.0)


class SecuritySettings(BaseModel):
    """HTTP boundary settings controlled by the deployment environment."""

    allowed_hosts: tuple[str, ...] = ()
    trusted_proxy_ips: tuple[str, ...] = ()

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_allowed_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            host = item.strip().lower().rstrip(".")
            if not host or any(character.isspace() for character in host):
                raise ValueError("Security allowed hosts must be non-empty host patterns.")
            if host != "*" and "*" in host and not host.startswith("*."):
                raise ValueError("Security allowed host wildcards must use the *.example.com form.")
            if host.count("*") > 1:
                raise ValueError("Security allowed host patterns may contain at most one wildcard.")
            if host not in normalized:
                normalized.append(host)
        return tuple(normalized)

    @field_validator("trusted_proxy_ips")
    @classmethod
    def normalize_trusted_proxy_ips(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            try:
                network = ipaddress.ip_network(item.strip(), strict=False)
            except ValueError as exc:
                raise ValueError(
                    "Trusted proxy IPs must be valid IP addresses or CIDR networks."
                ) from exc
            canonical = str(network)
            if canonical not in normalized:
                normalized.append(canonical)
        return tuple(normalized)


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
    mcp_gateway: McpGatewaySettings = Field(default_factory=McpGatewaySettings)
    credentials: CredentialSettings = Field(default_factory=CredentialSettings)
    governance: GovernanceSettings = Field(default_factory=GovernanceSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

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
            if self.permission_system.enabled and not self.mcp_gateway.enabled:
                raise ConfigurationError(
                    "PermissionSystem integration requires the MCP Gateway to be enabled."
                )
        if self.mcp_gateway.enabled:
            self._validate_mcp_gateway()
        if self.platform.enabled:
            self._validate_platform()
        if self.credentials.backend == CredentialVaultBackend.HASHICORP_VAULT:
            self._validate_vault()
        if self.observability.tracing_enabled:
            self._validate_observability()
        if self.environment == Environment.PRODUCTION:
            self._validate_production_security()
            if self.platform.enabled:
                self._validate_production_governance()

    def _validate_production_security(self) -> None:
        if not self.security.allowed_hosts:
            raise ConfigurationError(
                "Production requires an explicit HTTP allowed host list."
            )
        if "*" in self.security.allowed_hosts:
            raise ConfigurationError("Production HTTP allowed hosts must not contain '*'.")
        if not self.security.trusted_proxy_ips:
            raise ConfigurationError(
                "Production requires an explicit trusted proxy IP or CIDR list."
            )

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
        self._load_secret_files()
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
            self.platform.external_connection_redirect_uri.replace("{server_code}", "server"),
            "External connection redirect URI",
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

    def _validate_mcp_gateway(self) -> None:
        if not self.platform.enabled:
            raise ConfigurationError("P2 MCP Gateway requires the platform to be enabled.")

    def _validate_vault(self) -> None:
        credentials = self.credentials
        _validate_http_url(
            credentials.address,
            "HashiCorp Vault address",
            require_https=self.environment == Environment.PRODUCTION,
        )
        if not credentials.mount_path.strip("/") or not credentials.path_prefix.strip("/"):
            raise ConfigurationError("HashiCorp Vault mount path and path prefix are required.")
        _read_secret_file(credentials.token_file, "HashiCorp Vault token")

    def _validate_observability(self) -> None:
        if not self.observability.otlp_endpoint:
            raise ConfigurationError("OTLP endpoint is required when tracing is enabled.")
        _validate_http_url(
            self.observability.otlp_endpoint,
            "OTLP endpoint",
            require_https=False,
        )

    def _validate_production_governance(self) -> None:
        if not self.governance.enabled:
            raise ConfigurationError("Production platform requires governance to be enabled.")
        if not self.governance.quota.enabled:
            raise ConfigurationError(
                "Production platform requires distributed quotas to be enabled."
            )
        if self.credentials.backend != CredentialVaultBackend.HASHICORP_VAULT:
            raise ConfigurationError("Production platform requires the HashiCorp Vault backend.")
        if self.execution.mode != ExecutionMode.EXTERNAL_WORKER:
            raise ConfigurationError("Production platform requires external Worker execution.")
        if not self.platform.database_password_file:
            raise ConfigurationError("Production database password must be loaded from a file.")
        if not self.platform.redis_password_file:
            raise ConfigurationError("Production Redis password must be loaded from a file.")
        if not self.model.api_key_file:
            raise ConfigurationError("Production model API key must be loaded from a file.")
        if not self.governance.audit_integrity_key_file:
            raise ConfigurationError("Production audit integrity key must be loaded from a file.")
        database = urlsplit(self.platform.database_url)
        if database.password:
            raise ConfigurationError("Production database URL must not contain a password.")
        _read_secret_file(self.platform.database_password_file, "database password")
        _read_secret_file(self.platform.redis_password_file, "Redis password")
        _read_secret_file(self.governance.audit_integrity_key_file, "audit integrity key")

    def _load_secret_files(self) -> None:
        if self.model.api_key_file:
            self.model.api_key = SecretStr(
                _read_secret_file(self.model.api_key_file, "model API key")
            )

    def database_password(self) -> str | None:
        if not self.platform.database_password_file:
            return None
        return _read_secret_file(self.platform.database_password_file, "database password")

    def redis_password(self) -> str | None:
        if not self.platform.redis_password_file:
            return None
        return _read_secret_file(self.platform.redis_password_file, "Redis password")

    def audit_integrity_key(self) -> bytes | None:
        if not self.governance.audit_integrity_key_file:
            return None
        return _read_secret_file(
            self.governance.audit_integrity_key_file,
            "audit integrity key",
        ).encode("utf-8")


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


def _read_secret_file(value: str, label: str) -> str:
    if not value:
        raise ConfigurationError(f"{label} file is required.")
    path = Path(value)
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(f"{label} file cannot be read.") from exc
    if not secret:
        raise ConfigurationError(f"{label} file must not be empty.")
    return secret
