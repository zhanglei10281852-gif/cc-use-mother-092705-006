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

## 栖息地保留决策台账

公园落叶、枝堆可能是刺猬、昆虫及小型动物的越冬栖所。台账服务（`/api/habitat`）支持管护班组把"看起来杂乱"的现场登记为案件，由授权人员引用当时的生态依据决定保留、调整或移除，后续复查只能追加结论，紧急安全处置可先执行再限期补录。

- **登记合并**：`POST /api/habitat/cases/registrations` 登记照片摘要、堆放位置、季节风险、观察物种与公众反馈；同一地点（名称归一化）+ 同一生态周期（如 `2026-winter`）的重复登记自动并入同一案件，历史登记行不覆盖。
- **授权决定**：`POST /api/habitat/cases/{id}/decision` 需要 `habitat.decide` 权限，请求体必须携带 `eco_basis`（依据类型、名称、条款、引用要点与现场佐证），决定落库后追加为新序号，旧决定原样保留。`adjust` 必须填写具体措施。
- **复查/投诉**：`POST /api/habitat/cases/{id}/followups` 需要 `habitat.review` 权限；复查（`review`）只追加结论，投诉（`complaint`）使案件进入 `under_review` 等待重新决定。
- **紧急处置**：`POST /api/habitat/cases/{id}/emergencies` 需要 `habitat.emergency` 权限，记录实际处置时间、现场责任人、补录期限（默认 24 小时）与补录责任人；超过期限补录的案件标记为 `makeup_overdue`，在期限内为 `emergency_disposed`。
- **查询与权限**：`GET /api/habitat/cases`、`GET /api/habitat/cases/{id}` 需要 `habitat.read`；越权返回 403 并写入 `outcome=denied` 审计事件，全部成功操作写入对应 `habitat.*` 审计事件。


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
