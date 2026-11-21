"""请求模型：班次/发车时刻/带编号定位点的导入、旧仓点位、切换请求。日期均按 UTC。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import AwareDatetime, BaseModel, Field, model_validator


class PointIn(BaseModel):
    """带编号的定位点。seq 在一次提交内唯一。"""

    seq: int = Field(ge=1, description="定位点编号，同一趟车内唯一")
    sampled_at: AwareDatetime = Field(description="采样时刻（UTC）")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class TripImportItem(BaseModel):
    """单趟车的导入项：发车时刻与点位至少给一项。

    - 仅 departure_at：新登记班次或更正发车时刻（可能触发改期换日）
    - 仅 points：补传/更正点位（全量快照，生成新提交版本）
    - 两者都给：回填整趟车
    """

    trip_code: str = Field(min_length=1, max_length=64, description="班次号")
    departure_at: Optional[AwareDatetime] = Field(default=None, description="发车时刻（UTC）")
    points: Optional[List[PointIn]] = Field(default=None, description="整趟轨迹的全量点位快照")

    @model_validator(mode="after")
    def _check(self) -> "TripImportItem":
        if self.departure_at is None and self.points is None:
            raise ValueError("departure_at 与 points 至少提供一项")
        if self.points is not None:
            seqs = [p.seq for p in self.points]
            if len(seqs) != len(set(seqs)):
                raise ValueError("同一班次提交内 seq 编号重复")
        return self


class ImportRequest(BaseModel):
    trips: List[TripImportItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_dup(self) -> "ImportRequest":
        codes = [t.trip_code for t in self.trips]
        if len(codes) != len(set(codes)):
            raise ValueError("同一请求内 trip_code 重复")
        return self


class LegacyPointIn(BaseModel):
    trip_code: str = Field(min_length=1, max_length=64)
    sampled_at: AwareDatetime
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class LegacyImportRequest(BaseModel):
    """旧仓点位（按采样日入仓）。仅用于演示/灌入既有旧仓数据。"""

    points: List[LegacyPointIn] = Field(min_length=1)


class SwitchRequest(BaseModel):
    expected_state: str = Field(
        min_length=1,
        description="回填核对时拿到的状态指纹（GET /dates/{date}/state 的 state_token）",
    )
