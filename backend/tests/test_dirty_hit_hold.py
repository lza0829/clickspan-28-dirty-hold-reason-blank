"""dirty_hit_hold 功能题测试：缺字段脏点搁置 + 末击点时钟不被污染。"""

import json

from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from app.modules.dirty_hit_hold import validate_hit
from app.services import ingest

client = TestClient(app)


# ---------- 纯函数：入口校验 ----------

def test_validate_hit_clean():
    hit = {
        "id": "H1",
        "visitor_id": "V-1",
        "event_ts": "2026-11-17T09:00:00Z",
        "page_path": "/home",
        "event_name": "view",
    }
    assert validate_hit(hit) == []


def test_validate_hit_detects_each_missing_field():
    base = {
        "visitor_id": "V-1",
        "event_ts": "2026-11-17T09:00:00Z",
        "page_path": "/home",
        "event_name": "view",
    }
    for field, label in [
        ("visitor_id", "缺 visitor_id"),
        ("event_ts", "缺 event_ts"),
        ("page_path", "缺 page_path"),
        ("event_name", "缺 event_name"),
    ]:
        bad = dict(base)
        bad[field] = None
        assert label in validate_hit(bad)
        bad[field] = "   "
        assert label in validate_hit(bad)
        del bad[field]
        assert label in validate_hit(bad)


def test_validate_hit_non_dict():
    reasons = validate_hit(["not", "a", "dict"])
    assert reasons and reasons[0].startswith("非对象击点")


# ---------- 端到端：夹具混脏行 ----------

def test_batch_holds_dirty_and_writes_clean():
    res = ingest.run_batch()
    assert res["ok"] is True
    report = res["report"]
    assert report["input"] == 9
    assert report["written"] == 4  # H6 H7 H8 H9
    assert report["held"] == 5     # D1..D5
    assert len(report["hold_keys"]) == 5

    conn = connect()
    try:
        held = conn.execute("SELECT * FROM quarantine ORDER BY id").fetchall()
        assert len(held) == 5
        for row in held:
            assert row["batch_id"] == "B-dirty-001"
            assert row["hold_key"].startswith("hold:id:")
            assert "缺" in row["reason"]
            payload = json.loads(row["member_json"])
            assert isinstance(payload, dict) and payload["id"].startswith("D")
    finally:
        conn.close()


def test_dirty_rows_never_enter_formal_hits():
    ingest.run_batch()
    conn = connect()
    try:
        dirty_ids = [
            r[0]
            for r in conn.execute(
                "SELECT json_extract(member_json, '$.id') FROM quarantine"
            ).fetchall()
        ]
        assert sorted(dirty_ids) == ["D1", "D2", "D3", "D4", "D5"]
        for did in dirty_ids:
            assert conn.execute(
                "SELECT 1 FROM hits WHERE id=?", (did,)
            ).fetchone() is None

        # 正式成员只含合法点
        ids = sorted(r["id"] for r in conn.execute("SELECT id FROM hits").fetchall())
        assert ids == ["H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9"]
    finally:
        conn.close()


def test_open_session_clock_not_drifted_by_dirty_hits():
    """脏行时刻 23:56-23:59 晚于所有合法点；末击点必须停在合法点的 09:55/10:00。"""
    conn = connect()
    before = {
        r["id"]: r["ended_at"]
        for r in conn.execute(
            "SELECT id, ended_at FROM sessions WHERE status='open'"
        ).fetchall()
    }
    conn.close()

    ingest.run_batch()

    conn = connect()
    try:
        v1001 = conn.execute(
            "SELECT ended_at, hit_count FROM sessions WHERE visitor_id='V-1001'"
        ).fetchone()
        v1002 = conn.execute(
            "SELECT id, ended_at, hit_count FROM sessions WHERE visitor_id='V-1002'"
        ).fetchone()
        # H6 09:50 / H7 09:51 / H9 09:55 为 V-1001 合法新增末击点
        assert v1001["ended_at"] == "2026-11-17T09:55:00Z"
        assert v1001["ended_at"] < "2026-11-17T23:00:00Z"
        # V-1002 的脏行全部被搁置，会话计数与时钟零变化
        assert v1002["ended_at"] == before[v1002["id"]]
        assert v1002["hit_count"] == 2
    finally:
        conn.close()


def test_hit_count_excludes_dirty_rows():
    ingest.run_batch()
    conn = connect()
    try:
        counts = {
            r["visitor_id"]: r["hit_count"]
            for r in conn.execute("SELECT visitor_id, hit_count FROM sessions").fetchall()
        }
        assert counts["V-1001"] == 6   # H1-H3 + H6,H7,H9
        assert counts["V-1002"] == 2   # H4,H5；D4,D5 不得计数
        assert counts["V-1003"] == 1   # H8 新访客新会话
    finally:
        conn.close()


# ---------- 失败闭：意外异常整趟回滚（搁置也不留） ----------

def test_unexpected_failure_rolls_back_everything(monkeypatch):
    conn = connect()
    conn.execute(
        """
        CREATE TRIGGER fail_on_h6 BEFORE INSERT ON hits
        WHEN NEW.id = 'H6'
        BEGIN SELECT RAISE(ABORT, 'injected failure'); END
        """
    )
    conn.commit()
    conn.close()

    res = ingest.run_batch()
    assert res["ok"] is False
    assert "injected failure" in res["error"]

    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM hits WHERE id='H6'").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM hits").fetchone()["c"] == 5
        assert conn.execute(
            "SELECT hit_count FROM sessions WHERE visitor_id='V-1001'"
        ).fetchone()["hit_count"] == 3
        assert conn.execute("SELECT COUNT(*) AS c FROM ingest_batches").fetchone()["c"] == 0
    finally:
        conn.close()


# ---------- 只读核证接口 ----------

def test_attestation_endpoint():
    ingest.run_batch()
    res = client.get("/api/dirty-hold/attestation?batch_id=B-dirty-001")
    assert res.status_code == 200
    data = res.json()
    assert data["batch_id"] == "B-dirty-001"
    assert data["input"] == 9
    assert data["written"] == 4
    assert data["held"] == 5
    assert len(data["hold_keys"]) == 5
    assert data["clock_unchanged"] is True
    assert data["clock_drifted"] is False
    assert data["reasons_valid"] is True
    assert data["passed"] is True
    assert len(data["held_rows"]) == 5
    for row in data["held_rows"]:
        json.loads(row["member_json"])  # payload 可展开


def test_attestation_defaults_to_latest_batch():
    ingest.run_batch()
    res = client.get("/api/dirty-hold/attestation")
    assert res.status_code == 200
    assert res.json()["batch_id"] == "B-dirty-001"


def test_attestation_unknown_batch_404():
    res = client.get("/api/dirty-hold/attestation?batch_id=nope")
    assert res.status_code == 404


def test_quarantine_endpoint_payload_and_filter():
    ingest.run_batch()
    res = client.get("/api/quarantine")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 5
    assert all(r["batch_id"] == "B-dirty-001" for r in rows)
    assert all(r["hold_key"].startswith("hold:id:") for r in rows)

    filtered = client.get("/api/quarantine?batch_id=B-dirty-001").json()
    assert len(filtered) == 5
    assert client.get("/api/quarantine?batch_id=none").json() == []


def test_hold_reasons_are_field_specific():
    ingest.run_batch()
    conn = connect()
    try:
        reasons = {
            json.loads(r["member_json"])["id"]: r["reason"]
            for r in conn.execute("SELECT reason, member_json FROM quarantine").fetchall()
        }
    finally:
        conn.close()
    assert reasons["D1"] == "缺 event_name"
    assert reasons["D2"] == "缺 page_path"
    assert reasons["D3"] == "缺 visitor_id"
    assert reasons["D4"] == "缺 event_ts"
    assert reasons["D5"] == "缺 event_name"


def test_attestation_flags_contaminated_clock():
    """反向：人为把末击点顶到脏行时刻（模拟污染），核证必须判漂移。"""
    ingest.run_batch()
    conn = connect()
    try:
        sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-1002' LIMIT 1"
        ).fetchone()["id"]
        conn.execute(
            "UPDATE sessions SET ended_at='2026-11-17T23:56:00Z' WHERE id=?", (sid,)
        )
        conn.commit()
    finally:
        conn.close()

    data = client.get("/api/dirty-hold/attestation?batch_id=B-dirty-001").json()
    assert data["clock_drifted"] is True
    assert data["clock_unchanged"] is False
    assert data["passed"] is False
    assert data["clock_after"][sid] == "2026-11-17T23:56:00Z"
    assert data["clock_expected"][sid] == "2026-11-17T08:21:30Z"


# ---------- 旧库迁移幂等 ----------

def test_ensure_schema_idempotent_on_legacy_table():
    from app.modules.dirty_hit_hold import ensure_schema

    conn = connect()
    try:
        ensure_schema(conn)  # 已含新列，不应报错
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(quarantine)")}
        assert {"batch_id", "hold_key", "member_json", "reason"} <= cols
    finally:
        conn.close()
