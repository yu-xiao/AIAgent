"""Application-level errors with safe, non-secret messages."""


class AiAgentError(Exception):
    """Base error for expected application failures."""


class ConfigurationError(AiAgentError):
    """Raised when runtime configuration is incomplete or unsafe."""


class AuthenticationError(AiAgentError):
    """Raised when OAuth or token acquisition fails."""


class ProtocolValidationError(AiAgentError):
    """Raised when an external protocol response violates its contract."""


class McpConnectionError(AiAgentError):
    """Raised when an MCP server cannot be reached or negotiated safely."""


class GatewayTimeoutError(AiAgentError):
    """Raised when a Gateway call exceeds its deterministic timeout."""


class RateLimitExceededError(AiAgentError):
    """Raised when a Gateway rate limit is exceeded."""


class CircuitOpenError(AiAgentError):
    """Raised while an MCP Server circuit breaker is open."""


class AuthorizationError(AiAgentError):
    """Raised when a platform permission or organization boundary denies access."""


class ConflictError(AiAgentError):
    """Raised when a request conflicts with the current resource state."""


class ResourceNotFoundError(AiAgentError):
    """Raised when a tenant-scoped resource does not exist or is not visible."""


class RunLimitError(AiAgentError):
    """Raised when an Agent run would exceed a deterministic hard limit."""


class QuotaExceededError(AiAgentError):
    """Raised when a distributed user or organization quota denies a Run."""


class ModelProviderError(AiAgentError):
    """Raised when the configured model provider fails with a safe public message."""
