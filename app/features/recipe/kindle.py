"""菜谱渲染成 Kindle 可读的 txt，并通过 SSH/SFTP 推送到设备。

为什么是 txt 而不是 epub/pdf：
- KOReader 打开 txt 秒开，字体大小随手调，不用等版式重排
- 做饭时要的是"瞄一眼下一步"，不是精美排版
- 出问题时可以直接 cat 看，不用工具链

排版针对**厨房场景**：站在灶台前、手上是湿的、瞄一眼就要看懂。
故步骤独占一行且带大序号，配料表对齐成两列。

**一道菜一个文件、文件名就是菜名**（`回锅肉.txt`）：同一道菜再推一次直接覆盖，
Kindle 上每道菜只有最新一份。早期按推送时间命名、多道菜合并成一个「菜单」
文件，结果 Kindle 上堆满同名不同时间的版本，找最新的那份反而费劲。
"""

from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.core.settings import settings

logger = get_logger("recipe_kindle")

_TZ = ZoneInfo("Europe/Berlin")

# Kindle 屏幕窄，KOReader 默认字号下一行约 30 个汉字。分隔线取 32 个字符宽。
_RULE = "=" * 32
_THIN = "-" * 32

# 步骤开头的序号，如 "1. " / "2、" / "3)" —— 必须带分隔符才算序号，
# 否则会把「30g 米粉…」的用量当成序号剥掉（见 render_recipe 里的注释）。
_STEP_PREFIX = re.compile(r"^\d{1,2}\s*[.、)）]\s*")


def _safe_filename(name: str) -> str:
    """把用户给的文件名压成纯文件名。

    shlex.quote 能挡 shell 注入，但挡不住路径穿越 —— `../../etc/passwd` 被引用后
    依然是个合法的相对路径，会写到 documents 之外。故只取最后一段并剔除分隔符。
    """
    name = (name or "").strip().replace("\\", "/")
    name = name.split("/")[-1]
    name = name.replace("\x00", "").strip()
    if name in ("", ".", ".."):
        return "今日菜单.txt"
    return name[:120]


def _fmt_ingredient(ing: dict) -> str:
    """配料行：`· 五花肉片      200g`（有用量才右侧对齐，没有就只留名字）。"""
    name = (ing.get("name") or "").strip()
    amount = (ing.get("amount") or "").strip()
    note = (ing.get("note") or "").strip()

    line = f"· {name}"
    if amount:
        line = f"{line}  {amount}"
    if note:
        # 括注在厨房里基本是噪音（供应商名），缩进到下一行、低优先级
        line = f"{line}\n    （{note}）"
    return line


def render_recipe(recipe: dict, index: Optional[int] = None) -> str:
    """单道菜 → 纯文本段落。"""
    lines: list[str] = []

    title = recipe.get("title") or "未命名"
    heading = f"{index}. {title}" if index else title
    lines.append(heading)

    meta_bits = []
    if recipe.get("category"):
        meta_bits.append(recipe["category"])
    if recipe.get("servings"):
        meta_bits.append(recipe["servings"])
    if recipe.get("total_minutes"):
        meta_bits.append(f"约 {recipe['total_minutes']} 分钟")
    tags = (recipe.get("method_tags") or []) + (recipe.get("flavor_tags") or [])
    if tags:
        meta_bits.append(" ".join(tags))
    if meta_bits:
        lines.append("  ·  ".join(meta_bits))

    ingredients = recipe.get("ingredients") or []
    if ingredients:
        lines.append("")
        lines.append("【配料】")
        lines.extend(_fmt_ingredient(i) for i in ingredients)

    steps = recipe.get("steps") or []
    if steps:
        lines.append("")
        lines.append("【步骤】")
        for pos, step in enumerate(steps, 1):
            # 用 enumerate 而不是 steps.index(step)：dict 的 index() 按值相等查找，
            # 两个内容相同的步骤（"翻炒均匀" 出现两次）会都拿到第一个的位置。
            order = step.get("order") or pos
            text = (step.get("text") or "").strip()
            # 源数据的步骤文本常自带 "1. " 前缀，去掉避免出现 "[1] 1. xxx"。
            # 必须锚定成"开头的序号+分隔符"整体匹配，不能用 lstrip 逐字符剥：
            # lstrip("0123456789.、) ") 会把「30g 米粉用…」啃成「g 米粉用…」
            # —— 数字是配料用量的一部分，不是序号。
            text = _STEP_PREFIX.sub("", text, count=1).strip() or text
            lines.append("")
            lines.append(f"[{order}] {text}")

    if recipe.get("tips"):
        lines.append("")
        lines.append("【小贴士】")
        lines.append(recipe["tips"])

    return "\n".join(lines)


def recipe_filename(title: str, taken: Optional[set] = None, source: str = "") -> str:
    """推送文件名 = 菜名（无日期），同名推送直接覆盖 Kindle 上的旧版本。

    一道菜在 Kindle 上只留一份最新的：改过用量再推一次，拿起 Kindle 看到的
    就是新版，不用在一排带时间戳的同名文件里找最新那个。

    `taken` 是本批已用掉的文件名：同一批里两道同名菜（不同来源）不能互相
    覆盖，后来的那道补上来源后缀。跨批次的同名覆盖则正是想要的语义。
    """
    base = _safe_filename(title or "未命名")
    if base.lower().endswith(".txt"):
        base = base[:-4]
    name = f"{base}.txt"
    if taken is not None:
        if name in taken:
            name = f"{base}（{_safe_filename(source or '另一版')}）.txt"
            n = 2
            while name in taken:
                name = f"{base}（{n}）.txt"
                n += 1
        taken.add(name)
    return name


def render_recipe_file(recipe: dict, note: str = "") -> str:
    """单道菜 → 一个完整的 txt 文件。

    文件按菜名覆盖，故文件里写上生成时间 —— 这是在 Kindle 上判断「这是不是
    刚推的那版」的唯一依据。
    """
    now = datetime.now(_TZ)
    lines = [_RULE, render_recipe(recipe), ""]
    if note:
        lines[1:1] = [f"备注：{note}", _THIN]
    lines.extend([_THIN, f"由 MySpiders 生成 · {now.strftime('%Y-%m-%d %H:%M')}"])
    # Kindle/KOReader 对 \r\n 和 \n 都认，统一用 \n
    return "\n".join(lines) + "\n"


def _ssh_base() -> list[str]:
    """构造 ssh 公共参数。

    走公钥认证 + BatchMode：**任何情况下都不能弹交互式密码提示** ——
    这是服务进程，卡在 stdin 上就是永久挂起（交互式登录提示冻死服务进程
    是踩过的事故）。
    """
    base = [
        "ssh",
        "-p", str(settings.kindle_ssh_port),
        "-i", str(Path(settings.kindle_ssh_key).expanduser()),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={settings.kindle_ssh_timeout}",
        "-o", "LogLevel=ERROR",
    ]
    jump = (settings.kindle_ssh_jump or "").strip()
    if jump:
        # 用 ProxyCommand 而不是 -J：-J 会把上面的 -p 2222 / -i 也套到跳板那一跳上，
        # 而 Pi 的 sshd 在 22、用的是 Pi 自己的那把钥匙。ProxyCommand 里的跳板 ssh
        # 只读 ~/.ssh/config 与默认端口，两跳各管各的。BatchMode 两跳都要有。
        base += ["-o", f"ProxyCommand=ssh -o BatchMode=yes -o ConnectTimeout="
                       f"{settings.kindle_ssh_timeout} -W %h:%p {jump}"]
    return base


def check_kindle() -> dict:
    """探测 Kindle 是否在线可写。

    注意**不用 ping**：Kindle 默认丢 ICMP，ping 不通也能 SSH。只认 SSH 探测结果。
    """
    if not settings.kindle_host:
        return {"online": False, "error": "未配置 kindle_host"}

    target = f"{settings.kindle_user}@{settings.kindle_host}"
    cmd = [*_ssh_base(), target, f"test -d {shlex.quote(settings.kindle_recipe_dir)} && echo DIR_OK || echo NO_DIR"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=settings.kindle_ssh_timeout + 10,
        )
    except subprocess.TimeoutExpired:
        return {"online": False, "error": "SSH 超时（Kindle 可能在休眠或不在网内）"}
    except FileNotFoundError:
        return {"online": False, "error": "本机没有 ssh 命令"}

    if proc.returncode != 0:
        return {"online": False, "error": (proc.stderr or proc.stdout or "SSH 失败").strip()[:300]}

    out = (proc.stdout or "").strip()
    return {
        "online": True,
        "dir_exists": "DIR_OK" in out,
        "host": settings.kindle_host,
        "path": settings.kindle_recipe_dir,
    }


def push_text(content: str, filename: str) -> dict:
    """把文本写到 Kindle 的目标目录。

    用 `ssh ... 'cat > file'` 而非 scp：KOReader 自带的 dropbear **没有
    sftp-server**，scp（新版走 SFTP 协议）会失败。管道写入只依赖 shell 重定向，
    在任何 dropbear 上都成立。
    """
    if not settings.kindle_host:
        return {"ok": False, "error": "未配置 kindle_host（见 .env）"}

    target = f"{settings.kindle_user}@{settings.kindle_host}"
    remote_dir = settings.kindle_recipe_dir
    remote_path = f"{remote_dir.rstrip('/')}/{_safe_filename(filename)}"

    # mkdir -p 兜底：documents 目录一定在，但用户可能配置了子目录
    # 先写临时文件再 mv：文件按菜名覆盖，传到一半断线（Kindle 休眠、Wi-Fi 掉）
    # 时直接 `cat >` 会把旧版截成半截，而旧版本来是完好可用的。
    tmp_path = f"{remote_path}.part"
    remote_cmd = (
        f"mkdir -p {shlex.quote(remote_dir)} && cat > {shlex.quote(tmp_path)}"
        f" && mv -f {shlex.quote(tmp_path)} {shlex.quote(remote_path)}"
    )
    cmd = [*_ssh_base(), target, remote_cmd]

    try:
        proc = subprocess.run(
            cmd,
            input=content.encode("utf-8"),
            capture_output=True,
            timeout=settings.kindle_ssh_timeout + 60,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "推送超时（Kindle 可能在休眠）"}
    except FileNotFoundError:
        return {"ok": False, "error": "本机没有 ssh 命令"}

    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        logger.warning("推送 Kindle 失败: %s", err[:300])
        return {"ok": False, "error": err[:300] or f"ssh 退出码 {proc.returncode}"}

    logger.info("已推送 %s (%d 字节) 到 %s", filename, len(content.encode()), target)
    return {
        "ok": True,
        "path": remote_path,
        "bytes": len(content.encode("utf-8")),
    }


_status_cache = None


def kindle_status(force: bool = False) -> dict:
    """check_kindle 的缓存版：60 秒内直接回上次结果，过期先回旧值、后台重探。"""
    global _status_cache
    if _status_cache is None:
        from app.core.cache import SWRCache

        _status_cache = SWRCache(check_kindle, 60, "kindle_status")
    return {**_status_cache.get(force=force), "checked_at": _status_cache.cached_at}
