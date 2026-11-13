"""Scenario sample used by the docker compose demo.

Runs entirely against the public HTTP API of both processes, so it doubles as
a black-box acceptance check of the delivered environment:

1. shared points + roads are ingested through API A;
2. a referenced point goes missing -> no partial line, missing ids reported;
3. the point is re-supplied -> the line restores automatically;
4. a correction of one point fans out to every road referencing it;
5. a deliberately paused batch on A (assembly outside any write lock) runs
   while B keeps ingesting and repointing roads;
6. a failed batch rolls back; the final state is then re-read from B, and
   snapshots are validated against the persisted values;
7. topology check: transverse interior intersections are reported with both
   road ids and segment indices, endpoint contact is ignored;
8. a QC verdict confirms an overpass on A; B changing the bound road makes the
   stale confirmation fail with 409, and a fresh re-check re-confirms it.

Run locally::

    python -m examples.run_demo --base-a http://localhost:8001 \\
                                --base-b http://localhost:8002
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

import httpx

POINTS = {
    "PT-001": (116.397428, 39.909230),
    "PT-002": (116.407428, 39.913000),
    "PT-003": (116.417428, 39.918000),
    "PT-004": (116.427428, 39.921000),
    "PT-005": (116.437428, 39.926000),
    "PT-006": (116.447428, 39.930000),
}


def pretty(title: str, payload) -> None:
    print(f"\n=== {title} " + "=" * max(0, 66 - len(title)))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=60.0)

    def batch(self, body: dict, **params):
        r = self.http.post("/api/v1/batches", json=body, params=params)
        r.raise_for_status()
        return r.json()

    def road(self, road_id: str) -> dict:
        r = self.http.get(f"/api/v1/roads/{road_id}")
        r.raise_for_status()
        return r.json()

    def state(self) -> dict:
        r = self.http.get("/api/v1/state")
        r.raise_for_status()
        return r.json()

    def topo_check(self, road_ids: list[str]) -> dict:
        r = self.http.post("/api/v1/topology/checks", json={"road_ids": road_ids})
        r.raise_for_status()
        return r.json()

    def verdict(self, body: dict):
        return self.http.post("/api/v1/topology/verdicts", json=body)

    def verdicts(self, **params) -> dict:
        r = self.http.get("/api/v1/topology/verdicts", params=params)
        r.raise_for_status()
        return r.json()


def _run(args) -> None:
    a = Api(args.base_a)
    b = Api(args.base_b)
    print(f"API A = {a.base}\nAPI B = {b.base}  (both share /data/gis.db)")

    # -- 1 --------------------------------------------------------------
    points = [{"id": pid, "lon": lon, "lat": lat} for pid, (lon, lat) in POINTS.items()]
    roads = [
        {"id": "RD-101", "point_ids": ["PT-001", "PT-002", "PT-003"]},
        {"id": "RD-102", "point_ids": ["PT-003", "PT-004", "PT-005"]},
        {"id": "RD-103", "point_ids": ["PT-005", "PT-006"]},
    ]
    batch = a.batch({"points": points, "roads": roads})
    pretty("1. 批量入库（API A）: 测点 + 路段，线形已持久化", batch)

    # -- 2 --------------------------------------------------------------
    missing = a.batch({"delete_points": ["PT-004"]})
    pretty("2. 删除被引用测点 PT-004：不输出残线，列明缺失 ID", missing)
    assert missing["roads"][0]["geometry"] is None
    assert missing["roads"][0]["missing_point_ids"] == ["PT-004"]

    # -- 3 --------------------------------------------------------------
    restored = a.batch({"points": [{"id": "PT-004", "lon": POINTS["PT-004"][0], "lat": POINTS["PT-004"][1]}]})
    pretty("3. 补点后自动恢复线形（无需额外调用）", restored)
    assert restored["roads"][0]["status"] == "ok"

    # -- 4 --------------------------------------------------------------
    corrected = b.batch(
        {"points": [{"id": "PT-003", "lon": 116.417800, "lat": 39.919200}]}
    )
    pretty("4. 更正 PT-003（改由 API B 提交）：两条引用它的路段同时生效", corrected)
    assert sorted(corrected["affected_road_ids"]) == ["RD-101", "RD-102"]

    # -- 5 --------------------------------------------------------------
    # Paused batch on A: it reports affected roads based on the snapshot it
    # plans from, and commits only after B has already written.  Its commit
    # re-validates the snapshot, so it either waits or recomputes - but it can
    # never overwrite B's newer coordinates with its old ones.
    print("\n=== 5. 可控暂停下并发改线 " + "=" * 44)
    results: dict[str, object] = {}

    def paused_batch():
        t0 = time.time()
        results["a"] = a.batch(
            {
                "points": [{"id": "PT-006", "lon": 116.447900, "lat": 39.930400}],
                "roads": [{"id": "RD-104", "point_ids": ["PT-006", "PT-001"]}],
            },
            pause_ms=2000,
        )
        results["a_elapsed"] = round(time.time() - t0, 2)

    t = threading.Thread(target=paused_batch)
    t.start()
    time.sleep(0.3)  # the paused batch is now in its 2s assembly pause

    t0 = time.time()
    b_result = b.batch(
        {
            "points": [{"id": "PT-006", "lon": 116.447950, "lat": 39.930900}],
            "roads": [{"id": "RD-105", "point_ids": ["PT-006", "PT-002"]}],
        }
    )
    b_elapsed = round(time.time() - t0, 2)
    t.join()
    pretty(
        f"B 在 A 暂停期间完成入库，用时 {b_elapsed}s（未等待 A 的写锁）",
        b_result,
    )
    pretty(f"A 的暂停批次完成后返回，总用时 {results['a_elapsed']}s", results["a"])

    final = b.state()
    pretty("并发结束后（API B 读取）: 两条引用 PT-006 的路段都采用最后一次提交的值", {
        "PT-006": next(p for p in final["points"] if p["id"] == "PT-006"),
        "RD-104": b.road("RD-104"),
        "RD-105": b.road("RD-105"),
    })

    # -- 6 --------------------------------------------------------------
    before = b.state()
    try:
        a.batch(
            {
                "points": [
                    {"id": "PT-001", "lon": 1.0, "lat": 2.0},
                    {"id": "PT-999", "lon": 1.0, "lat": 3.0},
                ],
                "roads": [{"id": "RD-101", "point_ids": ["PT-999", "PT-002"]}],
            },
            fail=True,
        )
        raise SystemExit("failure injection did not fail")
    except httpx.HTTPStatusError as exc:
        pretty("6. 批内失败（写后注入异常）：整体回滚", exc.response.json())

    after = b.state()
    assert after == before, "failed batch leaked changes"
    pretty("回滚后状态与失败前完全一致", {"generation": after["generation"]})

    # consistency check on the whole snapshot
    coords = {p["id"]: [p["lon"], p["lat"]] for p in after["points"]}
    for road in after["roads"]:
        if road["geometry"] is None:
            # incomplete roads carry no partial line by design
            continue
        for pid, pos in zip(road["point_ids"], road["geometry"]["coordinates"]):
            assert pos == coords[pid], f"{road['id']} geometry does not match point {pid}"

    pretty(
        "最终一致性校验通过：每条线形的每个坐标都等于同一快照里的测点原值",
        {
            "generation": after["generation"],
            "points": len(after["points"]),
            "roads": {
                r["id"]: ("ok" if r["status"] == "ok" else f"缺 {r['missing_point_ids']}")
                for r in after["roads"]
            },
        },
    )

    # -- 7 --------------------------------------------------------------
    # Dedicated topology fixtures: TOPO-1 and TOPO-2 cross transversely in
    # their segment interiors; TOPO-3 only shares an endpoint with TOPO-1
    # (must be ignored); TOPO-X references a missing point (unchecked).
    topo_points = [
        {"id": "TP-1", "lon": 116.5000, "lat": 39.9000},
        {"id": "TP-2", "lon": 116.5400, "lat": 39.9400},
        {"id": "TP-3", "lon": 116.5000, "lat": 39.9400},
        {"id": "TP-4", "lon": 116.5400, "lat": 39.9000},
        {"id": "TP-5", "lon": 116.5600, "lat": 39.9600},
    ]
    topo_roads = [
        {"id": "TOPO-1", "point_ids": ["TP-1", "TP-2"]},
        {"id": "TOPO-2", "point_ids": ["TP-3", "TP-4"]},
        {"id": "TOPO-3", "point_ids": ["TP-2", "TP-5"]},  # endpoint contact only
        {"id": "TOPO-X", "point_ids": ["TP-1", "TP-GONE"]},  # incomplete
    ]
    a.batch({"points": topo_points, "roads": topo_roads})

    check = a.topo_check(["TOPO-1", "TOPO-2", "TOPO-3", "TOPO-X", "TOPO-GHOST"])
    pretty(
        "7. 拓扑核查：横向内部交点 + 双方 ID/线段序号；端点接触忽略、缺点/未知列未核查",
        check,
    )
    assert check["total"] == 1, check
    hit = check["intersections"][0]
    assert hit["road_a_id"] == "TOPO-1" and hit["road_b_id"] == "TOPO-2"
    assert hit["road_a_segment"] == 0 and hit["road_b_segment"] == 0
    # On a pristine database the intersection starts unreviewed; on a repeated
    # demo run it may already carry the verdict from a previous run.
    print("交点当前裁决状态:", hit["review_status"])
    unchecked = {u["road_id"]: u["reason"] for u in check["unchecked_roads"]}
    assert unchecked == {"TOPO-X": "incomplete", "TOPO-GHOST": "unknown_id"}

    # -- 8 --------------------------------------------------------------
    roads_now = {r["id"]: r for r in a.state()["roads"]}

    def binding(side: str) -> dict:
        return {
            "road_id": hit[f"road_{side}_id"],
            "segment": hit[f"road_{side}_segment"],
            "lineage": roads_now[hit[f"road_{side}_id"]]["lineage"],
            "revision": roads_now[hit[f"road_{side}_id"]]["revision"],
        }

    verdict_body = {
        "road_a": binding("a"),
        "road_b": binding("b"),
        "status": "confirmed",
        "reason": "现场复核为跨线桥，垂直净距 5.2m，确认无平面冲突",
    }
    confirmed = a.verdict(verdict_body)
    confirmed.raise_for_status()
    pretty("8a. 确认立交（API A）：裁决绑定双方 lineage/revision", confirmed.json())

    recheck = a.topo_check(["TOPO-1", "TOPO-2"])
    assert recheck["intersections"][0]["review_status"] == "confirmed"
    pretty("再次核查：该交点状态变为 confirmed，携带有效裁决", recheck)

    # B moves a survey point of the bound road while A holds the old binding.
    moved = b.batch(
        {"points": [{"id": "TP-1", "lon": 116.5004, "lat": 39.9002}]}
    )
    pretty("8b. 确认期间 API B 改线：TOPO-1 revision 前进，旧裁决失效", {
        "affected": moved["affected_road_ids"],
        "TOPO-1": next(r for r in moved["roads"] if r["id"] == "TOPO-1"),
    })

    stale = a.verdict(verdict_body)
    pretty("用旧绑定再次确认 -> 409，且不产生任何裁决", stale.json())
    assert stale.status_code == 409

    # Reverting to the original coordinate must NOT revive the old verdict.
    b.batch({"points": [{"id": "TP-1", "lon": 116.5000, "lat": 39.9000}]})
    still_stale = a.verdict(verdict_body)
    assert still_stale.status_code == 409
    listed = a.verdicts()
    assert listed["verdicts"][0]["valid"] is False
    pretty("改回原值后旧裁决仍失效（revision 已前进）", {
        "verdict_id": listed["verdicts"][0]["id"],
        "valid": listed["verdicts"][0]["valid"],
    })

    # A fresh check returns the new binding; the old verdict is invalid, so the
    # intersection is presented for review again. Re-confirming succeeds.
    fresh_check = a.topo_check(["TOPO-1", "TOPO-2"])
    fresh_hit = fresh_check["intersections"][0]
    assert fresh_hit["verdict"] is None or fresh_hit["verdict"]["valid"] is False
    fresh_roads = {r["id"]: r for r in a.state()["roads"]}

    def fresh_binding(side: str) -> dict:
        return {
            "road_id": fresh_hit[f"road_{side}_id"],
            "segment": fresh_hit[f"road_{side}_segment"],
            "lineage": fresh_roads[fresh_hit[f"road_{side}_id"]]["lineage"],
            "revision": fresh_roads[fresh_hit[f"road_{side}_id"]]["revision"],
        }

    reconfirmed = a.verdict(
        {
            "road_a": fresh_binding("a"),
            "road_b": fresh_binding("b"),
            "status": "confirmed",
            "reason": "改线后重新复核，维持立交结论",
        }
    )
    reconfirmed.raise_for_status()
    pretty("按新线形重新核查并确认 -> 201，裁决重新有效", reconfirmed.json())
    assert a.verdicts(status="confirmed")["total"] == 1

    print(
        "\n演示完成。重启 api-a / api-b 后测点、线形与有效裁决均保留"
        "（SQLite WAL 持久化）。"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-a", default="http://localhost:8001")
    ap.add_argument("--base-b", default="http://localhost:8002")
    _run(ap.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
