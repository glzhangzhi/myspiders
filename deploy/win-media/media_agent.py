#!/usr/bin/env python3
"""媒体下载 agent —— 跑在 3060ti 的 WSL 里，由 free-arm 的 myspider 经 ssh 调用。

## 定位

服务器（free-arm）管「有哪些来源、哪些条目、下没下完」，本脚本只管「在这台电脑上
执行一次」：检测一次 / 下载一批 / 停止。它**不存任何状态**（除了运行中的 pidfile），
所以服务器每次运行前都会把这份文件推过来覆盖（`cat > agent.py.tmp && mv`）——
仓库里只有一份，不会出现 Pi 那种「两边版本漂了」的问题。

## 协议

    ssh wsl-3060ti /root/media-agent/.venv/bin/python /root/media-agent/agent.py <cmd>

- stdin：一个 JSON 对象（`config` + 命令参数）
- stdout：**一行一个 JSON 事件**（`{"ev": "...", ...}`），最后一行一定是 `end`
- 退出码不承载语义，以 `end.status` 为准

## 两个下载后端

- **视频号**：调同机 Windows 上 ltaoo/wx_channels_download 的本地 API
  （`127.0.0.1:2022`；WSL 是 mirrored 网络，localhost 与 Windows 共享）。
  它借电脑微信里**已打开的视频号页面**的登录态调 finderUserPage，并负责解密。
  页面没开 = `/api/channels/status` 的 `available=false`，视频号部分整体跳过。
- **YouTube**：WSL 里的 Linux 版 yt-dlp（venv）+ ffmpeg + deno。
  不用 Windows 侧那个 yt-dlp.exe：那边没有 ffmpeg，最高只能到 ~720p。

## 约束

**只用标准库**（`tests/test_media_agent.py` 用 ast 钉着）：它跑在一个只有系统
python + 一个装了 yt-dlp 的 venv 的 WSL 里，多一个三方依赖就多一步部署、多一种
「那边忘了装」的静默失败。
"""

from __future__ import annotations

import json
import os
import re
import glob
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

AGENT_HOME = "/root/media-agent"
PIDFILE = os.path.join(AGENT_HOME, "run.pid")
TMP_DIR = os.path.join(AGENT_HOME, "tmp")

DEFAULT_CONFIG = {
    "ltaoo_url": "http://127.0.0.1:2022",
    "wx_root_win": "D:\\Downloads\\视频号",
    "yt_root": "/mnt/d/Downloads/YouTube",
    "yt_cookies": "",
    "ytdlp": os.path.join(AGENT_HOME, ".venv/bin/yt-dlp"),
    "deno_dir": os.path.join(AGENT_HOME, "deno/bin"),
    "wx_pages_per_scan": 5,
    "wx_pages_full": 100,      # 补全历史时最多翻多少页（每页约 15 条 → 1500 条）
    "wx_task_timeout": 1800,
    "wx_parallel": 2,
}

WX_DOWN_MSG = "视频号下载器没在运行（黑色窗口被关了？）—— 双击桌面「视频号下载器.bat」后再检测"
WX_PAGE_MSG = "视频号页面未连接 —— 在电脑微信里打开视频号后再检测"

# ltaoo 下载任务状态（docs/public/openapi/download_task/list.json）
TASK_WAITING, TASK_PREPARING, TASK_DOWNLOADING, TASK_PAUSED = 0, 1, 2, 3
TASK_MERGING, TASK_DONE, TASK_FAILED, TASK_CANCELLED = 4, 5, 6, 7

# ─────────────────────────────────────────────────────────────────────────
# 事件输出
# ─────────────────────────────────────────────────────────────────────────

_emit_lock = threading.Lock()
# 全局停止信号：信号处理、父进程消失、stdout 断管都置它。Ctx.stop 就是它。
STOP = threading.Event()


def emit(ev: str, **kw) -> None:
    kw["ev"] = ev
    line = json.dumps(kw, ensure_ascii=False)
    with _emit_lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, ValueError):
            # 服务器那头的 ssh 没了：没人听了，赶紧收工，别让 yt-dlp 在后台继续写盘
            STOP.set()


def log(msg: str) -> None:
    emit("log", msg=msg)


# ─────────────────────────────────────────────────────────────────────────
# 纯函数（有测试）
# ─────────────────────────────────────────────────────────────────────────

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def win_safe_name(name: str, max_len: int = 80, fallback: str = "untitled") -> str:
    """把任意字符串变成 Windows 上合法的单级文件/目录名。

    - `\\ / : * ? " < > |` 和控制字符换成全角或下划线（全角保留可读性）
    - 换行、制表符压成空格，连续空白合一
    - 结尾的点和空格 Windows 会静默吃掉（导致「建了却找不到」），去掉
    - CON/NUL 等保留名加下划线
    - 按**字符**截断（中文标题按字节截会切出半个字）
    """
    table = {
        "\\": "＼", "/": "／", ":": "：", "*": "＊", "?": "？",
        '"': "＂", "<": "＜", ">": "＞", "|": "｜",
    }
    s = "".join(table.get(c, c) for c in (name or ""))
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s[:max_len].rstrip(" .")
    if not s:
        s = fallback
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return s


def ytdlp_progress_template() -> str:
    """给 yt-dlp `--progress-template` 用的模板，与 `parse_ytdlp_progress` 配对。"""
    return (
        "download:MEDIAPROG|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
        "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s"
    )


def _num(s: str):
    try:
        if s in ("", "NA", "None", "none"):
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_ytdlp_progress(line: str):
    """解析一行进度输出；不是进度行返回 None。

    yt-dlp 对未知值输出 `NA`。total 优先用精确值、其次估计值；都没有就只报字节数。
    """
    line = (line or "").strip()
    if not line.startswith("MEDIAPROG|"):
        return None
    parts = line.split("|")
    if len(parts) < 6:
        return None
    done, total, est, speed, eta = (_num(p) for p in parts[1:6])
    tot = total or est
    pct = None
    if done is not None and tot:
        pct = max(0.0, min(100.0, done / tot * 100.0))
    return {
        "downloaded": int(done) if done is not None else None,
        "total": int(tot) if tot else None,
        "pct": round(pct, 1) if pct is not None else None,
        "speed": int(speed) if speed is not None else None,
        "eta": int(eta) if eta is not None else None,
    }


def ltaoo_payload(resp: dict):
    """剥掉 ltaoo 的外层信封，返回 (ok, inner, error)。

    视频号接口是两层：`{code, msg, data: {errCode, errMsg, data: {...}}}`；
    下载任务接口是一层：`{code, msg, data: {...}}`。两种都在这里处理。
    """
    if not isinstance(resp, dict):
        return False, None, "响应不是 JSON 对象"
    if resp.get("code") not in (0, None):
        return False, None, str(resp.get("msg") or f"code={resp.get('code')}")
    data = resp.get("data")
    if isinstance(data, dict) and "errCode" in data:
        if data.get("errCode") not in (0, None):
            return False, None, str(data.get("errMsg") or f"errCode={data.get('errCode')}")
        return True, data.get("data"), None
    return True, data, None


def parse_feed_list(inner: dict):
    """作者主页一页 → (视频列表, lastBuffer, has_more)。

    只收 `mediaType == 4`（视频）：图文（2）没有可下载的视频流，直播（9）没有 media。
    has_more 的判据抄自 wechat-mp-tools 的 profile.js：有 lastBuffer 且本页满 15 条。
    """
    inner = inner or {}
    objects = inner.get("object") or []
    last_buffer = inner.get("lastBuffer") or ""
    items = []
    for obj in objects:
        it = feed_to_item(obj)
        if it:
            items.append(it)
    if "continueFlag" in inner:
        has_more = bool(last_buffer) and bool(inner.get("continueFlag"))
    else:
        has_more = bool(last_buffer) and len(objects) >= 15
    return items, last_buffer, has_more, len(objects)


def feed_to_item(obj: dict):
    if not isinstance(obj, dict) or obj.get("liveInfo"):
        return None
    desc = obj.get("objectDesc") or {}
    media = (desc.get("media") or [None])[0] or {}
    media_type = media.get("mediaType", desc.get("mediaType"))
    if media_type != 4:
        return None
    contact = obj.get("contact") or {}
    title = (desc.get("description") or "").strip()
    return {
        "key": str(obj.get("id") or ""),
        "nonce_id": obj.get("objectNonceId") or "",
        "title": title,
        "author": contact.get("nickname") or "",
        "author_id": contact.get("username") or "",
        "published_at": int(obj.get("createtime") or 0) or None,
        "duration": media.get("videoPlayLen"),
        "size": media.get("fileSize"),
        "cover_url": media.get("coverUrl") or media.get("thumbUrl") or "",
    }


def parse_contacts(inner: dict):
    out = []
    for info in (inner or {}).get("infoList") or []:
        c = info.get("contact") or {}
        if c.get("username"):
            out.append({
                "username": c["username"],
                "nickname": c.get("nickname") or "",
                "head_url": c.get("headUrl") or "",
                "signature": c.get("signature") or "",
            })
    return out


def parse_follow_list(inner: dict):
    """关注列表一页 → (contacts, lastBuffer, has_more)。"""
    inner = inner or {}
    out = []
    for c in inner.get("contactList") or []:
        if c.get("username"):
            out.append({
                "username": c["username"],
                "nickname": c.get("nickname") or "",
                "head_url": c.get("headUrl") or "",
                "signature": c.get("signature") or "",
            })
    lb = inner.get("lastBuffer") or ""
    return out, lb, bool(lb) and bool(inner.get("continueFlag"))


def match_contacts(keyword: str, contacts: list) -> list:
    """在本地联系人里按昵称找：完全一致排最前，其次包含，大小写不敏感。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return list(contacts)
    exact = [c for c in contacts if c["nickname"].strip().lower() == kw]
    part = [c for c in contacts if kw in c["nickname"].lower() and c not in exact]
    return exact + part


def should_continue_paging(page_keys, known: set, slack: int = 3) -> bool:
    """这一页看完后还要不要翻下一页。

    **不能「遇到第一个已知就停」**：作者置顶的老视频会排在第一页最前面，
    一碰到它就停等于永远看不到新作品。判据：本页里已知的条目不超过 `slack` 个
    （留给置顶位）才继续。
    """
    n_known = sum(1 for k in page_keys if k in known)
    return n_known <= slack and len(page_keys) > 0


def parse_task_status(task: dict):
    """ltaoo 下载任务记录 → 归一化状态。"""
    task = task or {}
    st = task.get("status")
    files = task.get("files") or task.get("resources") or []
    path = ""
    # 实测 kind 是 MIME（"video/mp4"），文档示例里是 "video"/"file" —— 两种都认，
    # 实在认不出就取第一个有路径的文件。
    for f in files:
        kind = str(f.get("kind") or "")
        if f.get("file_path") and (kind.startswith("video") or kind in ("", "file")):
            path = f["file_path"]
            break
    if not path:
        path = next((f["file_path"] for f in files if f.get("file_path")), "")
    total = task.get("size") or 0
    done = task.get("downloaded") or 0
    # 用字节数自己算：实测下载中 progress 字段的口径不稳（出现过 0.1 而字节数早已过半）
    pct = done / total * 100 if total else task.get("progress")
    return {
        "status": st,
        "finished": st in (TASK_DONE, TASK_FAILED, TASK_CANCELLED),
        "ok": st == TASK_DONE,
        "pct": round(float(pct), 1) if pct is not None else None,
        "speed": task.get("speed"),
        "downloaded": done,
        "total": total,
        "error": task.get("error") or "",
        "path": path,
    }


def wx_filename(obj: dict) -> str:
    """视频号文件名：`YYYYMMDD_描述前 50 字`。描述里常带一串 #话题，去掉；
    描述为空就用 feed id（至少唯一）。"""
    desc = ((obj or {}).get("objectDesc") or {}).get("description") or ""
    desc = re.sub(r"#\S+", " ", desc)
    ts = (obj or {}).get("createtime")
    try:
        day = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().strftime("%Y%m%d") if ts else ""
    except (TypeError, ValueError, OSError):
        day = ""
    title = win_safe_name(desc, 50, fallback=str((obj or {}).get("id") or "video"))
    return f"{day}_{title}" if day else title


def to_windows_path(path: str) -> str:
    """`/mnt/d/Downloads/x.mp4` → `D:\\Downloads\\x.mp4`。面板给人看的一律是 Windows 路径
    （机主在资源管理器里找文件，不会去 WSL 里找）。"""
    m = re.match(r"^/mnt/([a-zA-Z])(/.*)?$", path or "")
    if not m:
        return path or ""
    return m.group(1).upper() + ":" + (m.group(2) or "/").replace("/", "\\")


def yt_cookie_hint(stderr: str) -> str:
    """根据 yt-dlp 的报错给一句人话提示；不是 cookie 问题返回空串。"""
    s = (stderr or "").lower()
    if "cookies are no longer valid" in s or "have likely been rotated" in s:
        return "（YouTube 登录凭据已失效：在 3060ti 上用 Firefox 登录一次 YouTube，之后会自动读取，不用再导出）"
    if "confirm your age" in s:
        return "（年龄限制视频，需要有效的 YouTube 登录：在 3060ti 的 Firefox 里登录 YouTube 后点重试）"
    if yt_auth_error(stderr):
        return "（私人列表需要有效的 YouTube 登录：在 3060ti 的 Firefox 里登录 YouTube）"
    return ""


def yt_auth_error(stderr: str) -> bool:
    s = (stderr or "").lower()
    return any(k in s for k in (
        "sign in to confirm", "playlist does not exist", "private video",
        "cookies are no longer valid", "login required", "members-only",
    ))


# ─────────────────────────────────────────────────────────────────────────
# 运行时
# ─────────────────────────────────────────────────────────────────────────

class Stopped(Exception):
    pass


class Ctx:
    def __init__(self, config: dict):
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.stop = STOP
        self.children: list[subprocess.Popen] = []
        self.wx_tasks: set[int] = set()
        self.lock = threading.Lock()

    def check(self):
        if self.stop.is_set():
            raise Stopped()


def http_json(ctx: Ctx, method: str, path: str, params=None, body=None, timeout=30):
    url = ctx.cfg["ltaoo_url"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    # 显式不走任何代理：ltaoo 自己会把 Windows 系统代理设成它的 2023 端口，
    # mirrored 网络下环境变量偶尔会带进来，访问 localhost 绕一圈只会失败。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"code": -1, "msg": "非 JSON 响应: " + raw[:200].decode("utf-8", "replace")}


def wx_status(ctx: Ctx):
    """(ltaoo 在跑吗, 视频号页面连着吗)"""
    try:
        resp = http_json(ctx, "GET", "/api/channels/status", timeout=4)
    except Exception:
        return False, False
    ok, inner, _ = ltaoo_payload(resp)
    return True, bool(ok and isinstance(inner, dict) and inner.get("available"))


def wx_call(ctx: Ctx, path: str, params: dict, timeout=40):
    resp = http_json(ctx, "GET", path, params=params, timeout=timeout)
    ok, inner, err = ltaoo_payload(resp)
    if not ok:
        raise RuntimeError(err)
    return inner or {}


def env_with_deno(ctx: Ctx):
    env = dict(os.environ)
    env["PATH"] = ctx.cfg["deno_dir"] + ":" + env.get("PATH", "")
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    return env


# ── YouTube 登录凭据 ─────────────────────────────────────────────────────
#
# 2026-11-02 实测：从 Chrome 导出的 cookies.txt **半小时左右就失效**（06:57 导出、
# 07:15 还能下年龄限制视频、07:35 已 "cookies are no longer valid"）。Chrome 新版
# 会频繁轮换 Google 登录 cookie（并逐步绑定到本机，DBSC），导出的那份是一张很快过期的
# 快照，再怎么导都撑不久。
#
# 根治：**直接读 Firefox 的 cookie 库**（`--cookies-from-browser firefox:<profile>`）。
# 浏览器自己负责保持登录、轮换 cookie，yt-dlp 每次运行都读到当下最新的那份；
# Firefox 的库不加密，从 WSL 经 /mnt/c 直接读得到（Chrome 的库是 App-Bound 加密，
# WSL 里解不开）。找不到登录过 YouTube 的 Firefox 时才退回 cookies.txt。

COOKIE_STATE = os.path.join(AGENT_HOME, "yt_cookie_state.json")
FIREFOX_GLOB = "/mnt/c/Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*/cookies.sqlite"
LOGIN_NAMES = ("LOGIN_INFO", "SID", "__Secure-1PSID", "__Secure-3PSID")


def firefox_profiles() -> list:
    """所有 Firefox profile 目录，最近用过的在前。"""
    dbs = glob.glob(FIREFOX_GLOB)
    dbs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return [os.path.dirname(p) for p in dbs]


def firefox_youtube_login(profile: str) -> bool:
    """这个 profile 里有没有 YouTube 登录 cookie。复制一份再读：Firefox 开着时库被锁。"""
    os.makedirs(TMP_DIR, exist_ok=True)
    tmp = os.path.join(TMP_DIR, "ff_probe.sqlite")
    try:
        shutil.copyfile(os.path.join(profile, "cookies.sqlite"), tmp)
        wal = os.path.join(profile, "cookies.sqlite-wal")
        if os.path.exists(wal):
            shutil.copyfile(wal, tmp + "-wal")
        con = sqlite3.connect(tmp)
        try:
            q = ("SELECT COUNT(*) FROM moz_cookies WHERE host LIKE ? AND name IN ("
                 + ",".join("?" * len(LOGIN_NAMES)) + ")")
            return con.execute(q, ("%youtube.com", *LOGIN_NAMES)).fetchone()[0] > 0
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return False
    finally:
        for f in (tmp, tmp + "-wal", tmp + "-shm"):
            try:
                os.unlink(f)
            except OSError:
                pass


def cookies_file_login(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            names = {l.split("\t")[5] for l in f if l.count("\t") >= 6 and not l.startswith("#")}
        return bool(names & set(LOGIN_NAMES))
    except OSError:
        return False


def yt_cookie_source(ctx: Ctx) -> dict:
    """当前该用哪份凭据：{"kind": "firefox"|"file"|"none", "path", "login"}。"""
    if ctx.cfg.get("yt_cookie_mode", "auto") != "file":
        for prof in firefox_profiles():
            if firefox_youtube_login(prof):
                return {"kind": "firefox", "path": prof, "login": True}
    src = ctx.cfg["yt_cookies"]
    if src and os.path.exists(src):
        return {"kind": "file", "path": src, "login": cookies_file_login(src)}
    return {"kind": "none", "path": "", "login": False}


def yt_cookie_args(ctx: Ctx) -> list:
    src = yt_cookie_source(ctx)
    if src["kind"] == "firefox":
        return ["--cookies-from-browser", "firefox:" + src["path"]]
    if src["kind"] == "file":
        # 每次从原件拷一份新的给 yt-dlp（它会往读的文件里写回）。**不能**像上一版那样
        # 持久保留副本：凭据一失效，YouTube 回的 Set-Cookie 会把 SID/LOGIN_INFO 删掉，
        # yt-dlp 照写回去，副本就永久残了（实测 2026-11-02：副本里登录 cookie 全没了）。
        os.makedirs(TMP_DIR, exist_ok=True)
        dst = os.path.join(TMP_DIR, "yt_cookies.txt")
        shutil.copyfile(src["path"], dst)
        os.chmod(dst, 0o600)
        return ["--cookies", dst]
    return []


def note_cookie_result(stderr: str, used_cookies: bool) -> None:
    """记下凭据最近一次的真实表现，ping 时给面板看。

    「cookie 文件里有登录字段」不等于「登录有效」—— 面板原先只看前者，失效了还显示正常。
    """
    if not used_cookies:
        return
    s = (stderr or "").lower()
    if "no longer valid" in s or "rotated" in s:
        state = {"ok": False, "at": time.time(), "msg": "YouTube 说凭据已失效"}
    elif "confirm your age" in s or "sign in to confirm" in s:
        state = {"ok": False, "at": time.time(), "msg": "需要登录才能看（凭据无效或已过期）"}
    else:
        state = {"ok": True, "at": time.time()}
    try:
        with open(COOKIE_STATE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


def read_cookie_state() -> dict:
    try:
        with open(COOKIE_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def run_capture(ctx: Ctx, args, timeout=300):
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env_with_deno(ctx), start_new_session=True)
    with ctx.lock:
        ctx.children.append(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _killpg(p)
        out, err = p.communicate()
        err = (err or b"") + b"\nERROR: timeout"
    finally:
        with ctx.lock:
            if p in ctx.children:
                ctx.children.remove(p)
    ctx.check()
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _killpg(p: subprocess.Popen):
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def last_error_line(stderr: str) -> str:
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    errs = [l for l in lines if l.startswith("ERROR")]
    line = errs[-1] if errs else (lines[-1] if lines else "未知错误")
    # yt-dlp 常在错误后面接一串「Use --cookies… See https://… for how to…」，面板上只是噪音
    line = re.split(r"\. (?:Use --|See  ?https?://|Also see)", line)[0]
    return line[:300]


# ─────────────────────────────────────────────────────────────────────────
# 命令
# ─────────────────────────────────────────────────────────────────────────

def cmd_ping(ctx: Ctx, _payload):
    ltaoo_up, wx_ok = wx_status(ctx)
    ytdlp_ver = ""
    try:
        rc, out, _ = run_capture(ctx, [ctx.cfg["ytdlp"], "--version"], timeout=30)
        ytdlp_ver = out.strip() if rc == 0 else ""
    except Exception:
        pass
    csrc = yt_cookie_source(ctx)
    cstate = read_cookie_state()
    free_gb = None
    root = ctx.cfg["yt_root"]
    probe = root if os.path.exists(root) else os.path.dirname(root.rstrip("/"))
    try:
        free_gb = round(shutil.disk_usage(probe).free / 1e9, 1)
    except Exception:
        pass
    emit("ping",
         ltaoo=ltaoo_up, wx_available=wx_ok,
         ytdlp=ytdlp_ver,
         ffmpeg=bool(shutil.which("ffmpeg")),
         deno=os.path.exists(os.path.join(ctx.cfg["deno_dir"], "deno")),
         cookies=csrc["kind"] != "none",
         cookies_login=csrc["login"],
         cookie_source=csrc["kind"],
         cookie_state=cstate,
         firefox_profiles=len(firefox_profiles()),
         free_gb=free_gb,
         running=_running_pid())
    return "ok"


def followed_contacts(ctx: Ctx, max_pages: int = 40) -> list:
    """当前微信号关注的全部视频号作者（每页 20 个，按 lastBuffer 翻）。"""
    out, marker, seen = [], "", set()
    for page in range(max_pages):
        ctx.check()
        if page:
            _sleep(ctx, 0.8)
        inner = wx_call(ctx, "/api/channels/follow/list", {"next_marker": marker})
        cs, marker, more = parse_follow_list(inner)
        for c in cs:
            if c["username"] not in seen:
                seen.add(c["username"])
                out.append(c)
        if not more:
            break
    return out


def search_contacts(ctx: Ctx, kw: str):
    """(候选, 说明)。先走微信搜索；它失败时（实测 2026-11 一直回
    JSAPI_JSONPARSE_FAILED / H5ExtTransfer:fail，是微信那边的接口问题）
    退回到「在我关注的作者里按昵称找」—— 要下载的作者基本都是关注了的。"""
    try:
        inner = wx_call(ctx, "/api/channels/contact/search", {"keyword": kw, "next_marker": ""})
        cands = parse_contacts(inner)
        if cands:
            return cands, ""
        note = "微信搜索没结果，已改在你关注的作者里找"
    except Stopped:
        raise
    except Exception as e:  # noqa: BLE001
        note = f"微信搜索不可用（{str(e)[:60]}），已改在你关注的作者里找"
    return match_contacts(kw, followed_contacts(ctx)), note


def cmd_search_wx(ctx: Ctx, payload):
    kw = (payload.get("keyword") or "").strip()
    _, avail = wx_status(ctx)
    if not avail:
        emit("search", candidates=[], error="视频号页面未连接：在电脑微信里打开视频号")
        return "ok"
    if not kw:
        emit("search", candidates=followed_contacts(ctx), note="你关注的全部作者")
        return "ok"
    cands, note = search_contacts(ctx, kw)
    emit("search", candidates=cands[:30], note=note)
    return "ok"


def cmd_scan(ctx: Ctx, payload):
    sources = payload.get("sources") or []
    wx = [s for s in sources if s.get("platform") == "wx"]
    yt = [s for s in sources if s.get("platform") == "yt"]

    if wx:
        _, avail = wx_status(ctx)
        emit("wx_status", available=avail)
        if not avail:
            up, _ = wx_status(ctx)
            for s in wx:
                emit("source_error", source_id=s["id"],
                     error=WX_PAGE_MSG if up else WX_DOWN_MSG, transient=True)
        else:
            for i, s in enumerate(wx):
                ctx.check()
                if i:
                    # 两个作者之间停一下：同一个微信号短时间连扫一串主页是最像机器人的形态
                    _sleep(ctx, 2 + (hash(s["id"]) % 20) / 10)
                try:
                    scan_wx_source(ctx, s)
                except Stopped:
                    raise
                except Exception as e:
                    # 实测 2026-11-01：扫到第 27 个作者时机主关了下载器窗口，后面 48 个作者
                    # 全记成「Connection refused」。断了就别一个个撞墙，剩下的统一说清楚原因。
                    up, avail = wx_status(ctx)
                    if not (up and avail):
                        why = WX_DOWN_MSG if not up else WX_PAGE_MSG
                        for rest in wx[i:]:
                            emit("source_error", source_id=rest["id"], error=why, transient=True)
                        log(f"视频号下载器中途断开，剩下 {len(wx) - i} 个作者这次没检测")
                        break
                    emit("source_error", source_id=s["id"], error=str(e)[:400])

    for s in yt:
        ctx.check()
        try:
            scan_yt_source(ctx, s)
        except Stopped:
            raise
        except Exception as e:
            emit("source_error", source_id=s["id"], error=str(e)[:400])
    return "ok"


def _sleep(ctx: Ctx, seconds: float):
    if ctx.stop.wait(seconds):
        raise Stopped()


def scan_wx_source(ctx: Ctx, s: dict):
    username = s.get("key") or ""
    if not username:
        kw = (s.get("input") or "").strip()
        cands, _ = search_contacts(ctx, kw)
        exact = [c for c in cands if c["nickname"] == kw]
        if len(exact) == 1:
            c = exact[0]
            emit("resolved", source_id=s["id"], username=c["username"],
                 nickname=c["nickname"], head_url=c["head_url"])
            username = c["username"]
            s = {**s, "key": username, "name": c["nickname"]}
        else:
            emit("needs_pick", source_id=s["id"], candidates=cands[:8])
            return

    known = set(s.get("known_ids") or [])
    first_time = not s.get("baseline_done")
    # 补全历史：模式是「全部」且历史还没翻到底。旧版服务器不传 full_done → 视为已补全，
    # 行为与之前一致（agent 每次运行都被推新，可能先于服务端重启上线）。
    backfill = s.get("mode") == "all" and s.get("full_done") is False
    if backfill:
        # 翻到底为止，**不能**按「本页大多已知就停」—— 已知的正是最新那几页，
        # 历史全在它们后面（2026-11-02：74 个作者全卡在首次检测的 15 条上）。
        max_pages = int(ctx.cfg["wx_pages_full"])
    elif first_time:
        max_pages = 1          # 「仅新作」只建基线：第一页足以让之后的增量检测有锚点
    else:
        max_pages = int(ctx.cfg["wx_pages_per_scan"])

    marker = ""
    found = []
    complete = exhausted = False
    page = 0
    for page in range(max_pages):
        ctx.check()
        if page:
            _sleep(ctx, 1.2)
        inner = wx_call(ctx, "/api/channels/contact/feed/list",
                        {"username": username, "next_marker": marker})
        items, marker, has_more, n_raw = parse_feed_list(inner)
        keys = [it["key"] for it in items]
        found.extend(it for it in items if it["key"] not in known)
        if not has_more:
            complete = True
            exhausted = True
            break
        if not backfill and not first_time and not should_continue_paging(keys, known):
            complete = True
            break
    name = s.get("name") or (found[0]["author"] if found else "")
    # exhausted = 真的翻到了作者主页的最后一页（补全历史完成的唯一判据）
    emit("found", source_id=s["id"], name=name, items=found, complete=complete,
         exhausted=exhausted, pages=page + 1)


def scan_yt_source(ctx: Ctx, s: dict):
    url = s.get("input") or ""
    # 不加 --no-warnings：「cookies 已失效」只以 WARNING 出现，要靠它判断凭据状态
    args = [ctx.cfg["ytdlp"], "--flat-playlist", "-J", "--ignore-no-formats-error"]
    ck = yt_cookie_args(ctx)
    args += ck
    args.append(url)
    rc, out, err = run_capture(ctx, args, timeout=600)
    note_cookie_result(err, bool(ck))
    try:
        data = json.loads(out) if out.strip() else None
    except ValueError:
        data = None
    if not data:
        msg = last_error_line(err) + yt_cookie_hint(err)
        emit("source_error", source_id=s["id"], error=msg)
        return
    entries = [e for e in (data.get("entries") or []) if e and e.get("id")]
    items = []
    for e in entries:
        title = e.get("title") or ""
        if title in ("[Private video]", "[Deleted video]"):
            continue
        items.append({
            "key": e["id"],
            "title": title,
            "author": e.get("channel") or e.get("uploader") or "",
            "published_at": e.get("timestamp") or e.get("release_timestamp"),
            "duration": e.get("duration"),
            "cover_url": (e.get("thumbnails") or [{}])[-1].get("url", ""),
            "url": f"https://www.youtube.com/watch?v={e['id']}",
        })
    emit("found", source_id=s["id"], name=data.get("title") or "", items=items, complete=True)


def cmd_download(ctx: Ctx, payload):
    items = payload.get("items") or []
    wx = [i for i in items if i.get("platform") == "wx"]
    yt = [i for i in items if i.get("platform") == "yt"]
    if wx:
        _, avail = wx_status(ctx)
        emit("wx_status", available=avail)
        if not avail:
            for it in wx:
                emit("item_failed", id=it["id"], retryable=True,
                     error="视频号页面未连接：在电脑微信里打开视频号后重试")
        else:
            download_wx_batch(ctx, wx)
    for it in yt:
        ctx.check()
        download_yt(ctx, it)
    return "ok"


def download_yt(ctx: Ctx, it: dict):
    root = ctx.cfg["yt_root"].rstrip("/")
    folder = win_safe_name(it.get("source_name") or "未分类", 60)
    os.makedirs(os.path.join(root, folder), exist_ok=True)
    tmp = os.path.join(TMP_DIR, "yt")
    os.makedirs(tmp, exist_ok=True)
    args = [
        ctx.cfg["ytdlp"],
        # 分辨率优先；同分辨率下偏好 H.264 + AAC —— Windows 自带播放器对 AV1/Opus 支持参差。
        # 4K 只有 VP9/AV1，res 在前保证照样拿最高画质。
        "-f", "bv*+ba/b", "-S", "res,fps,vcodec:h264,acodec:m4a", "--merge-output-format", "mp4",
        "--windows-filenames", "--no-playlist", "--no-overwrites", "--continue",
        "--newline", "--progress", "--progress-template", ytdlp_progress_template(),
        "--print", "after_move:MEDIAFILE|%(filepath)s",
        "--no-quiet",
        "--download-archive", os.path.join(root, ".archive.txt"),
        "-P", "home:" + os.path.join(root, folder), "-P", "temp:" + tmp,
        "-o", "%(upload_date>%Y%m%d|00000000)s_%(title).80s [%(id)s].%(ext)s",
    ]
    ck = yt_cookie_args(ctx)
    args += ck
    args.append(it.get("url") or f"https://www.youtube.com/watch?v={it['key']}")

    emit("item_start", id=it["id"])
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env_with_deno(ctx), start_new_session=True)
    with ctx.lock:
        ctx.children.append(p)
    err_lines: list[str] = []

    def _drain_err():
        for raw in p.stderr:
            err_lines.append(raw.decode("utf-8", "replace").rstrip())
            if len(err_lines) > 200:
                del err_lines[:100]

    t = threading.Thread(target=_drain_err, daemon=True)
    t.start()
    final_path = ""
    archived = False
    last_emit = 0.0
    for raw in p.stdout:
        line = raw.decode("utf-8", "replace").strip()
        prog = parse_ytdlp_progress(line)
        if prog:
            now = time.monotonic()
            if now - last_emit >= 1.5:
                emit("progress", id=it["id"], **prog)
                last_emit = now
        elif line.startswith("MEDIAFILE|"):
            final_path = line.split("|", 1)[1]
        elif "already been recorded in the archive" in line:
            archived = True
    p.wait()
    t.join(timeout=5)
    with ctx.lock:
        if p in ctx.children:
            ctx.children.remove(p)
    ctx.check()
    if p.returncode == 0 and (final_path or archived):
        note_cookie_result("\n".join(err_lines), bool(ck))
        size = os.path.getsize(final_path) if final_path and os.path.exists(final_path) else None
        emit("item_done", id=it["id"], path=to_windows_path(final_path), size=size,
             note="已在 .archive.txt 里（之前下载过）" if archived and not final_path else "")
    else:
        err = "\n".join(err_lines)
        note_cookie_result(err, bool(ck))
        msg = last_error_line(err) + yt_cookie_hint(err)
        emit("item_failed", id=it["id"], error=msg, retryable=True)


def download_wx_batch(ctx: Ctx, items: list):
    """最多 `wx_parallel` 个任务同时交给 ltaoo；它自己有并发控制，这里只是别一次塞满。"""
    queue = list(items)
    active: dict[int, dict] = {}
    started: dict[int, float] = {}
    last_change: dict[int, tuple] = {}
    par = max(1, int(ctx.cfg["wx_parallel"]))
    while queue or active:
        ctx.check()
        while queue and len(active) < par:
            it = queue.pop(0)
            tid = wx_create_task(ctx, it)
            if tid:
                active[tid] = it
                started[tid] = time.monotonic()
                with ctx.lock:
                    ctx.wx_tasks.add(tid)
        if not active:
            continue
        _sleep(ctx, 2)
        for tid, it in list(active.items()):
            resp = http_json(ctx, "GET", "/api/v1/download_task/list", params={"task_id": tid}, timeout=15)
            ok, task, err = ltaoo_payload(resp)
            if not ok or not isinstance(task, dict):
                continue
            st = parse_task_status(task)
            sig = (st["status"], st["downloaded"])
            if last_change.get(tid, (None,))[0:2] != sig:
                last_change[tid] = (*sig, time.monotonic())
            if st["finished"]:
                if st["ok"]:
                    emit("item_done", id=it["id"], path=st["path"], size=st["total"] or None)
                else:
                    emit("item_failed", id=it["id"], error=st["error"] or f"ltaoo 任务状态 {st['status']}",
                         retryable=True)
                del active[tid]
                with ctx.lock:
                    ctx.wx_tasks.discard(tid)
                continue
            emit("progress", id=it["id"], pct=st["pct"], speed=st["speed"],
                 downloaded=st["downloaded"], total=st["total"] or None, eta=None)
            idle = time.monotonic() - last_change[tid][2]
            if idle > float(ctx.cfg["wx_task_timeout"]) or (
                    st["status"] == TASK_PAUSED and idle > 120):
                emit("item_failed", id=it["id"], retryable=True,
                     error=f"ltaoo 任务 {tid} 超过 {int(idle)} 秒没有进展（状态 {st['status']}）")
                del active[tid]
                with ctx.lock:
                    ctx.wx_tasks.discard(tid)


def wx_create_task(ctx: Ctx, it: dict):
    """先取最新的 feed（检测时拿到的 CDN 链接带 token，会过期），再建 ltaoo 任务。"""
    emit("item_start", id=it["id"])
    try:
        params = {"oid": it["key"]}
        if it.get("nonce_id"):
            params["nid"] = it["nonce_id"]
        inner = wx_call(ctx, "/api/channels/feed/profile", params)
        obj = (inner or {}).get("object") or inner
        if not isinstance(obj, dict) or not obj.get("id"):
            raise RuntimeError("feed/profile 没有返回视频对象（可能已被删除或设为私密）")
        resp = http_json(ctx, "POST", "/api/v1/download_task/create", body={"objects": [{
            "platform": "wxchannels",
            "content": obj,
            # 每个作者一个子目录。文件名交给 ltaoo 的 filenameTemplate（默认「标题_规格」），
            # 它会按 Windows 规则清洗；这里再拼一层容易和它的扩展名逻辑打架。
            "download_dir": ctx.cfg["wx_root_win"].rstrip("\\") + "\\"
                            + win_safe_name(it.get("source_name") or it.get("author") or "未分类", 60),
            # 不传的话文件名是「feed id_规格.mp4」，一堆数字没法认。ltaoo 会在后面补 `_<规格>`。
            "filename": wx_filename(obj),
            "auto_start": True,
            # spec=original：默认会下 xWT111（720p 转码版）；original 才是作者上传的原片
            # （实测同一条 720x1280 → 1080x1920）。
            "config": {"existing_action": "skip", "spec": "original"},
        }]}, timeout=40)
        ok, data, err = ltaoo_payload(resp)
        if not ok:
            raise RuntimeError(err)
        tasks = (data or {}).get("tasks") or []
        t0 = tasks[0] if tasks else {}
        if t0.get("code") not in (0, None):
            raise RuntimeError(str(t0.get("msg") or "创建任务失败"))
        tid = (t0.get("data") or {}).get("id") or ((data or {}).get("ids") or [None])[0]
        if not tid:
            raise RuntimeError("ltaoo 没返回任务 id：" + json.dumps(data, ensure_ascii=False)[:300])
        return int(tid)
    except Stopped:
        raise
    except Exception as e:
        emit("item_failed", id=it["id"], error=str(e)[:400], retryable=True)
        return None


def cmd_update_tools(ctx: Ctx, _payload):
    pip = os.path.join(os.path.dirname(ctx.cfg["ytdlp"]), "pip")
    rc, out, err = run_capture(ctx, [pip, "install", "-q", "-U", "yt-dlp[default]"], timeout=600)
    rc2, ver, _ = run_capture(ctx, [ctx.cfg["ytdlp"], "--version"], timeout=30)
    if rc != 0:
        emit("log", msg="pip 失败：" + last_error_line(err))
        return "failed"
    emit("log", msg="yt-dlp 已是 " + ver.strip())
    return "ok"


def _running_pid():
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return pid


def cmd_stop(ctx: Ctx, _payload):
    pid = _running_pid()
    if not pid:
        emit("log", msg="没有正在运行的任务")
        return "ok"
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        time.sleep(0.25)
        if not _running_pid():
            emit("log", msg=f"已停止（pid {pid}）")
            return "ok"
    os.kill(pid, signal.SIGKILL)
    emit("log", msg=f"强制结束（pid {pid}）")
    return "ok"


COMMANDS = {
    "ping": cmd_ping,
    "search_wx": cmd_search_wx,
    "scan": cmd_scan,
    "download": cmd_download,
    "update_tools": cmd_update_tools,
    "stop": cmd_stop,
}
LONG_RUNNING = {"scan", "download", "update_tools"}


def _cleanup(ctx: Ctx):
    with ctx.lock:
        kids = list(ctx.children)
        tids = list(ctx.wx_tasks)
    for p in kids:
        _killpg(p)
    for tid in tids:
        try:
            http_json(ctx, "POST", "/api/v1/download_task/pause", body={"task_ids": [tid]}, timeout=5)
        except Exception:
            pass


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    if cmd not in COMMANDS:
        emit("end", status="failed", error=f"未知命令 {cmd!r}")
        return 2
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        emit("end", status="failed", error=f"stdin 不是 JSON: {e}")
        return 2
    ctx = Ctx(payload.get("config") or {})

    if cmd in LONG_RUNNING:
        other = _running_pid()
        if other and other != os.getpid():
            emit("end", status="failed", error=f"已有任务在跑（pid {other}）")
            return 1
        os.makedirs(AGENT_HOME, exist_ok=True)
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))

        def _on_signal(signum, _frame):
            # SIGTERM = 面板点了「停止」；SIGHUP = ssh 断了（服务器重启 / 断网）。
            # 两种都要把 yt-dlp 进程组和 ltaoo 任务收掉，不能留孤儿在后台继续写盘。
            ctx.stop.set()

        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGHUP, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
        watcher = threading.Thread(target=lambda: (ctx.stop.wait(), _cleanup(ctx)), daemon=True)
        watcher.start()

        # 不带 tty 的 ssh 会话断开时，远端进程**不一定**收到 SIGHUP（sshd 只是关掉管道），
        # 而 agent 在等 yt-dlp 时可能很久都不写 stdout、发现不了断管。
        # 所以再加一道：父进程（sshd 会话）没了 → 被 init 收养 → ppid 变了 → 停。
        ppid0 = os.getppid()

        def _orphan_watch():
            while not ctx.stop.wait(2):
                if os.getppid() != ppid0:
                    ctx.stop.set()

        threading.Thread(target=_orphan_watch, daemon=True).start()

    status, error = "ok", None
    try:
        status = COMMANDS[cmd](ctx, payload) or "ok"
    except Stopped:
        status = "stopped"
    except BrokenPipeError:
        ctx.stop.set()
        _cleanup(ctx)
        return 1
    except Exception as e:  # noqa: BLE001 —— 任何异常都要变成 end 事件，服务器才有话可说
        status, error = "failed", f"{type(e).__name__}: {e}"[:500]
    finally:
        if cmd in LONG_RUNNING:
            if ctx.stop.is_set():
                _cleanup(ctx)
            try:
                if _running_pid() == os.getpid():
                    os.unlink(PIDFILE)
            except OSError:
                pass
    try:
        emit("end", status=status, error=error, at=datetime.now(timezone.utc).isoformat())
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
