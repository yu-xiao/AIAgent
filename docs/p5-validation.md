# P5 生产治理验证说明

## 范围

P5 在 P0-P3 的基础上增加生产配置强校验、HashiCorp Vault KV v2、分布式 Run
配额、跨实例 MCP 限流和熔断、优雅停机、遗留 Run 恢复、Prometheus 指标、OTLP
Trace、审计留存、安全测试、容量探针及蓝绿发布制品。

P4 已明确跳过。本阶段不接入金蝶、MES、BI，不实现跨系统查询、业务写操作、
Kubernetes 或新的前端。

## 生产启动约束

`AI_AGENT_ENVIRONMENT=production` 且平台启用时，配置检查采用 fail-closed：

- 治理和分布式配额必须启用。
- 凭证后端必须为 HashiCorp Vault，Vault 地址必须使用 HTTPS。
- PostgreSQL 必须使用 `ssl=require`，Redis 必须使用 `rediss://`。
- 数据库、Redis、模型和 Vault 密钥必须通过只读文件加载。
- OIDC、模型和 PermissionSystem 地址必须使用 HTTPS。
- 模型价格必须为正数，确保费用上限可以执行。

生产示例见 `.env.production.example`。示例中的域名、模型和价格只是占位符，不得
不经审核直接用于生产。

## 配额语义

Run 创建前，Redis Lua 脚本原子检查并预留以下配额：

- 单用户并发 Run：2。
- 单组织并发 Run：20。
- 单用户每分钟 Run：10。
- 单用户每日 Token：1,000,000。
- 单组织每日 Token：20,000,000。
- 单用户每日费用：10 USD。
- 单组织每日费用：100 USD。

额度按照单 Run 的硬上限预留，模型返回 usage 后按实际 Token 和费用结算。模型在
返回 usage 前异常时保留悲观费用预留，避免供应商费用未知时绕过预算。每日额度按
UTC 日期计算，并在次日后自动过期。

## 安全验证

自动化测试覆盖：

- 生产配置缺少治理、TLS、Vault 或密钥文件时拒绝启动。
- Vault 引用不包含 Token，Vault Token 文件更新后无需重启即可生效。
- Tool 描述不直接进入模型，Tool 结果使用 `untrusted_tool_data` 安全边界封装。
- 恶意 Tool 输出无法突破 Tool allowlist 调用未授权 Tool。
- Tool 输入输出 Schema、响应大小、敏感字段脱敏和 SSRF 基线继续生效。
- 请求大小限制、安全响应头、CSRF、组织隔离和 OAuth state/nonce 继续回归。

提示注入不能仅靠文本分类彻底解决。本实现采用确定性控制：模型不能选择凭证、
不能扩展 allowlist、不能执行任意 URL/SQL/脚本，远端 Tool 描述不作为提示词，Tool
数据被明确标记为不可信。生产验收仍需使用企业真实攻击样本执行人工红队测试。

## 指标与 Trace

`GET /metrics` 提供低基数指标：

- HTTP 请求量、在途请求和延迟。
- Run 状态、延迟、模型输入/输出 Token 和费用。
- MCP Server 调用状态和延迟。
- 配额拒绝、Vault 操作和审计写失败。

指标不使用用户 ID、组织 ID 或 TraceId 作为标签。TraceId 继续通过请求、Run、模型、
Tool、引用和审计传播；启用 OTLP 后额外导出 FastAPI、Agent Run 和 MCP Tool Span。

## 自动化验收

```powershell
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
$env:PYTHONPATH = "src"
uv run --no-sync mypy src/ai_agent
uv run --no-sync pytest
uv run --no-sync ai-agent check-config
```

验证镜像和 Compose：

```powershell
docker build -t enterprise-ai-agent:0.3.0 .
docker compose -f deploy/compose.production.yaml config --quiet
```

## 容量验收

控制面目标为 20 并发、P95 不超过 500ms、平台错误率低于 1%。先执行不产生模型费用
的健康接口探针：

```powershell
uv run --no-sync python scripts/load_test.py --base-url https://agent.example.com --concurrency 20 --requests 200
```

Run 模式会真实调用模型并产生费用，必须显式确认：

```powershell
$env:AI_AGENT_LOAD_SESSION_COOKIE_FILE = "C:\secure\session.txt"
$env:AI_AGENT_LOAD_CSRF_FILE = "C:\secure\csrf.txt"
$env:AI_AGENT_LOAD_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000000"
uv run --no-sync python scripts/load_test.py --mode runs --base-url https://agent.example.com --concurrency 20 --requests 20 --confirm-live-cost
```

模型和 PermissionSystem 的端到端 P95 单独记录，不用外部系统延迟替代平台控制面 SLO。

## 环境验收边界

以下项目必须在生产等价环境执行，Mock 或本地 SQLite 结果不能代替：

- 真实 OIDC 登录、JWKS 轮换、退出和会话失效。
- Vault HA、Token 轮换、权限拒绝和快照恢复。
- PostgreSQL PITR、Redis TLS、网络隔离和 DNS/出站策略。
- PermissionSystem Token 过期、撤销、权限变化和 MCP 故障。
- OTLP、Prometheus、Alertmanager 实际接收链路。
- 蓝绿流量切换、在途 Run 排空、数据库迁移兼容和回滚。

完整操作步骤见 [生产运维手册](operations.md)。
