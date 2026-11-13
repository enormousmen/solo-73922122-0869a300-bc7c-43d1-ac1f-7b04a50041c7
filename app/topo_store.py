"""Topology checks and QC verdicts, built on the same committed-state model.

Check flow
----------
A check opens **one deferred read transaction** (a WAL snapshot), loads the
generation, the requested roads' persisted geometry/lineage/revision and the
existing verdicts, and only then runs the purely planar intersection math.
No write lock is ever taken, so an ingest batch on the other API process is
never blocked by a check, however large the geometry.

Verdict flow
------------
A verdict binds the exact two road *incarnations* seen at check time, captured
as ``(lineage, revision)`` per side plus the store generation.  On upsert a
short ``BEGIN IMMEDIATE`` transaction re-reads both roads and rejects the write
with HTTP 409 when either side:

* no longer exists (deleted, or deleted-and-recreated with the same id),
* has a different lineage (same id, new incarnation), or
* has a different revision (its coordinates or references changed).

Reverting a road to its old values still leaves a higher revision, and a
same-id recreate carries a fresh lineage, so neither can revive an old
verdict.  Changes to unrelated roads never touch the bound revisions, so
verdicts survive them.  Any failure rolls the whole transaction back: no
verdict row is left behind.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from . import topology
from .db import read_tx, write_tx
from .schemas import (
    IntersectionOut,
    TopologyCheckIn,
    TopologyCheckResult,
    UncheckedRoad,
    VerdictListResponse,
    VerdictOut,
    VerdictUpsertIn,
)


class TopologyConflict(Exception):
    """A verdict cannot be written: a bound road changed since the check (409)."""


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class RoadSnap:
    id: str
    geometry: dict | None
    revision: int
    lineage: str


@dataclass(frozen=True)
class VerdictRow:
    id: str
    road_a_id: str
    road_a_segment: int
    road_b_id: str
    road_b_segment: int
    road_a_lineage: str
    road_b_lineage: str
    road_a_revision: int
    road_b_revision: int
    intersection_point: list[float]
    status: str
    reason: str
    generation: int
    created_at: str
    updated_at: str


def _load_roads(conn, ids: list[str]) -> dict[str, RoadSnap]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[str, RoadSnap] = {}
    for row in conn.execute(
        f"SELECT id, geometry, revision, lineage FROM roads WHERE id IN ({marks})",
        ids,
    ):
        out[row["id"]] = RoadSnap(
            id=row["id"],
            geometry=json.loads(row["geometry"]) if row["geometry"] else None,
            revision=row["revision"],
            lineage=row["lineage"],
        )
    return out


def _load_missing_detail(conn, road_id: str) -> list[str]:
    row = conn.execute(
        "SELECT missing_point_ids FROM roads WHERE id = ?", (road_id,)
    ).fetchone()
    return json.loads(row["missing_point_ids"]) if row else []


def _load_verdicts(conn) -> list[VerdictRow]:
    rows = conn.execute(
        "SELECT * FROM topology_verdicts "
        "ORDER BY road_a_id, road_a_segment, road_b_id, road_b_segment"
    ).fetchall()
    return [
        VerdictRow(
            id=r["id"],
            road_a_id=r["road_a_id"],
            road_a_segment=r["road_a_segment"],
            road_b_id=r["road_b_id"],
            road_b_segment=r["road_b_segment"],
            road_a_lineage=r["road_a_lineage"],
            road_b_lineage=r["road_b_lineage"],
            road_a_revision=r["road_a_revision"],
            road_b_revision=r["road_b_revision"],
            intersection_point=json.loads(r["intersection_point"]),
            status=r["status"],
            reason=r["reason"],
            generation=r["generation"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def _is_valid(v: VerdictRow, roads: dict[str, RoadSnap]) -> bool:
    """Both bound incarnations must still be the current committed state."""
    a = roads.get(v.road_a_id)
    b = roads.get(v.road_b_id)
    if a is None or b is None:
        return False
    return (
        a.lineage == v.road_a_lineage
        and a.revision == v.road_a_revision
        and b.lineage == v.road_b_lineage
        and b.revision == v.road_b_revision
    )


def _to_out(v: VerdictRow, valid: bool) -> VerdictOut:
    return VerdictOut(
        id=v.id,
        road_a_id=v.road_a_id,
        road_a_segment=v.road_a_segment,
        road_b_id=v.road_b_id,
        road_b_segment=v.road_b_segment,
        status=v.status,
        reason=v.reason,
        valid=valid,
        generation=v.generation,
        intersection_point=v.intersection_point,
        road_a_lineage=v.road_a_lineage,
        road_b_lineage=v.road_b_lineage,
        road_a_revision=v.road_a_revision,
        road_b_revision=v.road_b_revision,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


def run_check(request: TopologyCheckIn) -> TopologyCheckResult:
    """Planar topology check from a single committed snapshot, no write lock."""
    requested = list(dict.fromkeys(request.road_ids))
    with read_tx() as conn:
        generation = int(
            conn.execute(
                "SELECT generation FROM store_meta WHERE id = 1"
            ).fetchone()[0]
        )
        roads = _load_roads(conn, requested)
        verdicts = _load_verdicts(conn)

        unchecked: list[UncheckedRoad] = []
        planar: dict[str, list] = {}
        for rid in requested:
            snap = roads.get(rid)
            if snap is None:
                # No row at all -> the road id does not exist.
                unchecked.append(UncheckedRoad(road_id=rid, reason="unknown_id"))
                continue
            if snap.geometry is None:
                missing = _load_missing_detail(conn, rid)
                unchecked.append(
                    UncheckedRoad(
                        road_id=rid,
                        reason="incomplete",
                        detail=f"missing points: {missing}",
                    )
                )
                continue
            coords = snap.geometry["coordinates"]
            if topology.crosses_antimeridian(coords):
                unchecked.append(
                    UncheckedRoad(
                        road_id=rid,
                        reason="antimeridian",
                        detail="line spans more than 180 degrees of longitude",
                    )
                )
                continue
            planar[rid] = coords

        # Everything below reads only values captured in this one snapshot.
        raw = topology.find_intersections(planar)

        # Verdicts need validity against the full current road state, not only
        # the checked subset (a verdict's other side may be outside this call).
        verdict_road_ids = set()
        for v in verdicts:
            verdict_road_ids.add(v.road_a_id)
            verdict_road_ids.add(v.road_b_id)
        validity_roads = _load_roads(conn, sorted(verdict_road_ids))
        validity_roads.update(roads)

    valid_by_key: dict[tuple, VerdictRow] = {}
    for v in verdicts:
        if _is_valid(v, validity_roads):
            valid_by_key[
                (v.road_a_id, v.road_a_segment, v.road_b_id, v.road_b_segment)
            ] = v

    intersections: list[IntersectionOut] = []
    for hit in raw:
        key = (
            hit["road_a_id"],
            hit["road_a_segment"],
            hit["road_b_id"],
            hit["road_b_segment"],
        )
        v = valid_by_key.get(key)
        intersections.append(
            IntersectionOut(
                road_a_id=hit["road_a_id"],
                road_a_segment=hit["road_a_segment"],
                road_b_id=hit["road_b_id"],
                road_b_segment=hit["road_b_segment"],
                coordinates=hit["coordinates"],
                review_status=v.status if v else "pending_review",
                verdict=_to_out(v, True) if v else None,
            )
        )

    return TopologyCheckResult(
        generation=generation,
        checked_road_ids=sorted(planar),
        unchecked_roads=unchecked,
        intersections=intersections,
        total=len(intersections),
    )


def _normalise_sides(
    payload: VerdictUpsertIn,
) -> tuple[VerdictUpsertIn, bool]:
    """Order the two sides by road id to match the intersection key."""
    a, b = payload.road_a, payload.road_b
    swapped = False
    if a.road_id > b.road_id:
        a, b = b, a
        swapped = True
    if swapped:
        payload = payload.model_copy(
            update={"road_a": a, "road_b": b}
        )
    return payload, swapped


def upsert_verdict(payload: VerdictUpsertIn) -> VerdictOut:
    """Validate against the live committed state, then insert/update atomically."""
    payload, _ = _normalise_sides(payload)
    stamp = _now()

    with write_tx() as conn:
        generation = int(
            conn.execute(
                "SELECT generation FROM store_meta WHERE id = 1"
            ).fetchone()[0]
        )
        rows = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, geometry, revision, lineage FROM roads WHERE id IN (?, ?)",
                (payload.road_a.road_id, payload.road_b.road_id),
            )
        }

        conflicts: list[str] = []
        for side in (payload.road_a, payload.road_b):
            row = rows.get(side.road_id)
            if row is None:
                conflicts.append(
                    f"road {side.road_id!r} no longer exists (deleted or recreated)"
                )
            elif row["lineage"] != side.lineage:
                conflicts.append(
                    f"road {side.road_id!r} was recreated with a new lineage "
                    f"(bound {side.lineage}, current {row['lineage']})"
                )
            elif row["revision"] != side.revision:
                conflicts.append(
                    f"road {side.road_id!r} changed since the check "
                    f"(bound revision {side.revision}, current {row['revision']})"
                )
        if conflicts:
            # Raising rolls the write transaction back: nothing is persisted.
            raise TopologyConflict("; ".join(conflicts))

        snap_a = _load_roads(conn, [payload.road_a.road_id])[payload.road_a.road_id]
        snap_b = _load_roads(conn, [payload.road_b.road_id])[payload.road_b.road_id]
        if snap_a.geometry is None or snap_b.geometry is None:
            raise TopologyConflict(
                "a bound road is currently incomplete (missing survey points); "
                "the intersection cannot be verified"
            )
        coords_a = snap_a.geometry["coordinates"]
        coords_b = snap_b.geometry["coordinates"]
        if not (0 <= payload.road_a.segment < len(coords_a) - 1) or not (
            0 <= payload.road_b.segment < len(coords_b) - 1
        ):
            raise TopologyConflict(
                "a bound segment index is out of range for the current geometry"
            )

        # The claimed crossing must still exist, in the interior of the same
        # two segments, in the current committed geometry.
        point = topology.proper_intersection(
            (
                tuple(coords_a[payload.road_a.segment]),
                tuple(coords_a[payload.road_a.segment + 1]),
            ),
            (
                tuple(coords_b[payload.road_b.segment]),
                tuple(coords_b[payload.road_b.segment + 1]),
            ),
        )
        if point is None:
            raise TopologyConflict(
                "the bound segments no longer have a transverse interior "
                "intersection in the current geometry"
            )

        existing = conn.execute(
            "SELECT id, created_at FROM topology_verdicts WHERE "
            "road_a_id = ? AND road_a_segment = ? AND road_b_id = ? AND road_b_segment = ?",
            (
                payload.road_a.road_id,
                payload.road_a.segment,
                payload.road_b.road_id,
                payload.road_b.segment,
            ),
        ).fetchone()
        if existing is None:
            verdict_id = uuid.uuid4().hex
            created_at = stamp
            conn.execute(
                "INSERT INTO topology_verdicts(id, road_a_id, road_a_segment, "
                "road_b_id, road_b_segment, road_a_lineage, road_b_lineage, "
                "road_a_revision, road_b_revision, intersection_point, status, "
                "reason, generation, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    verdict_id,
                    payload.road_a.road_id,
                    payload.road_a.segment,
                    payload.road_b.road_id,
                    payload.road_b.segment,
                    snap_a.lineage,
                    snap_b.lineage,
                    snap_a.revision,
                    snap_b.revision,
                    json.dumps(list(point), separators=(",", ":")),
                    payload.status,
                    payload.reason,
                    generation,
                    created_at,
                    stamp,
                ),
            )
        else:
            verdict_id = existing["id"]
            created_at = existing["created_at"]
            # Re-confirming an intersection always re-binds the *current*
            # incarnations and generation; an invalidated row is never revived
            # with stale bindings.
            conn.execute(
                "UPDATE topology_verdicts SET road_a_lineage = ?, road_b_lineage = ?, "
                "road_a_revision = ?, road_b_revision = ?, intersection_point = ?, "
                "status = ?, reason = ?, generation = ?, updated_at = ? WHERE id = ?",
                (
                    snap_a.lineage,
                    snap_b.lineage,
                    snap_a.revision,
                    snap_b.revision,
                    json.dumps(list(point), separators=(",", ":")),
                    payload.status,
                    payload.reason,
                    generation,
                    stamp,
                    verdict_id,
                ),
            )

    return VerdictOut(
        id=verdict_id,
        road_a_id=payload.road_a.road_id,
        road_a_segment=payload.road_a.segment,
        road_b_id=payload.road_b.road_id,
        road_b_segment=payload.road_b.segment,
        status=payload.status,
        reason=payload.reason,
        valid=True,
        generation=generation,
        intersection_point=list(point),
        road_a_lineage=snap_a.lineage,
        road_b_lineage=snap_b.lineage,
        road_a_revision=snap_a.revision,
        road_b_revision=snap_b.revision,
        created_at=created_at,
        updated_at=stamp,
    )


def list_verdicts(
    status: str | None = None, road_id: str | None = None
) -> VerdictListResponse:
    with read_tx() as conn:
        generation = int(
            conn.execute(
                "SELECT generation FROM store_meta WHERE id = 1"
            ).fetchone()[0]
        )
        verdicts = _load_verdicts(conn)
        all_road_ids = set()
        for v in verdicts:
            all_road_ids.add(v.road_a_id)
            all_road_ids.add(v.road_b_id)
        roads = _load_roads(conn, sorted(all_road_ids))

    out: list[VerdictOut] = []
    for v in verdicts:
        if status is not None and v.status != status:
            continue
        if road_id is not None and road_id not in (v.road_a_id, v.road_b_id):
            continue
        out.append(_to_out(v, _is_valid(v, roads)))
    return VerdictListResponse(
        generation=generation, verdicts=out, total=len(out)
    )
