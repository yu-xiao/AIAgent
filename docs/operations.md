# 生产运维手册

## 部署前检查

1. 将 `.env.production.example` 复制为不入库的 `.env.production`，替换所有示例域名、
   模型、价格和容量值。
2. 准备数据库密码、Redis 密码、模型 API Key、Vault Token、TLS 证书和私钥文件。
3. 确认 PostgreSQL TLS、备份/PITR，Redis TLS、持久化策略，以及 Vault HA/审计设备已
   由对应基础设施负责人验收。
4. 确认网络只允许 API 访问已登记的 OIDC、模型、Vault、PostgreSQL、Redis 和
   PermissionSystem MCP 端点。SSRF 的最终边界必须包含出站防火墙或代理，不能只依赖
   URL 字符串校验。
5. 执行 `ai-agent check-config` 和数据库备份，再运行 Alembic 迁移。

Compose 通过以下宿主机环境变量读取密钥文件路径，不读取密钥值：

```text
AI_AGENT_DATABASE_PASSWORD_FILE
AI_AGENT_REDIS_PASSWORD_FILE
AI_AGENT_MODEL_API_KEY_FILE
AI_AGENT_VAULT_TOKEN_FILE
AI_AGENT_TLS_CERTIFICATE_FILE
AI_AGENT_TLS_PRIVATE_KEY_FILE
AI_AGENT_IMAGE_BLUE
AI_AGENT_IMAGE_GREEN
AI_AGENT_UPSTREAM_CONFIG
```

## 蓝绿发布

初次启动蓝环境：

```powershell
$env:AI_AGENT_IMAGE_BLUE = "registry.example.com/ai-agent:0.3.0"
$env:AI_AGENT_UPSTREAM_CONFIG = "./nginx/upstream-blue.conf"
docker compose -f deploy/compose.production.yaml up -d migrate api-blue prometheus alertmanager proxy
```

灰度新版本：

```powershell
$env:AI_AGENT_IMAGE_GREEN = "registry.example.com/ai-agent:next"
docker compose -f deploy/compose.production.yaml --profile green up -d api-green
docker compose -f deploy/compose.production.yaml exec -T proxy wget -qO- http://api-green:8000/health/ready
$env:AI_AGENT_UPSTREAM_CONFIG = "./nginx/upstream-gray.conf"
docker compose -f deploy/compose.production.yaml up -d --force-recreate proxy
```

观察至少一个完整业务高峰窗口。确认 Run/MCP 成功率、P95、费用、配额拒绝、Vault、
审计和用户反馈均符合目标后，将 upstream 切换到 `upstream-green.conf`。回滚时立即切回
`upstream-blue.conf` 并重建 proxy。

数据库迁移必须采用 expand-contract：灰度期间新旧镜像都能读取迁移后的 Schema。
删除列、收紧约束或不可逆数据变换不得与应用发布同批执行。

## 动态关闭与排空

关闭新 Run，不影响查询历史和管理操作：

```powershell
uv run --no-sync ai-agent runs disable
uv run --no-sync ai-agent runs status
```

重新开放：

```powershell
uv run --no-sync ai-agent runs enable
```

容器收到停止信号后先停止接收新 Run，并等待最多 30 秒；仍未结束的任务被取消并记录
`service_shutdown`。启动时，超过 300 秒的 `running` Run 标记为中断失败，数据库中的
`queued` Run 会重新提交。不要通过直接修改数据库状态代替上述机制。

## 凭证轮换

- Vault Token 通过文件读取，每次 Vault 请求都会重新加载；原子替换 Token 文件后无需
  重启应用。
- 个人 OAuth 连接调用 `POST /api/v1/connections/{id}/refresh`，生成新 Vault 引用并撤销
  旧引用。
- 组织连接以相同 `server_code` 重新调用 `POST /api/v1/organization-connections`，操作会
  轮换引用并写入 `connection.credential_rotated` 审计。
- 轮换后验证旧引用返回 404/拒绝、新连接能够调用只读 Tool，日志和模型请求中没有
  Token。

## 审计留存

预览 365 天策略命中的记录：

```powershell
uv run --no-sync ai-agent prune-audit
```

完成合规归档并核对预览数量后执行：

```powershell
uv run --no-sync ai-agent prune-audit --execute
```

执行操作按组织写入 `governance.audit_retention_executed`。建议由受控调度器每日执行，
失败时触发运维告警。会话和 Run 暂不自动删除，直到业务 Owner 确认对应保留周期。

## 备份与恢复

数据库备份使用 custom format 并生成 SHA-256：

```powershell
.\scripts\backup.ps1 -OutputDirectory E:\Backups\AiAgent -DatabaseHost postgres.example.internal -DatabasePasswordFile C:\secure\db-password
```

若 Vault 使用集成 Raft 存储，可增加 `-IncludeVaultSnapshot`、`-VaultAddress` 和
`-VaultTokenFile`。托管 Vault 应使用供应商快照流程。Redis 仅保存会话、短期事件、
Catalog、配额和策略状态，不作为权威业务存储；灾难恢复时不回灌过期 Redis 数据。

恢复会清理并替换目标数据库对象，脚本要求显式确认：

```powershell
.\scripts\restore.ps1 -BackupFile E:\Backups\AiAgent\ai-agent-postgres-YYYYMMDD-HHMMSS.dump -DatabaseHost recovery-postgres.example.internal -DatabasePasswordFile C:\secure\recovery-db-password -ConfirmRestore
```

恢复后依次执行 Alembic、`check-config`、只读登录/MCP 冒烟测试和审计核对。恢复演练不得
直接指向生产主库。RPO/RTO 以季度恢复演练的实测结果为准。

## 故障演练

每季度至少执行以下演练，并保留 TraceId、指标截图、时间线和改进项：

- 模型超时或 5xx：Run 明确失败，费用未知时保留悲观配额，不生成伪造回答。
- PermissionSystem MCP 超时：触发熔断和告警，不绕过 MCP 直接查库/API。
- Redis 不可用：ready 失败，新 Run fail-closed，历史数据库数据保持完整。
- PostgreSQL 不可用：ready 失败，停止流量并验证恢复流程。
- Vault 拒绝或密封：ready 失败，凭证不降级到内存或明文配置。
- 灰度版本异常：切回蓝 upstream，等待绿色在途 Run 排空后停止。

告警接收器默认是空接口，正式上线前必须在 `deploy/alertmanager/alertmanager.yml` 中接入
经过批准的邮件、企业消息或事件平台，并完成一次端到端测试。
