"""端到端 API 测试：规划、择优、约束、错误与确认事务。"""
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app

SHIP = {"draft": 2.0, "height": 10.0, "margin": 0.5}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_node(c, name):
    r = c.post("/nodes", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_segment(c, **kw):
    r = c.post("/segments", json=kw)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_tide(c, level_by_minute):
    """level_by_minute: dict minute -> level，或常量 float（铺满 0..1439）。"""
    if isinstance(level_by_minute, (int, float)):
        levels = [{"minute": m, "level": float(level_by_minute)} for m in range(1440)]
    else:
        levels = [{"minute": m, "level": v} for m, v in sorted(level_by_minute.items())]
    r = c.post("/tide-revisions", json={"levels": levels})
    assert r.status_code == 201, r.text
    return r.json()["revision_id"]


def plan(c, frm, to, window=240, **ship):
    return c.post("/plans", json={"from_node": frm, "to_node": to, "window_minutes": window, **ship})


# ---------------------------------------------------------------- 规划

def test_earliest_arrival_with_waiting_at_node(client):
    a = make_node(client, "A")
    b = make_node(client, "B")
    d = make_node(client, "D")
    # s1 静水 2.4：需潮位 >= 2.5-2.4=0.1，前 10 分钟潮位 0.0 不可行 -> 在 A 等到分钟 10
    s1 = make_segment(client, from_node=a, to_node=b, travel_minutes=30, still_depth=2.4)
    s2 = make_segment(client, from_node=b, to_node=d, travel_minutes=40, still_depth=3.0)
    make_tide(client, {**{m: 0.0 for m in range(10)}, **{m: 1.0 for m in range(10, 1440)}})

    r = plan(client, a, d, **SHIP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["arrival_minute"] == 80
    assert [(l["segment_id"], l["depart_minute"], l["arrive_minute"]) for l in body["legs"]] == [
        (s1, 10, 40),
        (s2, 40, 80),
    ]


def test_tie_break_prefers_smaller_segment_id_sequence(client):
    a = make_node(client, "A")
    b = make_node(client, "B")
    cnode = make_node(client, "C")
    d = make_node(client, "D")
    s1 = make_segment(client, from_node=a, to_node=b, travel_minutes=30, still_depth=10)
    s2 = make_segment(client, from_node=b, to_node=d, travel_minutes=40, still_depth=10)
    s3 = make_segment(client, from_node=a, to_node=cnode, travel_minutes=20, still_depth=10)
    s4 = make_segment(client, from_node=cnode, to_node=d, travel_minutes=50, still_depth=10)
    make_tide(client, 1.0)

    r = plan(client, a, d, **SHIP)
    assert r.status_code == 200, r.text
    body = r.json()
    # 两条路径同为 70 分钟抵达，按航段编号序列 (s1,s2) < (s3,s4) 择优
    assert body["arrival_minute"] == 70
    assert [l["segment_id"] for l in body["legs"]] == [s1, s2]


def test_bridge_clearance_blocks_until_tide_falls(client):
    a = make_node(client, "A")
    b = make_node(client, "B")
    # 净空 11，船高 10 + 余量 0.5 -> 需潮位 <= 0.5；前 60 分钟潮位 1.0 不可通行
    s1 = make_segment(
        client, from_node=a, to_node=b, travel_minutes=30, still_depth=10, bridge_clearance=11.0
    )
    make_tide(client, {**{m: 1.0 for m in range(60)}, **{m: 0.0 for m in range(60, 1440)}})

    r = plan(client, a, b, **SHIP)
    assert r.status_code == 200, r.text
    assert r.json()["legs"] == [
        {"segment_id": s1, "from_node": a, "to_node": b, "depart_minute": 60, "arrive_minute": 90}
    ]


def test_same_origin_and_destination(client):
    a = make_node(client, "A")
    make_tide(client, 1.0)
    r = plan(client, a, a, **SHIP)
    assert r.status_code == 200, r.text
    assert r.json()["arrival_minute"] == 0
    assert r.json()["legs"] == []


# ---------------------------------------------------------------- 明确报错

def test_unknown_node_404(client):
    a = make_node(client, "A")
    make_tide(client, 1.0)
    r = plan(client, a, 999, **SHIP)
    assert r.status_code == 404
    assert "unknown node" in r.json()["detail"]


def test_missing_tide_minutes_422(client):
    a = make_node(client, "A")
    b = make_node(client, "B")
    make_segment(client, from_node=a, to_node=b, travel_minutes=10, still_depth=10)
    make_tide(client, {m: 1.0 for m in range(100)})  # 只覆盖 0..99

    r = plan(client, a, b, window=120, **SHIP)
    assert r.status_code == 422
    assert "tide level missing" in r.json()["detail"]


def test_no_feasible_itinerary_422(client):
    a = make_node(client, "A")
    b = make_node(client, "B")
    make_segment(client, from_node=a, to_node=b, travel_minutes=10, still_depth=1.0)  # 永远太浅
    make_tide(client, 0.0)

    r = plan(client, a, b, **SHIP)
    assert r.status_code == 422
    assert "no feasible itinerary" in r.json()["detail"]


def test_window_over_1440_rejected(client):
    a = make_node(client, "A")
    make_tide(client, 1.0)
    r = plan(client, a, a, window=1441, **SHIP)
    assert r.status_code == 422


def test_no_tide_data_409(client):
    a = make_node(client, "A")
    r = plan(client, a, a, **SHIP)
    assert r.status_code == 409
    assert "no tide data" in r.json()["detail"]


# ---------------------------------------------------------------- 确认与修订

def _setup_simple_route(client):
    a = make_node(client, "A")
    b = make_node(client, "B")
    s1 = make_segment(client, from_node=a, to_node=b, travel_minutes=30, still_depth=10)
    make_tide(client, 1.0)
    return a, b, s1


def test_confirm_then_stale_revision_conflict_and_snapshot_kept(client):
    a, b, s1 = _setup_simple_route(client)
    p = plan(client, a, b, **SHIP).json()
    assert p["revision_id"] == 1

    confirm_body = {
        "revision_id": p["revision_id"],
        "from_node": a,
        "to_node": b,
        "window_minutes": 240,
        **SHIP,
        "legs": [
            {"segment_id": l["segment_id"], "depart_minute": l["depart_minute"],
             "arrive_minute": l["arrive_minute"]}
            for l in p["legs"]
        ],
    }
    r = client.post("/confirmations", json=confirm_body)
    assert r.status_code == 201, r.text
    cid = r.json()["confirmation_id"]
    assert r.json()["arrival_minute"] == 30

    # 潮位更正 -> 新修订；旧修订确认 -> 409 且不保存
    make_tide(client, 0.8)
    r = client.post("/confirmations", json=confirm_body)
    assert r.status_code == 409
    assert "stale tide revision" in r.json()["detail"]

    lst = client.get("/confirmations").json()
    assert len(lst) == 1  # 冲突的确认未保存

    snap = client.get(f"/confirmations/{cid}").json()
    assert snap["revision_id"] == 1
    assert snap["result"] == {"arrival_minute": 30, "status": "confirmed"}
    assert snap["request"]["legs"][0]["segment_id"] == s1


def test_confirm_revalidates_itinerary(client):
    a, b, s1 = _setup_simple_route(client)
    bad = {
        "revision_id": 1,
        "from_node": a,
        "to_node": b,
        "window_minutes": 240,
        **SHIP,
        "legs": [{"segment_id": s1, "depart_minute": 0, "arrive_minute": 25}],  # 航时造假
    }
    r = client.post("/confirmations", json=bad)
    assert r.status_code == 422
    assert "revalidation failed" in r.json()["detail"]
    assert client.get("/confirmations").json() == []


def test_tide_revision_increments(client):
    make_node(client, "A")
    r1 = make_tide(client, 1.0)
    r2 = make_tide(client, 0.9)
    assert (r1, r2) == (1, 2)
    assert client.get("/tide-revisions/latest").json() == {"revision_id": 2}
