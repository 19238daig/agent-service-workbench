# Agent Service Workbench

基于 Python、FastAPI、LangGraph 与 React 的智能客服多 Agent 工作台。

这是从本地开发项目导出的独立源码仓库，不包含原仓库的 Git 历史、任何数据集、知识库原件、运行记录、账号、数据库内容或模型权重。`.env.example` 仅为配置模板。运行所需的数据和凭据由部署者在本地配置；本地原项目不受此导出影响。

## 功能与结构

- 四个模型角色：意图路由、知识检索、工单处理、合规审查；Supervisor 负责确定性分派、依赖调度和结果汇总。
- RAG 支持 PDF/HTML 等文件解析、本地 BGE-M3 向量、pgvector、可选 BM25/RRF、重排序、引用和权限检查。
- 工单流程包含类型化动作、用户确认、人工审批、事务执行、幂等回执和持久化恢复。
- React 工作台提供聊天、编排事件、工单、审批、知识管理与运行观察；FastAPI 提供鉴权和 SSE。
- PostgreSQL 持久化业务与运行状态；Redis 用于缓存，Celery/独立 Redis 用于后台任务。

```text
src/smart_cs/   FastAPI、Agent、业务服务、RAG、持久化与评测代码
frontend/      React + TypeScript + Vite
migrations/    数据库结构迁移
scripts/       初始化、账号管理、导入与验证工具
tests/         自动化测试（部分依赖自行准备的数据或 PostgreSQL）
deploy/        容器部署配置
docs/          技术说明与编排图
.env.example   空配置模板
```

## 本地启动（Windows / Python 3.12+、Node.js、Docker）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
Copy-Item .env.example .env
# 在根目录 .env 填写自己的模型接口、密钥和模型名。
.\.venv\Scripts\python.exe -m smart_cs.foundation configure
docker compose -p smart-cs-foundation up -d --wait postgres redis
.\.venv\Scripts\python.exe -m smart_cs.delivery init
Push-Location frontend
npm ci
npm run build
Pop-Location
.\.venv\Scripts\python.exe scripts/manage_accounts.py bootstrap
.\.venv\Scripts\python.exe -m smart_cs.delivery serve
```

浏览器访问 <http://127.0.0.1:8000/>，API 文档为 `/docs`。首次账号凭据保存在本地 `.local/auth/`，请按账号管理脚本的提示查看并妥善保管。默认以空数据库启动，业务订单和知识文档需要自行导入。不要将真实账号、订单、用户记录或知识文档提交到 Git。

`start.py` / Windows 启动快捷脚本包含本地演示数据准备逻辑；如需空库启动，请使用上面的 `delivery init` / `delivery serve` 命令。`foundation build-data` 只在本地生成程序内定义的虚构演示数据；原项目的训练/测试集、公开资料副本及评测候选集均未随本仓库上传。涉及这些文件的完整评测脚本需要先自行准备匹配的数据和清单，不能将源码检查等同于模型质量评测。

RAG 默认关闭。启用前，请配置自己的向量与重排服务、导入具有使用权限的文档、完成切分和索引；`scripts/download_public_knowledge.py` 保留下载流程代码，但不包含下载结果。示例业务约束见 [docs/business-scope.md](docs/business-scope.md)，仅适用于虚构演示场景。

## 检查与部署

```powershell
.\.venv\Scripts\python.exe scripts/check_source_snapshot.py
```

该检查只覆盖无需外部数据集和真实模型的契约测试；数据库锁、真实检索质量及模型效果需另行验证。前端检查使用 `npm run build`；浏览器测试使用 `npm run test:e2e`（需要安装 Playwright 浏览器）。[部署说明](docs/deployment.md)与[编排图](docs/agent-orchestration.md)。

## 来源与许可

本项目基于 [bcefghj/smart-cs-multi-agent](https://github.com/bcefghj/smart-cs-multi-agent) 的四 Agent 思路进行 Python 实现与扩展，保留上游 MIT 许可证及版权声明。Go/Java 实现未包含在本仓库。参见 [LICENSE](LICENSE)。
