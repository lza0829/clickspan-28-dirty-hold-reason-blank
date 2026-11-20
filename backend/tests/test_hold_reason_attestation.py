"""hold 原因核证与规则版本不可变测试。

覆盖硬约束：
- 入 hold 成功必须固化非空 reason / reason_code 与当时规则版本快照，缺一回滚；
- 规则页改版只追加新版本并影响之后收流，绝不回溯改写已落 hold 的原因/版本；
- 核证页（reason-attestation）与搁置页（quarantine）同源，原因不可能分叉；
- 新增只读接口含脏键、reason、规则版本、结论四要素。
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from app.modules.dirty_hit_hold import (
    REQUIRED_FIELDS,
    current_rules,
    hold_dirty_hit,
    validate_hit_reasons,
)
from app.services import ingest

client = TestClient(app)


def _ingest_and_fetch_holds():
    res = ingest.run_batch()
    assert res["ok"] is True
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM quarantine WHERE batch_id='B-dirty-001' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return res, rows


# ---------- 场景一：夹具缺字段收流后，原因/原因码/版本快照全部非空固化 ----------

def test_hold_rows_freeze_nonempty_reason_and_rule_version():
    _, rows = _ingest_and_fetch_holds()
    assert len(rows) == 5
    for r in rows:
        assert r["reason"] and r["reason"].strip()
        assert r["reason_code"] and r["reason_code"].strip()
        assert r["rule_version"] == "dirty-rules-v1"
        snap = json.loads(r["rule_snapshot_json"])
        assert snap["version"] == "dirty-rules-v1"
        assert snap["required_fields"] == list(REQUIRED_FIELDS)
        # 原因码与原因文案一一对应
        codes = r["reason_code"].split(";")
        labels = r["reason"].split(";")
        assert len(codes) == len(labels) and codes
        for code in codes:
            assert code.startswith("MISSING_") or code == "NON_OBJECT_HIT"


def test_batch_report_freezes_rule_version():
    res, _ = _ingest_and_fetch_holds()
    assert res["report"]["rule_version"] == "dirty-rules-v1"


def test_hold_reason_codes_are_field_specific():
    _, rows = _ingest_and_fetch_holds()
    by_dirty = {
        json.loads(r["member_json"])["id"]: (r["reason"], r["reason_code"]) for r in rows
    }
    assert by_dirty["D1"] == ("缺 event_name", "MISSING_EVENT_NAME")
    assert by_dirty["D2"] == ("缺 page_path", "MISSING_PAGE_PATH")
    assert by_dirty["D3"] == ("缺 visitor_id", "MISSING_VISITOR_ID")
    assert by_dirty["D4"] == ("缺 event_ts", "MISSING_EVENT_TS")
    assert by_dirty["D5"] == ("缺 event_name", "MISSING_EVENT_NAME")


# ---------- 场景二：改规则后，旧 hold 原因与版本不变，新规则只管之后收流 ----------

def test_rule_change_does_not_rewrite_existing_holds():
    _, before = _ingest_and_fetch_holds()
    frozen = [
        (r["hold_key"], r["reason"], r["reason_code"], r["rule_version"], r["rule_snapshot_json"])
        for r in before
    ]

    # 规则页发布 v2：收窄必填清单（只保留 visitor_id / event_ts）
    res = client.put(
        "/api/dirty-hold/rules",
        json={"required_fields": ["visitor_id", "event_ts"], "note": "收窄清单"},
    )
    assert res.status_code == 200
    assert res.json()["current_version"] == "dirty-rules-v2"

    # 旧 hold 行原因、原因码、版本、快照原样不动
    conn = connect()
    try:
        after = conn.execute(
            "SELECT hold_key, reason, reason_code, rule_version, rule_snapshot_json "
            "FROM quarantine WHERE batch_id='B-dirty-001' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    after_tuples = [tuple(r) for r in after]
    assert after_tuples == frozen
    assert all(r["rule_version"] == "dirty-rules-v1" for r in after)

    # 当前生效版本已切到 v2，之后收流按 v2 判定
    conn = connect()
    try:
        assert current_rules(conn)["version"] == "dirty-rules-v2"
        # D1 仅缺 event_name：v2 下不再是脏点（夹具批次号固定，直接验纯函数）
        d1 = {
            "id": "D1", "visitor_id": "V-1001",
            "event_ts": "2026-11-17T23:59:00Z",
            "page_path": "/dirty-no-event", "event_name": None,
        }
        assert validate_hit_reasons(d1, ("visitor_id", "event_ts")) == []
        d3 = {
            "id": "D3", "visitor_id": None,
            "event_ts": "2026-11-17T23:57:00Z",
            "page_path": "/x", "event_name": "click",
        }
        assert [x["code"] for x in validate_hit_reasons(d3, ("visitor_id", "event_ts"))] == [
            "MISSING_VISITOR_ID"
        ]
    finally:
        conn.close()


def test_new_ingest_after_rule_change_uses_new_version_without_touching_old_rows():
    from app.db import FIXTURES_DIR

    _ingest_and_fetch_holds()
    # 规则页发布 v2：只卡 event_name
    pub = client.put(
        "/api/dirty-hold/rules",
        json={"required_fields": ["event_name"], "note": "只卡事件名"},
    )
    assert pub.status_code == 200
    assert pub.json()["current_version"] == "dirty-rules-v2"

    # 用独立批次的临时夹具跑 v2 收流（不重放结构残缺的旧脏行）
    custom = {
        "batch_id": "B-v2-001",
        "hits": [
            {
                "id": "C1", "visitor_id": "V-1001",
                "event_ts": "2026-11-18T09:00:00Z",
                "page_path": "/new", "event_name": "view",
            },
            {
                "id": "E1", "visitor_id": "V-1001",
                "event_ts": "2026-11-18T09:01:00Z",
                "page_path": "/no-name", "event_name": None,
            },
            {
                # page_path 空白但 v2 不卡它：合法点，空白串可入 NOT NULL 列
                "id": "E2", "visitor_id": "V-1001",
                "event_ts": "2026-11-18T09:02:00Z",
                "page_path": "   ", "event_name": "click",
            },
        ],
    }
    fixture_path = FIXTURES_DIR / "ingest_batch.json"
    original = fixture_path.read_text(encoding="utf-8")
    fixture_path.write_text(json.dumps(custom, ensure_ascii=False), encoding="utf-8")
    try:
        res = ingest.run_batch()
    finally:
        fixture_path.write_text(original, encoding="utf-8")
    assert res["ok"] is True
    assert res["report"]["rule_version"] == "dirty-rules-v2"
    assert res["report"]["held"] == 1       # 仅 E1 缺 event_name
    assert res["report"]["written"] == 2    # C1、E2

    conn = connect()
    try:
        new_rows = conn.execute(
            "SELECT reason_code, rule_version FROM quarantine "
            "WHERE batch_id='B-v2-001' ORDER BY id"
        ).fetchall()
        assert len(new_rows) == 1
        assert new_rows[0]["reason_code"] == "MISSING_EVENT_NAME"
        assert new_rows[0]["rule_version"] == "dirty-rules-v2"
        # 旧 hold 仍全部钉在 v1，原因与版本零改动
        v1 = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine WHERE rule_version='dirty-rules-v1'"
        ).fetchone()["c"]
        assert v1 == 5
        versions = [
            r["version"]
            for r in conn.execute("SELECT version FROM dirty_rule_versions ORDER BY seq").fetchall()
        ]
        assert versions == ["dirty-rules-v1", "dirty-rules-v2"]
    finally:
        conn.close()


# ---------- DB 层硬约束：空原因拒绝落库，固化列禁止 UPDATE ----------

def test_db_rejects_hold_with_empty_reason():
    conn = connect()
    try:
        with pytest.raises(Exception):
            conn.execute(
                """
                INSERT INTO quarantine(
                  batch_id, hold_key, visitor_id, reason, reason_code,
                  rule_version, rule_snapshot_json, member_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                ("B-x", "hold:id:X", None, "", "MISSING_EVENT_NAME",
                 "dirty-rules-v1", "{}", "{}", "2026-11-17T00:00:00Z"),
            )
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"] == 0
    finally:
        conn.close()


def test_db_rejects_hold_with_empty_rule_version():
    conn = connect()
    try:
        with pytest.raises(Exception):
            conn.execute(
                """
                INSERT INTO quarantine(
                  batch_id, hold_key, visitor_id, reason, reason_code,
                  rule_version, rule_snapshot_json, member_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                ("B-x", "hold:id:X", None, "缺 event_name", "MISSING_EVENT_NAME",
                 "", "{}", "{}", "2026-11-17T00:00:00Z"),
            )
        conn.rollback()
    finally:
        conn.close()


def test_db_blocks_rewrite_of_frozen_hold_columns():
    _ingest_and_fetch_holds()
    conn = connect()
    try:
        with pytest.raises(Exception):
            conn.execute("UPDATE quarantine SET reason='unknown'")
        conn.rollback()
        with pytest.raises(Exception):
            conn.execute(
                "UPDATE quarantine SET rule_version='dirty-rules-v2'"
            )
        conn.rollback()
        with pytest.raises(Exception):
            conn.execute("UPDATE quarantine SET reason_code='MISSING_PAGE_PATH'")
        conn.rollback()
        with pytest.raises(Exception):
            conn.execute("UPDATE quarantine SET rule_snapshot_json='{}'")
        conn.rollback()
        # 非固化列不在禁止范围（只锁原因与版本快照）
        conn.execute("UPDATE quarantine SET hold_key=hold_key")
        conn.commit()
        # 原值原样
        row = conn.execute(
            "SELECT reason, reason_code, rule_version FROM quarantine ORDER BY id LIMIT 1"
        ).fetchone()
        assert row["reason"] == "缺 event_name"
        assert row["reason_code"] == "MISSING_EVENT_NAME"
        assert row["rule_version"] == "dirty-rules-v1"
    finally:
        conn.close()


def test_hold_dirty_hit_application_layer_guards_blank():
    conn = connect()
    try:
        rules = current_rules(conn)
        bad_reasons = [{"field": "event_name", "code": "", "label": "缺 event_name"}]
        with pytest.raises(ValueError):
            hold_dirty_hit(conn, {"id": "Z1"}, bad_reasons, "B-z", "2026-11-17T00:00:00Z", rules)
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"] == 0
    finally:
        conn.close()


def test_publish_rules_rejects_empty_or_unknown_fields():
    bad = client.put("/api/dirty-hold/rules", json={"required_fields": []})
    assert bad.status_code == 400
    bad2 = client.put("/api/dirty-hold/rules", json={"required_fields": ["not_a_field"]})
    assert bad2.status_code == 400


# ---------- 新增只读核证接口：脏键 / reason / 规则版本 / 结论 ----------

def test_reason_attestation_endpoint_shape_and_pass():
    _ingest_and_fetch_holds()
    res = client.get("/api/dirty-hold/reason-attestation?batch_id=B-dirty-001")
    assert res.status_code == 200
    data = res.json()
    assert data["passed"] is True
    assert data["held_count"] == 5
    dirty_keys = {it["dirty_key"] for it in data["items"]}
    assert dirty_keys == {"D1", "D2", "D3", "D4", "D5"}
    for it in data["items"]:
        # 四要素齐全
        assert it["dirty_key"] and it["reason"] and it["rule_version"] and it["conclusion"]
        assert it["rule_version"] == "dirty-rules-v1"
        assert it["reason_nonempty"] is True
        assert it["version_known"] is True
        assert it["snapshot_match"] is True
        assert it["reason_codes_known"] is True
        assert it["passed"] is True
        assert it["conclusion"] == "通过"
    # 版本对照可核
    versions = {v["version"] for v in data["rule_versions"]}
    assert "dirty-rules-v1" in versions


def test_reason_attestation_defaults_latest_and_404():
    _ingest_and_fetch_holds()
    assert client.get("/api/dirty-hold/reason-attestation").json()["batch_id"] == "B-dirty-001"
    assert client.get("/api/dirty-hold/reason-attestation?batch_id=nope").status_code == 404


def test_attestation_and_quarantine_share_same_frozen_source():
    """核证页原因与 hold 行不得分叉：两端读同一固化列。"""
    _ingest_and_fetch_holds()
    q = {r["id"]: r for r in client.get("/api/quarantine").json()}
    att = client.get("/api/dirty-hold/attestation?batch_id=B-dirty-001").json()
    reason_att = client.get(
        "/api/dirty-hold/reason-attestation?batch_id=B-dirty-001"
    ).json()
    assert att["reason_versions_ok"] is True
    assert att["passed"] is True
    att_by_id = {r["id"]: r for r in att["held_rows"]}
    assert set(q) == set(att_by_id)
    for qid, qrow in q.items():
        arow = att_by_id[qid]
        assert arow["reason"] == qrow["reason"]
        assert arow["reason_code"] == qrow["reason_code"]
        assert arow["rule_version"] == qrow["rule_version"]
        assert arow["rule_snapshot_json"] == qrow["rule_snapshot_json"]
    # reason-attestation 的结论与主核证一致
    assert reason_att["passed"] == att["reason_versions_ok"]


def test_rules_endpoint_lists_versions_and_current():
    res = client.get("/api/dirty-hold/rules")
    assert res.status_code == 200
    data = res.json()
    assert data["current_version"] == "dirty-rules-v1"
    v1 = next(v for v in data["versions"] if v["version"] == "dirty-rules-v1")
    assert set(v1["required_fields"]) == set(REQUIRED_FIELDS)
    assert v1["reason_codes"]["event_name"]["code"] == "MISSING_EVENT_NAME"
