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


class PointRebindMapping(BaseModel):
    """一条改挂对应关系：把源班次的 source_seq 点改挂到目标班次的 target_seq。"""

    source_seq: int = Field(ge=1, description="源班次的定位点编号")
    target_seq: int = Field(ge=1, description="目标班次上承载该点的定位点编号")


class RebindRequest(BaseModel):
    """班次改挂：把上一班误收的定位点改挂到下一班。

    两个班次都必须已在新仓供数；两个 batch_version 是数据员核对时看到的版本，
    用于确认"涉及的点位"自核对以来没有变化。改挂作用在双方**最新**批次上，
    因此核对之后补传的其他点会原样保留。
    """

    source_trip_code: str = Field(min_length=1, max_length=64, description="源班次号（误收点的一班）")
    source_batch_version: int = Field(ge=1, description="核对时源班次的批次版本")
    target_trip_code: str = Field(min_length=1, max_length=64, description="目标班次号（点应归属的一班）")
    target_batch_version: int = Field(ge=1, description="核对时目标班次的批次版本")
    mappings: List[PointRebindMapping] = Field(
        min_length=1,
        description="源点号→目标点号的一一对应关系（源点号、目标点号各自不得重复）",
    )

    @model_validator(mode="after")
    def _check(self) -> "RebindRequest":
        if self.source_trip_code == self.target_trip_code:
            raise ValueError("源班次与目标班次不能相同")
        sources = [m.source_seq for m in self.mappings]
        targets = [m.target_seq for m in self.mappings]
        if len(sources) != len(set(sources)):
            raise ValueError("同一请求内 source_seq 重复，改挂关系须一一对应")
        if len(targets) != len(set(targets)):
            raise ValueError("同一请求内 target_seq 重复，改挂关系须一一对应")
        return self
