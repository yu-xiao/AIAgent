# Enterprise AI Agent

这是企业 AI 智能体平台的 Python 3.12 项目。当前实现包含 P0-P3 及跳过 P4 后的 P5 生产治理：独立 OIDC 会话、组织 RBAC、PostgreSQL/Redis 持久化、LangGraph 单智能体、模型流式响应、Run/SSE/取消、硬限制、追加式审计、可信 MCP Server 注册、PermissionSystem 个人/组织连接、隔离 Tool Catalog、HashiCorp Vault、分布式配额、跨实例 MCP 限流/熔断、指标、Trace、审计留存和蓝绿发布制品。金蝶、MES、BI 暂未接入。

P1 的模型供应商、模型名、Base URL 和 API Key 只从本地环境变量读取，不写入仓库。P2 的 Token、Refresh Token 和 Client Secret 只进入 CredentialVault，不保存到数据库或普通日志。

## 快速开始

```powershell
.\scripts\bootstrap.ps1
Copy-Item .env.example .env
uv run --no-sync ai-agent check-config
uv run --no-sync ai-agent serve
```

启用 P1 前，先启动 PostgreSQL/Redis（本机需要 Docker），执行迁移并在 `.env` 中填写 OIDC 和模型配置：

```powershell
$env:AI_AGENT_POSTGRES_PASSWORD = "change-me"
docker compose up -d
uv run --no-sync alembic upgrade head
uv run --no-sync ai-agent check-config
```

然后将 `AI_AGENT_PLATFORM__ENABLED` 和 `AI_AGENT_MODEL__ENABLED` 设为 `true`，补充 `AI_AGENT_MODEL__BASE_URL`、`AI_AGENT_MODEL__MODEL`、`AI_AGENT_MODEL__API_KEY` 及正的输入/输出单价。生产环境必须使用 HTTPS OIDC、模型和数据库/Redis 安全连接。

`bootstrap.ps1` 优先执行标准 `uv sync`。若当前 Windows 环境的 uv 出现 PEP 517 临时结果文件异常，脚本会保留 uv 的锁定依赖，并使用同一虚拟环境完成 editable 安装。

服务启动后可访问：

- `GET http://127.0.0.1:8000/health/live`
- `GET http://127.0.0.1:8000/health/ready`
- `GET http://127.0.0.1:8000/api/v1/p0/status`
- `GET http://127.0.0.1:8000/api/v1/p1/status`
- `GET http://127.0.0.1:8000/api/v1/p2/status`
- `GET http://127.0.0.1:8000/api/v1/p3/status`
- `GET http://127.0.0.1:8000/api/v1/p5/status`
- `GET http://127.0.0.1:8000/metrics`
- `GET http://127.0.0.1:8000/docs`

## 协议探针

配置 `.env` 后运行：

```powershell
uv run --no-sync ai-agent probe-oidc
uv run --no-sync ai-agent probe-permission
uv run --no-sync ai-agent probe-permission --call-list-datasets
```

若 PermissionSystem 暂未提供 RFC 9728 Protected Resource Metadata，可在过渡期只验证 MCP：

```powershell
uv run --no-sync ai-agent probe-permission --skip-discovery
```

详细范围、配置和验收方式见 [P0 验证说明](docs/p0-validation.md)、[P1 实施说明](docs/p1-validation.md)、[P2 验证说明](docs/p2-validation.md)、[P3 验证说明](docs/p3-validation.md) 和 [P5 验证说明](docs/p5-validation.md)，生产操作见 [运维手册](docs/operations.md)，总体设计见 [实施方案](docs/ai-agent-implementation-plan.md)。启用 P2/P3 时设置 `AI_AGENT_MCP_GATEWAY__ENABLED=true`，并使用管理员身份注册经过审核的 PermissionSystem MCP Server；用户连接和 Tool Catalog 不接受任意 URL。
