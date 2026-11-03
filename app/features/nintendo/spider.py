"""任天堂数字商城促销爬虫。"""

import asyncio
import math
import re
from datetime import datetime, timezone
from typing import Dict

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.core.logging import get_logger
from app.core.settings import settings
from app.features.nintendo.models import NinPriceRecord
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


async def determine_total_pages(page) -> int:
    """估算总页数，避免漏抓。"""

    try:
        await page.wait_for_selector(AMOUNT_SELECTOR, timeout=5000)
        raw_amount = await page.locator(AMOUNT_SELECTOR).inner_text()
        total_items = extract_digits(raw_amount)
        if total_items <= 0:
            raise ValueError("no total items parsed")
        return max(1, math.ceil(total_items / PAGE_SIZE))
    except Exception as e:
        logger.warning(f"cannot get num_items, fallback to 1 page, err={e}")
        return 1


async def read_price(locator):
    """读取 locator 的文本并转换为价格。"""

    if await locator.count() == 0:
        return None
    text = (await locator.first.inner_text()).strip()
    if not text:
        return None
    return parse_price(text)


async def check_price():
    """抓取最新折扣并根据变化推送通知。"""

    sl = f"now is {datetime.now()}\n"

    price_records = repository.fetch_all()
    updated_records: Dict[str, NinPriceRecord] = {}

    base_url = f"{settings.url.rstrip('/')}/download-code/sale"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(base_url, wait_until="domcontentloaded")
            num_pages = await determine_total_pages(page)

            change_flag = False
            s = ""

            for page_idx in range(1, num_pages + 1):
                url = f"{base_url}?p={page_idx}"
                await page.goto(url, wait_until="domcontentloaded")

                try:
                    await page.wait_for_selector(PRODUCT_CARD_SELECTOR, timeout=7000)
                except PlaywrightTimeoutError:
                    logger.info(f"page {page_idx}: no product cards found")
                    continue

                product_cards = page.locator(PRODUCT_CARD_SELECTOR)
                count = await product_cards.count()
                if count == 0:
                    logger.info(f"page {page_idx}: no product cards found")
                    continue

                for idx in range(count):
                    card = product_cards.nth(idx)
                    id_attr = await card.get_attribute("id")
                    if not id_attr:
                        continue
                    pid = id_attr.split("-")[-1].split("_")[-1]

                    title_loc = card.locator(".product-item-link")
                    try:
                        title_raw = await title_loc.inner_text()
                    except Exception:
                        logger.debug(f"page {page_idx}: cannot get title for {pid}")
                        continue
                    title = title_raw.replace("\n", " ").strip()

                    try:
                        new_val = await read_price(
                            page.locator(f"#product-price-{pid} span")
                        )
                        if new_val is None:
                            continue
                        old_val = await read_price(
                            page.locator(f"#old-price-{pid} span")
                        )
                        if old_val is None:
                            old_val = new_val
                    except ValueError as e:
                        logger.warning(f"price parse failed for {pid}, err={e}")
                        continue

                    if old_val <= 0:
                        continue

                    discount = (old_val - new_val) / old_val * 100
                    line = f"{title}: {old_val} -> {new_val} ({discount:.2f}%)\n"

                    record = price_records.get(pid)
                    if record is None:
                        record = NinPriceRecord(
                            pid=pid,
                            title=title,
                            original_price=old_val,
                            current_price=new_val,
                            discount_percent=round(discount, 2),
                        )
                        price_records[pid] = record
                        updated_records[pid] = record
                        change_flag = True
                        s += line
                        sl += f"{line.rstrip()} [new item!]\n"
                    else:
                        if new_val < record.current_price:
                            updated_record = record.model_copy(
                                update={
                                    "title": title,
                                    "original_price": old_val,
                                    "current_price": new_val,
                                    "discount_percent": round(discount, 2),
                                    "updated_at": datetime.now(timezone.utc),
                                }
                            )
                            price_records[pid] = updated_record
                            updated_records[pid] = updated_record
                            change_flag = True
                            s += line
                            sl += f"{line.rstrip()} [higher discount!]\n"

        if change_flag:
            repository.bulk_upsert(updated_records.values())
            try:
                # low：逛街性质，路过看见悬浮横幅就行，不值得响铃打断。
                send_note(
                    "本日新增折扣信息",
                    s,
                    topic=TOPIC,
                    priority="low",
                    tags=["video_game", "moneybag"],
                )
            except PushServiceError as exc:
                logger.error(f"推送折扣信息失败: {exc}")
        else:
            # **没有折扣时不推通知**。原先每天 20:00 雷打不动推一条「今天没有新的
            # 折扣变化」——它不携带任何信息，却训练人忽略整个 nintendo topic，
            # 等真有折扣时也懒得看了。「没消息」本身就是消息。
            # 想确认爬虫是否还活着，看仪表盘的「上次运行」而不是靠每日空推。
            sl += "today no news\n"

        with open("log.log", "a", encoding="utf-8") as f:
            f.write(sl)

        logger.info(sl)
        print(sl)
    except Exception as exc:
        logger.exception(f"NIN 抓取失败: {exc}")
        raise


if __name__ == "__main__":
    asyncio.run(check_price())
