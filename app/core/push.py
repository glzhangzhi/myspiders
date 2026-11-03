"""统一推送服务 — 基于 ntfy 实现。全站所有手机通知都从这里出去。

═══════════════════════════════════════════════════════════════════════════
 优先级怎么选（**加新通知前必读**）
═══════════════════════════════════════════════════════════════════════════

ntfy 的官方描述（"高优先级会震动久一点"之类）在这里**没有参考价值** —— 最终
行为由机主手机的通知渠道设置决定。以下是 2026-09 在本人手机上逐级实测的结果，
这才是选级别的唯一依据：

    级别      锁屏可见   声音+振动   悬浮横幅   额外行为
    ────────────────────────────────────────────────────────────────
    urgent      ✅         ✅          ✅       **一直响到手动划掉**
    high        ✅         ✅          ✅       —
    default     ❌         ✅          ✅       —
    low         ❌         ❌          ✅       —
    min         ❌         ❌          ❌       只能进 ntfy 应用里翻

── 判据不是「重不重要」，是「要不要我立刻做一个动作」 ──

    urgent  必须立刻动手，**且划掉通知 ≈ 事情办完**
            （抢签证名额、队列排上了、兜底提交失败）
            ⚠️ 它会一直响。要是恢复手段是「打开电脑进仪表盘点个按钮」，
              那 urgent 只是折磨 —— 用 high。

    high    要尽快知道，且**锁屏上就得看见是什么事**
            （私信、新岗位、服务挂了、token 快过期）

    low     路过看见就行，不值得打断
            （日报、回执、周报小结、会自愈的降级提示、逛街性质的折扣）

    min     纯存档，零行动价值；想看时自己去 ntfy 翻
            （窗口重置小结这类）

── ⛔ default 是反模式，本项目一律不用 ──

本机没开 default 档的锁屏通知，于是它的实际效果是：**响+震把你从口袋里叫出来，
锁屏却一片空白，必须解锁才知道发生了什么** —— 高打扰 + 低信息，两头不讨好。
值得打扰就用 high（锁屏能直接看内容），不值得就用 low（不响，路过看见）。
2026-09 已把全站 7 处 default 清零，别再加回来。

注意 `priority=None` **等于 default**（本模块不塞该字段，服务端按 3 处理），
所以「不写 priority」不是中立选择，而是选中了最差的一档。**每个调用点都要
显式写 priority。** 同理，封装通知的辅助函数
签名默认值要挑 `low` 这种不打扰的一端。

── 另一条：「没消息」不要推通知 ──

eShop 原先每天 20:00 雷打不动推一条「本日无新折扣」。它不携带任何信息，却会
训练人忽略整个 topic，等真有折扣时也懒得点开 —— 2026-09 已删。想确认爬虫还
活着，看仪表盘的「上次运行」，别靠每日空推。

═══════════════════════════════════════════════════════════════════════════
 topic 一览
═══════════════════════════════════════════════════════════════════════════

完整 topic = `settings.ntfy_topic_prefix` + 下表的名字（如前缀 myspiders_ + nintendo → myspiders_nintendo）。
**手机必须订阅完整名，没订阅等于没通知** —— 这是最常见的「功能没生效」原因。

    写死在代码里：
      spider      服务级故障（如备份失败）+ 例行回执
      nintendo    eShop 折扣
      claude      Claude 用量预警

    配在各功能 settings 里（可改）：`*_ntfy_topic`，如 media_ntfy_topic = media、joybuy_ntfy_topic。

已知待办：`spider` 是个杂物抽屉，把「服务坏了」和「例行跑完了」混在一个 topic
里，导致没法在 ntfy 客户端里单独给故障类设铃声。拆分需要机主新订阅一个 topic，
尚未执行。

═══════════════════════════════════════════════════════════════════════════
 其他约定
═══════════════════════════════════════════════════════════════════════════

* **click_url 尽量给。** 通知的价值一半在「点了能直接到现场」。私信给消息深链
  （https://discord.com/channels/…），岗位给岗位页，签证给预约日历。
* **推送失败怎么办要想清楚。** 多数场景 catch 住只记日志（通知挂了不该拖垮主
  流程）；但「这条消息推失败了就永远补不回来」的场景要让它抛（例如某些推送
  失败就不落库，下轮仍算新的会重推）。
* **审计优先级用 ast 而不是 grep** —— 多行调用 grep 看不见，会漏。
"""

from __future__ import annotations

from typing import Optional

from app.clients.ntfy import NtfyError, get_ntfy_client

# 默认 topic
DEFAULT_TOPIC = "spider"

# 允许的优先级字面量。**不含 "default"** —— 见模块 docstring：它在本机是
# 「响+震但锁屏看不到内容」，高打扰低信息，全站不用。
PRIORITY_URGENT = "urgent"  # 立刻动手，且划掉通知≈办完；会一直响
PRIORITY_HIGH = "high"      # 尽快知道，锁屏要能看见内容
PRIORITY_LOW = "low"        # 路过看见就行，不响不震
PRIORITY_MIN = "min"        # 纯存档，手机无任何动静


class PushServiceError(Exception):
    """推送相关的统一异常。"""


def send_note(
    title: str,
    body: str,
    *,
    topic: str = DEFAULT_TOPIC,
    priority: Optional[str] = None,
    tags: Optional[list[str]] = None,
    click_url: Optional[str] = None,
    actions: Optional[list[dict]] = None,
):
    """发送文本通知。

    Args:
        title: 通知标题。锁屏上最先看到的就是它，要能独立说清「什么事」。
        body: 正文。
        topic: topic 名（不含前缀），见模块 docstring 的一览表。
        priority: **必填**（虽然签名可省）。urgent / high / low / min 四选一，
            选法见模块 docstring。**不传 = default = 本机最差的一档**
            （响+震但锁屏看不到内容），不是中立选择。
        tags: emoji 短码列表，如 ["warning"]。
        click_url: 点击通知跳转的地址。能给就给 —— 通知的价值一半在
            「点了能直接到现场」。
        actions: 通知上的按钮（ntfy 的 view/http 动作）。⚠️ panel. 在 Cloudflare
            Access 后面，ntfy App 发的 http 动作带不上 Access cookie、会被 302 到登录页 ——
            要落到本服务的动作一律用 view（在浏览器里打开，那里有 cookie）。

    Raises:
        PushServiceError: 推送失败。调用方要想清楚该吞还是该抛：多数场景
            catch 住只记日志；「漏了就再也补不回来」的场景要让它往上抛。
    """

    client = get_ntfy_client()
    try:
        return client.send(
            topic=topic,
            title=title,
            message=body,
            priority=priority,
            tags=tags,
            click_url=click_url,
            actions=actions,
        )
    except NtfyError as exc:
        raise PushServiceError(str(exc)) from exc


def send_link(
    title: str,
    body: str,
    url: str,
    *,
    topic: str = DEFAULT_TOPIC,
    priority: Optional[str] = None,
    tags: Optional[list[str]] = None,
):
    """发送带链接的通知（点击通知打开链接）。

    与 `send_note(..., click_url=url)` 等价，只是把链接提成必填位置参数。
    priority 的选法同 send_note，见模块 docstring。
    """

    client = get_ntfy_client()
    try:
        return client.send(
            topic=topic,
            title=title,
            message=body,
            priority=priority,
            tags=tags,
            click_url=url,
        )
    except NtfyError as exc:
        raise PushServiceError(str(exc)) from exc


__all__ = [
    "send_note",
    "send_link",
    "PushServiceError",
    "PRIORITY_URGENT",
    "PRIORITY_HIGH",
    "PRIORITY_LOW",
    "PRIORITY_MIN",
]
