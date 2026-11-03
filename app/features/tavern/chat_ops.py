"""聊天记录的**写**操作：重命名、删除、异地归档。

和 tavern_chats（只读扫文件）分开，因为写的纪律完全不同：

## 写必须调酒馆的 HTTP API，不能直接动文件

这条和 `tavern_catalog` / `tavern_sync_service` 那对「读扫文件、写走 ts.mjs」
是同一个道理，但**通道不同** —— tavern_sync 管的是角色卡/世界书/预设的内容，
聊天记录不归它管，它也没有对应的命令。聊天记录的唯一合法写入口是酒馆自己的
`/api/chats/rename` 与 `/api/chats/delete`。

直接 `os.rename` / `os.unlink` 的后果：酒馆内存里持有当前会话的完整消息数组，
下一次自动保存（每发一条消息都会）会按它内存里的文件名整个写回去 —— 表现为
「删了又回来」或者「改名后变成两份」。

## 酒馆的鉴权：单用户自动登录 + CSRF

酒馆开着 `enableUserAccounts: true`，但 `data/_storage` 里只有一个 handle 为
`default-user`、密码为空的用户，于是 `tryAutoLogin` 里的 `singleUserLogin`
直接放行。所以流程是：

1. `GET /csrf-token` —— 同时拿到 session cookie 和 token
2. 业务请求带上 `x-csrf-token` 头 + 那个 cookie

**第一次 GET 必须落在同一个 session 上**，token 是存进 session 的
（`csrf-sync` 的 storeTokenInState），换个 cookie 就对不上。故用
`requests.Session` 全程复用。

127.0.0.1 在酒馆的 whitelist 里，所以本机直连不会被 403。

## avatar_url 传的是 PNG 文件名，不是卡名

酒馆用 `String(avatar_url).replace('.png','')` 拼聊天目录。和
tavern_chats 的目录定位口径一致（见那个模块的文档），入口一律收卡名，
内部经 catalog 换成文件名。
"""

from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.core.logging import get_logger
from app.core.settings import settings
from app.features.tavern.catalog import TYPE_CHARACTER, tavern_catalog
from app.features.tavern.chats import TavernChatError, tavern_chats

logger = get_logger("tavern_chat_ops")

# 一次批量操作最多处理多少份。纯粹是防手滑（前端全选后误点），
# 不是性能限制 —— 真要删 200 份分两次就是了。
_MAX_BATCH = 100

# rclone 上传的超时。单份最大实测 5.3MB，给足余量。
_RCLONE_TIMEOUT = 300

_ops_lock = threading.Lock()


class TavernOpError(RuntimeError):
    """预期内的用户可读错误。"""


def _avatar_of(card_name: str) -> str:
    """卡名 → PNG 文件名（含 .png），即酒馆 API 要的 avatar_url。"""
    for item in tavern_catalog.all_items():
        if item.type == TYPE_CHARACTER and item.name == card_name:
            return item.file
    for item in tavern_catalog.all_items(force=True):
        if item.type == TYPE_CHARACTER and item.name == card_name:
            return item.file
    raise TavernOpError(f"酒馆里找不到名为 “{card_name}” 的角色卡")


class TavernHttpClient:
    """酒馆 HTTP API 的薄封装。

    session 复用但**不缓存到进程级**：酒馆重启会换掉 cookie secret，
    留着旧 session 只会在下次操作时莫名 403。每次操作新建一个，代价是
    多一次 `/csrf-token` 往返（本机毫秒级）。
    """

    def __init__(self) -> None:
        self.base = settings.tavern_http_base.rstrip("/")
        self.timeout = settings.tavern_http_timeout

    def _open(self) -> tuple[requests.Session, str]:
        sess = requests.Session()
        try:
            # 先摸一次首页：触发单用户自动登录，把 user 挂到 session 上。
            # 不做这步的话 csrf-token 能拿到，但业务请求会因为 req.user
            # 为空而被 requireLoginMiddleware 挡成 403（实测）。
            sess.get(f"{self.base}/", timeout=self.timeout, allow_redirects=True)
            r = sess.get(f"{self.base}/csrf-token", timeout=self.timeout)
            r.raise_for_status()
            token = r.json().get("token")
        except requests.RequestException as exc:
            raise TavernOpError(
                f"连不上酒馆（{self.base}）—— 它没在跑，或端口不对：{exc}"
            ) from exc
        except ValueError as exc:
            raise TavernOpError("酒馆返回的 csrf-token 不是 JSON，版本可能不兼容") from exc
        if not token:
            raise TavernOpError("酒馆没给出 csrf-token")
        return sess, token

    def post(self, path: str, payload: dict) -> dict:
        sess, token = self._open()
        try:
            r = sess.post(
                f"{self.base}{path}",
                json=payload,
                headers={"x-csrf-token": token},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TavernOpError(f"请求酒馆失败：{exc}") from exc
        finally:
            sess.close()

        if r.status_code == 403:
            raise TavernOpError("酒馆拒绝了请求（鉴权失败）—— 试试在浏览器里重新打开酒馆")
        if r.status_code >= 400:
            # 酒馆的失败体经常是 {"error":true} 或纯文本，都不值得原样抛给用户
            raise TavernOpError(f"酒馆拒绝了操作（HTTP {r.status_code}）")
        try:
            return r.json()
        except ValueError:
            return {}


class TavernChatOps:
    def __init__(self) -> None:
        self.http = TavernHttpClient()

    # ── 保护：这份是不是可能正开着 ──

    def _guard_active(self, path: Path) -> None:
        """最近被写过的记录拒绝改动。

        判据是**文件 mtime**，不是卡内嵌的 chat 指针：后者是「下次默认打开
        哪一份」的书签，每张聊过的卡都必然有一个，拿它当锁会误伤大批早已
        不用的记录（模块级决策，见 settings.tavern_chat_lock_hours 的注释）。
        """
        hours = max(0, settings.tavern_chat_lock_hours)
        if not hours:
            return
        try:
            age = datetime.now(timezone.utc) - datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            )
        except OSError:
            return
        if age.total_seconds() < hours * 3600:
            days = age.total_seconds() / 86400
            when = f"{age.total_seconds()/3600:.1f} 小时前" if days < 1 else f"{days:.1f} 天前"
            raise TavernOpError(
                f"「{path.stem}」{when}还在写入，可能正开着 —— "
                f"为免酒馆把它自动保存回来，{hours} 小时内的记录不允许删除或重命名。"
            )

    # ── 重命名 ──

    def rename(self, card_name: str, file_name: str, new_title: str) -> dict:
        """重命名一份聊天记录。new_title 不含 .jsonl。"""
        new_title = (new_title or "").strip()
        if not new_title:
            raise TavernOpError("新名称不能为空")
        # 酒馆用 sanitize() 洗文件名，路径分隔符会被悄悄吃掉而不是报错 ——
        # 与其让用户得到一个和输入不同的名字，不如在这里直接拒绝。
        if any(ch in new_title for ch in '/\\:*?"<>|') or any(
            ord(ch) < 32 for ch in new_title
        ):
            raise TavernOpError("名称里不能有 / \\ : * ? \" < > | 或控制字符")
        if new_title.endswith(".jsonl"):
            new_title = new_title[: -len(".jsonl")]
        # 纯点号的名字会拼出隐藏文件：`.` → `./.jsonl` → 规范化成 `..jsonl`，
        # 酒馆的 readdirSync 还列得出来，但所有隐藏点文件的地方都看不见，
        # 表现为「这份记录凭空消失」（实测真的弄丢过一份 17 层的记录）。
        # 前导点同理，一律拒绝。
        if not new_title.strip(".") or new_title.startswith("."):
            raise TavernOpError("名称不能以点号开头，也不能只由点号组成")

        path = tavern_chats._chat_path(card_name, file_name)
        self._guard_active(path)
        target = path.parent / f"{new_title}.jsonl"
        if target.exists():
            raise TavernOpError(f"这张卡下已经有叫「{new_title}」的记录了")

        # 原 mtime 要留着：酒馆的 rename 是 copyFileSync + unlink，新文件的
        # mtime 变成「此刻」。不还原的话有两个后果，都在实测中出现过：
        #   1. 刚改完名的记录立刻落进 _guard_active 的保护窗口，删不掉 ——
        #      而「整理时顺手改名，发现没用就删」正是最自然的工作流；
        #   2. 列表里「最后修改」一列变成改名时间，按「多久没聊了」排序失效。
        # mtime 不是酒馆的状态（它只按内存里的消息数组重写**内容**），
        # 所以还原时间戳不构成「越过酒馆写文件」。
        try:
            st = path.stat()
            times = (st.st_atime, st.st_mtime)
        except OSError:
            times = None

        with _ops_lock:
            self.http.post(
                "/api/chats/rename",
                {
                    "is_group": False,
                    "avatar_url": _avatar_of(card_name),
                    "original_file": file_name,
                    "renamed_file": f"{new_title}.jsonl",
                },
            )
            if times and target.exists():
                try:
                    os.utime(target, times)
                except OSError as exc:
                    # 还原失败只影响排序与保护窗口，重命名本身已经成功了，
                    # 不该因此报错让用户以为没改成
                    logger.warning("重命名后还原 mtime 失败 %s: %s", target.name, exc)

        tavern_chats.invalidate()
        logger.info("重命名聊天记录 %s: %s → %s", card_name, path.stem, new_title)
        return {"ok": True, "card": card_name, "file": f"{new_title}.jsonl"}

    # ── 删除 ──

    def delete(self, items: list[dict]) -> dict:
        """批量删除。items 每项 {card, file}。

        逐份独立处理：一份失败不该让其余的也不执行（用户勾了 20 份，
        其中一份正开着，剩下 19 份仍然该删掉）。返回成功与失败两张清单。
        """
        if not items:
            raise TavernOpError("没有选中任何记录")
        if len(items) > _MAX_BATCH:
            raise TavernOpError(f"一次最多删 {_MAX_BATCH} 份，选中了 {len(items)} 份")

        done: list[dict] = []
        failed: list[dict] = []
        for it in items:
            card = str(it.get("card") or "")
            fname = str(it.get("file") or "")
            try:
                path = tavern_chats._chat_path(card, fname)
                self._guard_active(path)
                size = path.stat().st_size
                with _ops_lock:
                    self.http.post(
                        "/api/chats/delete",
                        {"avatar_url": _avatar_of(card), "chatfile": fname},
                    )
                done.append({"card": card, "file": fname, "size": size})
            except (TavernOpError, TavernChatError) as exc:
                failed.append({"card": card, "file": fname, "error": str(exc)})

        tavern_chats.invalidate()
        if done:
            logger.info("删除聊天记录 %d 份（失败 %d）", len(done), len(failed))
        return {
            "deleted": done,
            "failed": failed,
            "freed": sum(d["size"] for d in done),
        }

    # ── 异地归档 ──

    def sync(self, items: list[dict]) -> dict:
        """把选中的记录原样传到 rclone remote。

        **原始 jsonl 按字节上传**，不做任何转码 —— 这份归档的用途是「能导回
        酒馆」，md/txt 是有损的。同 tavern_chats.export_one 里 jsonl 的处理。

        远端按卡分目录：`<remote>/<PNG文件名>/<聊天文件名>`。用 PNG 文件名
        而不是卡名，与酒馆本地的 chats/ 结构保持一致，恢复时能直接对着放回去。
        """
        if not items:
            raise TavernOpError("没有选中任何记录")
        if len(items) > _MAX_BATCH:
            raise TavernOpError(f"一次最多同步 {_MAX_BATCH} 份，选中了 {len(items)} 份")

        remote = settings.tavern_chat_remote.rstrip("/")
        done: list[dict] = []
        failed: list[dict] = []
        for it in items:
            card = str(it.get("card") or "")
            fname = str(it.get("file") or "")
            try:
                path = tavern_chats._chat_path(card, fname)
                # 同步是只读操作，不受 _guard_active 限制 —— 正在聊的那份
                # 备份一下完全无害，反而是最该备份的。
                dest = f"{remote}/{path.parent.name}/{path.name}"
                r = subprocess.run(
                    ["rclone", "copyto", str(path), dest],
                    capture_output=True,
                    text=True,
                    timeout=_RCLONE_TIMEOUT,
                )
                if r.returncode != 0:
                    raise TavernOpError(
                        f"rclone 失败（退出码 {r.returncode}）：{r.stderr.strip()[:200]}"
                    )
                done.append({"card": card, "file": fname, "dest": dest,
                             "size": path.stat().st_size})
            except FileNotFoundError as exc:
                raise TavernOpError("找不到 rclone 可执行文件") from exc
            except subprocess.TimeoutExpired:
                failed.append({"card": card, "file": fname,
                               "error": f"rclone 超时（{_RCLONE_TIMEOUT}s）"})
            except (TavernOpError, TavernChatError) as exc:
                failed.append({"card": card, "file": fname, "error": str(exc)})

        if done:
            logger.info("同步聊天记录 %d 份到 %s（失败 %d）",
                        len(done), remote, len(failed))
        return {
            "synced": done,
            "failed": failed,
            "bytes": sum(d["size"] for d in done),
            "remote": remote,
        }

    # ── 状态 ──

    def status(self) -> dict:
        """酒馆 HTTP 通道是否可用 —— 前端据此决定要不要灰掉写操作按钮。"""
        info: dict = {
            "base": self.http.base,
            "remote": settings.tavern_chat_remote,
            "lock_hours": settings.tavern_chat_lock_hours,
        }
        try:
            self.http._open()
            info["reachable"] = True
        except TavernOpError as exc:
            info["reachable"] = False
            info["error"] = str(exc)
        return info


tavern_chat_ops = TavernChatOps()

__all__ = ["tavern_chat_ops", "TavernOpError"]
