# 考古研究协作基础服务

这是一个供考古项目扩展业务模块的纯后端基础服务，提供研究项目登记、成员与角色、会话认证、审计事件、幂等请求和可恢复后台任务。服务使用 FastAPI 与 SQLite，不依赖另行部署的数据库、缓存或队列。

## 环境与安装

运行环境为 Python 3.11。安装开发依赖：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

基础接口包括 `/api/system/health`、`/api/projects`、`/api/users`、`/api/sessions`、`/api/audit` 和 `/api/jobs`。首次启动后可用命令行创建管理员，也可以通过测试夹具构造隔离数据库。

## 植物考古（植物遗存）模块

用于比较古河道、居址、灰坑等背景中的种子密度与出现率。代码位于 `app/botany/`，所有接口挂在 `/api/projects/{project_id}/botany` 下，按项目角色鉴权：

| 角色 | 导入/锁定 | 分类口径/统计版本 | 变更集 | 读取 |
| --- | --- | --- | --- | --- |
| owner / researcher | ✔ | ✔ | 提议（researcher 不能批准自己的意见，需 reviewer/owner 审定） | ✔ |
| recorder | ✔ | ✘ | ✘ | ✔ |
| reviewer | ✘ | 统计复算 ✔ | 批准/驳回 | ✔ |
| viewer | ✘ | 仅复算已有版本 | ✘ | ✔ |

### CSV 格式

单个 CSV 用 `record_type` 列区分四类记录（字段名兼容中文，见 `app/botany/csvio.py` 的别名表）：`batch`（浮选批次）、`sample`（土样）、`fraction`（重/轻组分）、`identification`（分类鉴定）。完整样例见 [`examples/botany_import.csv`](examples/botany_import.csv)。

关键记录约定：

- **体积** `volume_liters` 必须为正；零体积与非数字行进入拒绝清单，不落库。
- **筛网** `mesh_mm` 记孔径毫米；样品行可带筛网备注。
- **计数** `count`：普通非负数为精确计数；`<n`（如 `<3`）为**仅知上限**，保守按 0 参与密度点估计、按至多 n−1 粒给出上限口径密度，并仍计为一次检出；`nd`/未检出表示**低于检测限**，检测限在 `detection_limit` 列给出，计为未检出。
- **污染** `polluted` 标记现代根系侵入，定量统计默认剔除污染样品，可通过 `include_polluted=true` 纳入。
- **未知分类** `is_unknown=1` 时可留空学名，并在 `unknown_detail` 记录存疑描述；统计结果中独立打 `is_unknown` 标记。
- **置信度** `confidence`：high/medium/low/uncertain（高/中/低/存疑）随鉴定保存。

导入语义：

- 每行独立校验，错误行进**拒绝清单**（`GET .../imports/{id}/rejections` 下载带 BOM 的 CSV，含行号、错误代码、原始行），正确行正常入库；整批在单个事务内提交，意外中断整体回滚。
- 同项目下 **文件内容 SHA-256 相同即视为重传**：直接返回 `duplicate`，数据库唯一索引兜底，不重复累加；同文件/跨文件的重复样品、重复组分、重复鉴定分别拒绝或跳过。
- 批次经 `POST .../batches/{id}/lock` **确认锁定**后，任何对该批次（含其样品、组分、鉴定）的追加都会被拒绝。

### 统计与复算

`POST .../comparisons` 按可选**分类口径**（taxonomy view）计算：

- 各组每升密度（组内精确计数总和 / 体积总和）、平均样品密度、仅知上限口径密度、出现率（检出样品比例）；
- 组间两两比较的密度差与出现率差，均带**自助重采样置信区间**（百分位法，R type-7 分位数）；
- 生成不可变**统计版本**（`stat-versions`），其中保存随机种子、迭代次数、置信水平、参数与算法版本（`botany-bootstrap-1.0.0`）。同一组参数 + 种子逐位可复算，可用 `POST .../stat-versions/{id}/recompute` 校验；算法版本变更时拒绝复算并提示。

`?persist=false` 只试算不落版本（viewer 也可调用）；分类缺失的样品在逐分类统计中按零计数补入，保证出现率分母包含所有样品。

### 专家更名/合并意见（待审变更集）

鉴定专家通过 `POST .../changesets` 提交 `rename`/`merge` 意见（pending），reviewer/owner 在 `/review` 批准或驳回。**批准不会改写任何原始鉴定行**：系统把"原始分类 + 既往口径 + 本变更集"快照成一个新的分类口径视图，在同一事务内据此生成新的统计版本并关联变更集；合并链支持追溯且检测循环。

### 离线批量导入

大文件不经 HTTP，使用命令行（需要已存在的项目编码与用户名）：

```bash
python -m app.cli botany-import --project BOTANY --as owner --file data/2026Q3.csv \
    --rejections-out data/2026Q3.rejections.csv
```

退出码：0 成功（含完全重复文件）；2 参数/前置数据不存在；3 导入异常；4 全部行被拒绝。有拒绝行时在 `*.rejections.csv` 输出清单。

## 测试

```bash
python -m pytest
```

测试覆盖数据库初始化、项目成员权限、会话撤销、审计脱敏、幂等写入和后台任务领取与完成；植物考古模块另覆盖零体积、部分删失（`<n`/`nd`）、重复样品与重复鉴定、现代根系污染、未知分类、同文件重传幂等、批次锁定、固定随机种子逐位复算、分类变更集批准且原始鉴定不变、角色越权、事务中断整体回滚、拒绝清单下载与离线 CLI 导入。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内检查根路径、健康接口、数据库外键和 WAL 配置。

## 扩展约定

新研究模块应通过独立路由、服务和仓储接入，跨表写入放在即时事务中。外部标识、幂等键和审计载荷应保存原始值及规范化值；后台任务使用 SQLite 租约，不允许依赖外部队列。用户口令和会话令牌只保存摘要，审计事件会过滤密码、令牌等敏感字段。
