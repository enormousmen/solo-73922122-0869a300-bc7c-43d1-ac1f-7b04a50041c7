"""Pure planar topology helpers (no FastAPI, no SQL).

Scope and rules
---------------
* Coordinates are WGS84 lon/lat treated as a *planar* Cartesian plane.  This is
  only valid for urban data that does not cross the 180° meridian; callers must
  reject such requests before using these helpers.
* Only **transverse intersections in the interior of segments belonging to two
  different roads** are reported.
* Endpoint contact (the meeting point equals any segment endpoint, including
  shared survey points) is ignored.
* Collinear overlap / collinear touch is ignored (there is no single transverse
  crossing point for it).
* Self-intersections inside one road are not reported: a verdict always binds
  two *different* roads.
"""
from __future__ import annotations

from typing import Sequence

# Coordinates come from the same persisted JSON values, so this only has to
# absorb tiny float reordering noise, not measurement error.
EPS = 1e-12

Point = tuple[float, float]
Segment = tuple[Point, Point]


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _bbox_reject(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """True when the two segment bounding boxes cannot meet."""
    return (
        max(p1[0], p2[0]) < min(q1[0], q2[0]) - EPS
        or max(q1[0], q2[0]) < min(p1[0], p2[0]) - EPS
        or max(p1[1], p2[1]) < min(q1[1], q2[1]) - EPS
        or max(q1[1], q2[1]) < min(p1[1], p2[1]) - EPS
    )


def proper_intersection(s1: Segment, s2: Segment) -> Point | None:
    """Return the crossing point for a transverse *interior* intersection.

    Returns ``None`` when the segments do not cross properly: no intersection,
    endpoint contact, parallel/collinear overlap, or degenerate segments.
    """
    p1, p2 = s1
    q1, q2 = s2
    if _bbox_reject(p1, p2, q1, q2):
        return None

    r = (p2[0] - p1[0], p2[1] - p1[1])
    s = (q2[0] - q1[0], q2[1] - q1[1])
    r_cross_s = _cross(r[0], r[1], s[0], s[1])
    if abs(r_cross_s) <= EPS:
        # Parallel or collinear -> overlap/touch, never a transverse crossing.
        return None

    qp = (q1[0] - p1[0], q1[1] - p1[1])
    t = _cross(qp[0], qp[1], s[0], s[1]) / r_cross_s
    u = _cross(qp[0], qp[1], r[0], r[1]) / r_cross_s

    # Strict interior on both segments: t, u must be strictly inside (0, 1).
    # This single rule also excludes every endpoint-contact configuration.
    if not (EPS < t < 1.0 - EPS and EPS < u < 1.0 - EPS):
        return None

    return (p1[0] + t * r[0], p1[1] + t * r[1])


def _segments(coords: Sequence[Sequence[float]]) -> list[Segment]:
    pts: list[Point] = [(float(c[0]), float(c[1])) for c in coords]
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def find_intersections(
    roads: dict[str, list[Sequence[float]]],
) -> list[dict]:
    """Find transverse interior intersections between different roads.

    ``roads`` maps road id -> LineString coordinate list (only roads with a
    persisted, complete geometry).  Every crossing is reported once, with the
    two road ids ordered lexicographically and each side's zero-based segment
    index.  The result is sorted by (road_a, seg_a, road_b, seg_b).
    """
    ids = sorted(roads)
    out: list[dict] = []
    for i in range(len(ids)):
        id_a = ids[i]
        segs_a = _segments(roads[id_a])
        for j in range(i + 1, len(ids)):
            id_b = ids[j]
            segs_b = _segments(roads[id_b])
            for ia, sa in enumerate(segs_a):
                for ib, sb in enumerate(segs_b):
                    point = proper_intersection(sa, sb)
                    if point is None:
                        continue
                    a, b = id_a, id_b
                    sa_idx, sb_idx = ia, ib
                    if a > b:
                        a, b = b, a
                        sa_idx, sb_idx = sb_idx, sa_idx
                    out.append(
                        {
                            "road_a_id": a,
                            "road_a_segment": sa_idx,
                            "road_b_id": b,
                            "road_b_segment": sb_idx,
                            "coordinates": [point[0], point[1]],
                        }
                    )
    out.sort(
        key=lambda x: (
            x["road_a_id"],
            x["road_a_segment"],
            x["road_b_id"],
            x["road_b_segment"],
        )
    )
    return out


def crosses_antimeridian(coords: Sequence[Sequence[float]]) -> bool:
    """True when consecutive vertices span more than 180° of longitude.

    A genuine jump of >180° between adjacent vertices means the line wraps
    across the 180° meridian, where planar lon/lat geometry is meaningless.
    Data entirely on one side never trips this check.
    """
    for k in range(1, len(coords)):
        if abs(float(coords[k][0]) - float(coords[k - 1][0])) > 180.0:
            return True
    return False
