"""港口潮汐通航行程规划服务 —— FastAPI 入口。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Dict, List

from fastapi import FastAPI, HTTPException

from . import db
from .planner import (
    Leg,
    MissingTideError,
    Segment,
    Ship,
    earliest_itinerary,
    require_tide_coverage,
    validate_itinerary,
)
from .schemas import (
    ConfirmRequest,
    ConfirmResponse,
    LegOut,
    NodeCreate,
    NodeOut,
    PlanRequest,
    PlanResponse,
    SegmentCreate,
    SegmentOut,
    TideRevisionCreate,
    TideRevisionOut,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()  # 启动即初始化数据库（幂等）
    yield


app = FastAPI(title="Port Tidal Voyage Planner", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 工具

def _get_node(conn, node_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT id, name FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown node id: {node_id}")
    return row


def _load_segments(conn) -> List[Segment]:
    rows = conn.execute(
        "SELECT id, from_node, to_node, travel_minutes, still_depth, bridge_clearance FROM segments"
    ).fetchall()
    return [
        Segment(
            id=r["id"],
            from_node=r["from_node"],
            to_node=r["to_node"],
            travel_minutes=r["travel_minutes"],
            still_depth=r["still_depth"],
            bridge_clearance=r["bridge_clearance"],
        )
        for r in rows
    ]


def _latest_revision_id(conn) -> int:
    row = conn.execute("SELECT MAX(id) AS rid FROM tide_revisions").fetchone()
    if row["rid"] is None:
        raise HTTPException(status_code=409, detail="no tide data: create a tide revision first")
    return row["rid"]


def _load_levels(conn, revision_id: int) -> Dict[int, float]:
    rows = conn.execute(
        "SELECT minute, level FROM tide_levels WHERE revision_id = ?", (revision_id,)
    ).fetchall()
    return {r["minute"]: r["level"] for r in rows}


def _missing_tide_to_422(exc: MissingTideError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------- 节点 / 航段

@app.post("/nodes", response_model=NodeOut, status_code=201)
def create_node(body: NodeCreate):
    with db.connect() as conn:
        try:
            cur = conn.execute("INSERT INTO nodes (name) VALUES (?)", (body.name,))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"node name already exists: {body.name}")
        return NodeOut(id=cur.lastrowid, name=body.name)


@app.get("/nodes", response_model=List[NodeOut])
def list_nodes():
    with db.connect() as conn:
        rows = conn.execute("SELECT id, name FROM nodes ORDER BY id").fetchall()
        return [NodeOut(id=r["id"], name=r["name"]) for r in rows]


@app.post("/segments", response_model=SegmentOut, status_code=201)
def create_segment(body: SegmentCreate):
    with db.connect() as conn:
        _get_node(conn, body.from_node)
        _get_node(conn, body.to_node)
        cur = conn.execute(
            "INSERT INTO segments (from_node, to_node, travel_minutes, still_depth, bridge_clearance)"
            " VALUES (?, ?, ?, ?, ?)",
            (body.from_node, body.to_node, body.travel_minutes, body.still_depth, body.bridge_clearance),
        )
        return SegmentOut(id=cur.lastrowid, **body.model_dump())


@app.get("/segments", response_model=List[SegmentOut])
def list_segments():
    with db.connect() as conn:
        return [SegmentOut(**vars(s)) for s in _load_segments(conn)]


# ---------------------------------------------------------------- 潮位修订

@app.post("/tide-revisions", response_model=TideRevisionOut, status_code=201)
def create_tide_revision(body: TideRevisionCreate):
    minutes = [p.minute for p in body.levels]
    if len(set(minutes)) != len(minutes):
        raise HTTPException(status_code=422, detail="duplicate minute in levels")
    with db.immediate_transaction() as conn:
        cur = conn.execute("INSERT INTO tide_revisions DEFAULT VALUES")
        rid = cur.lastrowid
        conn.executemany(
            "INSERT INTO tide_levels (revision_id, minute, level) VALUES (?, ?, ?)",
            [(rid, p.minute, p.level) for p in body.levels],
        )
        created_at = conn.execute(
            "SELECT created_at FROM tide_revisions WHERE id = ?", (rid,)
        ).fetchone()["created_at"]
    return TideRevisionOut(revision_id=rid, point_count=len(body.levels), created_at=created_at)


@app.get("/tide-revisions/latest")
def latest_tide_revision():
    with db.connect() as conn:
        return {"revision_id": _latest_revision_id(conn)}


@app.get("/tide-revisions/{revision_id}")
def get_tide_revision(revision_id: int):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, created_at FROM tide_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown tide revision: {revision_id}")
        levels = conn.execute(
            "SELECT minute, level FROM tide_levels WHERE revision_id = ? ORDER BY minute", (revision_id,)
        ).fetchall()
        return {
            "revision_id": row["id"],
            "created_at": row["created_at"],
            "levels": [{"minute": r["minute"], "level": r["level"]} for r in levels],
        }


# ---------------------------------------------------------------- 行程规划

@app.post("/plans", response_model=PlanResponse)
def create_plan(body: PlanRequest):
    with db.connect() as conn:
        _get_node(conn, body.from_node)
        _get_node(conn, body.to_node)
        revision_id = _latest_revision_id(conn)
        levels = _load_levels(conn, revision_id)
        segments = _load_segments(conn)

    try:
        require_tide_coverage(levels, body.window_minutes)
    except MissingTideError as exc:
        raise _missing_tide_to_422(exc)

    ship = Ship(draft=body.draft, height=body.height, margin=body.margin)
    result = earliest_itinerary(
        segments, levels, body.from_node, body.to_node, body.window_minutes, ship
    )
    if result is None:
        raise HTTPException(status_code=422, detail="no feasible itinerary within the window")
    arrival, legs = result

    by_id = {s.id: s for s in segments}
    return PlanResponse(
        revision_id=revision_id,
        from_node=body.from_node,
        to_node=body.to_node,
        window_minutes=body.window_minutes,
        arrival_minute=arrival,
        legs=[
            LegOut(
                segment_id=leg.segment_id,
                from_node=by_id[leg.segment_id].from_node,
                to_node=by_id[leg.segment_id].to_node,
                depart_minute=leg.depart,
                arrive_minute=leg.arrive,
            )
            for leg in legs
        ],
    )


# ---------------------------------------------------------------- 行程确认

@app.post("/confirmations", response_model=ConfirmResponse, status_code=201)
def create_confirmation(body: ConfirmRequest):
    ship = Ship(draft=body.draft, height=body.height, margin=body.margin)
    legs = [Leg(segment_id=l.segment_id, depart=l.depart_minute, arrive=l.arrive_minute) for l in body.legs]

    # 同一事务内：校验修订号未过期 -> 复核行程 -> 落库；任一步失败整体回滚。
    with db.immediate_transaction() as conn:
        _get_node(conn, body.from_node)
        _get_node(conn, body.to_node)
        latest = _latest_revision_id(conn)
        if latest != body.revision_id:
            raise HTTPException(
                status_code=409,
                detail=f"stale tide revision: itinerary based on revision {body.revision_id}, "
                f"current revision is {latest}; not saved",
            )
        levels = _load_levels(conn, body.revision_id)
        segments_by_id = {s.id: s for s in _load_segments(conn)}
        try:
            arrival = validate_itinerary(
                segments_by_id, levels, body.from_node, body.to_node,
                body.window_minutes, ship, legs,
            )
        except MissingTideError as exc:
            raise _missing_tide_to_422(exc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"itinerary revalidation failed: {exc}")

        result_payload = {"arrival_minute": arrival, "status": "confirmed"}
        cur = conn.execute(
            "INSERT INTO confirmations (revision_id, request_json, result_json) VALUES (?, ?, ?)",
            (body.revision_id, body.model_dump_json(), json.dumps(result_payload)),
        )
        confirmation_id = cur.lastrowid

    return ConfirmResponse(
        confirmation_id=confirmation_id, revision_id=body.revision_id, arrival_minute=arrival
    )


@app.get("/confirmations")
def list_confirmations():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, revision_id, created_at FROM confirmations ORDER BY id"
        ).fetchall()
        return [
            {"confirmation_id": r["id"], "revision_id": r["revision_id"], "created_at": r["created_at"]}
            for r in rows
        ]


@app.get("/confirmations/{confirmation_id}")
def get_confirmation(confirmation_id: int):
    """已确认行程保留当时结果：即使之后产生新修订，仍返回确认时刻的快照。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, revision_id, request_json, result_json, created_at"
            " FROM confirmations WHERE id = ?",
            (confirmation_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown confirmation: {confirmation_id}")
    return {
        "confirmation_id": row["id"],
        "revision_id": row["revision_id"],
        "created_at": row["created_at"],
        "request": json.loads(row["request_json"]),
        "result": json.loads(row["result_json"]),
    }


@app.get("/health")
def health():
    return {"status": "ok"}
