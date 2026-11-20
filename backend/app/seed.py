import json

from app.db import FIXTURES_DIR, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visitors (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  hit_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS hits (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  session_id TEXT REFERENCES sessions(id),
  event_ts TEXT NOT NULL,
  page_path TEXT NOT NULL,
  event_name TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'primary',
  UNIQUE(visitor_id, event_ts, page_path, event_name)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT,
  hold_key TEXT,
  visitor_id TEXT,
  reason TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  rule_snapshot_json TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  -- 硬约束：hold 成功必须固化非空原因码与规则版本快照（trim 拦截纯空白）
  CHECK (
    reason IS NOT NULL AND trim(reason) <> ''
    AND reason_code IS NOT NULL AND trim(reason_code) <> ''
    AND rule_version IS NOT NULL AND trim(rule_version) <> ''
    AND rule_snapshot_json IS NOT NULL AND trim(rule_snapshot_json) <> ''
  )
);

-- 规则版本登记（版本 ↔ 原因码对照，可核）：发布规则只追加新版本，从不改写旧行
CREATE TABLE IF NOT EXISTS dirty_rule_versions (
  version TEXT PRIMARY KEY,
  seq INTEGER NOT NULL UNIQUE,
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  visitor_id TEXT NOT NULL,
  hit_ids_json TEXT NOT NULL,
  hit_count INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  created_at TEXT NOT NULL
);
"""


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    # 旧库兜底：quarantine 新增批次/搁置键/原因码/规则版本快照列（幂等）+ 不可变触发器
    from app.modules.dirty_hit_hold import ensure_initial_rules, ensure_schema

    ensure_schema(conn)
    # 幂等登记基线规则版本并置为当前收流版本
    ensure_initial_rules(conn)
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            ("1800",),
        )
    n = conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
    conn.commit()
    conn.close()


def _seed_fixtures(conn):
    fixture = FIXTURES_DIR / "seed_hits.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for v in data.get("visitors", []):
        conn.execute(
            "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
            (v["id"], v["label"], v["created_at"]),
        )
    # 基座：按访客整段落进一个 open 会话（超时切分留给功能题空桩）
    for visitor_id, group in _group_hits(data.get("hits", [])).items():
        group = sorted(group, key=lambda h: (h["event_ts"], h["id"]))
        sid = f"S-{visitor_id}-1"
        conn.execute(
            "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status) VALUES (?,?,?,?,?,?)",
            (
                sid,
                visitor_id,
                group[0]["event_ts"],
                group[-1]["event_ts"],
                len(group),
                "open",
            ),
        )
        hit_ids = []
        for h in group:
            conn.execute(
                """
                INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    h["id"],
                    visitor_id,
                    sid,
                    h["event_ts"],
                    h["page_path"],
                    h["event_name"],
                    h.get("channel", "primary"),
                ),
            )
            hit_ids.append(h["id"])
        conn.execute(
            """
            INSERT INTO session_proofs(session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at)
            VALUES (?,?,?,?,?,?,datetime('now'))
            """,
            (
                sid,
                visitor_id,
                json.dumps(hit_ids, ensure_ascii=False),
                len(hit_ids),
                group[0]["event_ts"],
                group[-1]["event_ts"],
            ),
        )


def _group_hits(hits):
    out = {}
    for h in hits:
        out.setdefault(h["visitor_id"], []).append(h)
    return out


def idle_timeout_sec(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    return int(row["value"]) if row else 1800
