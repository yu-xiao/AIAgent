# P2 连接中心与 MCP Gateway 验证说明

## 范围

P2 提供通用的可信 MCP Server 注册、个人 OAuth 连接、组织共享连接、凭证引用、Tool Catalog 和策略 Gateway。业务数据仍然只能通过 MCP 获取，P2 不直接访问业务数据库或业务 REST API。

## 数据迁移

```powershell
uv run --no-sync alembic upgrade head
```

`0002` 新增 `mcp_server_definitions`、`external_connections`、`external_authorization_grants`、`credential_references` 和 `connection_tool_grants`。数据库只保存凭证引用，不保存 Token、Refresh Token 或 Client Secret 明文。

## 配置

```text
AI_AGENT_MCP_GATEWAY__ENABLED=true
AI_AGENT_MCP_GATEWAY__CATALOG_TTL_SECONDS=300
AI_AGENT_MCP_GATEWAY__DEFAULT_TIMEOUT_SECONDS=10
AI_AGENT_MCP_GATEWAY__MAX_RESPONSE_BYTES=1000000
AI_AGENT_MCP_GATEWAY__RATE_LIMIT_PER_MINUTE=60
AI_AGENT_MCP_GATEWAY__MAX_CONCURRENCY=20
AI_AGENT_MCP_GATEWAY__CIRCUIT_BREAKER_THRESHOLD=3
AI_AGENT_MCP_GATEWAY__CIRCUIT_BREAKER_RECOVERY_SECONDS=30
```

生产环境的 Agent、MCP 和 OAuth 地址必须使用 HTTPS。MCP Server 只能由组织管理员登记，注册校验会拒绝凭证嵌入 URL、localhost、私有 IP、回环地址和保留地址。

## API 验收

- `GET /api/v1/p2/status`：查看 P2 能力和开关，不返回密钥。
- `GET /api/v1/admin/mcp-servers`：查看当前组织的可信 Server。
- `POST /api/v1/admin/mcp-servers`：管理员注册 Server，写请求需要 CSRF。
- `PUT /api/v1/admin/mcp-servers/{id}`：更新策略并使组织内 Catalog 缓存失效。
- `GET /api/v1/connections`：查看当前用户个人连接和组织共享连接。
- `POST /api/v1/connections/{server_code}/authorize`：开始 Authorization Code + PKCE。
- `GET /api/v1/connections/{server_code}/callback`：一次性消费 OAuth state 并建立连接。
- `POST /api/v1/connections/{id}/refresh`：刷新个人连接凭证。
- `DELETE /api/v1/connections/{id}`：断开连接并撤销凭证。
- `POST /api/v1/organization-connections`：管理员创建组织共享服务凭证连接。
- `GET /api/v1/connections/tools`：按连接、Scope、Tool Grant 和 Tool Set 返回隔离后的 Catalog。

所有写请求都需要 `X-Organization-Id` 和 `X-CSRF-Token`。OAuth state、nonce、Issuer、Audience 和回调地址必须匹配；回调事务过期、重复使用或属于其他用户时明确拒绝。

## Gateway 行为

Gateway 每次调用都会：

- 选择当前用户的个人连接，缺失时才考虑组织共享连接。
- 校验平台权限、Server 白名单、Connection Tool Grant 和输入 JSON Schema。
- 注入 TraceId 和调用身份上下文，不把凭证交给模型。
- 执行 Tool 超时、用户级限流、并发控制和 Server 熔断。
- 校验输出 Schema 和响应大小；超大结果只返回摘要 Digest 并标记 `truncated`。
- 返回来源系统、Server、Tool、查询时间和 TraceId 的 Citation。
- 追加写入 Tool 调用状态、耗时和安全错误摘要。

## 验证命令

```powershell
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
$env:PYTHONPATH = "src"
uv run --no-sync mypy src/ai_agent
uv run --no-sync pytest
```

测试覆盖注册和组织隔离、Catalog 缓存隔离、OAuth 一次性 state/nonce、凭证 Vault、输入输出 Schema、限流、超时和熔断。真实 PermissionSystem MCP 联调仍需在等价 OIDC、Vault、Redis、PostgreSQL 和 TLS 环境完成。
