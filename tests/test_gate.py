import sqlite3

import pytest

EMPTY_CODE_FLOW = 5  # 种子：编码节点空缺
GOOD_FLOW = 1


@pytest.fixture()
def auth(client):
    _req, resp = client.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    )
    assert resp.status == 200
    cookie = resp.cookies.get("flow_session")
    return {"Cookie": f"flow_session={cookie}"}


def get(client, headers, path):
    _req, resp = client.get(path, headers=headers)
    return resp


def post(client, headers, path, body=None):
    _req, resp = client.post(path, headers=headers, json=body or {})
    return resp


def patch(client, headers, path, body):
    _req, resp = client.patch(path, headers=headers, json=body)
    return resp


def item_of(resp):
    return resp.json["item"]


def test_list_shows_gate_states_and_policy_name(client, auth):
    r = get(client, auth, "/api/flows")
    assert r.status == 200
    assert r.json["policy"]["name"] in ("缺项拦截", "缺项待复核")
    for it in r.json["items"]:
        assert it["gate_state"] in ("待检", "已过", "待复核", "已退回", "核验中")
        assert "policy" in r.json


def test_unverified_cannot_land(client, auth):
    assert post(client, auth, f"/api/flows/{GOOD_FLOW}/extract").status == 200
    r = post(client, auth, f"/api/flows/{GOOD_FLOW}/land")
    assert r.status == 409
    assert r.json["reason"] == "unverified"
    assert "未核验" in r.json["detail"]


def test_empty_code_is_missing_and_lands_into_review(client, auth):
    # 钉死“缺项待复核”策略（默认）。
    r = post(client, auth, "/api/policy", {"key": "review"})
    assert r.status == 200
    assert r.json["policy"]["name"] == "缺项待复核"

    assert post(client, auth, f"/api/flows/{EMPTY_CODE_FLOW}/extract").status == 200
    r = post(client, auth, f"/api/flows/{EMPTY_CODE_FLOW}/verify")
    assert r.status == 200
    it = item_of(r)
    assert it["gate_state"] == "待复核"
    assert "编码节点" in it["missing_nodes"]

    r = post(client, auth, f"/api/flows/{EMPTY_CODE_FLOW}/land", {"path": "review"})
    assert r.status == 200
    it = item_of(r)
    assert it["status"] == "已落库"
    assert it["gate_state"] == "待复核"
    assert it["read_only"] is True

    # 列表要能分出已过态和待复核态。
    good = post(client, auth, f"/api/flows/{GOOD_FLOW}/extract")
    assert good.status == 200
    post(client, auth, f"/api/flows/{GOOD_FLOW}/verify")
    post(client, auth, f"/api/flows/{GOOD_FLOW}/land")
    listing = get(client, auth, "/api/flows").json["items"]
    by_id = {it["id"]: it for it in listing}
    assert by_id[GOOD_FLOW]["gate_state"] == "已过"
    assert by_id[EMPTY_CODE_FLOW]["gate_state"] == "待复核"


def test_block_policy_returns_batch_and_forbids_landing(client, auth):
    r = post(client, auth, "/api/policy", {"key": "block"})
    assert r.status == 200
    assert r.json["policy"]["name"] == "缺项拦截"

    post(client, auth, f"/api/flows/{EMPTY_CODE_FLOW}/extract")
    r = post(client, auth, f"/api/flows/{EMPTY_CODE_FLOW}/verify")
    it = item_of(r)
    assert it["gate_state"] == "已退回"
    assert it["status"] == "待抽取"
    assert "编码节点" in it["missing_nodes"]

    r = post(client, auth, f"/api/flows/{EMPTY_CODE_FLOW}/land", {"path": "block"})
    assert r.status == 409
    assert r.json["reason"] == "blocked"


def test_race_second_verifier_loses(client, auth):
    post(client, auth, f"/api/flows/{EMPTY_CODE_FLOW}/extract")
    first = post(client, auth, f"/api/flows/{EMPTY_CODE_FLOW}/verify")
    assert first.status == 200

    second = post(client, auth, f"/api/flows/{EMPTY_CODE_FLOW}/verify")
    assert second.status == 409
    assert second.json["reason"] == "lost"
    assert second.json["lost"] is True
    assert "抢检落败" in second.json["detail"]

    fresh = get(client, auth, f"/api/flows/{EMPTY_CODE_FLOW}").json["item"]
    assert fresh["verify_lost"] == 1
    # 缺项卡片数据仍在：展开后既有缺项名也有抢检落败线索。
    assert "编码节点" in fresh["missing_nodes"]


def test_concurrent_race_loser_gets_lost_while_holder_runs(client, auth):
    post(client, auth, f"/api/flows/{GOOD_FLOW}/extract")
    hold = post(
        client, auth, f"/api/flows/{GOOD_FLOW}/verify", {"hold_ms": 3000}
    )
    assert hold.status == 200 and hold.json["verifying"] is True

    loser = post(client, auth, f"/api/flows/{GOOD_FLOW}/verify")
    assert loser.status == 409
    assert loser.json["reason"] == "lost"
    assert loser.json["gate_state"] == "核验中"
    assert "抢检落败" in loser.json["detail"]


def test_verify_requires_extraction(client, auth):
    r = post(client, auth, f"/api/flows/{GOOD_FLOW}/verify")
    assert r.status == 409
    assert r.json["reason"] == "not_extracted"


def test_policy_pinned_for_the_day(client, auth):
    assert post(client, auth, "/api/policy", {"key": "review"}).status == 200
    again = post(client, auth, "/api/policy", {"key": "review"})
    assert again.status == 200
    changed = post(client, auth, "/api/policy", {"key": "block"})
    assert changed.status == 409
    assert changed.json["reason"] == "pinned"
    assert "不许换弦" in changed.json["detail"]


def test_policy_cannot_change_while_verifying(client, auth):
    assert post(client, auth, "/api/policy", {"key": "review"}).status == 200
    post(client, auth, f"/api/flows/{GOOD_FLOW}/extract")

    hold = post(
        client, auth, f"/api/flows/{GOOD_FLOW}/verify", {"hold_ms": 5000}
    )
    assert hold.status == 200
    assert hold.json["verifying"] is True

    r = post(client, auth, "/api/policy", {"key": "block"})
    assert r.status == 409
    assert r.json["reason"] == "verifying"
    assert "不准改齐套策略" in r.json["detail"]

    # 核验中也不准落库。
    land = post(client, auth, f"/api/flows/{GOOD_FLOW}/land")
    assert land.status == 409
    assert land.json["reason"] == "verifying"


def test_land_path_mismatch_is_flagged(client, auth):
    assert post(client, auth, "/api/policy", {"key": "review"}).status == 200
    post(client, auth, f"/api/flows/{GOOD_FLOW}/extract")
    post(client, auth, f"/api/flows/{GOOD_FLOW}/verify")

    r = post(client, auth, f"/api/flows/{GOOD_FLOW}/land", {"path": "block"})
    assert r.status == 409
    assert r.json["reason"] == "path_mismatch"
    assert r.json["path_mismatch"] is True
    assert "路径偏了" in r.json["detail"]


def test_block_flow_after_extraction_cannot_be_edited(client, auth):
    """改节点会把已落库的流打回待检；落库后应禁止改节点。"""
    post(client, auth, "/api/policy", {"key": "review"})
    post(client, auth, f"/api/flows/{GOOD_FLOW}/extract")
    post(client, auth, f"/api/flows/{GOOD_FLOW}/verify")
    assert post(client, auth, f"/api/flows/{GOOD_FLOW}/land").status == 200

    r = patch(client, auth, f"/api/flows/{GOOD_FLOW}/nodes", {"title": "改名"})
    assert r.status == 409
    assert r.json["reason"] == "landed"


def test_frontend_surfaces_gate_vocabulary():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    css = (root / "static" / "flow.css").read_text(encoding="utf-8")
    for token in ("待检", "已过", "待复核", "策略", "缺项卡片", "抢检落败", "路径偏了", "只读"):
        assert token in js
    assert "path-mismatch" in css


def test_legacy_schema_migrates_and_allows_empty_codes(tmp_path):
    """旧库 code 为 UNIQUE：空编码缺项不能撞键，且新列要补齐。"""
    from app.db import get_db, init_db, list_flows

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE flows (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          code TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL,
          source_uri TEXT NOT NULL,
          extracted INTEGER NOT NULL DEFAULT 0,
          landed INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO flows (title, code, status, source_uri)
        VALUES ('旧流甲', '', '待抽取', 'https://mock.local/a');
        """
    )
    conn.commit()
    conn.close()

    init_db(str(db))
    with get_db(str(db)) as conn:
        conn.execute(
            "INSERT INTO flows (title, code, status, source_uri) VALUES (?,?,?,?)",
            ("旧流乙", "", "待抽取", "https://mock.local/b"),
        )
        items = list_flows(conn)
    empty_codes = [it for it in items if it["code"] == ""]
    assert len(empty_codes) == 2
    assert all(it["gate_state"] == "待检" for it in empty_codes)
