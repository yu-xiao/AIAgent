# 企业 AI Agent Web 工作台

Vue 3 + TypeScript 前端，当前覆盖 U1/U2：本地注册登录、OIDC 登录入口、会话恢复、组织切换、对话历史、Run SSE、停止生成和错误恢复。

## 本地开发

先在项目根目录启动后端，然后运行：

```powershell
$env:AI_AGENT_PLATFORM__POST_LOGIN_REDIRECT_URI = "http://localhost:5173/"
npm install
npm run dev
```

访问 `http://localhost:5173/`。Vite 将 `/api` 和 `/health` 同源代理到 `http://127.0.0.1:8000`。

OIDC 暂不可用时，可在后端开发环境开启：

```text
AI_AGENT_LOCAL_AUTH__ENABLED=true
AI_AGENT_LOCAL_AUTH__REGISTRATION_ENABLED=true
```

注册使用邮箱、显示名称和至少 12 位密码；成功后自动登录并创建个人工作区。本地账号在生产环境不可启用。

## 构建与验证

```powershell
npm test
npm run typecheck
npm run build
```

生产构建输出到 `web/dist`。FastAPI 会从 `AI_AGENT_PLATFORM__WEB_DIST_PATH` 托管构建结果；默认路径为 `web/dist`。

## 安全约束

- 使用服务端 HttpOnly Session Cookie，不在浏览器保存 Access Token 或 Refresh Token。
- 密码只提交给同源认证接口，不写入浏览器存储。
- 所有修改请求携带 `/api/v1/me` 返回的 CSRF Token。
- 所有组织资源请求携带当前 `X-Organization-Id`。
- 消息按纯文本渲染，不直接执行模型返回的 HTML。
