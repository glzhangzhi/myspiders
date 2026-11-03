"""Joybuy 降价监控 —— 盯住一个商品，跌破阈值就推 ntfy。

为什么用纯 HTTP 不用 playwright：实测 **playwright 反而打不开**。无头/有头、
预热首页、改 UA、改指纹全部落到 `/user/risk/pic` 风控页；唯一的分水岭是
**有没有带登录态 cookie**。带上 `mfs_session` 之后 `curl` 一发就通，
`h5st` 签名都不需要。所以这里是 httpx 直连，启动快、无浏览器依赖。

⚠️ **`mfs_session` 是会话 cookie，会过期，且无法自动续**。匿名访问
`/login`、`/user/risk/pic`、`color-api` 都不下发它（实测 set-cookie 里只有
`__jda` 这种埋点 id），所以没有「自己刷一个」的办法 —— 过期了必须人去浏览器
里重新复制。本爬虫按「会断」设计：拦截时推一次 high 提醒（**只推一次**，
同一份 cookie 不重复刷屏），并在仪表盘标红，换 cookie 走 API 免重启。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from app.core.logging import get_logger
from app.core.settings import settings
from app.features.joybuy import repo as state
from app.features.joybuy.parse import JoybuyBlocked, parse_product
from app.core.push import PushServiceError, send_note

logger = get_logger("JOYBUY")

# 真实浏览器的 UA / 语言。缺 UA 时站点直接 307 到 /error403（实测），
# 所以这不是「装得像一点」，是硬性要求。
_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "de-DE,de;q=0.9,en;q=0.8",
}


def _topic() -> str:
    return settings.joybuy_ntfy_topic


def _fetch(url: str, cookie: str) -> str:
    """同步拉取商品页。follow_redirects=False —— 被风控时是 307，
    跟过去只会拿到一个 200 的登录页，反而更难分辨成功与失败。"""
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        resp = client.get(url, headers={**_HEADERS, "cookie": cookie})
    if resp.status_code in (301, 302, 303, 307, 308):
        raise JoybuyBlocked(
            f"HTTP {resp.status_code} 跳转到 {resp.headers.get('location', '?')[:120]}"
        )
    resp.raise_for_status()
    return resp.text


def _warn_blocked(reason: str) -> None:
    """登录态失效告警。high 而非 urgent：恢复手段是「开电脑进浏览器复制 cookie」，
    urgent 会一直响到划掉，那只是折磨（见 push_service 的判据）。
    同一份 cookie 只推一次，否则每小时一条。"""
    if not state.should_warn_blocked():
        logger.info(f"Joybuy 登录态失效（已提醒过，不重复推）: {reason}")
        return
    try:
        send_note(
            "Joybuy 登录态失效，降价监控已停",
            f"{reason}\n\n去浏览器复制新的 mfs_session，在仪表盘或 API 更新 cookie。",
            topic=_topic(),
            priority="high",
            tags=["warning", "cookie"],
            click_url=settings.joybuy_product_url,
        )
    except PushServiceError as exc:
        logger.error(f"推送登录态失效提醒失败: {exc}")


def check_price_sync() -> dict:
    """跑一轮检查。返回结构给 API / 仪表盘直接用。"""
    sku = settings.joybuy_sku
    url = settings.joybuy_product_url
    threshold = state.get_threshold()

    cookie = state.get_cookie() or settings.joybuy_cookie
    if not cookie:
        detail = "没有配置 cookie"
        state.record_check(sku, None, ok=False, detail=detail)
        logger.warning(f"Joybuy: {detail}")
        return {"ok": False, "detail": detail}

    try:
        html = _fetch(url, cookie)
        product = parse_product(html, sku)
    except JoybuyBlocked as exc:
        detail = str(exc)
        state.record_check(sku, None, ok=False, detail=detail)
        _warn_blocked(detail)
        return {"ok": False, "blocked": True, "detail": detail}
    except Exception as exc:  # noqa: BLE001 —— 网络抖动不该让调度器掉这个 job
        detail = f"{type(exc).__name__}: {exc}"
        state.record_check(sku, None, ok=False, detail=detail)
        logger.exception(f"Joybuy 抓取失败: {detail}")
        return {"ok": False, "detail": detail}

    if not product.ok:
        # 页面能打开但价格解析不出来 = **页面改版了**。这是降价监控里最隐蔽的
        # 故障：不报错、只是永远不触发。必须当成故障推一次，不能静静记日志。
        detail = "页面正常但解析不出价格（三个价格源全部失效，可能已改版）"
        state.record_check(sku, None, ok=False, detail=detail)
        logger.error(f"Joybuy: {detail}")
        if state.should_warn_blocked():
            try:
                send_note(
                    "Joybuy 价格解析失效",
                    f"{detail}\n需要更新 app/features/joybuy/parse.py 的正则。",
                    topic=_topic(),
                    priority="high",
                    tags=["warning"],
                    click_url=url,
                )
            except PushServiceError as exc:
                logger.error(f"推送解析失效提醒失败: {exc}")
        return {"ok": False, "detail": detail}

    price = product.price
    prev = state.record_check(
        sku,
        price,
        ok=True,
        detail="",
        extra={
            "title": product.title,
            "list_price": product.list_price,
            "in_stock": product.in_stock,
            "price_sources": product.price_sources,
        },
    )

    # 只剩一个价格源 = 另外两个已经改版了，下次可能全瞎。提前喊一声，
    # 别等到彻底解析不出来（那时促销可能已经错过了）。
    if len(product.price_sources) < 2:
        logger.warning(
            f"Joybuy 价格只命中 {product.price_sources}，其余源已失效，尽快核对正则"
        )

    changed = prev is not None and abs(prev - price) > 1e-9
    logger.info(
        f"Joybuy {sku}: €{price}"
        + (f"（上次 €{prev}）" if changed else "")
        + f" 阈值 €{threshold} 来源 {product.price_sources}"
    )

    hit = price < threshold
    notified = False
    if hit and state.should_notify_deal(sku, price):
        drop = ""
        if product.list_price:
            drop = f"（UVP €{product.list_price:.2f}）"
        try:
            # high：有时效、要尽快知道，且锁屏上就得看见是多少钱。
            # 不用 urgent —— 它会一直响到划掉，而这是「打开手机下个单」，
            # 不是「划掉≈办完」的事。
            send_note(
                f"🗑️ 垃圾桶降到 €{price:.2f}",
                f"{product.title or sku}\n"
                f"现价 €{price:.2f} {drop}\n"
                f"阈值 €{threshold:.2f}"
                + (f"\n上次看到 €{prev:.2f}" if prev is not None else "")
                + ("\n⚠️ 显示缺货" if product.in_stock is False else ""),
                topic=_topic(),
                priority="high",
                tags=["moneybag", "wastebasket"],
                click_url=url,
            )
            notified = True
        except PushServiceError as exc:
            logger.error(f"推送降价失败: {exc}")

    return {
        "ok": True,
        "sku": sku,
        "price": price,
        "prev_price": prev,
        "list_price": product.list_price,
        "threshold": threshold,
        "below_threshold": hit,
        "notified": notified,
        "title": product.title,
        "in_stock": product.in_stock,
        "price_sources": product.price_sources,
        "checked_at": datetime.now(timezone.utc),
    }


async def check_price() -> dict:
    """调度器入口。httpx 同步调用丢进线程，别阻塞事件循环（会卡住 SSE 长连接）。"""
    return await asyncio.to_thread(check_price_sync)


if __name__ == "__main__":
    print(check_price_sync())
