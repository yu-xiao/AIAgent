# P3 PermissionSystem 业务闭环验证说明

## 范围

P3 在 P2 连接中心和 MCP Gateway 之上完成 PermissionSystem 普通员工个人授权、受控只读查询、业务权限拒绝和来源引用闭环。Agent 不访问 PermissionSystem REST API 或数据库，业务明细只在 MCP 调用和模型上下文中短暂存在。

首批 Tool 使用 P0 已冻结的名称：

- \`list_datasets\`
- \`describe_dataset\`
- \`query_dataset\`

实际输入/输出 Schema 以 PermissionSystem MCP \`tools/list\` 返回值为准，并由 Gateway 在每次调用时校验。平台额外拒绝凭证、任意 URL、脚本和 SQL 字段。

## 数据迁移

\`\`\`powershell
uv run --no-sync alembic upgrade head
\`\`\`

\`0003\` 新增 \`tool_invocations\` 和 \`citations\`。它们只保存调用摘要、参数 Digest、状态、查询时间、来源和 TraceId，不保存业务响应明细或任何 Token。

## API 验收

- \`GET /api/v1/p3/status\`：查看 P3 开关、首批 Tool 和能力，不返回凭证。
- \`GET /api/v1/permission-system/tools\`：按当前用户个人连接和 PermissionSystem 业务权限返回可用只读 Tool。
- \`POST /api/v1/permission-system/tools/{tool_name}\`：执行已登记的只读 Tool，写请求需要 CSRF。
- \`GET /api/v1/runs/{run_id}/citations\`：读取当前用户 Run 的来源引用。
- \`GET /api/v1/runs/{run_id}/tool-invocations\`：读取不含业务参数的调用摘要和参数 Digest。

个人连接仍使用 P2 的：

- \`POST /api/v1/connections/permission-system/authorize\`
- \`GET /api/v1/connections/permission-system/callback\`
- \`POST /api/v1/connections/{id}/refresh\`
- \`DELETE /api/v1/connections/{id}\`

用户没有平台 \`tool:permission:use\`、没有 ACTIVE 个人连接、连接已撤销或 Tool 不在登记白名单时，Catalog 和 Gateway 均拒绝调用。组织共享连接不会被当作员工个人权限凭据。

## 离线回归评测

\`ai_agent.permission_system.evaluation\` 提供 \`DEFAULT_GOLDEN_QUESTIONS\`、\`evaluate_golden_questions\` 和 \`citation_coverage\`。业务 Owner 可以注入真实 Runner，检查 Tool 选择、权限拒绝和引用覆盖率：

\`\`\`python
from ai_agent.permission_system.evaluation import (
    DEFAULT_GOLDEN_QUESTIONS,
    evaluate_golden_questions,
)

results = await evaluate_golden_questions(run_question, DEFAULT_GOLDEN_QUESTIONS)
assert all(item.passed for item in results)
\`\`\`

## 验证命令

\`\`\`powershell
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
$env:PYTHONPATH = "src"
uv run --no-sync mypy src/ai_agent
uv run --no-sync pytest
\`\`\`

真实闭环还需要在等价环境完成 PermissionSystem OAuth Client、用户 Scope、数据范围、撤销和业务审计 TraceId 对齐验证。
