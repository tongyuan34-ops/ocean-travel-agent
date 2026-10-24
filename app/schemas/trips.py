"""行程导出模型：前端把行程工具参数整理成这个结构后提交。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TripStop(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    time: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=300)


class TripDay(BaseModel):
    date: str = Field(default="", max_length=40)
    title: str = Field(default="", max_length=80)
    stops: list[TripStop] = Field(default_factory=list)


class TripPlan(BaseModel):
    title: str = Field(default="我的行程", max_length=80)
    city: str = Field(default="", max_length=60)
    origin: str = Field(default="", max_length=120)
    mode: str = Field(default="driving", max_length=20)
    # ISO 日期（YYYY-MM-DD），用于日历时把第 1 天锚定到具体日期
    start_date: str = Field(default="", max_length=10)
    days: list[TripDay] = Field(default_factory=list)
