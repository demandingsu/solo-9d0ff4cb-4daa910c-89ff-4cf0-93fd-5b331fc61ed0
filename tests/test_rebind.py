"""改挂 API 自测：补传后的改挂、目标去重、冲突整笔退回、版本核对、读写一致性。"""
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

S = "R30-001"  # 上一班：误收了下一班的一段点
T = "R30-002"  # 下一班：这些点应归属它
DATE = "2026-11-20"

S_DEPARTURE = "2026-11-20T22:00:00Z"
T_DEPARTURE = "2026-11-20T23:30:00Z"

# S 自己的点 seq1-2，误收的点 seq3-4（实际是 T 的 12、13 号点）
S_POINTS = [
    {"seq": 1, "sampled_at": "2026-11-20T22:00:00Z", "lat": 31.00, "lon": 121.00},
    {"seq": 2, "sampled_at": "2026-11-20T22:20:00Z", "lat": 31.02, "lon": 121.02},
    {"seq": 3, "sampled_at": "2026-11-20T23:30:00Z", "lat": 31.10, "lon": 121.10},
    {"seq": 4, "sampled_at": "2026-11-20T23:45:00Z", "lat": 31.12, "lon": 121.12},
]
T_POINTS = [
    {"seq": 10, "sampled_at": "2026-11-20T23:35:00Z", "lat": 31.20, "lon": 121.20},
    {"seq": 11, "sampled_at": "2026-11-20T23:50:00Z", "lat": 31.22, "lon": 121.22},
]
MAPPINGS = [
    {"source_seq": 3, "target_seq": 12},
    {"source_seq": 4, "target_seq": 13},
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(path))
    return TestClient(create_app())


# ---------- 辅助 ----------

def import_trips(client, trips):
    r = client.post("/trips/import", json={"trips": trips})
    assert r.status_code == 200, r.text
    return r.json()


def switch_day(client, d=DATE):
    token = client.get(f"/dates/{d}/state").json()["state_token"]
    r = client.post(f"/dates/{d}/switch", json={"expected_state": token})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "switched"
    return token


def setup_two_active_trips(client, s_points=None, t_points=None):
    """回填两班并切换供数，返回 (源批次版本, 目标批次版本)。"""
    import_trips(client, [
        {"trip_code": S, "departure_at": S_DEPARTURE, "points": s_points or S_POINTS},
        {"trip_code": T, "departure_at": T_DEPARTURE, "points": t_points or T_POINTS},
    ])
    switch_day(client)
    body = query(client, DATE, DATE)
    src = next(t for t in body["days"][0]["trips"] if t["trip_code"] == S)
    dst = next(t for t in body["days"][0]["trips"] if t["trip_code"] == T)
    return src["batch_version"], dst["batch_version"]


def rebind(client, src_version, dst_version, mappings=None, s=S, t=T):
    return client.post("/trips/rebind", json={
        "source_trip_code": s,
        "source_batch_version": src_version,
        "target_trip_code": t,
        "target_batch_version": dst_version,
        "mappings": mappings or MAPPINGS,
    })


def query(client, dfrom=DATE, dto=DATE):
    r = client.get("/trips", params={"date_from": dfrom, "date_to": dto})
    assert r.status_code == 200, r.text
    return r.json()


def trip_view(client, code, d=DATE):
    body = query(client, d, d)
    matches = [t for day in body["days"] for t in day["trips"] if t["trip_code"] == code]
    assert len(matches) == 1, matches
    return matches[0]


def seqs(trip):
    return [p["seq"] for p in trip["points"]]


# ---------- 成功路径 ----------

def test_rebind_moves_points_and_keeps_late_uploaded_points(client):
    """核对后双方都补传过点（已有新批次），改挂在最新批次上，补传点保留。"""
    src_v, dst_v = setup_two_active_trips(client)

    # 数据员核对（版本 1/1）后，两班各自补传新点：S 加 seq5，T 加 seq14
    import_trips(client, [
        {"trip_code": S, "points": S_POINTS + [
            {"seq": 5, "sampled_at": "2026-11-21T00:00:00Z", "lat": 31.14, "lon": 121.14},
        ]},
        {"trip_code": T, "points": T_POINTS + [
            {"seq": 14, "sampled_at": "2026-11-21T00:05:00Z", "lat": 31.24, "lon": 121.24},
        ]},
    ])
    assert trip_view(client, S)["batch_version"] == 2
    assert trip_view(client, T)["batch_version"] == 2

    # 用核对时的版本发起改挂：源点内容未变、目标点号仍为空 → 允许
    r = rebind(client, src_v, dst_v)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rebound"
    assert body["moved"] == MAPPINGS
    assert body["deduped"] == []
    assert body["source"]["batch_version"] == 3
    assert body["target"]["batch_version"] == 3

    src = trip_view(client, S)
    dst = trip_view(client, T)
    assert seqs(src) == [1, 2, 5]          # 3、4 移走，补传的 5 保留
    assert seqs(dst) == [10, 11, 12, 13, 14]  # 补传的 14 保留，移入 12、13
    moved_points = {p["seq"]: p for p in dst["points"]}
    assert moved_points[12]["sampled_at"] == "2026-11-20T23:30:00Z"
    assert moved_points[13]["lat"] == 31.12
    # 响应里返回受影响日期的状态指纹
    assert {d["service_date"] for d in body["dates"]} == {DATE}


def test_dedupe_when_target_already_has_same_point(client):
    """目标点号已有相同采样时刻和坐标：只从源班次移除，目标不重复。"""
    t_points = T_POINTS + [
        {"seq": 12, "sampled_at": "2026-11-20T23:30:00Z", "lat": 31.10, "lon": 121.10},
    ]
    src_v, dst_v = setup_two_active_trips(client, t_points=t_points)

    r = rebind(client, src_v, dst_v, mappings=[{"source_seq": 3, "target_seq": 12}])
    assert r.status_code == 200, r.text
    assert r.json()["deduped"] == [{"source_seq": 3, "target_seq": 12}]
    assert r.json()["moved"] == []

    assert seqs(trip_view(client, S)) == [1, 2, 4]
    dst = trip_view(client, T)
    assert seqs(dst) == [10, 11, 12]  # 没有重复的 12
    assert len([p for p in dst["points"] if p["seq"] == 12]) == 1


def test_rebind_only_touches_listed_points(client):
    """未在对应关系中的源点留在原班次。"""
    src_v, dst_v = setup_two_active_trips(client)
    r = rebind(client, src_v, dst_v, mappings=[{"source_seq": 3, "target_seq": 12}])
    assert r.status_code == 200, r.text
    assert seqs(trip_view(client, S)) == [1, 2, 4]
    assert seqs(trip_view(client, T)) == [10, 11, 12]


# ---------- 冲突：整笔退回 ----------

def test_conflicts_listed_and_whole_request_rejected(client):
    """源点缺失 + 源点内容变化 + 目标点号被不同的点占用，一次列全并整笔退回。"""
    src_v, dst_v = setup_two_active_trips(client)

    # 核对后：S 的 seq4 坐标被更正（内容变化），T 的 seq10 是已占用的不同点
    changed = [dict(p) for p in S_POINTS]
    changed[3] = {**changed[3], "lat": 31.12001}
    import_trips(client, [{"trip_code": S, "points": changed}])

    before_s = trip_view(client, S)
    before_t = trip_view(client, T)

    r = rebind(client, src_v, dst_v, mappings=[
        {"source_seq": 3, "target_seq": 10},   # 目标被不同的点占用
        {"source_seq": 4, "target_seq": 13},   # 源点内容在核对后变化
        {"source_seq": 99, "target_seq": 14},  # 源点缺失
    ])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    reasons = {(c.get("source_seq"), c["reason"]) for c in detail["conflicts"]}
    assert (3, "target_occupied") in reasons
    assert (4, "point_changed") in reasons
    assert (99, "source_missing") in reasons
    assert {c.get("target_seq") for c in detail["conflicts"]} == {10, 13, 14}
    assert "退回" in detail["message"]

    # 整笔退回：两班批次与查询结果都没有任何变化
    after_s = trip_view(client, S)
    after_t = trip_view(client, T)
    assert after_s["batch_version"] == before_s["batch_version"]
    assert after_t["batch_version"] == before_t["batch_version"]
    assert after_s["points"] == before_s["points"]
    assert after_t["points"] == before_t["points"]


def test_target_record_removed_after_check_is_conflict(client):
    """核对时目标点号上有记录，核对后该记录被删/改号（点号变空）→ 退回并列出点号。"""
    t_points = T_POINTS + [
        {"seq": 12, "sampled_at": "2026-11-20T23:30:00Z", "lat": 31.10, "lon": 121.10},
    ]
    src_v, dst_v = setup_two_active_trips(client, t_points=t_points)

    # 核对后目标班次重新提交：12 号点改成了 15 号（12 号变空）
    import_trips(client, [{"trip_code": T, "points": T_POINTS + [
        {"seq": 15, "sampled_at": "2026-11-20T23:30:00Z", "lat": 31.10, "lon": 121.10},
    ]}])

    r = rebind(client, src_v, dst_v, mappings=[{"source_seq": 3, "target_seq": 12}])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["conflicts"] == [
        {"source_seq": 3, "target_seq": 12, "reason": "target_changed"}
    ]


def test_same_point_arriving_after_check_is_conflict(client):
    """核对后目标班次才补上同一个点：目标点号上的记录已与核对时不同 → 退回。"""
    src_v, dst_v = setup_two_active_trips(client)
    import_trips(client, [{"trip_code": T, "points": T_POINTS + [
        {"seq": 12, "sampled_at": "2026-11-20T23:30:00Z", "lat": 31.10, "lon": 121.10},
    ]}])

    r = rebind(client, src_v, dst_v, mappings=[{"source_seq": 3, "target_seq": 12}])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["conflicts"] == [
        {"source_seq": 3, "target_seq": 12, "reason": "target_changed"}
    ]
    # 整笔退回：源班次点位不动
    assert seqs(trip_view(client, S)) == [1, 2, 3, 4]


def test_dedupe_when_target_has_same_point_at_check(client):
    """目标点号在核对时就已有相同采样时刻和坐标：允许，只从源班次移除。"""
    t_points = T_POINTS + [
        {"seq": 12, "sampled_at": "2026-11-20T23:30:00Z", "lat": 31.10, "lon": 121.10},
    ]
    src_v, dst_v = setup_two_active_trips(client, t_points=t_points)
    # 核对后源班次补传一个新点 → 已有新批次，但涉及的点未变
    import_trips(client, [{"trip_code": S, "points": S_POINTS + [
        {"seq": 6, "sampled_at": "2026-11-21T00:15:00Z", "lat": 31.15, "lon": 121.15},
    ]}])

    r = rebind(client, src_v, dst_v, mappings=[{"source_seq": 3, "target_seq": 12}])
    assert r.status_code == 200, r.text
    assert r.json()["deduped"] == [{"source_seq": 3, "target_seq": 12}]
    assert seqs(trip_view(client, S)) == [1, 2, 4, 6]
    assert seqs(trip_view(client, T)) == [10, 11, 12]


def test_unknown_reference_batch_version_rejected(client):
    src_v, dst_v = setup_two_active_trips(client)
    r = rebind(client, 99, dst_v)
    assert r.status_code == 409, r.text
    assert {c["reason"] for c in r.json()["detail"]["conflicts"]} == {
        "source_batch_not_found"
    }

    r = rebind(client, src_v, 99)
    assert r.status_code == 409, r.text
    assert {c["reason"] for c in r.json()["detail"]["conflicts"]} == {
        "target_batch_not_found"
    }


def test_trip_not_active_in_new_rejected(client):
    """未在新仓供数的班次不能改挂。"""
    import_trips(client, [
        {"trip_code": S, "departure_at": S_DEPARTURE, "points": S_POINTS},
        {"trip_code": T, "departure_at": "2026-11-21T00:20:00Z", "points": T_POINTS},
    ])
    # 只切换 11-20：S 已激活，T（11-21）仍未在新仓供数
    switch_day(client, "2026-11-20")
    src_v = trip_view(client, S)["batch_version"]
    r = rebind(client, src_v, 1)
    assert r.status_code == 409, r.text
    assert "尚未在新仓供数" in r.json()["detail"]["message"]

    # 两班都未切换时同样拒绝
    import_trips(client, [
        {"trip_code": "R30-900", "departure_at": "2026-11-25T10:00:00Z", "points": [
            {"seq": 1, "sampled_at": "2026-11-25T10:00:00Z", "lat": 31.0, "lon": 121.0},
        ]},
    ])
    r = rebind(client, 1, 1)
    assert r.status_code == 409, r.text


def test_unknown_trip_is_404(client):
    src_v, dst_v = setup_two_active_trips(client)
    r = rebind(client, src_v, dst_v, s="NOPE")
    assert r.status_code == 404


def test_validation_requires_one_to_one_mapping(client):
    # 源点号重复
    r = client.post("/trips/rebind", json={
        "source_trip_code": S, "source_batch_version": 1,
        "target_trip_code": T, "target_batch_version": 1,
        "mappings": [{"source_seq": 3, "target_seq": 12},
                     {"source_seq": 3, "target_seq": 13}],
    })
    assert r.status_code == 422
    # 目标点号重复
    r = client.post("/trips/rebind", json={
        "source_trip_code": S, "source_batch_version": 1,
        "target_trip_code": T, "target_batch_version": 1,
        "mappings": [{"source_seq": 3, "target_seq": 12},
                     {"source_seq": 4, "target_seq": 12}],
    })
    assert r.status_code == 422
    # 源班次与目标班次相同
    r = client.post("/trips/rebind", json={
        "source_trip_code": S, "source_batch_version": 1,
        "target_trip_code": S, "target_batch_version": 1,
        "mappings": [{"source_seq": 3, "target_seq": 12}],
    })
    assert r.status_code == 422
    # 空对应关系
    r = client.post("/trips/rebind", json={
        "source_trip_code": S, "source_batch_version": 1,
        "target_trip_code": T, "target_batch_version": 1,
        "mappings": [],
    })
    assert r.status_code == 422


# ---------- 一致性 ----------

def test_rebind_across_departure_dates_keeps_range_consistent(client):
    """两班发车日不同：改挂后两班各自的最新批次一起生效，范围查询同时可见。"""
    import_trips(client, [
        {"trip_code": S, "departure_at": "2026-11-20T23:50:00Z", "points": S_POINTS},
        {"trip_code": T, "departure_at": "2026-11-21T00:20:00Z", "points": T_POINTS},
    ])
    switch_day(client, "2026-11-20")
    switch_day(client, "2026-11-21")
    body = query(client, "2026-11-20", "2026-11-21")
    src = next(t for d in body["days"] for t in d["trips"] if t["trip_code"] == S)
    dst = next(t for d in body["days"] for t in d["trips"] if t["trip_code"] == T)

    r = rebind(client, src["batch_version"], dst["batch_version"])
    assert r.status_code == 200, r.text

    body = query(client, "2026-11-20", "2026-11-21")
    src = next(t for d in body["days"] for t in d["trips"] if t["trip_code"] == S)
    dst = next(t for d in body["days"] for t in d["trips"] if t["trip_code"] == T)
    assert seqs(src) == [1, 2]
    assert seqs(dst) == [10, 11, 12, 13]


def test_range_query_never_mixes_pre_and_post_rebind(client):
    """读写交错：查询线程无论何时读，都只能看到改挂前或改挂后的完整状态。

    改挂反复在两个方向进行（S 的 3→T 的 12，再把 T 的 12 改回 S 的 3）。
    任一时刻两班中恰好有一班持有该点，且两班点位总数不变——不会出现"源班次已
    移除、目标班次还没收到"这类半笔状态，也不会读到某班次的中间批次。
    """
    setup_two_active_trips(client)
    stop = threading.Event()
    errors = []
    observations = [0]

    def checker():
        try:
            while not stop.is_set():
                body = query(client, DATE, DATE)
                trips = {t["trip_code"]: t for t in body["days"][0]["trips"]}
                s_seqs, t_seqs = set(seqs(trips[S])), set(seqs(trips[T]))
                has_point = (3 in s_seqs) + (12 in t_seqs)
                if has_point != 1:
                    errors.append((sorted(s_seqs), sorted(t_seqs)))
                    return
                if len(s_seqs) + len(t_seqs) != 6:  # 2 + 2 + 移走的 2
                    errors.append((sorted(s_seqs), sorted(t_seqs)))
                    return
                observations[0] += 1
        except Exception as exc:  # 读写并发下的异常也要暴露
            errors.append(repr(exc))

    thread = threading.Thread(target=checker)
    thread.start()
    try:
        for _ in range(8):
            src, dst = trip_view(client, S), trip_view(client, T)
            r = rebind(client, src["batch_version"], dst["batch_version"])
            assert r.status_code == 200, r.text
            src, dst = trip_view(client, S), trip_view(client, T)
            r = rebind(client, src["batch_version"], dst["batch_version"],
                       s=T, t=S,
                       mappings=[{"source_seq": 12, "target_seq": 3},
                                 {"source_seq": 13, "target_seq": 4}])
            assert r.status_code == 200, r.text
    finally:
        stop.set()
        thread.join(timeout=10)

    assert not errors, errors
    assert observations[0] > 0  # 查询线程确实在改挂期间读到了数据
