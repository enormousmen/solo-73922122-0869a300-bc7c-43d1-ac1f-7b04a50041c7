"""FastAPI application: batch ingest + persisted-geometry queries."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import store, topo_store
from .config import settings
from .db import init_db, read_tx
from .schemas import (
    BatchIn,
    BatchResult,
    HealthResponse,
    PointIn,
    PointsResponse,
    RoadOut,
    RoadsResponse,
    StateResponse,
    TopologyCheckIn,
    TopologyCheckResult,
    VerdictListResponse,
    VerdictOut,
    VerdictUpsertIn,
)

DESCRIPTION = """
测绘队共享测点 -> 路段线形加工服务。

* `POST /api/v1/batches` 一次事务内完成测点/路段的**批量新增、更正、删除**。
* 线形在提交时按路段引用的测点顺序组装并**持久保存**为 GeoJSON
  `LineString`；查询接口只读已持久化的表，不现场组线。
* 任一被引用测点缺失时**不输出残线**，在 `missing_point_ids` 里列明缺失
  的测点 ID；补齐测点后下一次批次自动恢复线形。
* 测点更正/删除、路段引用变更会影响的所有路段在同一事务内整批重算，
  失败整体回滚，数据库保持原状。
* `GET /api/v1/state` 在**一个读快照**里返回全部测点原值与已持久化线形，
  不会出现"新坐标 + 旧线形"的混合视图。

组线是唯一可能较慢的步骤，它发生在事务之外；提交只是很短的
`BEGIN IMMEDIATE`。两个 API 进程共用同一个 SQLite 文件（WAL 模式），
写者互斥、读者不阻塞，提交前还会复核快照代号，因此并发改线不会漏更新、
也不会写回旧坐标。

## 拓扑核查（topology 标签）

* `POST /api/v1/topology/checks` 接收 1~100 个路段 ID，按经纬度平面检查
  **不同路段线段内部的横向交点**，返回双方 ID 与线段序号；端点接触与共线
  重叠忽略，缺点/不存在/跨 180° 经线的路段列为未核查。核查只读、不持写锁。
* `POST /api/v1/topology/verdicts` 为所见交点填写理由、确认真实立交或改标
  待复查。裁决绑定双方线形的 lineage + revision：坐标或引用一变即失效，
  改回原值、删除后同 ID 重建都不能沿用旧裁决；确认时道路已变化返回 409，
  失败不留裁决，重启后有效裁决仍在。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Municipal Road Geometry Service",
    description=DESCRIPTION,
    version="1.1.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "ingest", "description": "测点/路段批量入库与查询"},
        {"name": "topology", "description": "路网拓扑核查与立交裁决"},
    ],
)


@app.get("/health", response_model=HealthResponse, tags=["ingest"])
def health() -> HealthResponse:
    with read_tx() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.execute("SELECT count(*) FROM points").fetchone()
    return HealthResponse(status="ok", db=settings.db_path, journal_mode=mode)


@app.post("/api/v1/batches", response_model=BatchResult, status_code=201, tags=["ingest"])
def post_batch(
    batch: BatchIn,
    pause_ms: int = Query(
        default=0,
        ge=0,
        le=30_000,
        description="演示用：规划完成后、进入写事务前暂停指定毫秒；证明组线期间"
        "不持写锁，另一进程仍可入库。",
    ),
    fail: bool = Query(
        default=False, description="演示用：在全部写入之后、提交之前注入异常以验证整批回滚。"
    ),
):
    try:
        result, attempts = store.execute_batch(
            batch, pause_ms=pause_ms, inject_failure=fail
        )
    except store.ConcurrentConflict as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except store.DemoFailure as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database busy: {exc}") from exc
    return result


@app.get("/api/v1/state", response_model=StateResponse, tags=["ingest"])
def get_state() -> StateResponse:
    """全部测点原值与全部已持久化线形，来自**同一个读快照**。

    返回的每个坐标与每条线形都属于同一已提交状态，不会出现“新坐标 + 旧线形”。
    """
    with read_tx() as conn:
        generation = store.get_generation(conn)
        points = store.list_points(conn)
        roads = store.list_roads(conn)
    return StateResponse(generation=generation, points=points, roads=roads)


@app.get("/api/v1/points", response_model=PointsResponse, tags=["ingest"])
def get_points() -> PointsResponse:
    with read_tx() as conn:
        points = store.list_points(conn)
    return PointsResponse(points=points, total=len(points))


@app.get("/api/v1/points/{point_id}", response_model=PointIn, tags=["ingest"])
def get_point(point_id: str) -> PointIn:
    with read_tx() as conn:
        point = store.get_point(conn, point_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"point {point_id!r} not found")
    return point


@app.get("/api/v1/roads", response_model=RoadsResponse, tags=["ingest"])
def get_roads() -> RoadsResponse:
    with read_tx() as conn:
        roads = store.list_roads(conn)
    return RoadsResponse(roads=roads, total=len(roads))


@app.get("/api/v1/roads/{road_id}", response_model=RoadOut, tags=["ingest"])
def get_road(road_id: str) -> RoadOut:
    with read_tx() as conn:
        roads = store.list_roads(conn, road_id=road_id)
    if not roads:
        raise HTTPException(status_code=404, detail=f"road {road_id!r} not found")
    return roads[0]


# ---------------------------------------------------------------------------
# Topology checks and QC verdicts
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/topology/checks",
    response_model=TopologyCheckResult,
    tags=["topology"],
    summary="对最多 100 个路段做平面横向交点核查",
)
def post_topology_check(body: TopologyCheckIn) -> TopologyCheckResult:
    """在**一个读快照**内完成核查：交点、线形与已有裁决全部来自同一已提交状态。

    * 仅返回**不同路段线段内部**的横向交点（含双方路段 ID 与 0 起线段序号）；
      端点接触、共线重叠一律忽略。
    * 不存在 / 缺点（incomplete）/ 跨 180° 经线的路段列入 `unchecked_roads`，不参与核查。
    * 核查全程只读，不持有 SQLite 写锁，不阻塞另一进程的入库。
    * 每个交点附带当前裁决状态：无有效裁决为 `pending_review`，已确认为 `confirmed`。
    """
    return topo_store.run_check(body)


@app.post(
    "/api/v1/topology/verdicts",
    response_model=VerdictOut,
    status_code=201,
    tags=["topology"],
    summary="对所见交点填写理由并确认立交（或改标待复查）",
)
def post_verdict(body: VerdictUpsertIn) -> VerdictOut:
    """裁决绑定核查时所见的双方线形（lineage + revision + 状态代号）。

    确认时若任一方道路的坐标或引用已经变化（含删除、同 ID 重建、改回原值），
    返回 **409** 且不写入任何裁决；无关路段变化不影响。失败整体回滚，不留半成品裁决。
    """
    try:
        return topo_store.upsert_verdict(body)
    except topo_store.TopologyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database busy: {exc}") from exc


@app.get(
    "/api/v1/topology/verdicts",
    response_model=VerdictListResponse,
    tags=["topology"],
    summary="列出裁决（可按状态 / 路段过滤，含有效性）",
)
def get_verdicts(
    status: str | None = Query(default=None, pattern="^(pending_review|confirmed)$"),
    road_id: str | None = Query(default=None, description="只返回涉及该路段 ID 的裁决"),
) -> VerdictListResponse:
    return topo_store.list_verdicts(status=status, road_id=road_id)
