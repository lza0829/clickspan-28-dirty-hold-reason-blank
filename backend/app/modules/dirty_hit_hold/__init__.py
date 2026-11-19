"""缺字段脏点搁置（dirty_hit_hold）。

路径选择（题目二选一）：
- 落库形态：搁置表挂 payload JSON —— 复用 quarantine 表，member_json 保存
  原始 payload，另挂 batch_id / hold_key 便于核证；不使用 hits.hold 标记。
- 失败闭：入口校验分流 —— 收流入口逐点校验必填字段，脏点即剔入搁置，
  只有合法点进入正式成员与末击点时钟；整批仍在单事务内，意外异常整趟回滚。

不变量：
- 缺 visitor_id / event_ts / page_path / event_name 任一（缺失、null、空白串）
  的击点必须进搁置，保留原始 payload 与原因；
- 脏点不得进正式 hits、不得改 sessions.hit_count、不得更新 open 会话末击点
  （ended_at 时钟）；
- 合法点不受影响，按既有切窗/挂会话逻辑入正式。
"""

import json
import uuid

STATUS = "implemented"

REQUIRED_FIELDS = ("visitor_id", "event_ts", "page_path", "event_name")
FIELD_LABELS = {
    "visitor_id": "缺 visitor_id",
    "event_ts": "缺 event_ts",
    "page_path": "缺 page_path",
    "event_name": "缺 event_name",
}


def validate_hit(hit) -> list[str]:
    """返回缺失原因列表；空列表表示合法点。

    缺失、null、空串或纯空白均判脏，避免空字符串绕过 NOT NULL 语义
    （SQLite 对 '' 不做 NOT NULL 拦截）。
    """
    reasons = []
    if not isinstance(hit, dict):
        return [f"非对象击点: {type(hit).__name__}"]
    for field in REQUIRED_FIELDS:
        value = hit.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            reasons.append(FIELD_LABELS[field])
    return reasons


def make_hold_key(hit) -> str:
    """搁置键：优先用击点 id，缺失则派生稳定伪键。"""
    if isinstance(hit, dict):
        raw_id = hit.get("id")
        if raw_id is not None and str(raw_id).strip():
            return f"hold:id:{str(raw_id).strip()}"
    return f"hold:anon:{uuid.uuid4().hex[:12]}"


def hold_dirty_hit(conn, hit, reasons, batch_id: str, now: str) -> str:
    """把脏点写入 quarantine（搁置表挂 payload JSON 形态）。返回 hold_key。"""
    hold_key = make_hold_key(hit)
    conn.execute(
        """
        INSERT INTO quarantine(batch_id, hold_key, visitor_id, reason, member_json, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            batch_id,
            hold_key,
            hit.get("visitor_id") if isinstance(hit, dict) else None,
            "",
            json.dumps(hit, ensure_ascii=False),
            now,
        ),
    )
    return hold_key


def quarantine_columns(conn) -> set[str]:
    return {r["name"] for r in conn.execute("PRAGMA table_info(quarantine)").fetchall()}


def ensure_schema(conn):
    """对既有库做幂等加列（init_db 建表语句已含新列，此处兜底旧库）。"""
    cols = quarantine_columns(conn)
    if "batch_id" not in cols:
        conn.execute("ALTER TABLE quarantine ADD COLUMN batch_id TEXT")
    if "hold_key" not in cols:
        conn.execute("ALTER TABLE quarantine ADD COLUMN hold_key TEXT")


def refresh_hold_reasons(conn, new_reason: str = "unknown") -> int:
    """规则页保存后回写历史 hold 原因文案。"""
    cur = conn.execute(
        "UPDATE quarantine SET reason=? WHERE reason IS NULL OR reason='' OR reason LIKE '%缺%'",
        (new_reason,),
    )
    return cur.rowcount


def attestation(conn, batch_id: str | None = None) -> dict:
    """脏点核证（只读）：批次、搁置键、原因、正式写入数、末击点是否未漂。

    末击点未漂的判定：
    - 批次开始前快照每个 open 会话的 ended_at，存入批次报告 clock_before；
    - 核证时取每个会话正式 hits 的 MAX(event_ts) 作为合法末击点 clock_expected；
      当前 ended_at（clock_after）必须等于它，无正式成员的会话须停在批次前值；
    - 若脏行顶替过时钟，ended_at 会晚于正式成员最大时刻，clock_drifted 即真；
    - 另校验所有搁置原因均为必填字段缺失（脏点本就不该带合法时钟）。
    """
    if batch_id is None:
        row = conn.execute(
            "SELECT id, report_json FROM ingest_batches ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, report_json FROM ingest_batches WHERE id=?", (batch_id,)
        ).fetchone()
    if not row:
        return {"found": False}

    report = json.loads(row["report_json"] or "{}")
    held_rows = conn.execute(
        """
        SELECT id, hold_key, visitor_id, reason, member_json, created_at
        FROM quarantine WHERE batch_id=? ORDER BY id
        """,
        (row["id"],),
    ).fetchall()

    clock_before = report.get("clock_before", {})
    clock_after = {}
    clock_expected = {}
    for sid in clock_before:
        cur = conn.execute(
            "SELECT ended_at FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        clock_after[sid] = cur["ended_at"] if cur else None
        # 合法末击点 = 该会话正式成员的最大 event_ts；无正式成员则应停在批次前
        mx = conn.execute(
            "SELECT MAX(event_ts) AS m FROM hits WHERE session_id=?", (sid,)
        ).fetchone()["m"]
        clock_expected[sid] = mx if mx is not None else clock_before[sid]

    # 未漂 = 当前末击点恰由正式成员推出；脏行顶替会使 ended_at 晚于正式最大时刻
    clock_drifted = any(
        clock_after.get(sid) != expected for sid, expected in clock_expected.items()
    )
    expected_reasons = set(FIELD_LABELS.values())

    def _part_ok(part: str) -> bool:
        return part in expected_reasons or part.startswith("非对象击点")

    reasons_ok = all(
        all(_part_ok(part) for part in (r["reason"] or "").split(";") if part)
        for r in held_rows
    )

    return {
        "found": True,
        "batch_id": row["id"],
        "input": report.get("input"),
        "written": report.get("written"),
        "held": report.get("held"),
        "hold_keys": report.get("hold_keys", []),
        "held_rows": [dict(r) for r in held_rows],
        "clock_before": clock_before,
        "clock_after": clock_after,
        "clock_expected": clock_expected,
        "clock_drifted": clock_drifted,
        "clock_unchanged": not clock_drifted,
        "reasons_valid": reasons_ok,
        "passed": (not clock_drifted) and reasons_ok,
    }
