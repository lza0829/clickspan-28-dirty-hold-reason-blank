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

- **落库形态（择一）**：搁置表挂 payload JSON —— 写入 `quarantine`，`member_json` 保存
  原始 payload，另有 `batch_id`、`hold_key`、`reason` 列；不使用 `hits.hold` 标记。
- **失败闭（择一）**：入口校验分流 —— 收流入口逐点校验，脏点即剔入搁置，合法点才写
  正式 hits 并推进 open 会话末击点；整批单事务，意外异常整趟回滚。
- 脏点不进正式成员、不改 `hit_count`、不更新末击点 `ended_at`；禁止只打日志。

| 接口 | 说明 |
| --- | --- |
| `GET /api/quarantine?batch_id=` | 搁置记录（含可展开 payload、搁置键、原因） |
| `GET /api/dirty-hold/attestation?batch_id=` | 只读核证：批次、搁置键、原因、正式写入数、末击点是否未漂 |
| `POST /api/ingest/run` | 收流一批（夹具 `fixtures/ingest_batch.json` 混有脏行） |

末击点未漂判定：会话当前 `ended_at` 必须等于其正式成员的 `MAX(event_ts)`；
被脏行顶替会晚于该值，核证 `clock_drifted=true`、`passed=false`。

后端测试：`cd backend && .venv/bin/python -m pytest tests/`（独立临时 SQLite，不污染本地库）。
