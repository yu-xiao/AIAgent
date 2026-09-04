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
