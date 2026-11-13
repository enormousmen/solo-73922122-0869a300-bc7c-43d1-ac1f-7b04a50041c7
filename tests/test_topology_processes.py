"""Black-box two-process tests for topology checks and verdicts.

Reuses the real two-server launcher so every assertion runs against two
independent uvicorn processes sharing one SQLite file.
"""
from __future__ import annotations

import threading
import time

import httpx
import pytest

from tests.test_concurrency import two_apis


@pytest.fixture(scope="module")
def apis(tmp_path_factory):
    db = tmp_path_factory.mktemp("topo") / "gis.db"
    with two_apis(db) as bases:
        yield bases


CROSS_POINTS = [
    {"id": "T1", "lon": 0.0, "lat": 0.0},
    {"id": "T2", "lon": 4.0, "lat": 4.0},
    {"id": "T3", "lon": 0.0, "lat": 4.0},
    {"id": "T4", "lon": 4.0, "lat": 0.0},
]


def _seed(apis):
    a, _ = apis
    r = httpx.post(
        a + "/api/v1/batches",
        json={
            "points": CROSS_POINTS,
            "roads": [
                {"id": "TR1", "point_ids": ["T1", "T2"]},
                {"id": "TR2", "point_ids": ["T3", "T4"]},
            ],
        },
        timeout=10,
    )
    assert r.status_code == 201, r.text


def _check(base):
    r = httpx.post(
        base + "/api/v1/topology/checks",
        json={"road_ids": ["TR1", "TR2"]},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _bindings(apis, hit):
    a, _ = apis
    roads = {
        x["id"]: x
        for x in httpx.get(a + "/api/v1/roads", timeout=10).json()["roads"]
    }
    return (
        {
            "road_id": hit["road_a_id"],
            "segment": hit["road_a_segment"],
            "lineage": roads[hit["road_a_id"]]["lineage"],
            "revision": roads[hit["road_a_id"]]["revision"],
        },
        {
            "road_id": hit["road_b_id"],
            "segment": hit["road_b_segment"],
            "lineage": roads[hit["road_b_id"]]["lineage"],
            "revision": roads[hit["road_b_id"]]["revision"],
        },
    )


def test_check_and_verdict_work_across_processes(apis):
    _seed(apis)
    a, b = apis
    hit = _check(a)["intersections"][0]
    ba, bb = _bindings(apis, hit)
    r = httpx.post(
        b + "/api/v1/topology/verdicts",
        json={"road_a": ba, "road_b": bb, "status": "confirmed", "reason": "B 确认"},
        timeout=10,
    )
    assert r.status_code == 201, r.text
    # The verdict committed by B is immediately visible to A, and the check on
    # A reports confirmed.
    check_a = _check(a)
    assert check_a["intersections"][0]["review_status"] == "confirmed"
    assert check_a["intersections"][0]["verdict"]["reason"] == "B 确认"


def test_check_never_blocks_ingest_on_the_other_process(apis):
    """A long-running read-only check must not hold the SQLite write lock."""
    a, b = apis

    # Monkeypatch-free load simulation: fire many checks concurrently on A
    # while B performs writes; every write must complete promptly.
    stop = threading.Event()
    errors: list[str] = []

    def checker():
        try:
            while not stop.is_set():
                r = httpx.post(
                    a + "/api/v1/topology/checks",
                    json={"road_ids": ["TR1", "TR2"]},
                    timeout=10,
                )
                if r.status_code != 200:
                    errors.append(f"check {r.status_code}")
        except Exception as exc:  # pragma: no cover
            errors.append(str(exc))

    threads = [threading.Thread(target=checker) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        for i in range(8):
            t0 = time.time()
            r = httpx.post(
                b + "/api/v1/batches",
                json={"points": [{"id": "T1", "lon": i / 1000.0, "lat": 0.0}]},
                timeout=15,
            )
            elapsed = time.time() - t0
            assert r.status_code == 201, r.text
            assert elapsed < 5.0, f"ingest blocked by topology check ({elapsed:.2f}s)"
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)
    assert not errors, errors
    # restore the original coordinate so later tests keep a clean crossing
    httpx.post(
        b + "/api/v1/batches",
        json={"points": [{"id": "T1", "lon": 0.0, "lat": 0.0}]},
        timeout=10,
    )


def test_concurrent_repointing_makes_stale_verdict_return_409(apis):
    """Confirm on A while B changes the bound road: A must get 409, no row."""
    a, b = apis
    # re-seed fresh roads so bindings are deterministic regardless of history
    httpx.post(
        a + "/api/v1/batches",
        json={
            "points": [
                {"id": "U1", "lon": 0.0, "lat": 0.0},
                {"id": "U2", "lon": 4.0, "lat": 4.0},
                {"id": "U3", "lon": 0.0, "lat": 4.0},
                {"id": "U4", "lon": 4.0, "lat": 0.0},
            ],
            "roads": [
                {"id": "UR1", "point_ids": ["U1", "U2"]},
                {"id": "UR2", "point_ids": ["U3", "U4"]},
            ],
        },
        timeout=10,
    )
    r = httpx.post(
        a + "/api/v1/topology/checks",
        json={"road_ids": ["UR1", "UR2"]},
        timeout=10,
    )
    hit = r.json()["intersections"][0]
    roads = {
        x["id"]: x
        for x in httpx.get(a + "/api/v1/roads", timeout=10).json()["roads"]
    }
    payload = {
        "road_a": {
            "road_id": hit["road_a_id"],
            "segment": hit["road_a_segment"],
            "lineage": roads[hit["road_a_id"]]["lineage"],
            "revision": roads[hit["road_a_id"]]["revision"],
        },
        "road_b": {
            "road_id": hit["road_b_id"],
            "segment": hit["road_b_segment"],
            "lineage": roads[hit["road_b_id"]]["lineage"],
            "revision": roads[hit["road_b_id"]]["revision"],
        },
        "status": "confirmed",
        "reason": "stale",
    }

    # B changes a bound road between A's check and A's confirm.
    r = httpx.post(
        b + "/api/v1/batches",
        json={"points": [{"id": "U1", "lon": 0.4, "lat": 0.2}]},
        timeout=10,
    )
    assert r.status_code == 201

    r = httpx.post(a + "/api/v1/topology/verdicts", json=payload, timeout=10)
    assert r.status_code == 409
    listed = httpx.get(a + "/api/v1/topology/verdicts", timeout=10).json()
    assert all(
        v["road_a_id"] != "UR1" or not v["valid"] for v in listed["verdicts"]
    )

    # A fresh check + confirm with the new binding succeeds.
    hit2 = httpx.post(
        a + "/api/v1/topology/checks",
        json={"road_ids": ["UR1", "UR2"]},
        timeout=10,
    ).json()["intersections"][0]
    roads2 = {
        x["id"]: x
        for x in httpx.get(a + "/api/v1/roads", timeout=10).json()["roads"]
    }
    payload2 = {
        "road_a": {
            "road_id": hit2["road_a_id"],
            "segment": hit2["road_a_segment"],
            "lineage": roads2[hit2["road_a_id"]]["lineage"],
            "revision": roads2[hit2["road_a_id"]]["revision"],
        },
        "road_b": {
            "road_id": hit2["road_b_id"],
            "segment": hit2["road_b_segment"],
            "lineage": roads2[hit2["road_b_id"]]["lineage"],
            "revision": roads2[hit2["road_b_id"]]["revision"],
        },
        "status": "confirmed",
        "reason": "fresh",
    }
    r = httpx.post(a + "/api/v1/topology/verdicts", json=payload2, timeout=10)
    assert r.status_code == 201, r.text
    assert r.json()["valid"] is True
