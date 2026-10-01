"""API 请求/响应模型。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

MAX_WINDOW_MINUTES = 1440


class NodeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class NodeOut(BaseModel):
    id: int
    name: str


class SegmentCreate(BaseModel):
    from_node: int
    to_node: int
    travel_minutes: int = Field(gt=0, description="正整数分钟航时")
    still_depth: float = Field(description="静水深度（米）")
    bridge_clearance: Optional[float] = Field(default=None, description="可选桥底净空（米）")


class SegmentOut(BaseModel):
    id: int
    from_node: int
    to_node: int
    travel_minutes: int
    still_depth: float
    bridge_clearance: Optional[float]


class TidePoint(BaseModel):
    minute: int = Field(ge=0)
    level: float = Field(description="潮位（米，可正可负）")


class TideRevisionCreate(BaseModel):
    levels: List[TidePoint] = Field(min_length=1, description="逐分钟潮位；每次提交产生一个新修订")


class TideRevisionOut(BaseModel):
    revision_id: int
    point_count: int
    created_at: str


class ShipParams(BaseModel):
    draft: float = Field(ge=0, description="船舶吃水（米）")
    height: float = Field(ge=0, description="水面以上高度（米）")
    margin: float = Field(ge=0, description="安全余量（米）")


class PlanRequest(ShipParams):
    from_node: int
    to_node: int
    window_minutes: int = Field(ge=1, le=MAX_WINDOW_MINUTES, description="规划窗口（分钟，<=1440）")


class LegOut(BaseModel):
    segment_id: int
    from_node: int
    to_node: int
    depart_minute: int
    arrive_minute: int


class PlanResponse(BaseModel):
    revision_id: int
    from_node: int
    to_node: int
    window_minutes: int
    arrival_minute: int
    legs: List[LegOut]


class ConfirmLeg(BaseModel):
    segment_id: int
    depart_minute: int = Field(ge=0)
    arrive_minute: int = Field(ge=0)


class ConfirmRequest(ShipParams):
    revision_id: int = Field(description="行程所依据的潮位修订号")
    from_node: int
    to_node: int
    window_minutes: int = Field(ge=1, le=MAX_WINDOW_MINUTES)
    legs: List[ConfirmLeg]


class ConfirmResponse(BaseModel):
    confirmation_id: int
    revision_id: int
    arrival_minute: int
    status: str = "confirmed"
