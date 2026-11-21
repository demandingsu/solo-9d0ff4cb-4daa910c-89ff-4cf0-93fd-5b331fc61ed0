"""迁仓业务逻辑：导入（回填/补传/更正/改期）、状态指纹、供数切换、范围查询。

供数规则
--------
1. 每个供数日期有一个开关（date_switches），未切换的日期仍从旧仓按采样日供数。
2. 已切换的日期从新仓供数：取发车日为当日的班次，整趟轨迹（含跨零点点位）归到发车日。
3. 班次一旦在新仓"激活"（所在发车日被切换，或导入/改期进入已切换日），激活状态粘滞：
   之后改期到未切换的日期，整趟轨迹仍从新仓供数——改期换日与两天的供数来源无关。
4. 已激活班次的旧仓点位在所有日期被抑制，保证按日期范围查询时每趟车只出现一次。
5. 每次点位导入是一个全量快照批次，查询只读当前批次，点位必然来自同一提交状态。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

UTC = timezone.utc
MAX_RANGE_DAYS = 31


# ---------- 时间工具（全部按 UTC 归一化） ----------

def utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def utc_date_str(dt: datetime) -> str:
    return dt.astimezone(UTC).date().isoformat()


def now_iso() -> str:
    return utc_iso(datetime.now(UTC))


# ---------- 状态指纹 ----------

def _points_hash(points: list[dict]) -> str:
    h = hashlib.sha256()
    for p in points:
        h.update(f"{p['seq']}|{p['sampled_at']}|{p['lat']:.7f}|{p['lon']:.7f}\n".encode())
    return h.hexdigest()


def date_state(conn, service_date: str) -> dict:
    """某供数日期的回填状态指纹。

    覆盖该日全部班次的：班次号、发车时刻、当前提交版本与点位内容哈希。
    回填之后任何变更（补传点位、更正发车时刻、班次改期进出该日）都会改变指纹，
    切换时据此识别"回填后该日轨迹有变"。
    """
    rows = conn.execute(
        """
        SELECT t.trip_code, t.departure_at,
               b.version AS batch_version, b.point_count, b.points_hash
        FROM trips t
        LEFT JOIN batches b ON b.id = t.current_batch_id
        WHERE t.departure_date = ?
        ORDER BY t.trip_code
        """,
        (service_date,),
    ).fetchall()
    h = hashlib.sha256()
    for r in rows:
        h.update(
            f"{r['trip_code']}|{r['departure_at']}|{r['batch_version']}|"
            f"{r['point_count']}|{r['points_hash']}\n".encode()
        )
    return {
        "service_date": service_date,
        "trip_count": len(rows),
        "point_count": sum(r["point_count"] or 0 for r in rows),
        "state_token": h.hexdigest()[:16],
    }


def _is_switched(conn, service_date: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM date_switches WHERE service_date = ?", (service_date,)
    ).fetchone()
    return row is not None


# ---------- 导入 ----------

def import_legacy(conn, payload) -> dict:
    """旧仓点位导入（模拟既有旧仓），按采样时刻的 UTC 日期归集。"""
    rows = [
        (p.trip_code, utc_iso(p.sampled_at), utc_date_str(p.sampled_at), p.lat, p.lon)
        for p in payload.points
    ]
    conn.executemany(
        "INSERT INTO legacy_points (trip_code, sampled_at, sample_date, lat, lon)"
        " VALUES (?,?,?,?,?)",
        rows,
    )
    return {"inserted": len(rows)}


def import_trips(conn, payload) -> dict:
    """导入班次、发车时刻、带编号定位点。

    同一接口覆盖：逐日回填、补传点位、更正发车时刻、班次改期。
    切换前后均可调用。整个请求一个事务，要么全部生效要么全部回滚。
    """
    now = now_iso()
    imported = []
    affected_dates: set[str] = set()

    for item in payload.trips:
        code = item.trip_code
        row = conn.execute("SELECT * FROM trips WHERE trip_code = ?", (code,)).fetchone()
        old_date: Optional[str] = row["departure_date"] if row else None

        if row is None and item.departure_at is None:
            raise HTTPException(422, f"新班次 {code} 必须提供 departure_at")

        if row is None:
            dep_iso = utc_iso(item.departure_at)
            dep_date = utc_date_str(item.departure_at)
            active = 1 if _is_switched(conn, dep_date) else 0
            cur = conn.execute(
                "INSERT INTO trips (trip_code, departure_at, departure_date,"
                " active_in_new, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (code, dep_iso, dep_date, active, now, now),
            )
            trip_id = cur.lastrowid
        else:
            trip_id = row["id"]
            if item.departure_at is not None:
                dep_iso = utc_iso(item.departure_at)
                dep_date = utc_date_str(item.departure_at)
                # 激活粘滞：已激活班次改期不失效；进入已切换日期则立即激活
                active = 1 if (row["active_in_new"] or _is_switched(conn, dep_date)) else 0
                conn.execute(
                    "UPDATE trips SET departure_at=?, departure_date=?,"
                    " active_in_new=?, updated_at=? WHERE id=?",
                    (dep_iso, dep_date, active, now, trip_id),
                )

        if item.points is not None:
            # 每次提交为全量快照：新版本批次 + 原子切换 current 指针
            norm = [
                {
                    "seq": p.seq,
                    "sampled_at": utc_iso(p.sampled_at),
                    "lat": p.lat,
                    "lon": p.lon,
                }
                for p in sorted(item.points, key=lambda p: p.seq)
            ]
            prev = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM batches WHERE trip_id = ?",
                (trip_id,),
            ).fetchone()["v"]
            cur = conn.execute(
                "INSERT INTO batches (trip_id, version, submitted_at, point_count, points_hash)"
                " VALUES (?,?,?,?,?)",
                (trip_id, prev + 1, now, len(norm), _points_hash(norm)),
            )
            batch_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO points (batch_id, seq, sampled_at, lat, lon) VALUES (?,?,?,?,?)",
                [(batch_id, p["seq"], p["sampled_at"], p["lat"], p["lon"]) for p in norm],
            )
            conn.execute(
                "UPDATE trips SET current_batch_id=?, updated_at=? WHERE id=?",
                (batch_id, now, trip_id),
            )

        t = conn.execute(
            "SELECT t.*, b.version AS batch_version FROM trips t"
            " LEFT JOIN batches b ON b.id = t.current_batch_id WHERE t.id = ?",
            (trip_id,),
        ).fetchone()
        if old_date is not None:
            affected_dates.add(old_date)
        affected_dates.add(t["departure_date"])
        imported.append(
            {
                "trip_code": t["trip_code"],
                "departure_at": t["departure_at"],
                "departure_date": t["departure_date"],
                "rescheduled": old_date is not None and old_date != t["departure_date"],
                "active_in_new": bool(t["active_in_new"]),
                "batch_version": t["batch_version"],
            }
        )

    return {
        "imported": imported,
        "dates": [date_state(conn, d) for d in sorted(affected_dates)],
    }


# ---------- 供数来源切换 ----------

def switch_date(conn, service_date: date, expected_state: str) -> dict:
    d = service_date.isoformat()
    state = date_state(conn, d)

    row = conn.execute(
        "SELECT * FROM date_switches WHERE service_date = ?", (d,)
    ).fetchone()
    if row:
        # 切换是单向的；重试同一请求幂等返回。切换后的更正无需再次切换。
        return {
            "status": "already_switched",
            "service_date": d,
            "switched_at": row["switched_at"],
            "current_state": state["state_token"],
        }

    if expected_state != state["state_token"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该日轨迹在回填核对后已发生变更，切换请求已退回，请重新核对（重做回填确认）后再切换",
                "service_date": d,
                "expected_state": expected_state,
                "current_state": state["state_token"],
            },
        )

    now = now_iso()
    conn.execute(
        "INSERT INTO date_switches (service_date, switched_at, state_token) VALUES (?,?,?)",
        (d, now, state["state_token"]),
    )
    cur = conn.execute(
        "UPDATE trips SET active_in_new = 1, updated_at = ? WHERE departure_date = ?",
        (now, d),
    )
    return {
        "status": "switched",
        "service_date": d,
        "switched_at": now,
        "activated_trips": cur.rowcount,
        "state_token": state["state_token"],
    }


# ---------- 范围查询 ----------

def query_range(conn, date_from: date, date_to: date) -> dict:
    if date_to < date_from:
        raise HTTPException(422, "date_to 不能早于 date_from")
    if (date_to - date_from).days > MAX_RANGE_DAYS:
        raise HTTPException(422, f"单次查询跨度不能超过 {MAX_RANGE_DAYS} 天")

    active_codes = {
        r["trip_code"]
        for r in conn.execute("SELECT trip_code FROM trips WHERE active_in_new = 1")
    }

    days = []
    d = date_from
    while d <= date_to:
        ds = d.isoformat()
        switched = _is_switched(conn, ds)
        trips_out = []

        # 新仓：已激活且发车日为当日的班次（含改期到未切换日的班次），整趟轨迹归发车日
        rows = conn.execute(
            "SELECT t.*, b.version AS batch_version FROM trips t"
            " LEFT JOIN batches b ON b.id = t.current_batch_id"
            " WHERE t.departure_date = ? AND t.active_in_new = 1 ORDER BY t.trip_code",
            (ds,),
        ).fetchall()
        for r in rows:
            points = []
            if r["current_batch_id"] is not None:
                points = [
                    {
                        "seq": p["seq"],
                        "sampled_at": p["sampled_at"],
                        "lat": p["lat"],
                        "lon": p["lon"],
                    }
                    for p in conn.execute(
                        "SELECT seq, sampled_at, lat, lon FROM points"
                        " WHERE batch_id = ? ORDER BY seq",
                        (r["current_batch_id"],),
                    )
                ]
            trips_out.append(
                {
                    "trip_code": r["trip_code"],
                    "source": "new",
                    "service_date": ds,
                    "departure_at": r["departure_at"],
                    "batch_version": r["batch_version"],
                    "points": points,
                }
            )

        # 旧仓：未切换日期按采样日切片；已激活班次的旧点位全局抑制（每趟车只出现一次）
        if not switched:
            legacy_rows = conn.execute(
                "SELECT trip_code, sampled_at, lat, lon FROM legacy_points"
                " WHERE sample_date = ? ORDER BY trip_code, sampled_at, id",
                (ds,),
            ).fetchall()
            slices: dict[str, list] = {}
            for p in legacy_rows:
                if p["trip_code"] in active_codes:
                    continue
                slices.setdefault(p["trip_code"], []).append(
                    {"sampled_at": p["sampled_at"], "lat": p["lat"], "lon": p["lon"]}
                )
            for code in sorted(slices):
                trips_out.append(
                    {
                        "trip_code": code,
                        "source": "legacy",
                        "service_date": ds,
                        "points": slices[code],
                    }
                )

        days.append(
            {"date": ds, "source": "migrated" if switched else "legacy", "trips": trips_out}
        )
        d += timedelta(days=1)

    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "days": days,
    }
