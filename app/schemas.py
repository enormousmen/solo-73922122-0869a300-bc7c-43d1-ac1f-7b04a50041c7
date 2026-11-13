"""Pydantic contract for the public API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_ABS_LON = 180.0
MAX_ABS_LAT = 85.0511287798066


class PointIn(BaseModel):
    """共享测点：ID + WGS84 经纬度。"""

    model_config = {
        "json_schema_extra": {
            "examples": [{"id": "PT-001", "lon": 116.397428, "lat": 39.909230}]
        }
    }

    id: str = Field(min_length=1, max_length=64, description="测点 ID（全局唯一，已存在即更正）")
    lon: float = Field(description="WGS84 经度")
    lat: float = Field(description="WGS84 纬度")

    @field_validator("id")
    @classmethod
    def _clean_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("id must not be blank")
        return v

    @field_validator("lon")
    @classmethod
    def _lon(cls, v: float) -> float:
        if not (-MAX_ABS_LON <= v <= MAX_ABS_LON):
            raise ValueError("lon out of WGS84 range")
        return v

    @field_validator("lat")
    @classmethod
    def _lat(cls, v: float) -> float:
        if not (-MAX_ABS_LAT <= v <= MAX_ABS_LAT):
            raise ValueError("lat out of WGS84 range")
        return v


class RoadIn(BaseModel):
    """路段：ID + 至少两个有序测点 ID。"""

    model_config = {
        "json_schema_extra": {
            "examples": [{"id": "RD-101", "point_ids": ["PT-001", "PT-002", "PT-003"]}]
        }
    }

    id: str = Field(min_length=1, max_length=64, description="路段 ID（已存在即更正引用）")
    point_ids: list[str] = Field(
        min_length=2,
        description="有序测点 ID，至少两个且互不重复；可引用尚不存在的测点（路段将标记为 incomplete）",
    )

    @field_validator("id")
    @classmethod
    def _clean_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("id must not be blank")
        return v

    @field_validator("point_ids")
    @classmethod
    def _clean_points(cls, v: list[str]) -> list[str]:
        cleaned = [p.strip() for p in v]
        if any(not p for p in cleaned):
            raise ValueError("point ids must not be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("point ids must be unique within a road")
        return cleaned


class BatchIn(BaseModel):
    """一个批次 = 一个事务。

    列表均为 upsert：id 已存在即更正，不存在即新增；`delete_*` 在同一事务内
    随后生效。执行顺序：测点 upsert → 路段 upsert → 路段删除 → 测点删除。
    所有受影响路段的线形与缺项在同一事务内整批重算；任一步失败整体回滚。
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "新增/更正测点与路段",
                    "value": {
                        "points": [
                            {"id": "PT-001", "lon": 116.397428, "lat": 39.90923},
                            {"id": "PT-002", "lon": 116.407428, "lat": 39.913},
                            {"id": "PT-003", "lon": 116.417428, "lat": 39.918},
                        ],
                        "roads": [
                            {
                                "id": "RD-101",
                                "point_ids": ["PT-001", "PT-002", "PT-003"],
                            }
                        ],
                    },
                },
                {
                    "summary": "批量删除路段与测点",
                    "value": {"delete_roads": ["RD-102"], "delete_points": ["PT-004"]},
                },
            ]
        }
    }

    points: list[PointIn] = Field(default_factory=list, description="新增或更正测点")
    roads: list[RoadIn] = Field(default_factory=list, description="新增或更正路段（引用变更即改线）")
    delete_points: list[str] = Field(default_factory=list, description="删除测点（引用它的路段转为 incomplete 或随之恢复）")
    delete_roads: list[str] = Field(default_factory=list, description="删除路段及其引用")

    @field_validator("delete_points", "delete_roads")
    @classmethod
    def _no_dupes(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate id in delete list")
        return v

    @model_validator(mode="after")
    def _disjoint(self) -> "BatchIn":
        pt_upserts = [p.id for p in self.points]
        if len(set(pt_upserts)) != len(pt_upserts):
            raise ValueError("duplicate point id in points list")
        rd_upserts = [r.id for r in self.roads]
        if len(set(rd_upserts)) != len(rd_upserts):
            raise ValueError("duplicate road id in roads list")
        if set(pt_upserts) & set(self.delete_points):
            raise ValueError("a point id cannot be upserted and deleted in one batch")
        if set(rd_upserts) & set(self.delete_roads):
            raise ValueError("a road id cannot be upserted and deleted in one batch")
        return self


class BatchResult(BaseModel):
    points_upserted: int = Field(description="本批次实际写入（新增或坐标变化）的测点数")
    roads_upserted: int = Field(description="本批次新建的路段数")
    roads_updated: int = Field(description="本批次重算并更新线形/缺项/引用的已有路段数")
    points_deleted: int = Field(description="实际删除的测点数")
    roads_deleted: int = Field(description="实际删除的路段数")
    affected_road_ids: list[str] = Field(description="本批次影响到的全部路段 ID（均已持久化到最终状态）")
    roads: list["RoadOut"] = Field(description="受影响路段的最终状态，按 ID 升序")
    commit_attempts: int = Field(default=1, description="提交尝试次数；>1 表示规划期间检测到并发提交并已整体重算")


class RoadOut(BaseModel):
    id: str = Field(description="路段 ID")
    point_ids: list[str] = Field(description="持久化的有序测点引用")
    status: Literal["ok", "incomplete"] = Field(description="ok=线形完整；incomplete=缺测点，无残线")
    geometry: dict | None = Field(description="持久化的 GeoJSON LineString；incomplete 时为 null")
    missing_point_ids: list[str] = Field(description="缺失的测点 ID（按路段顺序去重）；补齐后自动恢复")
    revision: int = Field(description="版本号：每次该路段受影响 +1；删除后同 ID 重建则回到 1")
    lineage: str = Field(description="当前线形的化身标识：删除后同 ID 重建会更换，旧拓扑裁决不得沿用")
    updated_at: str = Field(description="最近持久化时间（UTC, ISO-8601）")


class PointsResponse(BaseModel):
    points: list[PointIn]
    total: int


class RoadsResponse(BaseModel):
    roads: list[RoadOut]
    total: int


class StateResponse(BaseModel):
    """Points and persisted geometry read from one committed snapshot."""

    generation: int
    points: list[PointIn]
    roads: list[RoadOut]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    db: str
    journal_mode: str


# ---------------------------------------------------------------------------
# Topology check + QC verdicts
# ---------------------------------------------------------------------------

VERDICT_STATUSES = ("pending_review", "confirmed")
UNCHECKED_REASONS = ("unknown_id", "incomplete", "antimeridian")


class TopologyCheckIn(BaseModel):
    """一次拓扑核查请求：最多 100 个路段 ID。"""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"road_ids": ["RD-101", "RD-102", "RD-103", "RD-104"]}
            ]
        }
    }

    road_ids: list[str] = Field(
        min_length=1,
        max_length=100,
        description="待核查路段 ID，1~100 个、不可重复；缺点/不存在的路段列入未核查",
    )

    @field_validator("road_ids")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        cleaned = [rid.strip() for rid in v]
        if any(not rid for rid in cleaned):
            raise ValueError("road ids must not be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("road ids must be unique")
        return cleaned


class UncheckedRoad(BaseModel):
    road_id: str = Field(description="未参与核查的路段 ID")
    reason: Literal["unknown_id", "incomplete", "antimeridian"] = Field(
        description="unknown_id=路段不存在；incomplete=缺测点无持久化线形；"
        "antimeridian=线形跨 180° 经线，平面法不适用"
    )
    detail: str = Field(default="", description="补充说明（如缺失测点 ID 列表）")


class IntersectionOut(BaseModel):
    road_a_id: str
    road_a_segment: int = Field(description="交点所在 A 线段序号（0 起，顶点 i 与 i+1 之间）")
    road_b_id: str
    road_b_segment: int = Field(description="交点所在 B 线段序号（0 起）")
    coordinates: list[float] = Field(description="平面交点 [lon, lat]")
    review_status: Literal["pending_review", "confirmed"] = Field(
        description="pending_review=待复查（无有效裁决）；confirmed=已确认真实立交"
    )
    verdict: "VerdictOut | None" = Field(
        description="当前有效裁决；无有效裁决（从未裁决或已因线形变化失效）时为 null"
    )


class TopologyCheckResult(BaseModel):
    generation: int = Field(description="本次核查所基于的已提交状态代号；交点、线形、裁决均来自该同一状态")
    checked_road_ids: list[str] = Field(description="实际参与核查的路段 ID（按升序）")
    unchecked_roads: list[UncheckedRoad]
    intersections: list[IntersectionOut]
    total: int = Field(description="横向交点总数")


class VerdictRoadBinding(BaseModel):
    """裁决对一方路段的绑定：化身 + 版本，任一变化裁决即失效。"""

    road_id: str = Field(min_length=1, max_length=64)
    segment: int = Field(ge=0, description="交点所在线段序号（0 起）")
    lineage: str = Field(min_length=1, description="核查响应/路段信息中该路段当前的 lineage（化身标识）")
    revision: int = Field(ge=1, description="核查响应/路段信息中该路段当前的 revision")


class VerdictUpsertIn(BaseModel):
    """对所见交点填写理由并确认立交（或改标待复查）。

    绑定信息直接取自核查响应；确认时若任一方路段的坐标或引用已变化
    （lineage 或 revision 不符），服务返回 409 且不写入任何裁决。
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "road_a": {
                        "road_id": "RD-101",
                        "segment": 1,
                        "lineage": "a1b2c3d4e5f60718",
                        "revision": 3,
                    },
                    "road_b": {
                        "road_id": "RD-102",
                        "segment": 0,
                        "lineage": "9f8e7d6c5b4a3928",
                        "revision": 2,
                    },
                    "status": "confirmed",
                    "reason": "现场复核为跨线桥，垂直净距 5.2m，无平面冲突",
                }
            ]
        }
    }

    road_a: VerdictRoadBinding
    road_b: VerdictRoadBinding
    status: Literal["pending_review", "confirmed"] = Field(
        description="confirmed=确认真实立交；pending_review=改回待复查"
    )
    reason: str = Field(default="", max_length=500, description="质检理由（可空）")

    @model_validator(mode="after")
    def _distinct_roads(self) -> "VerdictUpsertIn":
        if self.road_a.road_id == self.road_b.road_id:
            raise ValueError("a verdict must bind two different roads")
        return self


class VerdictOut(BaseModel):
    id: str
    road_a_id: str
    road_a_segment: int
    road_b_id: str
    road_b_segment: int
    status: Literal["pending_review", "confirmed"]
    reason: str
    valid: bool = Field(
        description="是否仍有效：双方 lineage/revision 与当前已提交状态一致；"
        "任一方坐标/引用变化、删除或同 ID 重建后为 false"
    )
    generation: int = Field(description="裁决所绑定的已提交状态代号")
    intersection_point: list[float] = Field(description="裁决时所见的交点 [lon, lat]")
    road_a_lineage: str
    road_b_lineage: str
    road_a_revision: int
    road_b_revision: int
    created_at: str
    updated_at: str


class VerdictListResponse(BaseModel):
    generation: int
    verdicts: list[VerdictOut]
    total: int


BatchResult.model_rebuild()
TopologyCheckResult.model_rebuild()
