# 市政道路线形加工与拓扑核查服务

测绘队共享测点 → 路段线形（GeoJSON LineString）加工入库，并在交付前做路网拓扑核查与立交裁决。
FastAPI + SQLite（WAL），两个 API 进程共用一个数据库文件，Docker Compose 一键启动。

## 启动

```bash
docker compose up --build -d
```

* API A：<http://localhost:8001>  Swagger UI：<http://localhost:8001/docs>
* API B：<http://localhost:8002>  Swagger UI：<http://localhost:8002/docs>
* 两个进程共用卷 `gis-data:/data/gis.db`（命名卷，`docker compose down` 不会丢数据；要清空请 `docker compose down -v`）。

跑一遍样例（可选，会自动做在线演示，见文末）：

```bash
docker compose --profile demo run --rm demo
```

重启验证（数据仍在）：

```bash
docker compose restart api-a api-b
curl -s http://localhost:8001/api/v1/state | head -c 400
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/batches` | 一个批次=一个事务：批量新增/更正测点与路段、批量删除；返回受影响路段的最终状态 |
| GET | `/api/v1/state` | **一个读快照**返回全部测点原值 + 全部已持久化线形（含 `generation`、`lineage`） |
| GET | `/api/v1/points` / `/api/v1/points/{id}` | 查询测点 |
| GET | `/api/v1/roads` / `/api/v1/roads/{id}` | 查询路段（只读已持久化的 `geometry`，不现场组线） |
| POST | `/api/v1/topology/checks` | 对 1~100 个路段做平面拓扑核查，返回不同路段线段内部的横向交点（双方 ID + 线段序号） |
| POST | `/api/v1/topology/verdicts` | 对所见交点填写理由、确认真实立交或改标待复查；道路已变化时返回 409 |
| GET | `/api/v1/topology/verdicts` | 列出裁决（可按 `status` / `road_id` 过滤，含 `valid` 有效性） |
| GET | `/health` | 健康检查（含 WAL 模式） |

批次请求体（所有字段可选，按 `points → roads → delete_roads → delete_points` 顺序生效）：

```json
{
  "points":       [{"id": "PT-001", "lon": 116.397428, "lat": 39.909230}],
  "roads":        [{"id": "RD-101", "point_ids": ["PT-001", "PT-002", "PT-003"]}],
  "delete_points": ["PT-004"],
  "delete_roads":  ["RD-102"]
}
```

* 路段 `point_ids`：≥ 2 个、有序、批内不可重复；允许引用**尚不存在**的测点（该路段进入 `incomplete`）。
* 已存在 id 视为**更正**（upsert）：测点改坐标、路段改引用顺序/成员。
* 同一 id 不能在同一批次里既 upsert 又 delete（返回 422）。
* 演示用查询参数：`pause_ms`（组线暂停，**事务外**）、`fail=true`（写完后注入异常以演示回滚）。
  `/docs` 的 “Try it out” 可直接选中这两个参数演示。

响应里的路段：

```json
{
  "id": "RD-101",
  "point_ids": ["PT-001", "PT-002", "PT-003"],
  "status": "ok",                 // ok | incomplete
  "geometry": {"type": "LineString", "coordinates": [[116.397428, 39.909230], ...]},
  "missing_point_ids": [],        // incomplete 时按路段顺序列出缺失测点 ID
  "revision": 3,                  // 每受影响一次 +1；删除后同 ID 重建回到 1
  "lineage": "9f8e7d6c5b4a3928",  // 线形化身标识；同 ID 重建会更换
  "updated_at": "2026-11-13T07:42:30.316Z"
}
```

## 关键行为

1. **组线即持久化**：线形在提交事务内按 `point_ids` 顺序组装，写入 `roads.geometry`；
   查询只 SELECT 该列，绝不临时组线。
2. **缺点不输出残线**：任一引用测点不存在 → `geometry = null`、`status = incomplete`、
   `missing_point_ids` 列明缺失 ID（按路段顺序去重）。补点后**下一次批次**自动恢复线形，无需额外调用。
3. **影响面完整**：更正/删除测点、改变路段引用时，所有受影响路段（含引用该测点的全部路段）在
   **同一事务**里重算；未受影响的路段不重写（`revision` 不变）。
4. **整批原子**：批次中途任何异常（含写完后注入）整体回滚，`generation` 不前进，库保持原状。
5. **重启保留**：SQLite 文件在命名卷里，`docker compose restart` / 重建容器后数据仍在。

## 拓扑核查与立交裁决

### 核查 `POST /api/v1/topology/checks`

请求体只含 1~100 个不重复路段 ID。对**不跨 180° 经线**的城区数据，把经纬度当作平面
直角坐标（lon/lat 直接参与运算），逐线段求交：

* 只返回**不同路段、线段内部**的横向（proper）交点，给出双方路段 ID 与 **0 起线段序号**
  （线段 i = 顶点 i 与 i+1 之间）及平面交点坐标；
* **端点接触**（含共用测点、T 形接头）与**共线重叠/贴合**一律忽略，同一路段的自交不报；
* 未核查路段在 `unchecked_roads` 列明原因：`unknown_id`（ID 不存在）、`incomplete`
  （缺测点、无持久化线形，附缺失测点 ID）、`antimeridian`（相邻顶点经度跳变 >180°）；
* 响应携带 `generation`：交点、所用线形与所附裁决全部来自**同一个已提交读快照**；
* 核查全程只读，**不持 SQLite 写锁**，另一个进程的入库不会被核查挡住。

每个交点附 `review_status` 与 `verdict`：没有有效裁决时为 `pending_review` + `verdict=null`；
存在有效裁决时为 `confirmed`（或人工改标的 `pending_review`）并内联裁决详情。

### 裁决 `POST /api/v1/topology/verdicts`

质检员为**核查时所见**的交点填写 `reason` 并提交 `status`（`confirmed` 确认真实立交，
或 `pending_review` 改回待复查）。请求里每一方携带核查响应/路段信息中的 `lineage` 与
`revision`，服务端在一个短写事务内：

1. 重读双方路段：任一方不存在、`lineage` 不符（删除后同 ID 重建）或 `revision` 不符
   （坐标或引用变化，**包括改回原值**）→ 返回 **409**，事务回滚，不留裁决；
2. 复核所报两条线段在当前线形上仍存在横向内部交点（线段越界/交点消失同样 409）；
3. 校验通过才写入/更新裁决，绑定双方当前 `(lineage, revision)` 与 `generation`。

裁决失效规则（`GET .../verdicts` 中 `valid=false`，核查响应不再内联）：

* 任一方相关坐标或引用变化即失效；**改回原值不能恢复**（revision 只增不减）；
* 路段删除后以同 ID 重建会获得**新 lineage**，旧裁决不能沿用；
* 无关路段、无关测点的变化不触及绑定 revision，裁决保持有效；
* 失败（含 409）整体回滚不留半成品；裁决持久化在 SQLite 中，**重启后有效裁决仍在**。

`roads` 响应中的 `lineage` 即当前线形的化身标识：每次新建（含同 ID 重建）重新生成，
普通更正保持不变、仅 `revision` 递增。

## 并发模型（两个进程共库）

* 每个请求一条短连接；写事务一律 `BEGIN IMMEDIATE` + `busy_timeout=30s`，两个进程的写入互斥但排队等待，不报 busy。
* **组线在事务之外**：先在一个 WAL 读快照上把批次规划完（含组线），再进入极短的写事务。
  因此 `pause_ms=2000` 的慢批次不会挡住另一进程的入库（演示第 5 步实测 < 1s 完成）。
* **提交前复核快照代号**（`store_meta.generation`）：规划期间若有别的进程提交过，
  本次规划整体作废、从新快照重算（响应里 `commit_attempts` 可见），
  绝不把基于旧坐标算出的线形写回库；并发新增 / 改引用的路段也在重算中被重新纳入，
  不会漏更新。
* **读一致性**：`/api/v1/state` 在一个延迟事务里完成全部读取，返回的测点原值与线形必定来自同一已提交状态。
* WAL 下读者不阻塞写者、写者不阻塞读者；`synchronous=FULL` 保证已提交数据断电安全。

## 本地运行与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
GIS_DB_PATH=/tmp/gis.db uvicorn app.main:app --port 8001

pytest            # 功能 + 双进程并发（测试会真的起两个 API 进程共库运行）
```

（无 docker 环境时也可直接验证整套并发行为：`tests/test_concurrency.py` 用
`tests/server_runner.py` 起两个独立进程，连接同一个 SQLite 文件。）

`tests/test_concurrency.py` 起两个真实 API 进程共库运行：跨进程一致性、暂停组线不持写锁、
并发改线无漏更新/旧坐标、`/state` 在持续写入下的快照一致性；
`tests/test_retry.py` 用可控方式制造"规划后另一进程提交"，断言旧规划被丢弃并重算。
`tests/test_topology.py` 覆盖交点判定（横向相交、端点/共线忽略、自交不报、线段序号、
跨 180°、未核查原因）与裁决全生命周期（确认/待复查、409、改回原值不复活、同 ID 重建、
无关变化不影响、失败不留痕、重启保留）；
`tests/test_topology_processes.py` 在两个真实进程间验证核查不阻塞入库、跨进程确认与
确认期间改线的 409 竞争。

## 在 /docs 手动演示

启动后打开 <http://localhost:8001/docs>（拓扑接口在 **topology** 标签下）：

1. `POST /api/v1/batches` 用下方示例体入库相交路段；
2. `POST /api/v1/topology/checks` 填路段 ID，Try it out 查看交点与线段序号；
3. 复制交点双方的 `road_id/segment`，再从 `GET /api/v1/roads/{id}` 取各自
   `lineage` 与 `revision`，填入 `POST /api/v1/topology/verdicts` 确认立交；
4. 再次调用核查接口确认状态变为 `confirmed`；
5. 用 `POST /api/v1/batches` 移动任一方测点后，用**原绑定**再确认 → 409，
   `GET /api/v1/topology/verdicts` 可见原裁决 `valid=false`。

## 演示样例（要点）

`examples/run_demo.py`（compose profile `demo`）对两个进程逐步演示：

1. 批量入库测点 + 3 条路段，线形已持久化；
2. 删除被引用测点 → `geometry=null`、`missing_point_ids` 列明；
3. 补回该点 → 线形自动恢复；
4. 改由 API B 更正共享测点 → 两条引用它的路段同批生效；
5. **可控暂停**：A 提交一个 `pause_ms=2000` 的批次，B 在暂停期间完成测点更正 + 新建路段
   （用时 < 1s，未被写锁阻塞）；A 提交时发现代号已变，整批重算后 `commit_attempts=2`，
   重算结果把 B 新建的 RD-105 一并纳入（`affected_road_ids` 含 RD-103/104/105），
   两条路段与 PT-006 原值一致；
6. `fail=true` 批次写完后抛错 → 整体回滚，`/state` 与失败前逐字段一致；
7. **拓扑核查**：两条路段线段内部横向相交被报出（双方 ID + 线段序号 + 交点坐标），
   仅端点相接的路段被忽略，缺点路段与不存在 ID 列入 `unchecked_roads`；
8. **立交确认与确认期间改线**：A 确认立交后，B 在 A 再次确认前移动绑定测点 →
   旧绑定提交得到 **409** 且不新增裁决；改回原值旧裁决仍失效；按新线形重新核查、
   用新 `lineage/revision` 再确认成功，裁决重新有效。

本地直接跑（不用 compose）：

```bash
uvicorn app.main:app --port 8001 &  uvicorn app.main:app --port 8002 &
python -m examples.run_demo --base-a http://localhost:8001 --base-b http://localhost:8002
```

## 说明

* 缺测点用 `point_id` 不加外键约束是刻意设计：悬空引用是"可表示状态"，才能稳定报告缺失 ID
  并在补点后自动恢复；路段删除时其引用行由 `ON DELETE CASCADE` 清理。
* 单机 SQLite 的写入吞吐上限是设计边界：本方案保证**正确性**，写入串行化 + 组线在事务外
  使得暂停/慢组线不会拖住其他入库。若未来需要多机横向扩展，应将 `store_meta.generation`
  复核替换为外部分布式锁/版本号。
