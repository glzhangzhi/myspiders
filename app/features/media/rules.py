"""媒体下载的纯规则（无 IO，有测试）。

两件事放这里而不是 service：
1. **来源输入怎么认** —— 用户往一个框里粘任何东西（`v2_…@finder`、昵称、
   YouTube 的 watch?v=…&list=… 链接、playlist 链接、裸 list id）。
2. **检测到的条目进哪一档** —— pending（待下载）还是 skipped（历史作品）。
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

WX_USERNAME_RE = re.compile(r"^v2_[0-9a-z]+@finder$", re.I)
YT_LIST_ID_RE = re.compile(r"^(PL|UU|LL|WL|FL|OL|RD)[\w-]*$")


@dataclass
class ParsedSource:
    platform: str          # "wx" | "yt"
    key: str               # wx: finder username（未解析时为 ""）；yt: playlist id
    input: str             # 规整后的输入：wx 为昵称/username，yt 为 playlist URL
    error: Optional[str] = None


def parse_source_input(platform: str, raw: str) -> ParsedSource:
    raw = (raw or "").strip()
    if platform == "wx":
        if not raw:
            return ParsedSource("wx", "", "", "请输入视频号昵称或 v2_…@finder")
        if WX_USERNAME_RE.match(raw):
            return ParsedSource("wx", raw, raw)
        if raw.startswith("http"):
            return ParsedSource("wx", "", raw, "视频号请填作者昵称（或 v2_…@finder），不要填视频链接")
        return ParsedSource("wx", "", raw)

    if platform == "yt":
        list_id = ""
        if raw.startswith("http") or "youtube.com" in raw or "youtu.be" in raw:
            url = raw if raw.startswith("http") else "https://" + raw
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            list_id = (q.get("list") or [""])[0]
        elif YT_LIST_ID_RE.match(raw):
            list_id = raw
        if not list_id:
            return ParsedSource("yt", "", raw, "没找到播放列表 id（链接里要有 list=…）")
        if list_id.startswith("RD"):
            return ParsedSource("yt", "", raw, "这是 YouTube 自动生成的合辑（RD…），内容会变，不能当下载清单")
        return ParsedSource("yt", list_id, f"https://www.youtube.com/playlist?list={list_id}")

    return ParsedSource(platform, "", raw, f"未知平台 {platform!r}")


def source_id(platform: str, key: str) -> str:
    return f"{platform}:{key}"


def item_id(platform: str, key: str) -> str:
    return f"{platform}:{key}"


def initial_status(mode: str, baseline_done: bool) -> str:
    """检测新发现的条目该进哪一档。

    - YouTube 列表默认 `all`：列表本身就是机主手工维护的「要下载的清单」，
      列表里有的全都要。
    - 视频号作者默认 `new_only`：添加一个作者时他可能已有几百个作品，
      全进队列会一口气下几十 GB。**首次检测**（还没基线）看到的记 skipped，
      之后出现的才是「更新」、记 pending。skipped 的可以在面板手动加回队列。
    """
    if mode == "all":
        return "pending"
    return "pending" if baseline_done else "skipped"


def pick_exact_candidate(keyword: str, candidates: list[dict]) -> Optional[dict]:
    """昵称**完全一致且只有一个**才自动绑定；否则交给人挑。

    搜「龙虾」会返回「我不叫龙虾.」「辣个龙虾…」—— 模糊匹配自动选第一个，
    就会静默地盯错人，而检测结果看起来一切正常。
    """
    kw = (keyword or "").strip()
    exact = [c for c in candidates or [] if (c.get("nickname") or "").strip() == kw]
    return exact[0] if len(exact) == 1 else None


# 条目状态
PENDING, SKIPPED, DOWNLOADING, DONE, FAILED = "pending", "skipped", "downloading", "done", "failed"
ITEM_STATUSES = (PENDING, DOWNLOADING, DONE, FAILED, SKIPPED)
