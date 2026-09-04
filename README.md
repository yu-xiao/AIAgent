# Enterprise AI Agent

这是企业 AI 智能体平台的 Python 3.12 项目。当前实现范围是 P0：验证独立认证与业务系统隔离的架构，以及 OAuth/OIDC、MCP Streamable HTTP 的关键协议链路。

P0 不包含正式用户会话、凭证持久化、LangGraph 编排、模型调用和前端。

## 快速开始

```powershell
.\scripts\bootstrap.ps1
Copy-Item .env.example .env
uv run --no-sync ai-agent check-config
uv run --no-sync ai-agent serve
```

`bootstrap.ps1` 优先执行标准 `uv sync`。若当前 Windows 环境的 uv 出现 PEP 517 临时结果文件异常，脚本会保留 uv 的锁定依赖，并使用同一虚拟环境完成 editable 安装。

服务启动后可访问：

- `GET http://127.0.0.1:8000/health/live`
- `GET http://127.0.0.1:8000/health/ready`
- `GET http://127.0.0.1:8000/api/v1/p0/status`
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

详细范围、配置和验收方式见 [P0 验证说明](docs/p0-validation.md)，总体设计见 [实施方案](docs/ai-agent-implementation-plan.md)。
