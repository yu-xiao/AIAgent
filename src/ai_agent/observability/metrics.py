"""Low-cardinality Prometheus metrics shared across application modules."""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "ai_agent_http_requests_total",
    "HTTP requests by method, normalized route and status",
    ["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "ai_agent_http_request_duration_seconds",
    "HTTP request duration by method and normalized route",
    ["method", "route"],
)
HTTP_IN_PROGRESS = Gauge(
    "ai_agent_http_requests_in_progress",
    "HTTP requests currently in progress",
)
RUNS_TOTAL = Counter("ai_agent_runs_total", "Agent Runs by final status", ["status"])
RUN_DURATION = Histogram("ai_agent_run_duration_seconds", "Agent Run duration")
MODEL_TOKENS = Counter(
    "ai_agent_model_tokens_total",
    "Reported model tokens",
    ["provider", "model", "direction"],
)
MODEL_COST = Counter(
    "ai_agent_model_cost_usd_total",
    "Calculated model cost in USD",
    ["provider", "model"],
)
MCP_CALLS = Counter(
    "ai_agent_mcp_tool_calls_total",
    "MCP Tool calls by server and status",
    ["server", "status"],
)
MCP_DURATION = Histogram(
    "ai_agent_mcp_tool_duration_seconds",
    "MCP Tool duration by server",
    ["server"],
)
QUOTA_REJECTIONS = Counter(
    "ai_agent_quota_rejections_total",
    "Run admission rejections by policy reason",
    ["reason"],
)
VAULT_OPERATIONS = Counter(
    "ai_agent_vault_operations_total",
    "Credential Vault operations",
    ["operation", "status"],
)
AUDIT_WRITE_FAILURES = Counter(
    "ai_agent_audit_write_failures_total",
    "Audit records that could not be persisted",
)
RECOVERED_RUNS = Counter(
    "ai_agent_recovered_runs_total",
    "Incomplete Runs handled during startup recovery",
    ["outcome"],
)
