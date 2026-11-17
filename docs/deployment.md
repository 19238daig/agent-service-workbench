# 无私有数据的部署

1. 安装根目录锁定的 Python 依赖，并执行 `pip install -e . --no-deps`。
2. 从 `.env.example` 创建根目录 `.env`，填写自己的模型服务配置。
3. 执行 `python scripts/prepare_delivery.py`，在 `.local/delivery/environment.env` 生成独立的本地部署凭据。
4. 后续命令统一使用：

```powershell
docker compose --project-directory . --env-file .local/delivery/environment.env -f deploy/compose.delivery.yml build app
docker compose --project-directory . --env-file .local/delivery/environment.env -f deploy/compose.delivery.yml up -d --wait postgres redis job-redis
docker compose --project-directory . --env-file .local/delivery/environment.env -f deploy/compose.delivery.yml run --rm init
docker compose --project-directory . --env-file .local/delivery/environment.env -f deploy/compose.delivery.yml run --rm app python scripts/manage_accounts.py bootstrap
docker compose --project-directory . --env-file .local/delivery/environment.env -f deploy/compose.delivery.yml up -d app
```

页面默认位于 `http://127.0.0.1:18000/`。账号生成结果按脚本提示在持久化的本地卷中查看。镜像和初始化流程不包含数据集；`data` 卷初始为空。需要自行录入业务资料、导入有权使用的 PDF/HTML 并构建检索索引，才可演示真实知识问答和工单业务。

原项目的全量回归、备份恢复演练和评测脚本保留在 scripts 中，其中涉及固定候选集、订单场景或知识资料的流程需要另外准备本地数据；本源码仓库的 CI 运行不依赖这些私有文件的契约检查和前端构建。

模型权重、凭据、数据库卷、备份、用户文件和评测结果应存放于源码之外或被忽略的本地目录。部署到外部网络前请配置 HTTPS、安全 Cookie、精确 Origin 与访问控制，并使用自己的业务规则及授权数据完成验收。
