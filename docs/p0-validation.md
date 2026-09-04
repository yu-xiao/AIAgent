# P0 架构与协议验证说明

## 1. 目标与边界

P0 用 3 至 5 个工作日验证以下关键假设：

1. Agent 使用独立 OIDC 身份体系，不依赖任何业务系统登录。
2. 业务系统仅通过 MCP Server 暴露能力，Agent 不访问其数据库和内部 API。
3. 外部系统可采用用户 Authorization Code + PKCE 或服务 Client Credentials 获取短期 Token。
4. MCP Streamable HTTP 能完成 `initialize`、`tools/list` 和受控的只读 `tools/call`。
5. 每次 MCP 探针携带 Bearer Token 和独立 `X-Trace-Id`，且输出与日志不包含 Token。

P0 是协议验证服务，不是可供员工使用的完整智能体。正式登录 Session、外部账号连接存储、Token 加密持久化、模型与 Agent 编排属于 P1。

## 2. 已实现内容

```text
FastAPI P0 Service
  +-- 独立 OIDC discovery / Authorization Code + PKCE 基线
  +-- OAuth Protected Resource / Authorization Server discovery
  +-- Client Credentials Token Provider（内存缓存、提前刷新）
  +-- 官方 MCP Python SDK Streamable HTTP Client
  |     +-- initialize
  |     +-- tools/list + Tool Schema
  |     +-- 可选 list_datasets 只读调用
  +-- 健康检查、配置检查和命令行真实联调入口
```

安全边界：Token 使用 `SecretStr`，不进入探针输出；生产配置强制 HTTPS；禁止 URL 内嵌凭证和重定向跟随；业务 Tool 缺失或返回协议不合规时直接失败。

## 3. 配置

复制 `.env.example` 为 `.env`，再按环境填写。变量采用 `AI_AGENT_` 前缀和双下划线嵌套。

PermissionSystem 探针凭证二选一：

- `AI_AGENT_PERMISSION_SYSTEM__ACCESS_TOKEN`：仅用于短期人工联调，不应写入版本库。
- `AI_AGENT_PERMISSION_SYSTEM__SERVICE_CLIENT_ID` 与 `AI_AGENT_PERMISSION_SYSTEM__SERVICE_CLIENT_SECRET`：由探针通过 Client Credentials 换取 Token。

个人用户授权所需的 `AUTHORIZATION_CLIENT_ID`、回调 URI 和 PKCE 已定义，但 P0 不保存授权交易和用户 Token；这部分将在 P1 使用服务端 Session 与凭证保险库完成。

## 4. PermissionSystem 需要满足的契约

MCP 端点默认是 `http://localhost:5071/mcp`，Token 端点默认是 `http://localhost:5264/connect/token`。真实环境需要：

- Streamable HTTP MCP Server 支持当前官方 SDK 协议协商。
- 验证 `Authorization: Bearer <token>`，不接受手机号作为调用者身份。
- Token 的 audience/resource 和 scope 与 MCP Server 匹配。
- 业务用户权限由 PermissionSystem 基于 Token 中稳定的 subject 最终校验。
- 提供 `list_datasets`、`describe_dataset`、`query_dataset` 三个只读 Tool 及稳定 JSON Schema。
- 接收或生成 TraceId，并把 Tool 调用写入业务侧审计。
- 建议提供 RFC 9728 `/.well-known/oauth-protected-resource[/mcp]` 和 OAuth/OIDC Server Metadata。

若其他金蝶、MES、BI 系统接入，只需新增相同边界的 MCP Server/Adapter 和独立连接配置，不共享 PermissionSystem 凭证或用户 ID。

## 5. 验收命令

```powershell
.\scripts\bootstrap.ps1
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy
uv run --no-sync pytest
uv build
```

当前机器上的 `uv 0.9.21` 存在 PEP 517 后端已成功、但 uv 读取临时结果文件失败的问题。`scripts/bootstrap.ps1` 已提供局部兼容路径；该问题不影响锁文件解析、依赖安装、运行和测试，但 CI 应使用干净环境验证标准 `uv sync` 与 `uv build`。

本地 API：

```powershell
uv run --no-sync ai-agent check-config
uv run --no-sync ai-agent serve
```

真实协议联调：

```powershell
uv run --no-sync ai-agent probe-oidc
uv run --no-sync ai-agent probe-permission
uv run --no-sync ai-agent probe-permission --call-list-datasets
```

验收通过条件：配置校验成功；OIDC issuer 与 discovery 一致并支持所需端点；MCP 初始化成功；三个预期 Tool 及 Schema 可发现；`list_datasets` 在当前凭证权限下返回结构化结果；Agent 与 PermissionSystem 审计可通过 TraceId 对齐。

## 6. 已知风险与 P1 输入

- PermissionSystem 必须实际启动并提供有效 OAuth Client/Token 后，才能完成真实系统验收。
- 服务凭证只能证明组织共享连接链路，不能证明普通员工权限继承；严格用户权限必须使用用户授权或 OBO Token。
- P0 Token 只在进程内存中使用，不提供持久化、吊销同步和多实例协调。
- Tool 输出的数据分级、脱敏、结果大小限制和审计落库尚未实现。
- 在 P1 开始前应冻结 PermissionSystem Tool Schema、错误码、OAuth audience/scope 和审计字段。
