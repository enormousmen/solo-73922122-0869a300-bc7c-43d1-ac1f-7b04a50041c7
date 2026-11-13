"""Functional tests for planar topology checks and QC verdicts."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "db_path", str(tmp_path / "gis.db"))
    with TestClient(app) as c:
        yield c


def batch(client, body, **params):
    r = client.post("/api/v1/batches", json=body, params=params)
    assert r.status_code == 201, r.text
    return r.json()


def roads_map(client):
    return {r["id"]: r for r in client.get("/api/v1/roads").json()["roads"]}


CROSS = {
    "points": [
        {"id": "P1", "lon": 0.0, "lat": 0.0},
        {"id": "P2", "lon": 4.0, "lat": 4.0},
        {"id": "P3", "lon": 0.0, "lat": 4.0},
        {"id": "P4", "lon": 4.0, "lat": 0.0},
        {"id": "P5", "lon": 8.0, "lat": 8.0},
    ],
    "roads": [
        {"id": "R1", "point_ids": ["P1", "P2"]},
        {"id": "R2", "point_ids": ["P3", "P4"]},
        {"id": "R3", "point_ids": ["P2", "P5"]},
    ],
}


def _binding(roads, hit, side):
    return {
        "road_id": hit[f"road_{side}_id"],
        "segment": hit[f"road_{side}_segment"],
        "lineage": roads[hit[f"road_{side}_id"]]["lineage"],
        "revision": roads[hit[f"road_{side}_id"]]["revision"],
    }


def test_check_reports_transverse_intersection_with_ids_and_segments(client):
    batch(client, CROSS)
    r = client.post("/api/v1/topology/checks", json={"road_ids": ["R1", "R2", "R3"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    hit = body["intersections"][0]
    assert hit["road_a_id"] == "R1" and hit["road_b_id"] == "R2"
    assert hit["road_a_segment"] == 0 and hit["road_b_segment"] == 0
    assert hit["coordinates"] == pytest.approx([2.0, 2.0])
    assert hit["review_status"] == "pending_review"
    assert hit["verdict"] is None
    assert body["unchecked_roads"] == []
    assert body["checked_road_ids"] == ["R1", "R2", "R3"]
    assert "generation" in body


def test_endpoint_contact_and_collinear_overlap_are_ignored(client):
    # R1 and R2 share endpoint P2 (endpoint contact); R3 is collinear with R1
    # and overlaps its interior segment (R1 P1->P2, R3 P-mid->P5 through P2).
    batch(client, {
        "points": [
            {"id": "P1", "lon": 0.0, "lat": 0.0},
            {"id": "P2", "lon": 4.0, "lat": 4.0},
            {"id": "P3", "lon": 0.0, "lat": 4.0},
            {"id": "PM", "lon": 2.0, "lat": 2.0},
            {"id": "P5", "lon": 6.0, "lat": 6.0},
        ],
        "roads": [
            {"id": "R1", "point_ids": ["P1", "P2"]},
            {"id": "R2", "point_ids": ["P3", "P2"]},
            {"id": "R3", "point_ids": ["PM", "P5"]},
        ],
    })
    body = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2", "R3"]}
    ).json()
    assert body["total"] == 0
    assert body["intersections"] == []


def test_self_intersection_is_not_reported(client):
    # A bowtie road crosses its own two segments; checks compare different
    # roads only, so nothing is returned for a single-road request.
    batch(client, {
        "points": [
            {"id": "A", "lon": 0.0, "lat": 0.0},
            {"id": "B", "lon": 4.0, "lat": 4.0},
            {"id": "C", "lon": 0.0, "lat": 4.0},
            {"id": "D", "lon": 4.0, "lat": 0.0},
        ],
        "roads": [{"id": "R1", "point_ids": ["A", "B", "C", "D"]}],
    })
    body = client.post("/api/v1/topology/checks", json={"road_ids": ["R1"]}).json()
    assert body["total"] == 0


def test_intersection_on_second_segment_reports_correct_index(client):
    # R2's second segment (P4->P5) crosses R1; its first segment (P3->P4)
    # stays above R1 and only meets its support line outside R1.
    batch(client, {
        "points": [
            {"id": "P1", "lon": 0.0, "lat": 2.0},
            {"id": "P2", "lon": 6.0, "lat": 2.0},
            {"id": "P3", "lon": 1.0, "lat": 4.0},
            {"id": "P4", "lon": 2.0, "lat": 4.0},
            {"id": "P5", "lon": 4.0, "lat": 0.0},
        ],
        "roads": [
            {"id": "R1", "point_ids": ["P1", "P2"]},
            {"id": "R2", "point_ids": ["P3", "P4", "P5"]},
        ],
    })
    hits = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()["intersections"]
    assert len(hits) == 1
    hit = hits[0]
    assert hit["road_a_id"] == "R1" and hit["road_a_segment"] == 0
    assert hit["road_b_id"] == "R2" and hit["road_b_segment"] == 1
    assert hit["coordinates"] == pytest.approx([3.0, 2.0])


def test_unchecked_roads_unknown_incomplete(client):
    batch(client, {
        "points": CROSS["points"],
        "roads": [
            {"id": "R1", "point_ids": ["P1", "P2"]},
            {"id": "RX", "point_ids": ["P1", "GONE"]},
        ],
    })
    body = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "RX", "GHOST"]}
    ).json()
    reasons = {u["road_id"]: u["reason"] for u in body["unchecked_roads"]}
    assert reasons == {"RX": "incomplete", "GHOST": "unknown_id"}
    assert body["checked_road_ids"] == ["R1"]
    detail = next(u for u in body["unchecked_roads"] if u["road_id"] == "RX")
    assert "GONE" in detail["detail"]


def test_antimeridian_road_is_unchecked(client):
    batch(client, {
        "points": [
            {"id": "A", "lon": 179.0, "lat": 1.0},
            {"id": "B", "lon": -179.0, "lat": 2.0},
        ],
        "roads": [{"id": "RW", "point_ids": ["A", "B"]}],
    })
    body = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["RW"]}
    ).json()
    assert body["unchecked_roads"][0]["reason"] == "antimeridian"
    assert body["checked_road_ids"] == []


def test_request_limits_and_duplicates(client):
    r = client.post("/api/v1/topology/checks", json={"road_ids": []})
    assert r.status_code == 422
    r = client.post(
        "/api/v1/topology/checks", json={"road_ids": [f"R{i}" for i in range(101)]}
    )
    assert r.status_code == 422
    r = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R1"]}
    )
    assert r.status_code == 422


def test_confirm_then_check_shows_confirmed(client):
    batch(client, CROSS)
    roads = roads_map(client)
    hit = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()["intersections"][0]
    payload = {
        "road_a": _binding(roads, hit, "a"),
        "road_b": _binding(roads, hit, "b"),
        "status": "confirmed",
        "reason": "现场核实为跨线桥",
    }
    r = client.post("/api/v1/topology/verdicts", json=payload)
    assert r.status_code == 201, r.text
    verdict = r.json()
    assert verdict["valid"] is True
    assert verdict["intersection_point"] == pytest.approx([2.0, 2.0])
    assert verdict["generation"] >= 0

    again = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()
    hit2 = again["intersections"][0]
    assert hit2["review_status"] == "confirmed"
    assert hit2["verdict"]["reason"] == "现场核实为跨线桥"
    assert hit2["verdict"]["id"] == verdict["id"]

    listed = client.get("/api/v1/topology/verdicts").json()
    assert listed["total"] == 1 and listed["verdicts"][0]["valid"] is True
    only_confirmed = client.get(
        "/api/v1/topology/verdicts", params={"status": "confirmed"}
    ).json()
    assert only_confirmed["total"] == 1
    assert client.get(
        "/api/v1/topology/verdicts", params={"status": "pending_review"}
    ).json()["total"] == 0


def test_sides_are_normalised_when_submitted_reversed(client):
    batch(client, CROSS)
    roads = roads_map(client)
    hit = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()["intersections"][0]
    payload = {
        "road_a": _binding(roads, hit, "b"),
        "road_b": _binding(roads, hit, "a"),
        "status": "confirmed",
        "reason": "reversed sides",
    }
    r = client.post("/api/v1/topology/verdicts", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["road_a_id"] == "R1" and r.json()["road_b_id"] == "R2"


def test_change_bound_road_returns_409_and_invalidates(client):
    batch(client, CROSS)
    roads = roads_map(client)
    hit = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()["intersections"][0]
    payload = {
        "road_a": _binding(roads, hit, "a"),
        "road_b": _binding(roads, hit, "b"),
        "status": "confirmed",
        "reason": "ok",
    }
    assert client.post("/api/v1/topology/verdicts", json=payload).status_code == 201

    # Move a point referenced by R1 only.
    batch(client, {"points": [{"id": "P1", "lon": 0.5, "lat": 0.0}]})
    r = client.post("/api/v1/topology/verdicts", json=payload)
    assert r.status_code == 409
    assert "R1" in r.json()["detail"]

    verdicts = client.get("/api/v1/topology/verdicts").json()["verdicts"]
    assert len(verdicts) == 1
    assert verdicts[0]["valid"] is False

    # The check no longer presents a valid verdict.
    check = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()
    assert check["intersections"][0]["review_status"] == "pending_review"
    assert check["intersections"][0]["verdict"] is None


def test_reverting_to_original_values_does_not_revive_verdict(client):
    batch(client, CROSS)
    roads = roads_map(client)
    hit = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()["intersections"][0]
    payload = {
        "road_a": _binding(roads, hit, "a"),
        "road_b": _binding(roads, hit, "b"),
        "status": "confirmed",
        "reason": "ok",
    }
    client.post("/api/v1/topology/verdicts", json=payload)
    batch(client, {"points": [{"id": "P1", "lon": 0.5, "lat": 0.0}]})
    batch(client, {"points": [{"id": "P1", "lon": 0.0, "lat": 0.0}]})  # revert
    verdicts = client.get("/api/v1/topology/verdicts").json()["verdicts"]
    assert verdicts[0]["valid"] is False
    assert client.post("/api/v1/topology/verdicts", json=payload).status_code == 409


def test_unrelated_road_change_keeps_verdict_valid(client):
    batch(client, CROSS)
    roads = roads_map(client)
    hit = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()["intersections"][0]
    payload = {
        "road_a": _binding(roads, hit, "a"),
        "road_b": _binding(roads, hit, "b"),
        "status": "confirmed",
        "reason": "ok",
    }
    client.post("/api/v1/topology/verdicts", json=payload)
    # P5 is only referenced by R3.
    batch(client, {"points": [{"id": "P5", "lon": 9.0, "lat": 9.0}]})
    verdict = client.get("/api/v1/topology/verdicts").json()["verdicts"][0]
    assert verdict["valid"] is True


def test_delete_and_recreate_same_id_cannot_reuse_old_verdict(client):
    batch(client, CROSS)
    roads = roads_map(client)
    hit = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()["intersections"][0]
    payload = {
        "road_a": _binding(roads, hit, "a"),
        "road_b": _binding(roads, hit, "b"),
        "status": "confirmed",
        "reason": "ok",
    }
    client.post("/api/v1/topology/verdicts", json=payload)
    old_lineage = roads["R1"]["lineage"]

    batch(client, {"delete_roads": ["R1"]})
    assert client.post("/api/v1/topology/verdicts", json=payload).status_code == 409
    batch(client, {"roads": [{"id": "R1", "point_ids": ["P1", "P2"]}]})
    recreated = client.get("/api/v1/roads/R1").json()
    assert recreated["lineage"] != old_lineage
    assert recreated["revision"] == 1
    assert client.post("/api/v1/topology/verdicts", json=payload).status_code == 409
    assert client.get("/api/v1/topology/verdicts").json()["verdicts"][0]["valid"] is False


def test_reconfirm_with_fresh_binding_rebinds_and_revalidates(client):
    batch(client, CROSS)
    roads = roads_map(client)
    hit = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()["intersections"][0]
    old_payload = {
        "road_a": _binding(roads, hit, "a"),
        "road_b": _binding(roads, hit, "b"),
        "status": "confirmed",
        "reason": "first",
    }
    first = client.post("/api/v1/topology/verdicts", json=old_payload).json()

    batch(client, {"points": [{"id": "P1", "lon": 0.3, "lat": 0.1}]})
    assert client.post("/api/v1/topology/verdicts", json=old_payload).status_code == 409

    roads2 = roads_map(client)
    hit2 = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()["intersections"][0]
    new_payload = {
        "road_a": _binding(roads2, hit2, "a"),
        "road_b": _binding(roads2, hit2, "b"),
        "status": "confirmed",
        "reason": "second",
    }
    second = client.post("/api/v1/topology/verdicts", json=new_payload)
    assert second.status_code == 201, second.text
    # Same logical intersection -> same row, rebound, reason replaced.
    assert second.json()["id"] == first["id"]
    assert second.json()["reason"] == "second"
    assert second.json()["valid"] is True
    assert client.get("/api/v1/topology/verdicts").json()["total"] == 1


def test_verdict_requiring_same_road_rejected(client):
    batch(client, CROSS)
    roads = roads_map(client)
    r = client.post(
        "/api/v1/topology/verdicts",
        json={
            "road_a": {"road_id": "R1", "segment": 0,
                       "lineage": roads["R1"]["lineage"], "revision": 1},
            "road_b": {"road_id": "R1", "segment": 1,
                       "lineage": roads["R1"]["lineage"], "revision": 1},
            "status": "confirmed",
            "reason": "x",
        },
    )
    assert r.status_code == 422


def test_failed_verdict_leaves_no_row_and_keeps_generation(client):
    batch(client, CROSS)
    before = client.get("/api/v1/state").json()["generation"]
    r = client.post(
        "/api/v1/topology/verdicts",
        json={
            "road_a": {"road_id": "GHOST1", "segment": 0,
                       "lineage": "deadbeef", "revision": 1},
            "road_b": {"road_id": "GHOST2", "segment": 0,
                       "lineage": "deadbeef", "revision": 1},
            "status": "confirmed",
            "reason": "x",
        },
    )
    assert r.status_code == 409
    assert client.get("/api/v1/topology/verdicts").json()["total"] == 0
    assert client.get("/api/v1/state").json()["generation"] == before


def test_pending_review_verdict_is_distinct_from_confirmed(client):
    batch(client, CROSS)
    roads = roads_map(client)
    hit = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()["intersections"][0]
    payload = {
        "road_a": _binding(roads, hit, "a"),
        "road_b": _binding(roads, hit, "b"),
        "status": "pending_review",
        "reason": "需要外业补测",
    }
    r = client.post("/api/v1/topology/verdicts", json=payload)
    assert r.status_code == 201
    check = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()
    assert check["intersections"][0]["review_status"] == "pending_review"
    assert check["intersections"][0]["verdict"]["reason"] == "需要外业补测"


def test_verdict_filter_by_road(client):
    batch(client, CROSS)
    roads = roads_map(client)
    hit = client.post(
        "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
    ).json()["intersections"][0]
    client.post(
        "/api/v1/topology/verdicts",
        json={
            "road_a": _binding(roads, hit, "a"),
            "road_b": _binding(roads, hit, "b"),
            "status": "confirmed",
            "reason": "x",
        },
    )
    assert client.get(
        "/api/v1/topology/verdicts", params={"road_id": "R2"}
    ).json()["total"] == 1
    assert client.get(
        "/api/v1/topology/verdicts", params={"road_id": "R3"}
    ).json()["total"] == 0


def test_restart_keeps_valid_verdicts(tmp_path, monkeypatch):
    from app import config

    db = str(tmp_path / "gis.db")
    monkeypatch.setattr(config.settings, "db_path", db)
    with TestClient(app) as c:
        batch(c, CROSS)
        roads = roads_map(c)
        hit = c.post(
            "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
        ).json()["intersections"][0]
        c.post(
            "/api/v1/topology/verdicts",
            json={
                "road_a": _binding(roads, hit, "a"),
                "road_b": _binding(roads, hit, "b"),
                "status": "confirmed",
                "reason": "persist me",
            },
        )
    with TestClient(app) as c2:
        verdicts = c2.get("/api/v1/topology/verdicts").json()["verdicts"]
        assert verdicts[0]["valid"] is True
        check = c2.post(
            "/api/v1/topology/checks", json={"road_ids": ["R1", "R2"]}
        ).json()
        assert check["intersections"][0]["review_status"] == "confirmed"
