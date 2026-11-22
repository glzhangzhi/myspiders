"""机票查询面板的配置项（环境变量名与字段名相同，大小写不敏感）。

由 app.core.settings 自动合并进全局 `settings`，调用方一律在调用时读 `settings.<字段>`。
约定与踩坑见 docs/flights.md。
"""

from pydantic import BaseModel


class FlightsSettings(BaseModel):
    # 两次 Trip 请求之间的间隔（秒），在 [min, max] 里随机取。全局串行，
    # 自动补全以外的每一次 Trip 搜索请求都受它约束。
    flights_request_interval_min: float = 1.0
    flights_request_interval_max: float = 2.0
    # 单日原始结果的缓存时长（分钟）。缓存的是解析后的**全部**行程而非过滤结果，
    # 换筛选开关时不必重新请求。
    flights_cache_ttl_minutes: int = 30
    # 一次查询的日期区间上限（天，含首尾）
    flights_max_days_oneway: int = 31
    flights_max_days_roundtrip: int = 15
    # 币种：请求头、请求体、下单链接三处一起用
    flights_currency: str = "EUR"
    # Trip 单次最多返回的条数；达到它就换排序方式补查
    flights_page_cap: int = 25
    flights_timeout_seconds: float = 30.0
    # 用普通桌面 Chrome 的 UA。2026-11 实测 HeadlessChrome 也没被拦，但 Trip 的 whaleguard 拦截时回 HTTP 432，别赌
    flights_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    )
