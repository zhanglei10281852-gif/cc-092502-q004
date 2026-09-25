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

## 测试

```bash
python -m pytest
```

测试覆盖数据库初始化、项目成员权限、会话撤销、审计脱敏、幂等写入和后台任务领取与完成。

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

## 植物遗存分析模块（app/paleo）

面向植物考古浮选数据的后端模块，按 土样 → 浮选批次 → 重/轻浮选组分 → 分类鉴定 四级建模，支持古河道、居址、灰坑等遗迹类型的分组比较。

### CSV 批量导入

四类 CSV 通过 `POST /api/projects/{id}/paleo/imports/{kind}` 上传（multipart 文件字段 `file`），`kind` 依次为 `samples`、`batches`、`fractions`、`identifications`，需按依赖顺序导入。表头支持中英文别名（如 `sample_code`/`样品号`、`volume_liters`/`土样体积`、`fraction_type`/`组分`），未知列被忽略。

- **samples**：`sample_code`、`context_type`（古河道/居址/灰坑 → paleochannel/dwelling/ash_pit）必填。
- **batches**：`batch_code`、`sample_code`、`volume_liters` 必填；体积为 0 允许导入（密度计算时排除，出现率仍计入），负体积进入拒绝清单。
- **fractions**：`batch_code`、`fraction_type`（轻浮/重浮 → light/heavy）必填，`mesh_size_mm`（筛网孔径）可选。
- **identifications**：`batch_code`、`fraction_type`、`taxon` 必填；`item_no` 缺省时按文件内顺序自动编号。计数支持精确值、仅知上限（`<10`、`≤10`）、仅检出（`present`/`检出`）、未检出（`0`/`absent`，即检测限记录）；置信度接受 高/中/低、high/medium/low、cf. 等写法并规范化；污染列识别 现代根系 → `modern_root`；`未知`/`unidentified` 等归为未知分类。

无法解析或引用的行（未知样品/批次、组分不唯一、文件内重复等）写入拒绝清单，可经 `GET /api/projects/{id}/paleo/imports/{import_id}/rejects` 下载 CSV。导入按 项目+类别+文件SHA256 幂等：同一文件重传直接返回首次结果（`idempotent_replay: true`），不重复累加；不同文件对同一自然键的写入采用 upsert 语义。批次经 `POST .../paleo/batches/{batch_code}/confirm` 确认后，重传文件中涉及该批次的行一律跳过、不改写。整个导入在单个即时事务中完成，中途失败整体回滚。

离线批量导入（不启动 HTTP 服务）：

```bash
python -m app.cli paleo-import --project BAOJIA --kind identifications --file idents.csv --user labtech
```

### 统计版本

`POST /api/projects/{id}/paleo/stats/runs` 创建统计版本，参数：`rank`（family/genus/species 分类口径，缺失时沿分类阶元回退到原始名称）、`seed`、`iterations`（100–100000）、`include_contaminated`、`min_confidence`。每个版本保存算法版本号（`paleo-stats/1.0.0`）、随机种子、迭代次数与应用的变更集，保证可复算。输出：

- 每升密度：组内计数合计/体积合计（ratio of sums）；仅知上限的计数按区间处理，同时给出 `density_min`/`density_max` 与中点点估计；零体积样品不参与密度但计入出现率。
- 出现率（ubiquity）：组内检出样品比例。
- 分组比较：两两遗迹类型的密度差。
- 置信区间：样品级自助重采样 95% percentile 区间；每组另附最小可检密度（1/总体积）。

### 鉴定变更集

鉴定专家的更名/合并意见通过 `POST .../paleo/changesets` 提交为待审变更集（`ops` 为 `rename`/`merge` 操作，可附 `stats_params`）。owner 或 reviewer 角色的其他成员（不能是提案人本人）经 `POST .../changesets/{id}/approve` 批准后，系统自动生成新统计版本；名称映射只在统计时应用，原始鉴定记录永不改写。`POST .../changesets/{id}/reject` 驳回则不产生新版本。

### 角色矩阵

| 操作 | owner | researcher | recorder | reviewer | viewer |
|---|---|---|---|---|---|
| 导入 CSV / 确认批次以外的写入 | ✓ | ✓ | ✓ | | |
| 确认批次、创建统计版本、提案变更集 | ✓ | ✓ | | | |
| 批准/驳回变更集 | ✓ | | | ✓ | |
| 查询（样品、批次、导入、统计、变更集） | ✓ | ✓ | ✓ | ✓ | ✓ |

