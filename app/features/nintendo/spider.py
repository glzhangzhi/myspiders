"""任天堂数字商城促销爬虫。"""

import asyncio
import math
import re
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.core.logging import get_logger
from app.core.settings import settings
from app.features.nintendo.reconcile import Scraped, reconcile
from app.features.nintendo.repo import NinPriceRepository
from app.core.push import PushServiceError, send_note

logger = get_logger("NIN")
repository = NinPriceRepository()

TOPIC = "nintendo"

PAGE_SIZE = 24
PRODUCT_CARD_SELECTOR = '//div[contains(@class,"product-item-info")]'
AMOUNT_SELECTOR = '//*[@id="toolbar-amount"]/span[last()]'
PRICE_SANITIZE_RE = re.compile(r"[^\d.]")


def extract_digits(value: str) -> int:
    """提取字符串中的数字，便于解析总条目数。"""

    digits = "".join(ch for ch in value if ch.isdigit())
    return int(digits) if digits else 0


def parse_price(raw: str) -> float:
    """把诸如 'HK$ 55.00' 之类的价格转换为 float。"""

    cleaned = PRICE_SANITIZE_RE.sub("", raw.replace(",", "")).strip()
    if not cleaned:
        raise ValueError(f"无法解析价格: {raw!r}")
    return float(cleaned)


async def determine_total_pages(page) -> Tuple[int, Optional[int]]:
    """返回 (页数, 列表总条数)。读不到总数时退回 1 页、总数 None —— 这一轮就不算完整。"""

    try:
        await page.wait_for_selector(AMOUNT_SELECTOR, timeout=5000)
        raw_amount = await page.locator(AMOUNT_SELECTOR).inner_text()
        total_items = extract_digits(raw_amount)
        if total_items <= 0:
            raise ValueError("no total items parsed")
        return max(1, math.ceil(total_items / PAGE_SIZE)), total_items
    except Exception as e:
        logger.warning(f"cannot get num_items, fallback to 1 page, err={e}")
        return 1, None


async def read_price(locator):
    """读取 locator 的文本并转换为价格。"""

    if await locator.count() == 0:
        return None
    text = (await locator.first.inner_text()).strip()
    if not text:
        return None
    return parse_price(text)


async def scrape_sale_list() -> Tuple[List[Scraped], Set[str], bool]:
    """抓特价列表全部页。返回 (解析成功的条目, 列表里出现过的全部 pid, 是否完整)。

    「完整」= 读到了总条数、每一页都加载出了商品、看到的 pid 数不少于总数的 95%
    （翻页期间排序会微动，个别游戏两页都没赶上是正常的，reconcile 对此有容错）。
    不完整的一轮不做「折扣结束」判定，见 reconcile.py。
    """
    base_url = f"{settings.url.rstrip('/')}/download-code/sale"
    items: List[Scraped] = []
    listed: Set[str] = set()
    ok = True

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(base_url, wait_until="domcontentloaded")
        num_pages, total = await determine_total_pages(page)
        if total is None:
            ok = False

        for page_idx in range(1, num_pages + 1):
            await page.goto(f"{base_url}?p={page_idx}", wait_until="domcontentloaded")
            try:
                await page.wait_for_selector(PRODUCT_CARD_SELECTOR, timeout=7000)
            except PlaywrightTimeoutError:
                logger.warning(f"page {page_idx}: no product cards found")
                ok = False
                continue

            cards = page.locator(PRODUCT_CARD_SELECTOR)
            count = await cards.count()
            if count == 0:
                ok = False
                continue

            for idx in range(count):
                card = cards.nth(idx)
                id_attr = await card.get_attribute("id")
                if not id_attr:
                    continue
                pid = id_attr.split("-")[-1].split("_")[-1]
                listed.add(pid)   # 先记下：价格解析失败也说明它还在列表里，不能判为结束

                try:
                    title = (await card.locator(".product-item-link").inner_text()).replace("\n", " ").strip()
                    new_val = await read_price(page.locator(f"#product-price-{pid} span"))
                    if new_val is None:
                        continue
                    old_val = await read_price(page.locator(f"#old-price-{pid} span"))
                    if old_val is None:
                        old_val = new_val
                except Exception as e:
                    logger.warning(f"parse failed for {pid}, err={e}")
                    continue
                if old_val <= 0:
                    continue
                items.append(Scraped(pid=pid, title=title, original_price=old_val, current_price=new_val))

        await browser.close()

    if total is not None and len(listed) < total * 0.95:
        logger.warning(f"incomplete scrape: saw {len(listed)} of {total}")
        ok = False
    return items, listed, ok


_EVENT_TAG = {"new": "新折扣", "resale": "重新打折", "deeper": "更低价"}


async def check_price():
    """抓取特价列表，并进记录，有新折扣 / 重新打折 / 更低价时推送。"""

    try:
        items, listed, complete = await scrape_sale_list()
    except Exception as exc:
        logger.exception(f"NIN 抓取失败: {exc}")
        raise

    now = datetime.now(timezone.utc)
    existing = repository.fetch_all()
    changed, events = reconcile(existing, items, now, listed if complete else None)
    repository.bulk_upsert(changed)

    ended = sum(1 for r in changed if not r.on_sale and r.sale_ended_at == now)
    summary = (f"listed={len(listed)} parsed={len(items)} complete={complete} "
               f"events={len(events)} ended={ended}")
    logger.info(summary)

    if events:
        events.sort(key=lambda e: e.record.discount_percent, reverse=True)
        body = "".join(
            f"[{_EVENT_TAG[e.kind]}] {e.record.title}: {e.record.original_price:g} → "
            f"{e.record.current_price:g} (-{e.record.discount_percent:.0f}%)\n"
            for e in events
        )
        try:
            # low：逛街性质，路过看见悬浮横幅就行，不值得响铃打断。
            send_note("本日新增折扣信息", body, topic=TOPIC, priority="low",
                      tags=["video_game", "moneybag"])
        except PushServiceError as exc:
            logger.error(f"推送折扣信息失败: {exc}")
    # **没有折扣变化时不推通知**：每日空推不携带信息，只会训练人忽略整个 topic。
    # 想确认爬虫是否还活着，看仪表盘的「上次运行」。
    if not complete:
        # 不完整的一轮不判定折扣结束；连续不完整说明页面改版了，要让任务在面板上显示失败
        if not listed:
            raise RuntimeError("特价列表一条都没抓到，页面可能改版了")


if __name__ == "__main__":
    asyncio.run(check_price())
