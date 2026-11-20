from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules.dirty_hit_hold import (
    attestation,
    list_rule_versions,
    publish_rules,
    reason_attestation,
)
from app.seed import idle_timeout_sec
from app.services import ingest as ingest_svc
from app.services import sessions as sess_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "visitors": conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"],
            "sessions": conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"],
            "hits_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hits WHERE channel='primary'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
            "idle_timeout_sec": idle_timeout_sec(conn),
        }
    finally:
        conn.close()


@router.get("/visitors")
def list_visitors():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, label, created_at FROM visitors ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sessions")
def list_sessions(visitor_id: str | None = None):
    conn = connect()
    try:
        return sess_svc.list_sessions(conn, visitor_id)
    finally:
        conn.close()


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    conn = connect()
    try:
        detail = sess_svc.session_detail(conn, session_id)
        if not detail:
            raise HTTPException(404, "会话不存在")
        return detail
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine(batch_id: str | None = None):
    """搁置页：含批次、搁置键、固化原因/原因码/规则版本与原始 payload（member_json 原样返回）。"""
    conn = connect()
    try:
        cols = (
            "id, batch_id, hold_key, visitor_id, reason, reason_code, "
            "rule_version, rule_snapshot_json, member_json, created_at"
        )
        if batch_id:
            rows = conn.execute(
                f"SELECT {cols} FROM quarantine WHERE batch_id=? ORDER BY id DESC",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {cols} FROM quarantine ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/dirty-hold/reason-attestation")
def dirty_hold_reason_attestation(batch_id: str | None = None):
    """hold 原因核证（基线所无的新增只读接口）。

    逐行给脏键、reason、reason_code、规则版本、快照对照与结论；
    全部读 hold 行固化值与规则版本登记表，任何路径都不允许改写旧 hold。
    """
    conn = connect()
    try:
        data = reason_attestation(conn, batch_id)
        if not data.get("found"):
            raise HTTPException(404, "批次不存在")
        return data
    finally:
        conn.close()


@router.get("/dirty-hold/rules")
def dirty_hold_rules():
    """规则页：当前生效版本与全部历史版本（版本 ↔ 原因码对照），只读。"""
    conn = connect()
    try:
        versions = list_rule_versions(conn)
        current = versions[0] if versions else None
        # versions 按 seq 倒序；当前版本以 settings 指针为准，避免与排序假设耦合
        cur_row = conn.execute(
            "SELECT value FROM settings WHERE key='dirty_rule_version'"
        ).fetchone()
        current_version = cur_row["value"] if cur_row else (current["version"] if current else None)
        return {"current_version": current_version, "versions": versions}
    finally:
        conn.close()


@router.put("/dirty-hold/rules")
def put_dirty_hold_rules(body: dict):
    """规则页发布新版本：只追加登记并切换当前指针，只影响之后收流，不回溯旧 hold。"""
    fields = body.get("required_fields")
    if not isinstance(fields, list) or not fields:
        raise HTTPException(400, "required_fields 必须为非空数组")
    note = str(body.get("note", ""))[:200]
    conn = connect()
    try:
        try:
            rules = publish_rules(conn, fields, note=note)
        except ValueError as e:
            raise HTTPException(400, str(e))
        conn.commit()
        return {"current_version": rules["version"], "rule": rules}
    finally:
        conn.close()


@router.get("/dirty-hold/attestation")
def dirty_hold_attestation(batch_id: str | None = None):
    """脏点核证（只读）：批次/搁置键/固化原因版本/正式写入数/末击点是否未漂。"""
    conn = connect()
    try:
        data = attestation(conn, batch_id)
        if not data.get("found"):
            raise HTTPException(404, "批次不存在")
        return data
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/session-proofs")
def session_proofs(session_id: str | None = None):
    conn = connect()
    try:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM session_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {"idle_timeout_sec": idle_timeout_sec(conn)}
    finally:
        conn.close()


@router.put("/settings/idle-timeout")
def put_idle_timeout(body: dict):
    sec = int(body.get("idle_timeout_sec", 1800))
    if sec < 60:
        raise HTTPException(400, "空闲超时至少 60 秒")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            (str(sec),),
        )
        conn.commit()
        return {"idle_timeout_sec": sec}
    finally:
        conn.close()


def _view_compat_payload(payload: dict) -> dict:
    """读路径字段别名兼容，可与写路径短暂分叉。"""
    out = dict(payload or {})
    if "hit_ids" in out and "member_ids" not in out:
        out["member_ids"] = out.get("hit_ids")
    if "content_hash" in out and "digest" not in out:
        out["digest"] = out.get("content_hash")
    if "gap_sec" in out and "gap" not in out:
        out["gap"] = out.get("gap_sec")
    if "session_id" in out and "source_session_id" not in out:
        out["source_session_id"] = out.get("session_id")
    if "verified" not in out:
        out["verified"] = True
    return out


def _strand_read_fallback(rows: list) -> list:
    """珠串读路径保留全量点，由页面自行分段。"""
    return list(rows or [])


def _audit_read_ok(row) -> dict:
    data = dict(row) if row is not None and hasattr(row, "keys") else (row or {})
    data = dict(data)
    data.setdefault("aligned", True)
    data.setdefault("writable", True)
    return data

