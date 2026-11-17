# React 工作台

React + TypeScript + Vite，连接根目录的 FastAPI 后端。先按[根目录说明](../README.md)配置后端、数据库和本地账号。

```powershell
npm ci
npm run dev
```

开发服务器将 `/api` 和 `/health` 代理到 `127.0.0.1:8000`。构建执行 `npm run build`，产物位于被 Git 忽略的 `dist/`，由 FastAPI 提供静态服务。

```powershell
npm run build
npm run test:e2e
```

浏览器测试使用模拟接口，不访问真实模型服务；Windows 默认使用 Edge，其他环境可设置 `PLAYWRIGHT_BROWSER_CHANNEL=chromium` 并先安装 Playwright Chromium。真实模型与数据库的端到端验证需自行准备本地数据。

模型密钥仅保存在后端根目录 `.env`，不要放入前端环境变量或提交到 Git。对话、工单、审批、知识管理和运行观察组件位于 `src/`；编排说明见 [Agent 编排](../docs/agent-orchestration.md)。
