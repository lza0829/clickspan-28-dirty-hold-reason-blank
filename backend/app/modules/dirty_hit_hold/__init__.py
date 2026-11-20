"""缺字段脏点搁置（dirty_hit_hold）。

路径选择（题目二选一）：
- 落库形态：**hold 行不可变 reason + 规则版本快照列** —— quarantine 每行在入 hold
  同事务写死 reason（原因文案）、reason_code（原因码）、rule_version（当时规则版本号）
  与 rule_snapshot_json（当时规则快照）；另有 dirty_rule_versions 规则版本登记表
  （版本 ↔ 原因码对照，可核），不使用 hits.hold 标记。
- 失败闭：入口校验分流 —— 收流入口逐点按**当前规则版本**的必填字段校验，脏点即剔入
  搁置，只有合法点进入正式成员与末击点时钟；整批单事务，意外异常（含原因/版本缺失）
  整趟回滚。即「入 hold 同事务写死原因与版本并写对照，缺一回滚」。

不变量（硬约束）：
- 缺 visitor_id / event_ts / page_path / event_name 任一（缺失、null、空白串）
  的击点必须进搁置，hold 成功即固化非空 reason 与 reason_code 及当时规则版本快照，
  缺一不可，数据库 CHECK/触发器与应用层双拦，缺一则整事务回滚；
- hold 行的 reason / reason_code / rule_version / rule_snapshot_json 不可变：
  触发器禁止 UPDATE 改写；规则页发布新版本只写新登记行并切换当前版本指针，
  只影响之后收流，绝不回溯旧 hold；
- 核证只读 hold 行固化值并与 dirty_rule_versions 登记快照逐行对照，
  原因与 hold 行同源，不可能分叉；
- 脏点不得进正式 hits、不得改 sessions.hit_count、不得更新 open 会话末击点。
"""

import json
import uuid
from datetime import datetime, timezone

STATUS = "implemented"

REQUIRED_FIELDS = ("visitor_id", "event_ts", "page_path", "event_name")
FIELD_LABELS = {
    "visitor_id": "缺 visitor_id",
    "event_ts": "缺 event_ts",
    "page_path": "缺 page_path",
    "event_name": "缺 event_name",
}
# 原因码：规则版本内稳定，供核证与跨版本对照
REASON_CODES = {
    "visitor_id": "MISSING_VISITOR_ID",
    "event_ts": "MISSING_EVENT_TS",
    "page_path": "MISSING_PAGE_PATH",
    "event_name": "MISSING_EVENT_NAME",
}
NON_OBJECT_CODE = "NON_OBJECT_HIT"

RULE_SETTING_KEY = "dirty_rule_version"
INITIAL_VERSION = "dirty-rules-v1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 纯函数：入口校验 ----------

def validate_hit_reasons(hit, required_fields=REQUIRED_FIELDS) -> list[dict]:
    """按给定规则版本的必填字段校验，返回编码后的原因列表；空列表表示合法点。

    缺失、null、空串或纯空白均判脏，避免空字符串绕过 NOT NULL 语义
    （SQLite 对 '' 不做 NOT NULL 拦截）。
    每项形如 {"field": "event_name", "code": "MISSING_EVENT_NAME", "label": "缺 event_name"}；
    非对象击点 field 为 None。
    """
    if not isinstance(hit, dict):
        return [
            {
                "field": None,
                "code": NON_OBJECT_CODE,
                "label": f"非对象击点: {type(hit).__name__}",
            }
        ]
    reasons = []
    for field in required_fields:
        value = hit.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            reasons.append(
                {"field": field, "code": REASON_CODES[field], "label": FIELD_LABELS[field]}
            )
    return reasons


def validate_hit(hit) -> list[str]:
    """基线接口：返回缺失原因文案列表；空列表表示合法点。"""
    return [r["label"] for r in validate_hit_reasons(hit)]


def make_hold_key(hit) -> str:
    """搁置键：优先用击点 id，缺失则派生稳定伪键。"""
    if isinstance(hit, dict):
        raw_id = hit.get("id")
        if raw_id is not None and str(raw_id).strip():
            return f"hold:id:{str(raw_id).strip()}"
    return f"hold:anon:{uuid.uuid4().hex[:12]}"


# ---------- 规则版本登记（版本 ↔ 原因码对照） ----------

def build_rule_snapshot(version: str, seq: int, required_fields, now: str, note: str) -> dict:
    """构造规则版本快照：版本号、序号、必填字段及各字段原因码/文案对照。"""
    return {
        "version": version,
        "seq": seq,
        "required_fields": list(required_fields),
        "reason_codes": {
            field: {"code": REASON_CODES[field], "label": FIELD_LABELS[field]}
            for field in required_fields
        },
        "created_at": now,
        "note": note or "",
    }


def _rules_row_to_dict(row) -> dict:
    snapshot = json.loads(row["snapshot_json"])
    return {
        "version": row["version"],
        "seq": row["seq"],
        "required_fields": snapshot["required_fields"],
        "reason_codes": snapshot["reason_codes"],
        "created_at": row["created_at"],
        "note": snapshot.get("note", ""),
        "snapshot": snapshot,
    }


def ensure_initial_rules(conn, now: str | None = None) -> dict:
    """幂等写入基线规则版本 v1 并置为当前版本（启动时调用）。"""
    row = conn.execute(
        "SELECT version FROM dirty_rule_versions WHERE version=?", (INITIAL_VERSION,)
    ).fetchone()
    if row:
        return current_rules(conn)
    ts = now or _now()
    snapshot = build_rule_snapshot(INITIAL_VERSION, 1, REQUIRED_FIELDS, ts, "基线缺字段规则")
    conn.execute(
        """
        INSERT INTO dirty_rule_versions(version, seq, snapshot_json, created_at, note)
        VALUES (?,?,?,?,?)
        """,
        (
            INITIAL_VERSION,
            1,
            json.dumps(snapshot, ensure_ascii=False),
            ts,
            snapshot["note"],
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
        (RULE_SETTING_KEY, INITIAL_VERSION),
    )
    return _rules_row_to_dict(
        conn.execute(
            "SELECT * FROM dirty_rule_versions WHERE version=?", (INITIAL_VERSION,)
        ).fetchone()
    )


def current_rules(conn) -> dict:
    """读当前生效规则版本（settings 指针 → 登记表快照）。"""
    pointer = conn.execute(
        "SELECT value FROM settings WHERE key=?", (RULE_SETTING_KEY,)
    ).fetchone()
    if not pointer:
        return ensure_initial_rules(conn)
    row = conn.execute(
        "SELECT * FROM dirty_rule_versions WHERE version=?", (pointer["value"],)
    ).fetchone()
    if not row:
        return ensure_initial_rules(conn)
    return _rules_row_to_dict(row)


def list_rule_versions(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM dirty_rule_versions ORDER BY seq DESC"
    ).fetchall()
    return [_rules_row_to_dict(r) for r in rows]


def publish_rules(conn, required_fields, note: str = "", now: str | None = None) -> dict:
    """发布新规则版本：只登记新版本并切换当前指针，不触碰任何已落 hold 行。

    之后收流按新版本校验；旧 hold 的原因与版本快照由触发器保证不可回溯改写。
    """
    canonical = tuple(REQUIRED_FIELDS)
    wanted = list(required_fields or [])
    unknown = [f for f in wanted if f not in canonical]
    if unknown:
        raise ValueError(f"未知必填字段: {unknown}")
    fields = [f for f in canonical if f in set(wanted)]
    if not fields:
        raise ValueError("至少保留一个必填字段，规则清单不能为空")

    last_seq = conn.execute(
        "SELECT MAX(seq) AS m FROM dirty_rule_versions"
    ).fetchone()["m"] or 0
    seq = int(last_seq) + 1
    version = f"dirty-rules-v{seq}"
    ts = now or _now()
    snapshot = build_rule_snapshot(version, seq, fields, ts, note)
    conn.execute(
        """
        INSERT INTO dirty_rule_versions(version, seq, snapshot_json, created_at, note)
        VALUES (?,?,?,?,?)
        """,
        (
            version,
            seq,
            json.dumps(snapshot, ensure_ascii=False),
            ts,
            snapshot["note"],
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
        (RULE_SETTING_KEY, version),
    )
    return _rules_row_to_dict(
        conn.execute(
            "SELECT * FROM dirty_rule_versions WHERE version=?", (version,)
        ).fetchone()
    )


# ---------- hold 物化：同事务写死原因 + 版本快照，缺一回滚 ----------

def hold_dirty_hit(conn, hit, reasons: list[dict], batch_id: str, now: str, rules: dict) -> str:
    """把脏点写入 quarantine（hold 行不可变 reason + 规则版本快照列形态）。

    原因文案、原因码、规则版本号、规则快照全部在本事务内固化；
    任一为空即抛错，由收流整事务回滚（DB 触发器/CHECK 兜底）。返回 hold_key。
    """
    reason_text = ";".join(r["label"] for r in reasons)
    reason_code = ";".join(r["code"] for r in reasons)
    rule_version = rules["version"]
    rule_snapshot_json = json.dumps(rules["snapshot"], ensure_ascii=False)

    # 应用层硬约束：禁止 hold 成功而 reason/版本为空 —— 缺一回滚
    if (
        not reason_text.strip()
        or not reason_code.strip()
        or not rule_version.strip()
        or not rule_snapshot_json.strip()
    ):
        raise ValueError("hold 行必须固化非空 reason_code 与规则版本快照，缺一则拒绝入 hold")

    hold_key = make_hold_key(hit)
    conn.execute(
        """
        INSERT INTO quarantine(
          batch_id, hold_key, visitor_id, reason, reason_code,
          rule_version, rule_snapshot_json, member_json, created_at
        )
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            hold_key,
            hit.get("visitor_id") if isinstance(hit, dict) else None,
            reason_text,
            reason_code,
            rule_version,
            rule_snapshot_json,
            json.dumps(hit, ensure_ascii=False),
            now,
        ),
    )
    return hold_key


def quarantine_columns(conn) -> set[str]:
    return {r["name"] for r in conn.execute("PRAGMA table_info(quarantine)").fetchall()}


# 不可变触发器：旧库与新库统一靠它兜底（建表语句对新库另带 CHECK）
_IMMUTABLE_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS trg_quarantine_hold_reason_not_null_insert
    BEFORE INSERT ON quarantine
    BEGIN
      SELECT CASE
        WHEN NEW.reason IS NULL OR trim(NEW.reason) = ''
          OR NEW.reason_code IS NULL OR trim(NEW.reason_code) = ''
          OR NEW.rule_version IS NULL OR trim(NEW.rule_version) = ''
          OR NEW.rule_snapshot_json IS NULL OR trim(NEW.rule_snapshot_json) = ''
        THEN RAISE(ABORT, 'hold 行必须固化非空 reason_code 与规则版本快照，缺一则回滚')
      END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_quarantine_hold_immutable_update
    BEFORE UPDATE ON quarantine
    BEGIN
      SELECT CASE
        WHEN COALESCE(OLD.reason, '') <> COALESCE(NEW.reason, '')
          OR COALESCE(OLD.reason_code, '') <> COALESCE(NEW.reason_code, '')
          OR COALESCE(OLD.rule_version, '') <> COALESCE(NEW.rule_version, '')
          OR COALESCE(OLD.rule_snapshot_json, '') <> COALESCE(NEW.rule_snapshot_json, '')
        THEN RAISE(ABORT, 'hold 原因与规则版本快照不可变，禁止回溯改写')
      END;
    END
    """,
)


def ensure_schema(conn):
    """对既有库做幂等加列 + 规则登记表 + 不可变触发器（init_db 建表语句已含新列，此处兜底旧库）。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dirty_rule_versions (
          version TEXT PRIMARY KEY,
          seq INTEGER NOT NULL UNIQUE,
          snapshot_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cols = quarantine_columns(conn)
    if "batch_id" not in cols:
        conn.execute("ALTER TABLE quarantine ADD COLUMN batch_id TEXT")
    if "hold_key" not in cols:
        conn.execute("ALTER TABLE quarantine ADD COLUMN hold_key TEXT")
    if "reason_code" not in cols:
        conn.execute(
            "ALTER TABLE quarantine ADD COLUMN reason_code TEXT NOT NULL DEFAULT ''"
        )
    if "rule_version" not in cols:
        conn.execute(
            "ALTER TABLE quarantine ADD COLUMN rule_version TEXT NOT NULL DEFAULT ''"
        )
    if "rule_snapshot_json" not in cols:
        conn.execute(
            "ALTER TABLE quarantine ADD COLUMN rule_snapshot_json TEXT NOT NULL DEFAULT ''"
        )
    for stmt in _IMMUTABLE_TRIGGERS:
        conn.execute(stmt)


# ---------- 只读核证 ----------

def _load_batch(conn, batch_id: str | None):
    if batch_id is None:
        return conn.execute(
            "SELECT id, report_json FROM ingest_batches ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return conn.execute(
        "SELECT id, report_json FROM ingest_batches WHERE id=?", (batch_id,)
    ).fetchone()


def _version_catalog(conn) -> dict:
    return {
        r["version"]: json.loads(r["snapshot_json"])
        for r in conn.execute("SELECT version, snapshot_json FROM dirty_rule_versions").fetchall()
    }


_HOLD_SELECT = """
    SELECT id, batch_id, hold_key, visitor_id, reason, reason_code,
           rule_version, rule_snapshot_json, member_json, created_at
    FROM quarantine WHERE batch_id=? ORDER BY id
"""


def _row_reason_checks(row, catalog: dict) -> dict:
    """逐行核：原因非空、版本可查、快照与登记一致、原因码在该版本对照内。"""
    reason = row["reason"] or ""
    reason_code = row["reason_code"] or ""
    rule_version = row["rule_version"] or ""
    reason_nonempty = bool(reason.strip()) and bool(reason_code.strip())
    version_known = rule_version in catalog
    snapshot_match = False
    codes_known = False
    if version_known:
        try:
            frozen = json.loads(row["rule_snapshot_json"] or "{}")
        except json.JSONDecodeError:
            frozen = {}
        snapshot_match = frozen == catalog[rule_version]
        valid_codes = {v["code"] for v in catalog[rule_version].get("reason_codes", {}).values()}
        valid_codes.add(NON_OBJECT_CODE)
        codes = [c for c in reason_code.split(";") if c]
        codes_known = bool(codes) and all(c in valid_codes for c in codes)
    ok = reason_nonempty and version_known and snapshot_match and codes_known
    return {
        "reason_nonempty": reason_nonempty,
        "version_known": version_known,
        "snapshot_match": snapshot_match,
        "reason_codes_known": codes_known,
        "conclusion": "通过" if ok else "不通过：原因/版本快照核证失败",
        "passed": ok,
    }


def reason_attestation(conn, batch_id: str | None = None) -> dict:
    """hold 原因核证（只读，基线所无的新接口数据源）。

    每行给出：脏键（hold_key/载荷 id）、reason、reason_code、rule_version、
    规则快照对照结果与结论；整体 passed 要求逐行通过且无空原因。
    """
    row = _load_batch(conn, batch_id)
    if not row:
        return {"found": False}
    catalog = _version_catalog(conn)
    held_rows = conn.execute(_HOLD_SELECT, (row["id"],)).fetchall()

    items = []
    for r in held_rows:
        try:
            payload = json.loads(r["member_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        dirty_key = payload.get("id") or r["hold_key"]
        checks = _row_reason_checks(r, catalog)
        items.append(
            {
                "hold_key": r["hold_key"],
                "dirty_key": dirty_key,
                "reason": r["reason"],
                "reason_code": r["reason_code"],
                "rule_version": r["rule_version"],
                "rule_snapshot": json.loads(r["rule_snapshot_json"] or "{}"),
                "created_at": r["created_at"],
                **checks,
            }
        )
    # 无搁置行时原因核证真空为真（干净批次不该被判不通过）
    passed = all(i["passed"] for i in items)
    return {
        "found": True,
        "batch_id": row["id"],
        "rule_versions": list_rule_versions(conn),
        "held_count": len(items),
        "items": items,
        "passed": passed,
    }


def attestation(conn, batch_id: str | None = None) -> dict:
    """脏点核证（只读）：批次、搁置键、固化原因/版本、正式写入数、末击点是否未漂。

    末击点未漂的判定：
    - 批次开始前快照每个 open 会话的 ended_at，存入批次报告 clock_before；
    - 核证时取每个会话正式 hits 的 MAX(event_ts) 作为合法末击点 clock_expected；
      当前 ended_at（clock_after）必须等于它，无正式成员的会话须停在批次前值；
    - 若脏行顶替过时钟，ended_at 会晚于正式成员最大时刻，clock_drifted 即真；
    - 原因核证直接读 hold 行固化值并与规则版本登记快照对照，与搁置页同源不分叉。
    """
    row = _load_batch(conn, batch_id)
    if not row:
        return {"found": False}

    report = json.loads(row["report_json"] or "{}")
    held_rows = conn.execute(_HOLD_SELECT, (row["id"],)).fetchall()
    catalog = _version_catalog(conn)

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

    # 原因合法性以各行**固化时的规则快照**为准（规则改版不影响旧 hold 判定）
    def _part_ok(held_row, part: str) -> bool:
        snap = catalog.get(held_row["rule_version"])
        if snap:
            valid_labels = {v["label"] for v in snap.get("reason_codes", {}).values()}
            if part in valid_labels:
                return True
        return part.startswith("非对象击点")

    reasons_ok = all(
        bool((r["reason"] or "").strip())
        and all(_part_ok(r, part) for part in (r["reason"] or "").split(";") if part)
        for r in held_rows
    )
    reason_audit = reason_attestation(conn, row["id"])
    reason_versions_ok = reason_audit["passed"]

    return {
        "found": True,
        "batch_id": row["id"],
        "input": report.get("input"),
        "written": report.get("written"),
        "held": report.get("held"),
        "hold_keys": report.get("hold_keys", []),
        "rule_version": report.get("rule_version"),
        "held_rows": [dict(r) for r in held_rows],
        "reason_items": reason_audit["items"],
        "clock_before": clock_before,
        "clock_after": clock_after,
        "clock_expected": clock_expected,
        "clock_drifted": clock_drifted,
        "clock_unchanged": not clock_drifted,
        "reasons_valid": reasons_ok,
        "reason_versions_ok": reason_versions_ok,
        "passed": (not clock_drifted) and reasons_ok and reason_versions_ok,
    }
