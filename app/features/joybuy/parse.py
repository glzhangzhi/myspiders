"""Joybuy 商品页解析 —— 纯函数，不碰网络不碰 DB，故归 tests 覆盖。

为什么解析 HTML 而不是调 API：`color-api.joybuy.de` 走 functionId 路由，但商品详情
那个 functionId 猜不出来（`color_trade_item_get` 等名字一律回
`{"code":"2","echo":"the current API does not exist"}`），而**商品页 HTML 一次请求
就带齐了全部数据** —— JSON-LD（名称/评分/评价）+ Next.js flight data（价格/库存/
变体/重量）。少一层猜测、少一个会变的签名（h5st）。

⚠️ **价格必须三源互证，不能只认一个正则**。页面里同一个价格出现在三处：

    1. flight data 的 `"firprice":"79.99","secprice":"199.98"`（埋点字段）
    2. flight data 的 `"finalPrice":"79.99","discount":"119.99"`（价格楼层）
    3. 可见 DOM 的 `priceFloor_skuPriceReal__*` 里的 `79`+`99`（德语千分位用逗号）

三者任一改版都可能静默失效 —— 而**静默失效在降价监控里是最坏的一种**：它不报错，
只是永远不触发，等你发现时促销早结束了。故 `parse_price` 依次尝试，**并把命中的
来源一起返回**，爬虫侧据此判断「是不是只剩最后一个源还活着」并告警。

另一条:**绝不把解析失败当成「价格很高」**。阈值判断是 `price < threshold`，
解析失败时若兜底返回 0 会立刻误触发推送。失败一律返回 None 并让调用方显式处理。
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── 价格三源 ──────────────────────────────────────────────────────────────
# flight data 是 JSON-in-JS-string，引号被转义成 \" ，故正则要同时容忍 " 和 \"
_Q = r'\\?"'

_RE_FIRPRICE = re.compile(
    rf'{_Q}secprice{_Q}:{_Q}([\d.]*){_Q},{_Q}firprice{_Q}:{_Q}([\d.]*){_Q}'
)
_RE_FINALPRICE = re.compile(
    rf'{_Q}discount{_Q}:{_Q}([\d.]*){_Q},{_Q}finalPrice{_Q}:{_Q}([\d.]*){_Q}'
)
# 可见 DOM：<span>79</span>,<span>99</span> €  —— 德语用逗号做小数点
_RE_DOM_PRICE = re.compile(
    r'priceFloor_skuPriceReal__\w+.{0,400}?>(\d+)<.{0,120}?,.{0,120}?>(\d{2})<',
    re.S,
)

_RE_LD_JSON = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)
_RE_TITLE_H1 = re.compile(r'<h1 class="skuNameFloor_skuTitle__\w+">(.*?)</h1>', re.S)
_RE_BRAND = re.compile(r'<span class="productBrandFloor_name__\w+">(.*?)</span>', re.S)
_RE_VARIANT = re.compile(
    rf'stockState{_Q}:(\d+),{_Q}skuList{_Q}:{_Q}\$\w+{_Q},'
    rf'{_Q}serialNumber{_Q}:(\d+),{_Q}text{_Q}:{_Q}([^"\\]+)'
)
# 登录态失效时服务端不报错，只是把页面换成风控页/登录页 —— 标题是唯一可靠判据
_RE_HTML_TITLE = re.compile(r'<title>(.*?)</title>', re.S)

_BLOCKED_TITLE_MARKERS = ("403", "Verboten", "Melden Sie sich", "Anmelden", "Risk")


@dataclass
class JoybuyProduct:
    """一次解析的结果。price 为 None 表示**没解析出来**，不是免费。"""

    sku: str
    price: Optional[float] = None
    list_price: Optional[float] = None  # UVP 划线价
    price_sources: List[str] = field(default_factory=list)
    title: Optional[str] = None
    brand: Optional[str] = None
    in_stock: Optional[bool] = None
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    image: Optional[str] = None
    variants: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.price is not None


class JoybuyBlocked(Exception):
    """登录态失效 / 被风控挡回。与「解析失败」分开 —— 处置方式完全不同：
    前者要人去换 cookie，后者要人去改正则。"""


def _f(raw: str) -> Optional[float]:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    # 0 价不可能是真价格，多半是字段存在但没填 —— 当成解析失败而非「免费」
    return v if v > 0 else None


def detect_blocked(html: str) -> Optional[str]:
    """返回拦截原因；None 表示页面正常。

    判据用 <title> 而不是 HTTP 状态码：风控页返回的是 **200**，
    只有标题写着「403 Verboten」。只看状态码会把拦截当成成功。
    """
    m = _RE_HTML_TITLE.search(html)
    title = _html.unescape(m.group(1)).strip() if m else ""
    if not title:
        return "页面没有标题（响应可能被截断）"
    for marker in _BLOCKED_TITLE_MARKERS:
        if marker.lower() in title.lower():
            return f"被拦截：{title[:80]}"
    return None


def parse_price(html: str) -> tuple[Optional[float], Optional[float], List[str]]:
    """三源解析价格，返回 (现价, 划线价, 命中的来源列表)。

    来源名用于告警：掉到只剩 1 个源时说明页面改版了，再不修下次就全瞎。
    三个源给出不同现价时取**最小值**并把全部来源记下 —— 促销页常有「到手价」
    与「标价」并存，宁可按低的报，用户点进去看到更贵不会错过机会，反之会。
    """
    found: Dict[str, float] = {}
    list_price: Optional[float] = None

    if m := _RE_FIRPRICE.search(html):
        if (v := _f(m.group(2))) is not None:
            found["firprice"] = v
        list_price = list_price or _f(m.group(1))

    if m := _RE_FINALPRICE.search(html):
        if (v := _f(m.group(2))) is not None:
            found["finalPrice"] = v

    if m := _RE_DOM_PRICE.search(html):
        if (v := _f(f"{m.group(1)}.{m.group(2)}")) is not None:
            found["dom"] = v

    if not found:
        return None, list_price, []
    return min(found.values()), list_price, sorted(found)


def parse_ld_json(html: str) -> Dict[str, Any]:
    """JSON-LD 里的 Product 块。解析失败返回空 dict —— 它只是锦上添花，
    价格不靠它（offers.price 实测为空），不该让它的异常冲掉整次抓取。"""
    m = _RE_LD_JSON.search(html)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_variants(html: str) -> List[Dict[str, Any]]:
    """颜色变体。stockState 是平台内部码，不做语义解释，原样留档供日后比对。"""
    out = []
    for m in _RE_VARIANT.finditer(html):
        out.append(
            {
                "name": _html.unescape(m.group(3)).strip(),
                "stock_state": int(m.group(1)),
                "index": int(m.group(2)),
            }
        )
    return out


def parse_product(html: str, sku: str) -> JoybuyProduct:
    """主入口。被风控挡回时抛 JoybuyBlocked，解析不出价格时返回 ok=False 的结果。"""
    if reason := detect_blocked(html):
        raise JoybuyBlocked(reason)

    price, list_price, sources = parse_price(html)
    ld = parse_ld_json(html)

    title = None
    if m := _RE_TITLE_H1.search(html):
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    if not title:
        title = (ld.get("name") or "").strip() or None

    brand = None
    if m := _RE_BRAND.search(html):
        brand = _html.unescape(m.group(1)).strip()

    in_stock = None
    offers = ld.get("offers")
    if isinstance(offers, dict) and offers.get("availability"):
        in_stock = "InStock" in str(offers["availability"])

    rating = rating_count = None
    agg = ld.get("aggregateRating")
    if isinstance(agg, dict):
        try:
            rating = float(agg.get("ratingValue"))
        except (TypeError, ValueError):
            rating = None
        try:
            rating_count = int(agg.get("ratingCount"))
        except (TypeError, ValueError):
            rating_count = None

    return JoybuyProduct(
        sku=sku,
        price=price,
        list_price=list_price,
        price_sources=sources,
        title=title,
        brand=brand,
        in_stock=in_stock,
        rating=rating,
        rating_count=rating_count,
        image=ld.get("image") or None,
        variants=parse_variants(html),
    )
