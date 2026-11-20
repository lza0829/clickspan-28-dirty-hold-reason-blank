# 点跨（Clickspan）

点击流装载 → 按访客空闲超时切会话 → 脏事件隔离 → 会话证明互证。

**不是** 运单轨迹时间线（途迹）、变更投影流（changecast）、CDC Sink、券核销钉扎、日结封账、双边对齐、改价价签、证据包放行。  
去皮后是：**事件装载 + 会话窗物化 + 闸/隔离 + 证明**。

## 启动

```powershell
# 后端
cd backend
pip install -r requirements.txt
$env:DATA_DIR = "$PWD\data"
$env:FIXTURES_DIR = "$PWD\..\fixtures"
uvicorn app.main:app --host 127.0.0.1 --port 10900

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5900 |
| 接口 | http://127.0.0.1:10900 |

## UI

**泳道织机**：顶栏品牌 + 顶栏菜单（织机 / 收流 / 搁置 / 核证 / 切窗）+ 横向会话串珠。  
不用底栏导航、不用三栏测绘板、不用左轨编号、不用流程 ribbon。

## 脏点搁置（dirty_hit_hold）

缺 `visitor_id` / `event_ts` / `page_path` / `event_name`（缺失、null、纯空白）的击点处理：

- **落库形态（择一）**：**hold 行不可变 reason + 规则版本快照列** —— 写入 `quarantine`，
  `member_json` 保存原始 payload，另有 `batch_id`、`hold_key`、非空 `reason`（原因文案）、
  非空 `reason_code`（原因码）、`rule_version` 与 `rule_snapshot_json`（入 hold 当时的
  规则版本快照）；不使用 `hits.hold` 标记。规则版本登记在 `dirty_rule_versions`
  （版本 ↔ 原因码对照，只追加不可改）。
- **失败闭（择一）**：入口校验分流 —— 收流入口按**当前规则版本**逐点校验，脏点即剔入
  搁置并在同事务写死原因与版本快照（缺一回滚，DB CHECK + 触发器双拦），合法点才写
  正式 hits 并推进 open 会话末击点；整批单事务，意外异常整趟回滚。
- 脏点不进正式成员、不改 `hit_count`、不更新末击点 `ended_at`；禁止只打日志。
- **不可变**：`quarantine` 的 reason / reason_code / rule_version / rule_snapshot_json
  由 BEFORE UPDATE 触发器锁定，规则页发布新版本只追加登记并切换当前指针，
  **只影响之后收流，绝不回溯改写旧 hold**；核证与搁置页读同一固化列，不可能分叉。

| 接口 | 说明 |
| --- | --- |
| `GET /api/quarantine?batch_id=` | 搁置记录（payload、搁置键、固化原因/原因码/规则版本快照） |
| `GET /api/dirty-hold/attestation?batch_id=` | 只读核证：批次、搁置键、固化原因版本、正式写入数、末击点是否未漂 |
| `GET /api/dirty-hold/reason-attestation?batch_id=` | **hold 原因核证（只读，基线所无）**：逐行脏键、reason、reason_code、规则版本、快照对照与结论 |
| `GET /api/dirty-hold/rules` | 规则页：当前生效版本与全部历史版本（原因码对照） |
| `PUT /api/dirty-hold/rules` | 发布新规则版本（追加登记，不回溯旧 hold） |
| `POST /api/ingest/run` | 收流一批（夹具 `fixtures/ingest_batch.json` 混有脏行） |

末击点未漂判定：会话当前 `ended_at` 必须等于其正式成员的 `MAX(event_ts)`；
被脏行顶替会晚于该值，核证 `clock_drifted=true`、`passed=false`。

后端测试：`cd backend && .venv/bin/python -m pytest tests/`（独立临时 SQLite，不污染本地库）。
