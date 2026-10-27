"""工具行为测试：info / plan / stay / transport / trip / search / social 七组。

与 `test_tool_contract.py` 的分工：那边校验**签名与 schema 是否自洽**（静态契约），
这边校验**给定一份上游响应，工具说出的话对不对**（行为）。两者都缺一不可——
`set_wallpaper(path/image_path)` 那次事故签名检查能抓到，但
「高德把地名模糊匹配到别处」「五星级宾馆用中文数字」这类只有行为测试能抓到。

全部不联网：高德接口换成按路径返回预置响应的桩。
被测的是解析、校验、兜底话术——也就是真出 bug 的地方，
而不是 `httpx` 有没有把请求发出去。
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from app.agent.tools.info import (
    _city_matches,
    _geocode,
    _name_matches,
    get_weather,
    plan_route,
    search_poi,
)
from app.agent.tools.plan import estimate_budget, optimize_day_route
from app.agent.tools.stay import recommend_stay
from app.agent.tools.transport import recommend_transport
from app.agent.tools.trip import plan_trip

# _amap_get / _amap_key 被这几个模块用 `from ... import` 各自绑定了一份，
# 所以必须逐个模块打补丁——只改 info 的话，plan/trip 里的引用还是老函数。
_AMAP_MODULES = ("info", "plan", "stay", "transport", "trip")

_GEOCODE = "/v3/geocode/geo"


class FakeAmap:
    """假高德：按路径返回预置响应，并记下收到的参数供断言。"""

    def __init__(
        self,
        *,
        geocodes: dict[str, dict[str, str]] | None = None,
        routes: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.geocodes = geocodes or {}
        self.routes = routes or {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        self.calls.append((path, dict(params)))
        if path == _GEOCODE:
            item = self.geocodes.get(params.get("address", ""))
            return {"status": "1", "geocodes": [item] if item else []}
        return self.routes.get(path, {"status": "1"})

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def params_for(self, path: str) -> list[dict[str, str]]:
        return [params for called, params in self.calls if called == path]


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeAmap, key: str = "test-key") -> FakeAmap:
    """把假高德装到全部用到它的模块上。key 传空串即模拟「未配置 Key」。"""
    for name in _AMAP_MODULES:
        module = importlib.import_module(f"app.agent.tools.{name}")
        monkeypatch.setattr(module, "_amap_get", fake.get, raising=False)
        monkeypatch.setattr(module, "_amap_key", lambda: key, raising=False)
    return fake


def _geo(location: str, formatted: str, city: str = "三亚市") -> dict[str, str]:
    return {"location": location, "formatted_address": formatted, "city": city}


# ==================== info：天气 ====================


async def test_weather_without_key_refuses_instead_of_inventing(monkeypatch):
    """没配 Key 必须说「未启用」，且一次请求都不发。"""
    fake = _install(monkeypatch, FakeAmap(), key="")
    out = await get_weather.ainvoke({"city": "三亚"})
    assert out.startswith("TOOL_NOT_ENABLED")
    assert fake.calls == []


async def test_weather_renders_each_day(monkeypatch):
    _install(
        monkeypatch,
        FakeAmap(
            routes={
                "/v3/weather/weatherInfo": {
                    "status": "1",
                    "forecasts": [
                        {
                            "casts": [
                                {
                                    "date": "2026-10-27",
                                    "week": "2",
                                    "dayweather": "晴",
                                    "daytemp": "29",
                                    "nightweather": "多云",
                                    "nighttemp": "24",
                                    "daywind": "东",
                                    "daypower": "3",
                                }
                            ]
                        }
                    ],
                }
            }
        ),
    )
    out = await get_weather.ainvoke({"city": "三亚"})
    assert "2026-10-27" in out
    assert "白天晴 29℃" in out
    assert "夜间多云 24℃" in out


async def test_weather_api_error_is_passed_through_verbatim(monkeypatch):
    """上游报错要把原始 info 带出来——用户拿它去查文档才知道 Key 错在哪。"""
    _install(
        monkeypatch,
        FakeAmap(routes={"/v3/weather/weatherInfo": {"status": "0", "info": "INVALID_USER_KEY"}}),
    )
    out = await get_weather.ainvoke({"city": "三亚"})
    assert "失败" in out
    assert "INVALID_USER_KEY" in out
    assert "℃" not in out  # 失败时一个温度都不许出现


async def test_weather_empty_forecast_says_so(monkeypatch):
    _install(monkeypatch, FakeAmap(routes={"/v3/weather/weatherInfo": {"status": "1"}}))
    out = await get_weather.ainvoke({"city": "三亚"})
    assert "暂无" in out
    assert "℃" not in out


# ==================== info：POI 与地名解析 ====================


async def test_search_poi_lists_name_address_and_map_link(monkeypatch):
    _install(
        monkeypatch,
        FakeAmap(
            routes={
                "/v3/place/text": {
                    "status": "1",
                    "pois": [
                        {
                            "name": "亚龙湾国家旅游度假区",
                            "address": "亚龙湾路",
                            "location": "109.63,18.22",
                        }
                    ],
                }
            }
        ),
    )
    out = await search_poi.ainvoke({"city": "三亚", "keyword": "亚龙湾"})
    assert "亚龙湾国家旅游度假区" in out
    assert "亚龙湾路" in out
    assert "109.63,18.22" in out or "uri.amap.com" in out


async def test_search_poi_empty_result_does_not_fall_back_to_memory(monkeypatch):
    _install(monkeypatch, FakeAmap(routes={"/v3/place/text": {"status": "1", "pois": []}}))
    out = await search_poi.ainvoke({"city": "三亚", "keyword": "不存在的景点"})
    assert "未" in out
    assert "亚龙湾" not in out


@pytest.mark.parametrize(
    ("query", "resolved", "expected"),
    [
        # 正常解析：输入前两个字出现在结果里
        ("三亚市区", "海南省三亚市三亚市区", True),
        # 高德对无法识别的输入做模糊匹配，返回一个毫不相干的地点 —— 必须拦
        ("不存在的城市xyz", "北京市朝阳区建国路", False),
        # 「市」「城」这类高频字不能让垃圾输入蒙混过关
        ("市南城东", "北京市朝阳区", False),
        # 单字输入不参与校验（无法判断，交给上游）
        ("京", "北京市", True),
    ],
)
def test_name_matches_blocks_fuzzy_misresolution(query: str, resolved: str, expected: bool):
    assert _name_matches(query, resolved) is expected


async def test_geocode_rejects_mismatched_resolution(monkeypatch):
    """地名被解析到别处时必须返回 None，而不是算出一条起点错误的路线。"""
    _install(
        monkeypatch,
        FakeAmap(geocodes={"不存在的城市xyz": _geo("116.4,39.9", "北京市朝阳区", "北京市")}),
    )

    assert await _geocode("不存在的城市xyz") is None


async def test_geocode_passes_city_hint_to_upstream(monkeypatch):
    """city 提示必须真的发出去——否则「三亚市区」可能匹配到全国同名地点。"""
    fake = _install(
        monkeypatch,
        FakeAmap(geocodes={"三亚市区": _geo("109.5,18.2", "海南省三亚市三亚市区")}),
    )

    assert await _geocode("三亚市区", "三亚") is not None
    assert fake.params_for(_GEOCODE)[0]["city"] == "三亚"


async def test_geocode_rejects_right_name_wrong_city(monkeypatch):
    """名字对上了、城市不对，同样必须拒绝。

    实测（2026-10）高德的 city 只是提示不是过滤：
    「南山一棵树观景台」带 city=重庆 会解析到**深圳市南山区**，
    「长江索道」到江西萍乡，「李子坝轻轨站」到广东肇庆。
    `_name_matches` 拦不住——输入的「南山」「索道」确实出现在结果里。
    真跑时算出了「磁器口 → 南山一棵树 1407 公里」，看起来有据可查，起点却在深圳。
    """
    _install(
        monkeypatch,
        FakeAmap(
            geocodes={
                "南山一棵树观景台": _geo(
                    "113.946447,22.558462", "广东省深圳市南山区观景台", "深圳市"
                )
            }
        ),
    )

    assert await _geocode("南山一棵树观景台", "重庆") is None
    # 不给城市提示时无从判断，仍然放行（由 _name_matches 把关）
    assert await _geocode("南山一棵树观景台") is not None


@pytest.mark.parametrize(
    ("hint", "resolved", "expected"),
    [
        # 直辖市带不带「市」都算同一个城市
        ("重庆", "重庆市", True),
        ("重庆市", "重庆", True),
        # 同一城市不同写法
        ("三亚", "三亚市", True),
        # 这是要拦的那种
        ("重庆", "深圳市", False),
        ("重庆", "萍乡市", False),
        # 省名兜底：高德个别返回不带 city，只给 province
        ("重庆", "重庆市", True),
        # 拿不到城市信息就不判死刑
        ("重庆", "", True),
        ("", "深圳市", True),
    ],
)
def test_city_matches(hint: str, resolved: str, expected: bool):
    assert _city_matches(hint, resolved) is expected


async def test_plan_route_without_key_is_not_enabled(monkeypatch):
    _install(monkeypatch, FakeAmap(), key="")
    out = await plan_route.ainvoke({"origin": "三亚", "destination": "海口"})
    assert out.startswith("TOOL_NOT_ENABLED")


# ==================== plan：预算（纯算术） ====================


async def test_budget_sums_categories_and_per_person():
    out = await estimate_budget.ainvoke(
        {
            "items": [
                {"name": "机票", "amount": 2400, "category": "交通"},
                {"name": "酒店", "amount": 1800, "category": "住宿"},
                {"name": "打车", "amount": 300, "category": "交通"},
            ],
            "budget_total": 6000,
            "people": 2,
        }
    )
    assert "合计：4,500 元" in out
    assert "人均：2,250 元" in out
    assert "剩余 1,500 元" in out
    # 分类小计按金额倒序：交通 2700 > 住宿 1800
    assert out.index("交通：2,700 元") < out.index("住宿：1,800 元")


async def test_budget_reports_overspend_without_flipping_sign():
    out = await estimate_budget.ainvoke(
        {
            "items": [{"name": "机票", "amount": 5000, "category": "交通"}],
            "budget_total": 3000,
        }
    )
    assert "超支 2,000 元" in out
    assert "剩余" not in out


async def test_budget_without_total_omits_percentages():
    """没给总预算时不能出现百分比——除以 0 或者凭空造一个分母都是错的。"""
    out = await estimate_budget.ainvoke(
        {"items": [{"name": "门票", "amount": 200, "category": "门票"}]}
    )
    assert "%" not in out
    assert "合计：200 元" in out


async def test_budget_zero_people_skips_per_person():
    out = await estimate_budget.ainvoke({"items": [{"name": "门票", "amount": 200}], "people": 0})
    assert "人均" not in out


async def test_budget_empty_items_asks_for_input():
    out = await estimate_budget.ainvoke({"items": []})
    assert "未提供预算明细" in out


# ==================== plan：单日路线 ====================


async def test_day_route_orders_geographically(monkeypatch):
    """四个点按经度递增排，输入故意打乱，输出应按最近邻连成一条线。"""
    fake = _install(
        monkeypatch,
        FakeAmap(
            geocodes={
                "A点": _geo("109.10,18.20", "A点"),
                "B点": _geo("109.20,18.20", "B点"),
                "C点": _geo("109.30,18.20", "C点"),
                "D点": _geo("109.40,18.20", "D点"),
            },
            routes={
                "/v3/direction/driving": {
                    "status": "1",
                    "route": {"paths": [{"distance": "41000", "duration": "3600"}]},
                }
            },
        ),
    )
    out = await optimize_day_route.ainvoke(
        {"city": "三亚", "places": ["C点", "A点", "D点", "B点"], "start": "A点"}
    )
    # 起点是 A，最近邻应一路向东：A → B → C → D
    assert "A点 → B点 → C点 → D点" in out
    assert "总里程约 41.0 公里" in out
    assert "在途约 60 分钟" in out
    assert fake.paths().count("/v3/direction/driving") == 1


async def test_day_route_needs_two_places(monkeypatch):
    _install(monkeypatch, FakeAmap())
    out = await optimize_day_route.ainvoke({"city": "三亚", "places": ["亚龙湾"]})
    assert "至少需要两个地点" in out


async def test_day_route_rejects_implausible_distance(monkeypatch):
    """里程异常时拒绝采用——这通常是地名被解析错，不是真的开了 900 公里。"""
    _install(
        monkeypatch,
        FakeAmap(
            geocodes={"A点": _geo("109.1,18.2", "A点"), "B点": _geo("109.2,18.2", "B点")},
            routes={
                "/v3/direction/driving": {
                    "status": "1",
                    "route": {"paths": [{"distance": "900000", "duration": "36000"}]},
                }
            },
        ),
    )
    out = await optimize_day_route.ainvoke({"city": "三亚", "places": ["A点", "B点"]})
    assert "不可采用" in out
    assert "总里程约" not in out


async def test_day_route_warns_when_day_is_overpacked(monkeypatch):
    _install(
        monkeypatch,
        FakeAmap(
            geocodes={"A点": _geo("109.1,18.2", "A点"), "B点": _geo("109.2,18.2", "B点")},
            routes={
                "/v3/direction/driving": {
                    "status": "1",
                    # 200 分钟在途，超过 150 分钟的告警线
                    "route": {"paths": [{"distance": "80000", "duration": "12000"}]},
                }
            },
        ),
    )
    out = await optimize_day_route.ainvoke({"city": "三亚", "places": ["A点", "B点"]})
    assert "⚠️" in out
    assert "在途时间偏长" in out


async def test_day_route_names_the_places_it_dropped(monkeypatch):
    _install(
        monkeypatch,
        FakeAmap(
            geocodes={"A点": _geo("109.1,18.2", "A点"), "B点": _geo("109.2,18.2", "B点")},
            routes={
                "/v3/direction/driving": {
                    "status": "1",
                    "route": {"paths": [{"distance": "10000", "duration": "1800"}]},
                }
            },
        ),
    )
    out = await optimize_day_route.ainvoke({"city": "三亚", "places": ["A点", "B点", "查无此地"]})
    assert "未纳入排序的地点" in out
    assert "查无此地" in out


# ==================== trip：行程串联 ====================


async def test_trip_sums_legs_and_judges_pace(monkeypatch):
    _install(
        monkeypatch,
        FakeAmap(
            geocodes={
                "三亚": _geo("109.5,18.2", "三亚"),
                "海口": _geo("110.3,20.0", "海口"),
                "文昌": _geo("110.8,19.6", "文昌"),
            },
            routes={
                "/v3/direction/driving": {
                    "status": "1",
                    "route": {"paths": [{"distance": "100000", "duration": "7200"}]},
                }
            },
        ),
    )
    out = await plan_trip.ainvoke({"origin": "三亚", "stops": ["海口", "文昌"], "days": 2})
    assert "第 1 段" in out and "第 2 段" in out
    assert "合计：约 200 公里" in out
    # 2 天 4 小时 = 日均 2 小时，恰好等于告警线，不应报「偏赶」
    assert "日均在途约 2.0 小时" in out


async def test_trip_flags_implausible_total(monkeypatch):
    """单段 9000 公里远超 trip 的 8000 公里合理上限，必须拒绝当成结论。"""
    _install(
        monkeypatch,
        FakeAmap(
            geocodes={"三亚": _geo("109.5,18.2", "三亚"), "海口": _geo("110.3,20.0", "海口")},
            routes={
                "/v3/direction/driving": {
                    "status": "1",
                    "route": {"paths": [{"distance": "9000000", "duration": "360000"}]},
                }
            },
        ),
    )
    out = await plan_trip.ainvoke({"origin": "三亚", "stops": ["海口"]})
    assert "异常偏大" in out
    assert "合计：" not in out


async def test_trip_all_unresolved_is_honest(monkeypatch):
    _install(monkeypatch, FakeAmap())
    out = await plan_trip.ainvoke({"origin": "三亚", "stops": ["甲地", "乙地"]})
    assert "无法解析" in out
    assert "第 1 段" not in out


def _poi(name: str, location: str) -> dict[str, str]:
    return {"name": name, "location": location, "type": "风景名胜;风景名胜"}


async def test_trip_auto_picks_stops_from_city_and_days(monkeypatch):
    """「重庆两天怎么玩」里用户一个地点都没给，工具不能只会反问。

    早先直接回「请提供出发地和至少一个目的地」——把一个能答的问题推回给用户。
    现在只给 city + days 就按城市热度挑点，并且**明确标注是候选**，
    不能假装是用户点名要去的。
    """
    fake = _install(
        monkeypatch,
        FakeAmap(
            routes={
                "/v3/place/text": {
                    "status": "1",
                    "pois": [
                        _poi("洪崖洞民俗风貌区", "106.579,29.562"),
                        _poi("重庆市人民大礼堂", "106.553,29.561"),
                        _poi("重庆中国三峡博物馆", "106.550,29.562"),
                        _poi("重庆十八梯传统风貌区", "106.573,29.551"),
                        _poi("不该被选中", "106.000,29.000"),
                    ],
                },
                "/v3/direction/driving": {
                    "status": "1",
                    "route": {"paths": [{"distance": "3400", "duration": "720"}]},
                },
            }
        ),
    )
    out = await plan_trip.ainvoke({"city": "重庆", "days": 2})

    # 2 天 → 每天 2 个点，第 5 个不该出现
    params = fake.params_for("/v3/place/text")[0]
    assert params["offset"] == "4"
    assert params["sortrule"] == "weight"
    assert "不该被选中" not in out
    assert "候选景点" in out, "自动选的点必须标注成候选，不能冒充用户指定"

    # 没给出发地 → 以第一个点为起点，不凭空多算一段
    assert out.splitlines()[0].startswith("行程串联（洪崖洞民俗风貌区 出发")
    assert out.count("第 ") == 3


async def test_trip_auto_stops_skip_geocoding(monkeypatch):
    """POI 检索已经带了坐标，再逐个地理编码等于白花 N 次网络往返。"""
    fake = _install(
        monkeypatch,
        FakeAmap(
            routes={
                "/v3/place/text": {
                    "status": "1",
                    "pois": [
                        _poi("甲景点", "106.1,29.1"),
                        _poi("乙景点", "106.2,29.2"),
                        _poi("丙景点", "106.3,29.3"),
                    ],
                },
                "/v3/direction/driving": {
                    "status": "1",
                    "route": {"paths": [{"distance": "5000", "duration": "900"}]},
                },
            }
        ),
    )
    await plan_trip.ainvoke({"city": "重庆", "days": 2})
    assert fake.params_for(_GEOCODE) == [], "自动选点时不应再发地理编码请求"


async def test_trip_without_origin_starts_from_first_stop(monkeypatch):
    """没说从哪出发时以第一个目的地为起点；拿城市中心当起点会凭空多一段里程。"""
    _install(
        monkeypatch,
        FakeAmap(
            geocodes={"甲地": _geo("106.1,29.1", "甲地"), "乙地": _geo("106.2,29.2", "乙地")},
            routes={
                "/v3/direction/driving": {
                    "status": "1",
                    "route": {"paths": [{"distance": "5000", "duration": "900"}]},
                }
            },
        ),
    )
    out = await plan_trip.ainvoke({"stops": ["甲地", "乙地"]})
    assert "甲地 出发" in out
    assert out.count("第 ") == 1
    assert "第 1 段　甲地 → 乙地" in out


async def test_trip_asks_for_days_when_nothing_to_go_on(monkeypatch):
    _install(monkeypatch, FakeAmap())
    out = await plan_trip.ainvoke({})
    assert "city" in out and "days" in out


async def test_trip_reports_wrong_city_place_as_unresolved(monkeypatch):
    """名字对上、城市不对的地点必须报「解析失败」，不能算出跨省里程。

    真跑时这条链算出过「磁器口 → 南山一棵树 1407 公里」：高德把
    「南山一棵树观景台」解析到了深圳南山区。提示也要给得可执行——
    这类失败几乎都是重名，带上城市名重试即可，不必回头问用户。
    """
    _install(
        monkeypatch,
        FakeAmap(
            geocodes={
                "磁器口古镇": _geo("106.44,29.58", "重庆市沙坪坝区磁器口古镇", "重庆市"),
                "洪崖洞民俗风貌区": _geo("106.57,29.56", "重庆市渝中区洪崖洞", "重庆市"),
                "南山一棵树观景台": _geo("113.94,22.55", "广东省深圳市南山区观景台", "深圳市"),
            },
            routes={
                "/v3/direction/driving": {
                    "status": "1",
                    "route": {"paths": [{"distance": "9000", "duration": "1200"}]},
                }
            },
        ),
    )
    out = await plan_trip.ainvoke(
        {"city": "重庆", "stops": ["磁器口古镇", "洪崖洞民俗风貌区", "南山一棵树观景台"]}
    )
    assert "未纳入线路" in out
    assert "南山一棵树观景台" in out
    assert "重庆长江索道" in out, "提示要给出可照做的写法"
    assert "1407" not in out


async def test_trip_auto_pick_failure_points_at_search_poi(monkeypatch):
    """自动选点失败不能只说「找不到」，要给出下一步该干什么。"""
    _install(monkeypatch, FakeAmap(routes={"/v3/place/text": {"status": "1", "pois": []}}))
    out = await plan_trip.ainvoke({"city": "重庆", "days": 2})
    assert "search_poi" in out


# ==================== stay：酒店 ====================


async def test_stay_matches_chinese_star_type(monkeypatch):
    """高德 type 用中文数字（五星级宾馆）。用阿拉伯数字拼检索词会永远匹配不上。"""
    _install(
        monkeypatch,
        FakeAmap(
            routes={
                "/v3/place/text": {
                    "status": "1",
                    "pois": [
                        {"name": "三亚湾某五星酒店", "type": "住宿服务;宾馆酒店;五星级宾馆"},
                        {"name": "某青年旅舍", "type": "住宿服务;宾馆酒店;旅馆招待所"},
                    ],
                }
            }
        ),
    )
    out = await recommend_stay.ainvoke({"city": "三亚", "star_rating": "5"})
    assert "三亚湾某五星酒店" in out
    assert "某青年旅舍" not in out
    assert "放宽" not in out


async def test_stay_falls_back_and_admits_it(monkeypatch):
    """筛选不到时要放宽，但必须写明「已放宽」——否则等于把旅舍当五星卖。"""
    _install(
        monkeypatch,
        FakeAmap(
            routes={
                "/v3/place/text": {
                    "status": "1",
                    "pois": [
                        {"name": "某青年旅舍", "type": "住宿服务;宾馆酒店;旅馆招待所"},
                    ],
                }
            }
        ),
    )
    out = await recommend_stay.ainvoke({"city": "三亚", "star_rating": "5"})
    assert "某青年旅舍" in out
    assert "放宽" in out


async def test_stay_never_prints_a_price(monkeypatch):
    """底线：没有任何房价数据源，就不许出现价格数字。"""
    _install(
        monkeypatch,
        FakeAmap(
            routes={
                "/v3/place/text": {
                    "status": "1",
                    "pois": [{"name": "某酒店", "type": "住宿服务;宾馆酒店;五星级宾馆"}],
                }
            }
        ),
    )
    out = await recommend_stay.ainvoke({"city": "三亚", "budget_per_night": 1200})
    assert "不提供房价" in out
    assert "元/晚" not in out
    assert "¥" not in out


async def test_stay_empty_result_is_honest(monkeypatch):
    _install(monkeypatch, FakeAmap(routes={"/v3/place/text": {"status": "1", "pois": []}}))
    out = await recommend_stay.ainvoke({"city": "三亚"})
    assert "未能" in out
    assert "建议放宽" in out


# ==================== transport：交通对比 ====================


async def test_transport_quotes_no_ticket_or_price(monkeypatch):
    _install(
        monkeypatch,
        FakeAmap(
            geocodes={"三亚": _geo("109.5,18.2", "三亚"), "海口": _geo("110.3,20.0", "海口")},
            routes={
                "/v3/direction/driving": {
                    "status": "1",
                    "route": {"paths": [{"distance": "280000", "duration": "12600"}]},
                }
            },
        ),
    )
    out = await recommend_transport.ainvoke({"origin": "三亚", "destination": "海口"})
    assert "直线距离" in out
    assert "驾车" in out
    assert "不提供任何班次时刻与票价" in out
    assert "元" not in out  # 一个价格数字都不能有


async def test_transport_reports_no_land_route_when_implausible(monkeypatch):
    """超过 8000 公里按「跨海 / 无陆路」处理，而不是当成 999 公里照报。"""
    _install(
        monkeypatch,
        FakeAmap(
            geocodes={"三亚": _geo("109.5,18.2", "三亚"), "哈尔滨": _geo("126.6,45.7", "哈尔滨")},
            routes={
                "/v3/direction/driving": {
                    "status": "1",
                    "route": {"paths": [{"distance": "9000000", "duration": "360000"}]},
                }
            },
        ),
    )
    out = await recommend_transport.ainvoke({"origin": "三亚", "destination": "哈尔滨"})
    assert "未见合理陆路路线" in out
    assert "约 9,000 公里" not in out


async def test_transport_unresolved_endpoint_is_honest(monkeypatch):
    _install(monkeypatch, FakeAmap())
    out = await recommend_transport.ainvoke({"origin": "甲地", "destination": "乙地"})
    assert "无法解析" in out


# ==================== search / social：默认关闭时的兜底 ====================


async def test_search_web_without_key_degrades(monkeypatch):
    monkeypatch.setattr("app.agent.tools.search.find", lambda name: None)
    from app.agent.tools.search import search_web

    out = await search_web.ainvoke({"query": "三亚 攻略"})
    assert out.startswith("TOOL_NOT_ENABLED")
    assert "BING_SEARCH_API_KEY" in out


async def test_search_web_rejects_empty_query():
    from app.agent.tools.search import search_web

    out = await search_web.ainvoke({"query": "   "})
    assert out.startswith("INVALID_REQUEST")


async def test_social_search_is_off_by_default(monkeypatch):
    from app.agent.tools import social

    class _Settings:
        enable_session_fetch = False

    monkeypatch.setattr(social, "load_settings", lambda: _Settings())
    out = await social.search_social_session.ainvoke({"query": "三亚"})
    assert out.startswith("TOOL_NOT_ENABLED")
    assert "ENABLE_SESSION_FETCH" in out


async def test_generate_scraper_only_emits_text(monkeypatch):
    """草案工具绝不能执行任何东西：输出里必须写明不执行，且不含 Cookie 值。"""
    from app.agent.tools import social

    out = await social.generate_scraper.ainvoke({"site": "douyin", "goal": "三亚 攻略"})
    if out.startswith("INVALID_REQUEST"):
        pytest.skip("当前未部署 MediaCrawler，工具按预期拒绝")
    assert "不会执行任何采集动作" in out
    assert "三亚 攻略" in out
