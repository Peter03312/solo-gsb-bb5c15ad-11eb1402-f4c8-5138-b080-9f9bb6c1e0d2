"""潮汐约束下的最早抵达行程规划（时间依赖最短路 + 航段编号序列择优）。

时间模型：
- 规划窗口为分钟 [0, window]，船在整分钟从节点出发，可在节点整分钟等待；
- 航段耗时 d 分钟、于分钟 t 出发，则占用分钟 t, t+1, ..., t+d-1，于分钟 t+d 到达；
- 占用的每一分钟 m 必须满足：
    静水深度 + 潮位(m) >= 吃水 + 安全余量
    桥底净空 - 潮位(m) >= 水面以上高度 + 安全余量   （仅当航段设有桥底净空）
- 抵达分钟必须 <= window。

择优规则：先按抵达分钟最小；抵达相同再按航段编号序列字典序最小。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Segment:
    id: int
    from_node: int
    to_node: int
    travel_minutes: int
    still_depth: float
    bridge_clearance: Optional[float]


@dataclass(frozen=True)
class Ship:
    draft: float   # 吃水（米）
    height: float  # 水面以上高度（米）
    margin: float  # 安全余量（米）


@dataclass(frozen=True)
class Leg:
    segment_id: int
    depart: int
    arrive: int


class MissingTideError(Exception):
    """规划窗口内缺少逐分钟潮位数据。"""

    def __init__(self, minutes: Sequence[int]):
        self.minutes = list(minutes)
        preview = ", ".join(str(m) for m in self.minutes[:10])
        suffix = " ..." if len(self.minutes) > 10 else ""
        super().__init__(f"tide level missing for {len(self.minutes)} minute(s): {preview}{suffix}")


def minute_ok(seg: Segment, tide: float, ship: Ship) -> bool:
    """单分钟可行性：水深约束 + （可选）净空约束。"""
    if seg.still_depth + tide < ship.draft + ship.margin:
        return False
    if seg.bridge_clearance is not None and seg.bridge_clearance - tide < ship.height + ship.margin:
        return False
    return True


def require_tide_coverage(levels: Dict[int, float], window: int) -> None:
    """潮位必须覆盖窗口内船可能占用的每一分钟 [0, window-1]。"""
    missing = [m for m in range(window) if m not in levels]
    if missing:
        raise MissingTideError(missing)


def _next_ok_starts(seg: Segment, levels: Dict[int, float], window: int, ship: Ship) -> List[Optional[int]]:
    """next_ok[t] = 不早于 t 的最早可行出发分钟；无则 None。出发分钟 t 要求 t+travel <= window。"""
    travel = seg.travel_minutes
    bad_prefix = [0] * (window + 1)
    for m in range(window):
        bad_prefix[m + 1] = bad_prefix[m] + (0 if minute_ok(seg, levels[m], ship) else 1)
    last_start = window - travel
    next_ok: List[Optional[int]] = [None] * (window + 1)
    nxt: Optional[int] = None
    for t in range(window, -1, -1):
        if 0 <= t <= last_start and bad_prefix[t + travel] - bad_prefix[t] == 0:
            nxt = t
        next_ok[t] = nxt
    return next_ok


def earliest_itinerary(
    segments: Sequence[Segment],
    levels: Dict[int, float],
    start: int,
    goal: int,
    window: int,
    ship: Ship,
) -> Optional[Tuple[int, List[Leg]]]:
    """返回 (抵达分钟, 航段行程)；无可行行程返回 None。levels 须已通过 require_tide_coverage。"""
    if start == goal:
        return 0, []

    by_from: Dict[int, List[Segment]] = {}
    by_id: Dict[int, Segment] = {}
    for seg in segments:
        by_from.setdefault(seg.from_node, []).append(seg)
        by_id[seg.id] = seg

    next_ok_cache: Dict[int, List[Optional[int]]] = {}

    def next_ok(seg: Segment) -> List[Optional[int]]:
        cached = next_ok_cache.get(seg.id)
        if cached is None:
            cached = _next_ok_starts(seg, levels, window, ship)
            next_ok_cache[seg.id] = cached
        return cached

    # Dijkstra：键为 (抵达分钟, 航段编号序列)。等待使网络满足 FIFO；
    # 且航段耗时 >= 1 分钟，任何能改进节点键的松弛都来自键更小的节点，故弹出即最优。
    best: Dict[int, Tuple[int, Tuple[int, ...]]] = {start: (0, ())}
    pq: List[Tuple[int, Tuple[int, ...], int]] = [(0, (), start)]
    final_path: Optional[Tuple[int, ...]] = None
    final_time = 0
    while pq:
        t, path, u = heapq.heappop(pq)
        if best.get(u) != (t, path):
            continue
        if u == goal:
            final_time, final_path = t, path
            break
        for seg in by_from.get(u, []):
            nxt = next_ok(seg)
            dep = nxt[t]
            if dep is None:
                continue
            arr = dep + seg.travel_minutes
            cand = (arr, path + (seg.id,))
            cur = best.get(seg.to_node)
            if cur is None or cand < cur:
                best[seg.to_node] = cand
                heapq.heappush(pq, (arr, cand[1], seg.to_node))

    if final_path is None:
        return None

    # 沿最优编号序列重放，恢复每段的出发/到达分钟（与松弛时的选择一致）。
    legs: List[Leg] = []
    now = 0
    for seg_id in final_path:
        seg = by_id[seg_id]
        dep = next_ok(seg)[now]
        assert dep is not None  # 该路径在搜索中已被验证可行
        legs.append(Leg(segment_id=seg_id, depart=dep, arrive=dep + seg.travel_minutes))
        now = legs[-1].arrive
    return final_time, legs


def validate_itinerary(
    segments_by_id: Dict[int, Segment],
    levels: Dict[int, float],
    start: int,
    goal: int,
    window: int,
    ship: Ship,
    legs: Sequence[Leg],
) -> int:
    """复核一份行程：结构连续 + 逐分钟约束。合法则返回抵达分钟，否则抛 ValueError / MissingTideError。"""
    node = start
    now = 0
    for i, leg in enumerate(legs):
        seg = segments_by_id.get(leg.segment_id)
        if seg is None:
            raise ValueError(f"leg {i}: unknown segment id {leg.segment_id}")
        if seg.from_node != node:
            raise ValueError(f"leg {i}: segment {seg.id} starts at node {seg.from_node}, not {node}")
        if leg.depart < now:
            raise ValueError(f"leg {i}: departs at minute {leg.depart} before previous arrival {now}")
        if leg.arrive != leg.depart + seg.travel_minutes:
            raise ValueError(
                f"leg {i}: segment {seg.id} takes {seg.travel_minutes} min, "
                f"but depart={leg.depart} arrive={leg.arrive}"
            )
        if leg.arrive > window:
            raise ValueError(f"leg {i}: arrival minute {leg.arrive} exceeds window {window}")
        for m in range(leg.depart, leg.arrive):
            tide = levels.get(m)
            if tide is None:
                raise MissingTideError([m])
            if not minute_ok(seg, tide, ship):
                raise ValueError(f"leg {i}: tidal/bridge constraint violated at minute {m}")
        node = seg.to_node
        now = leg.arrive
    if node != goal:
        raise ValueError(f"itinerary ends at node {node}, not destination {goal}")
    return now
