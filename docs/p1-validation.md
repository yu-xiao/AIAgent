# P1 Agent 平台骨架说明

## 范围

P1 是模块化单体后端，提供独立 OIDC 登录会话、开发/内部测试用本地注册登录、组织与基础 RBAC、会话/消息/Run 持久化、LangGraph 单智能体、OpenAI-compatible 流式模型适配、SSE 事件、取消、指标和追加式审计。

P1 不包含外部账号连接中心、MCP Gateway、业务 Tool、前端和业务写操作。模型配置只来自环境变量；示例文件中的值为空，不应将真实 Key 写入版本库。

## 本地运行

复制 `.env.example` 为 `.env`，设置一个本地 PostgreSQL 密码：

```powershell
$env:AI_AGENT_POSTGRES_PASSWORD = "change-me"
docker compose up -d
uv run --no-sync alembic upgrade head
```

填写以下配置后启用 P1：

```text
AI_AGENT_PLATFORM__ENABLED=true
AI_AGENT_MODEL__ENABLED=true
AI_AGENT_MODEL__BASE_URL=https://your-compatible-provider.example/v1
AI_AGENT_MODEL__MODEL=<provider-model-name>
AI_AGENT_MODEL__REASONING_EFFORT=<optional-provider-supported-value>
AI_AGENT_MODEL__API_KEY=<local-only-secret>
AI_AGENT_MODEL__INPUT_PRICE_PER_MILLION_TOKENS=<positive-number>
AI_AGENT_MODEL__OUTPUT_PRICE_PER_MILLION_TOKENS=<positive-number>
AI_AGENT_OIDC__ENABLED=true
```

OIDC 测试服务尚未准备好时，开发环境可以改用本地注册登录：

```text
AI_AGENT_OIDC__ENABLED=false
AI_AGENT_LOCAL_AUTH__ENABLED=true
AI_AGENT_LOCAL_AUTH__REGISTRATION_ENABLED=true
```

启动服务：

```powershell
uv run --no-sync ai-agent check-config
uv run --no-sync ai-agent serve
```

OIDC 首次登录使用 `GET /api/v1/auth/login`；本地账号通过工作台注册或登录。两种方式最终都创建同一类不透明 HttpOnly Session Cookie；`GET /api/v1/me` 返回 CSRF Token 和当前用户可见组织。创建会话、发送消息、取消 Run 等写请求需要 `X-Organization-Id` 和 `X-CSRF-Token`。本地账号只允许开发和内部测试，生产环境仍要求 OIDC。

## API 验收

- `GET /health/live`：进程存活。
- `GET /health/ready`：配置、PostgreSQL 和 Redis 可用。
- `GET /api/v1/auth/options`：返回工作台可展示的登录方式，不返回密钥。
- `POST /api/v1/auth/register`、`POST /api/v1/auth/local/login`：本地注册与登录。
- `GET /api/v1/p1/status`：P1 功能开关和能力状态，不返回密钥。
- `POST /api/v1/organizations`：创建组织，创建者自动获得组织管理员角色。
- `GET /api/v1/conversations`、`POST /api/v1/conversations`：用户自己的会话。
- `POST /api/v1/conversations/{id}/messages`：创建幂等 Run，返回 `events_url`。
- `GET /api/v1/runs/{id}/events`：SSE，支持 `Last-Event-ID` 断线恢复。
- `POST /api/v1/runs/{id}/cancel`：设置取消信号，执行器在模型流事件边界检查。
- `GET /api/v1/admin/audit-logs`：需要 `audit:view`，只读追加式记录。
- `GET /metrics`：Run 状态计数和执行耗时。

平台硬限制默认来自实施计划：问题 4,000 字符、模型输入 16,000 Token、输出 2,048 Token、最长 90 秒、模型流最多单轮，且按配置单价执行费用上限。模型流没有返回 usage 时直接失败，不写入不完整的费用审计。

## 验收命令

```powershell
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
$env:PYTHONPATH = "src"
uv run --no-sync mypy src/ai_agent
uv run --no-sync pytest
```

没有 Docker 时仍可运行上述单元和 API 测试；PostgreSQL、Redis、OIDC 和真实模型的联调必须在等价环境执行。生产部署前还需完成真实 OIDC JWKS、模型流式 usage、数据库迁移、TLS、密钥轮换、限流和故障演练。
