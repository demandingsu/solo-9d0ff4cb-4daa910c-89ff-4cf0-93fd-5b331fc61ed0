"""迁仓 API 自测：跨夜归仓、逐日回填与切换守卫、改期换日、同一提交状态。"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

R1 = "R10-001"  # 跨夜班次：11-20 23:40 发车，轨迹跨到 11-21
R2 = "R10-002"  # 11-21 早班
R3 = "R10-003"  # 11-22 晚班

# 旧仓里 R1 的点位按采样日拆在两天（迁仓前形态）
LEGACY_R1 = [
    {"trip_code": R1, "sampled_at": "2026-11-20T23:40:00Z", "lat": 31.10, "lon": 121.40},
    {"trip_code": R1, "sampled_at": "2026-11-20T23:55:00Z", "lat": 31.12, "lon": 121.43},
    {"trip_code": R1, "sampled_at": "2026-11-21T00:10:00Z", "lat": 31.15, "lon": 121.46},
    {"trip_code": R1, "sampled_at": "2026-11-21T00:30:00Z", "lat": 31.18, "lon": 121.49},
]
LEGACY_R2 = [
    {"trip_code": R2, "sampled_at": "2026-11-21T08:00:00Z", "lat": 31.20, "lon": 121.50},
    {"trip_code": R2, "sampled_at": "2026-11-21T08:15:00Z", "lat": 31.22, "lon": 121.52},
]
LEGACY_R3 = [
    {"trip_code": R3, "sampled_at": "2026-11-22T22:00:00Z", "lat": 31.30, "lon": 121.60},
    {"trip_code": R3, "sampled_at": "2026-11-22T22:30:00Z", "lat": 31.32, "lon": 121.62},
]

# 新仓回填：R1 整趟轨迹（含跨零点点位）一次全量提交
R1_POINTS = [
    {"seq": 1, "sampled_at": "2026-11-20T23:40:00Z", "lat": 31.10, "lon": 121.40},
    {"seq": 2, "sampled_at": "2026-11-20T23:55:00Z", "lat": 31.12, "lon": 121.43},
    {"seq": 3, "sampled_at": "2026-11-21T00:10:00Z", "lat": 31.15, "lon": 121.46},
    {"seq": 4, "sampled_at": "2026-11-21T00:30:00Z", "lat": 31.18, "lon": 121.49},
]
R1_DEPARTURE = "2026-11-20T23:40:00Z"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    return TestClient(create_app())


# ---------- 辅助 ----------

def seed_legacy(client, points):
    r = client.post("/legacy/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def import_trips(client, trips):
    r = client.post("/trips/import", json={"trips": trips})
    assert r.status_code == 200, r.text
    return r.json()


def backfill_r1(client, points=None):
    return import_trips(
        client,
        [{"trip_code": R1, "departure_at": R1_DEPARTURE, "points": points or R1_POINTS}],
    )


def state_token(client, d):
    r = client.get(f"/dates/{d}/state")
    assert r.status_code == 200, r.text
    return r.json()["state_token"]


def switch(client, d, token):
    return client.post(f"/dates/{d}/switch", json={"expected_state": token})


def switch_day(client, d):
    r = switch(client, d, state_token(client, d))
    assert r.status_code == 200 and r.json()["status"] == "switched", r.text
    return r.json()


def query(client, dfrom, dto):
    r = client.get("/trips", params={"date_from": dfrom, "date_to": dto})
    assert r.status_code == 200, r.text
    return r.json()


def occurrences(body, code):
    """该班次在范围查询结果中出现的 (date, source, trip) 列表。"""
    return [
        (day["date"], t["source"], t)
        for day in body["days"]
        for t in day["trips"]
        if t["trip_code"] == code
    ]


def day_of(body, d):
    return next(day for day in body["days"] if day["date"] == d)


# ---------- 用例 ----------

def test_unswitched_date_serves_legacy_by_sampling_day(client):
    """未切换的日期仍从旧仓按采样日取数：跨夜班次被拆在两天。"""
    seed_legacy(client, LEGACY_R1 + LEGACY_R2)
    body = query(client, "2026-11-20", "2026-11-21")

    assert day_of(body, "2026-11-20")["source"] == "legacy"
    assert day_of(body, "2026-11-21")["source"] == "legacy"
    occ = occurrences(body, R1)
    assert len(occ) == 2  # 旧仓形态：同一趟车按采样日拆成两天的切片
    assert {p["sampled_at"] for _, _, t in occ for p in t["points"]} == {
        p["sampled_at"] for p in LEGACY_R1
    }


def test_cross_midnight_trip_attributed_to_departure_date(client):
    """跨夜整趟轨迹归到发车日；切换生效；每趟车只出现一次。"""
    seed_legacy(client, LEGACY_R1 + LEGACY_R2)
    backfill_r1(client)

    # 回填后、切换前：仍从旧仓取数
    body = query(client, "2026-11-20", "2026-11-21")
    assert len(occurrences(body, R1)) == 2

    switch_day(client, "2026-11-20")
    body = query(client, "2026-11-20", "2026-11-21")

    day20 = day_of(body, "2026-11-20")
    assert day20["source"] == "migrated"
    occ = occurrences(body, R1)
    assert len(occ) == 1  # 每趟车只出现一次
    date, source, trip = occ[0]
    assert (date, source) == ("2026-11-20", "new")
    # 整趟轨迹（含 11-21 凌晨的点）都归到发车日 11-20
    assert [p["seq"] for p in trip["points"]] == [1, 2, 3, 4]
    assert trip["points"][-1]["sampled_at"] == "2026-11-21T00:30:00Z"
    # 11-21 上 R1 的旧仓零点点位被抑制，但其他班次的旧仓数据不受影响
    day21 = day_of(body, "2026-11-21")
    assert [t["trip_code"] for t in day21["trips"]] == [R2]
    assert day21["trips"][0]["source"] == "legacy"


def test_switch_rejected_when_backfill_changed(client):
    """回填后该日轨迹有变（补传点位/更正时刻），切换被退回并提示重做。"""
    backfill_r1(client)
    token1 = state_token(client, "2026-11-20")

    # 数据员核对后又补传了一个点 → 状态指纹变化
    import_trips(client, [{"trip_code": R1, "points": R1_POINTS + [
        {"seq": 5, "sampled_at": "2026-11-21T00:45:00Z", "lat": 31.19, "lon": 121.50}
    ]}])
    token2 = state_token(client, "2026-11-20")
    assert token1 != token2

    r = switch(client, "2026-11-20", token1)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "重做" in detail["message"] or "重新核对" in detail["message"]
    assert detail["current_state"] == token2

    # 用最新指纹重切 → 成功
    r = switch(client, "2026-11-20", token2)
    assert r.status_code == 200 and r.json()["status"] == "switched"


def test_departure_correction_changes_state_token(client):
    """更正发车时刻同样使回填指纹失效。"""
    backfill_r1(client)
    token1 = state_token(client, "2026-11-20")
    import_trips(client, [{"trip_code": R1, "departure_at": "2026-11-20T23:50:00Z"}])
    assert state_token(client, "2026-11-20") != token1


def test_corrections_allowed_after_switch(client):
    """切换后仍可补传点位、更正发车时刻，查询即时反映。"""
    seed_legacy(client, LEGACY_R1)
    backfill_r1(client)
    switch_day(client, "2026-11-20")

    # 切换后补传点位
    new_points = R1_POINTS + [
        {"seq": 5, "sampled_at": "2026-11-21T00:45:00Z", "lat": 31.19, "lon": 121.50}
    ]
    import_trips(client, [{"trip_code": R1, "points": new_points}])
    # 切换后更正发车时刻（不跨日）
    import_trips(client, [{"trip_code": R1, "departure_at": "2026-11-20T23:45:00Z"}])

    body = query(client, "2026-11-20", "2026-11-20")
    [(_, _, trip)] = occurrences(body, R1)
    assert trip["departure_at"] == "2026-11-20T23:45:00Z"
    assert [p["seq"] for p in trip["points"]] == [1, 2, 3, 4, 5]
    assert trip["batch_version"] == 2
    assert day_of(body, "2026-11-20")["source"] == "migrated"


def test_reschedule_moves_whole_trip_to_unswitched_date(client):
    """改期：整趟轨迹随之换日，即使新日期仍未切换（供数来源不同）。"""
    seed_legacy(client, LEGACY_R1 + LEGACY_R2)
    backfill_r1(client)
    switch_day(client, "2026-11-20")

    # 发车时刻更正为次日凌晨 → 发车日从 11-20（已切换）改到 11-21（未切换）
    res = import_trips(client, [{"trip_code": R1, "departure_at": "2026-11-21T00:10:00Z"}])
    assert res["imported"][0]["rescheduled"] is True
    assert res["imported"][0]["departure_date"] == "2026-11-21"

    body = query(client, "2026-11-20", "2026-11-21")
    occ = occurrences(body, R1)
    assert len(occ) == 1  # 只出现一次，且在新日期
    date, source, trip = occ[0]
    assert date == "2026-11-21" and source == "new"
    assert [p["seq"] for p in trip["points"]] == [1, 2, 3, 4]  # 整趟轨迹随之换日
    # 11-20（已切换）上该班次消失；11-21 上其旧仓零点点位被抑制，R2 旧仓切片不受影响
    assert [t["trip_code"] for t in day_of(body, "2026-11-20")["trips"]] == []
    assert {t["trip_code"] for t in day_of(body, "2026-11-21")["trips"]} == {R1, R2}


def test_reschedule_from_unswitched_into_switched_date(client):
    """反向改期：从未切换日改入已切换日，班次立即从新仓供数，旧仓点位被抑制。"""
    seed_legacy(client, LEGACY_R3)
    import_trips(client, [
        {"trip_code": R3, "departure_at": "2026-11-22T22:00:00Z", "points": [
            {"seq": 1, "sampled_at": "2026-11-22T22:00:00Z", "lat": 31.30, "lon": 121.60},
            {"seq": 2, "sampled_at": "2026-11-22T22:30:00Z", "lat": 31.32, "lon": 121.62},
        ]},
    ])
    switch_day(client, "2026-11-21")  # 空日也可切换

    # 切换前回填的班次尚未激活：11-22 仍走旧仓
    body = query(client, "2026-11-22", "2026-11-22")
    [(date, source, _)] = occurrences(body, R3)
    assert source == "legacy"

    # 改期进已切换的 11-21 → 激活并从新仓供数
    import_trips(client, [{"trip_code": R3, "departure_at": "2026-11-21T23:50:00Z"}])
    body = query(client, "2026-11-21", "2026-11-22")
    occ = occurrences(body, R3)
    assert len(occ) == 1
    assert (occ[0][0], occ[0][1]) == ("2026-11-21", "new")
    assert [p["seq"] for p in occ[0][2]["points"]] == [1, 2]
    assert day_of(body, "2026-11-22")["trips"] == []  # 旧仓点位被抑制


def test_points_come_from_single_submission(client):
    """点位须来自同一提交状态：查询只读最新批次，不混杂历史版本。"""
    seed_legacy(client, LEGACY_R1)
    backfill_r1(client)
    switch_day(client, "2026-11-20")

    # 第二次提交：改 seq4 坐标并追加 seq5（全量快照）
    v2 = R1_POINTS[:3] + [
        {"seq": 4, "sampled_at": "2026-11-21T00:30:00Z", "lat": 31.181, "lon": 121.491},
        {"seq": 5, "sampled_at": "2026-11-21T00:45:00Z", "lat": 31.19, "lon": 121.50},
    ]
    import_trips(client, [{"trip_code": R1, "points": v2}])

    body = query(client, "2026-11-20", "2026-11-20")
    [(_, _, trip)] = occurrences(body, R1)
    assert trip["batch_version"] == 2
    got = [(p["seq"], p["lat"], p["lon"]) for p in trip["points"]]
    assert got == [(p["seq"], p["lat"], p["lon"]) for p in v2]  # 与 v2 完全一致，无 v1 残留


def test_switch_is_idempotent(client):
    backfill_r1(client)
    token = state_token(client, "2026-11-20")
    assert switch(client, "2026-11-20", token).json()["status"] == "switched"
    r = switch(client, "2026-11-20", token)
    assert r.status_code == 200 and r.json()["status"] == "already_switched"


def test_utc_offset_normalized(client):
    """带时区偏移的时间统一按 UTC 归仓。"""
    import_trips(client, [{
        "trip_code": "R20-001",
        "departure_at": "2026-11-21T07:40:00+08:00",  # = 2026-11-20T23:40Z
        "points": [{"seq": 1, "sampled_at": "2026-11-21T08:00:00+08:00",
                    "lat": 31.0, "lon": 121.0}],
    }])
    res = import_trips(client, [{"trip_code": "R20-001", "departure_at": "2026-11-20T23:40:00Z"}])
    assert res["imported"][0]["rescheduled"] is False  # 同一 UTC 时刻，不算改期
    assert res["imported"][0]["departure_date"] == "2026-11-20"


def test_validation_errors(client):
    # 同一班次内点位编号重复
    r = client.post("/trips/import", json={"trips": [{
        "trip_code": "X", "departure_at": "2026-11-20T10:00:00Z",
        "points": [
            {"seq": 1, "sampled_at": "2026-11-20T10:00:00Z", "lat": 1, "lon": 1},
            {"seq": 1, "sampled_at": "2026-11-20T10:01:00Z", "lat": 1, "lon": 1},
        ]}]})
    assert r.status_code == 422
    # 发车时刻与点位都不给
    r = client.post("/trips/import", json={"trips": [{"trip_code": "X"}]})
    assert r.status_code == 422
    # 新班次缺发车时刻
    r = client.post("/trips/import", json={"trips": [
        {"trip_code": "X", "points": [
            {"seq": 1, "sampled_at": "2026-11-20T10:00:00Z", "lat": 1, "lon": 1}]}]})
    assert r.status_code == 422
    # 无时区时间
    r = client.post("/trips/import", json={"trips": [
        {"trip_code": "X", "departure_at": "2026-11-20T10:00:00"}]})
    assert r.status_code == 422
    # 查询区间倒置 / 超长
    assert client.get("/trips", params={
        "date_from": "2026-11-21", "date_to": "2026-11-20"}).status_code == 422
    assert client.get("/trips", params={
        "date_from": "2026-11-01", "date_to": "2026-12-31"}).status_code == 422


def test_import_response_reports_affected_dates(client):
    """导入返回受影响日期的状态指纹，供数据员核对后切换。"""
    seed_legacy(client, LEGACY_R1)
    res = backfill_r1(client)
    assert res["dates"] == [{
        "service_date": "2026-11-20",
        "trip_count": 1,
        "point_count": 4,
        "state_token": state_token(client, "2026-11-20"),
    }]
    # 改期跨日时，新旧两个日期的指纹都返回
    res = import_trips(client, [{"trip_code": R1, "departure_at": "2026-11-21T00:10:00Z"}])
    assert {d["service_date"] for d in res["dates"]} == {"2026-11-20", "2026-11-21"}
