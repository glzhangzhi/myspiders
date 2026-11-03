"""Joybuy 商品页解析的纯函数测试。

这是降价监控里**唯一值得测的部分**，也是最该测的：解析失效不会报错，只会让
监控永远不触发 —— 等人发现时促销早结束了。所以这里钉住三件事：

  1. 三个价格源各自单独可用（任一改版，另两个还能顶住）
  2. 风控页**返回 200** 却必须被判为拦截（只看状态码会把它当成功）
  3. 解析失败绝不兜底成 0 —— 阈值判断是 `price < 10`，0 会立刻误触发

    .venv/bin/pytest tests/test_joybuy_parse.py -v
"""

from __future__ import annotations

import pytest

from app.features.joybuy.parse import (
    JoybuyBlocked,
    detect_blocked,
    parse_price,
    parse_product,
    parse_variants,
)

# 真实页面的三个价格源（引号在 flight data 里是转义过的 \"）
_FIRPRICE = r'e6:{\"secprice\":\"199.98\",\"firprice\":\"79.99\",\"2ndpriceType\":\"2\"}'
_FINALPRICE = r'{\"crossOffPriceNameInDetail\":\"UVP\",\"discount\":\"119.99\",\"finalPrice\":\"79.99\",\"hide\":1}'
_DOM = (
    '<div class="priceFloor_skuPriceReal__Lstkp">'
    '<span style="font-size:32px">79</span><span>,</span><span>99</span>'
    '<span> €</span></div>'
)

_OK_TITLE = "<title>JOYFURNOS Recyclingmülleimer | Joybuy</title>"


def _page(*parts: str, title: str = _OK_TITLE) -> str:
    return f"<html><head>{title}</head><body>{''.join(parts)}</body></html>"


# ── 价格三源 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "block,source",
    [(_FIRPRICE, "firprice"), (_FINALPRICE, "finalPrice"), (_DOM, "dom")],
)
def test_each_price_source_works_alone(block: str, source: str):
    """任意单独一个源都能解析出价格 —— 另外两个改版时监控仍然活着。"""
    price, _, sources = parse_price(_page(block))
    assert price == 79.99
    assert sources == [source]


def test_all_three_sources_agree():
    price, list_price, sources = parse_price(_page(_FIRPRICE, _FINALPRICE, _DOM))
    assert price == 79.99
    assert list_price == 199.98
    assert sources == ["dom", "finalPrice", "firprice"]


def test_sources_disagree_takes_lowest():
    """多个源不一致时取最低价：按低的报，用户点进去看到更贵不会错过机会，
    反之会（按高的报可能永远不触发阈值）。"""
    cheap = r'{\"secprice\":\"199.98\",\"firprice\":\"8.99\"}'
    price, _, sources = parse_price(_page(cheap, _FINALPRICE))
    assert price == 8.99
    assert len(sources) == 2


def test_no_price_returns_none_not_zero():
    """⚠️ 核心防线：解析不出来必须是 None。
    兜底成 0 会让 `price < threshold` 立刻成立，推一条假的「降到 €0」。"""
    price, _, sources = parse_price(_page("<div>nichts</div>"))
    assert price is None
    assert sources == []


def test_zero_price_is_treated_as_parse_failure():
    """字段存在但值是 0 —— 那是没填，不是免费。"""
    zero = r'{\"secprice\":\"0\",\"firprice\":\"0\"}'
    price, _, _ = parse_price(_page(zero))
    assert price is None


# ── 拦截判定 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title",
    [
        "<title>403 Verboten | Joybuy</title>",
        "<title>Melden Sie sich bei Ihrem Konto an | Joybuy</title>",
    ],
)
def test_risk_and_login_pages_are_blocked(title: str):
    """风控页与登录页都返回 **HTTP 200**，判据只能是标题。"""
    assert detect_blocked(_page(_FIRPRICE, title=title)) is not None


def test_normal_page_not_blocked():
    assert detect_blocked(_page(_FIRPRICE)) is None


def test_parse_product_raises_on_blocked():
    """拦截要抛异常而不是返回 ok=False —— 两者处置方式不同：
    前者要人去换 cookie，后者要人去改正则。"""
    page = _page(_FIRPRICE, title="<title>403 Verboten | Joybuy</title>")
    with pytest.raises(JoybuyBlocked):
        parse_product(page, "102790346")


def test_parse_product_not_ok_when_price_missing():
    p = parse_product(_page("<div>leer</div>"), "102790346")
    assert p.ok is False
    assert p.price is None


# ── 其他字段 ──────────────────────────────────────────────────────────────


def test_variants_parsed():
    flight = (
        r'stockState\":34,\"skuList\":\"$11d\",\"serialNumber\":0,\"text\":\"Mattgrau\"}'
        r'stockState\":33,\"skuList\":\"$120\",\"serialNumber\":1,\"text\":\"Sandbeige\"}'
    )
    variants = parse_variants(_page(flight))
    assert [v["name"] for v in variants] == ["Mattgrau", "Sandbeige"]
    assert variants[0]["stock_state"] == 34


def test_ld_json_failure_does_not_break_price():
    """JSON-LD 只是锦上添花（offers.price 实测为空），它坏了不该冲掉整次抓取。"""
    bad = '<script type="application/ld+json">{nicht json</script>'
    p = parse_product(_page(_FIRPRICE, bad), "102790346")
    assert p.price == 79.99
    assert p.rating is None
