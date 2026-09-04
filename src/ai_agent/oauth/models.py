"""Protocol models used by OAuth and MCP authorization discovery."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class OidcProviderMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str | None = None
    revocation_endpoint: str | None = None
    code_challenge_methods_supported: list[str] = Field(default_factory=list)


class ProtectedResourceMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    resource: str
    authorization_servers: list[str]
    scopes_supported: list[str] = Field(default_factory=list)


class OAuthToken(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: SecretStr
    token_type: str = "Bearer"
    expires_in: int = Field(default=300, gt=0)
    refresh_token: SecretStr | None = None
    id_token: SecretStr | None = None
    scope: str | None = None
    subject: str | None = None

    def safe_summary(self) -> dict[str, object]:
        return {
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "has_refresh_token": self.refresh_token is not None,
            "has_id_token": self.id_token is not None,
            "scope": self.scope,
        }
