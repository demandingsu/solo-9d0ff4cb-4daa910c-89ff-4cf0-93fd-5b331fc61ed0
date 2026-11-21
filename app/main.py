"""公交轨迹迁仓 API。

旧仓按采样日入仓；运营日报需要把跨夜整趟轨迹归到发车日（UTC）。
数据员逐日回填（导入班次、发车时刻、带编号定位点），核对后切换该日供数来源；
未切换的日期仍从旧仓取数。详见 README.md。
"""
from __future__ import annotations

import os
from datetime import date

from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse

from . import db, service
from .schemas import ImportRequest, LegacyImportRequest, SwitchRequest

DESCRIPTION = """
公交定位点迁仓：按采样日入仓 → 跨夜整趟轨迹归发车日（UTC）。

**操作流程**
1. `POST /legacy/points` 灌入旧仓点位（模拟既有旧仓，仅演示用）。
2. `POST /trips/import` 逐日回填：导入班次、发车时刻、带编号定位点；
   同一接口也用于切换前后的补传点位、更正发车时刻与班次改期。
3. `GET /dates/{date}/state` 查看该日回填状态指纹。
4. `POST /dates/{date}/switch` 携带回填时确认的指纹切换供数来源；
   回填后轨迹有变时指纹失效，切换被退回并提示重做。
5. `GET /trips` 按日期范围查询：每趟车只出现一次，点位来自同一提交状态。
"""


def create_app() -> FastAPI:
    db_path = os.environ.get("DB_PATH", db.DEFAULT_DB_PATH)
    db.init_db(db_path)

    app = FastAPI(
        title="公交轨迹迁仓 API",
        version="1.0.0",
        description=DESCRIPTION,
    )

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse(url="/docs")

    @app.post(
        "/legacy/points",
        tags=["旧仓（演示）"],
        summary="灌入旧仓点位（按采样日入仓）",
    )
    def import_legacy_points(payload: LegacyImportRequest):
        with db.transaction(db_path) as conn:
            return service.import_legacy(conn, payload)

    @app.post(
        "/trips/import",
        tags=["迁移"],
        summary="导入班次、发车时刻与带编号定位点",
        description="逐日回填 / 补传点位 / 更正发车时刻 / 班次改期共用此接口；"
        "切换前后均可调用。点位按全量快照提交，返回受影响日期的状态指纹。",
    )
    def import_trips(payload: ImportRequest):
        with db.transaction(db_path) as conn:
            return service.import_trips(conn, payload)

    @app.get(
        "/dates/{service_date}/state",
        tags=["迁移"],
        summary="查看某日回填状态指纹",
    )
    def get_date_state(service_date: date):
        with db.readonly(db_path) as conn:
            return service.date_state(conn, service_date.isoformat())

    @app.post(
        "/dates/{service_date}/switch",
        tags=["迁移"],
        summary="切换该日供数来源到新仓",
        description="须携带回填核对时的状态指纹；回填后轨迹有变则退回并提示重做。"
        "未切换的日期仍从旧仓取数。切换单向，重试幂等。",
    )
    def switch_service_date(service_date: date, payload: SwitchRequest):
        with db.transaction(db_path) as conn:
            return service.switch_date(conn, service_date, payload.expected_state)

    @app.get(
        "/trips",
        tags=["查询"],
        summary="按日期范围查询轨迹",
        description="已切换日期从新仓供数（整趟轨迹归发车日）；未切换日期从旧仓按采样日供数。"
        "每趟车只出现一次，点位来自同一提交状态。",
    )
    def query_trips(
        date_from: date = Query(description="起始日期（UTC，含）"),
        date_to: date = Query(description="结束日期（UTC，含）"),
    ):
        with db.readonly(db_path) as conn:
            return service.query_range(conn, date_from, date_to)

    return app


app = create_app()
