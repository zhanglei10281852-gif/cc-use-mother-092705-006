# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。

## 生态保留决策台账

公园落叶、枝堆可能是刺猬、昆虫及小型动物的越冬栖所，`/api/ecology/cases` 提供专门的保留决策台账：

- `POST /api/ecology/cases/complaints`：公众投诉入口（无需登录），登记照片摘要、堆放位置与反馈，自动合并到同地点同周期案件。
- `POST /api/ecology/cases/registrations`：工作人员登记现场照片摘要、堆放位置、季节风险（需 `eco.register`）。
- `POST /api/ecology/cases/{id}/decisions`：授权人员（`eco.decide`）决定保留、调整或移除，必须填写当时引用的生态依据及出处；“调整”必须写明措施。
- `POST /api/ecology/cases/{id}/reviews`：复查只能新增结论（维持保留/建议调整/建议移除/继续观察），不覆盖案件状态与历史结论，数据库触发器禁止改写或删除决定、复查记录。
- `POST /api/ecology/cases/emergencies`：紧急安全处置可先执行后补录，记录实际处置时间、补录期限（默认 48 小时）与责任人；`POST /api/ecology/cases/{id}/supplement` 完成补录，逾期补录会单独标记，`GET /api/ecology/cases/emergencies/overdue` 列出超期未补录记录。
- `GET /api/ecology/cases`、`GET /api/ecology/cases/{id}`：台账查询（需 `eco.read`）。

同一“地点 + 周期”（11 月至次年 2 月归为同一越冬周期）的重复登记、投诉和紧急处置合并到同一案件；越权操作返回 403 并写入 `denied` 审计事件，所有状态变更均可在 `/api/audit` 中按 `eco_case` 资源追溯。

新增角色：`eco_manager`（生态保留授权人员，含全部 `eco.*` 权限）；`clerk` 默认可登记、复查与紧急处置，`auditor` 只读。
