"""Claude Code 账户用量服务 — 自适应拉取、历史记录、阈值预警、消耗聚合。

数据源：GET https://api.anthropic.com/api/oauth/usage （即 /usage 命令的后端）。
鉴权：  只读同机凭证源的 OAuth access token（不刷新、不写回）。按序尝试：
          1. dsh subscriptions 插件的 auth.json —— 它自身运行就需要保持 token 新鲜
             （过期前 5 分钟预抢刷新 + 5 分钟后台巡检），本服务搭它的便车；
          2. ~/.claude/.credentials.json —— Claude Code CLI 的凭证，装了 CLI 才会被轮换。
        取第一个"存在且未过期"的源；都不可用则沿用上次快照并标记 stale。

核心机制：
  - 自适应轮询：按三个窗口的最大利用率决定下次间隔——闲时 30min，逼近上限时 5min，
    既把请求数压到最低，又能及时抓到 50% / 90% 阈值。调度器每 5min 调一次 tick()，
    由本服务决定本次是否真正请求。
  - 历史记录：每次成功拉取追加一条快照到 Mongo（claude_usage_history）。
  - 阈值预警：每个窗口首次过 50% / 90% 各推一次 ntfy（每窗口去重，重置后清零）。
  - 重置小结：某窗口 resets_at 变化（新周期开始）时，推送上一周期峰值用量。
  - 消耗聚合：utilization 是会重置的滚动窗口百分比，"消耗"用同窗口相邻样本差值还原，
    再按 小时 / 周几 / 日期 / 热力图 聚合。

「只读凭证」策略：本服务永不执行 refresh grant。refresh token 是一次性的（用掉即
轮换，旧的立即作废），两个独立的刷新方各存各的副本必然互相把对方的凭证变成废纸，
且这种损坏是静默且永久的。让唯一的刷新方（dsh / Claude Code）独占轮换权，本服务
只读它的成果，是唯一不会产生轮换竞争的做法 —— 代价仅是"本机必须有人维护活 token"，
而这恰是那些进程本就在做的事，不因被读取而增加任何请求。

凭证全部不可用时不强行请求，沿用上次快照并标记 stale；刷新方恢复后自动痊愈。
本服务对登录态零副作用。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.core.paths import DATA_DIR
from app.core.logging import get_logger
from app.core.settings import settings
from app.features.claude_usage.repo import claude_usage_repo
from app.core import push as push_service

logger = get_logger("CLAUDE_USAGE")

USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"

# access token 至少还需这么多寿命才敢用：低于此值视同已过期，直接换下一个源。
# 覆盖一次请求的往返 + 时钟漂移，避免拿着"发出时还有效、到达时已过期"的 token。
TOKEN_MIN_TTL_MS = 60 * 1000
SNAPSHOT_FILE = DATA_DIR / "claude_usage.json"

NTFY_TOPIC = "claude"

# 上一周期峰值低于此值则不推重置小结（避免空闲 5h 窗口反复刷屏）
RESET_SUMMARY_MIN_PEAK = 5.0

# 被监控的三个窗口：(payload 字段, 显示名)
WINDOWS = [
    ("five_hour", "5 小时窗口"),
    ("seven_day", "7 天窗口"),
    ("seven_day_sonnet", "7 天 Sonnet"),
]

# 前端 window 参数 → payload 字段
WINDOW_KEYS = {
    "5h": "five_hour",
    "7d": "seven_day",
    "7d_sonnet": "seven_day_sonnet",
}

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _desired_interval_sec(max_util: float) -> int:
    """按当前最大利用率决定下次轮询间隔（自适应）。"""
    if max_util >= 80:
        return 5 * 60
    if max_util >= 40:
        return 10 * 60
    return 30 * 60


def _same_window(a, b) -> bool:
    """两个 resets_at 是否属于同一窗口(未重置)。

    API 返回的 resets_at 会随请求时刻在秒/亚秒级漂移,且重置时刻常落在整分钟边界
    (如 16:30:00),截断到分钟比较会在 16:29↔16:30 间抖动而误判。但真实重置是 5h/7d
    级的大跳变。故以"跳变幅度是否超过阈值"判断:差 ≤10 分钟视为同窗口(仅漂移),
    超过才算真正重置。
    """
    if a is None or b is None:
        return a is b  # 仅当两者都为 None 才算同窗口
    if a == b:
        return True
    try:
        da = datetime.fromisoformat(a)
        db = datetime.fromisoformat(b)
        return abs((da - db).total_seconds()) <= 600
    except Exception:
        return a == b


class ClaudeUsageManager:
    def __init__(self) -> None:
        self._snapshot: dict | None = None
        self._last_fetch_ts: float = 0.0
        self._last_max_util: float = 0.0
        # 用量面板是给(身处德国的)使用者看的，按德国时间聚合/展示，
        # 与调度器、通知服务一致(均用 Europe/Berlin)。default_timezone(上海)
        # 只服务于中文网站爬虫类数据，不适用于此。
        self._tz = ZoneInfo("Europe/Berlin")
        self._load_snapshot()

    # ── 最新快照持久化（给顶部面板用）──

    def _load_snapshot(self) -> None:
        try:
            if SNAPSHOT_FILE.exists():
                self._snapshot = json.loads(SNAPSHOT_FILE.read_text("utf-8"))
        except Exception:
            self._snapshot = None

    def _save_snapshot(self) -> None:
        try:
            SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SNAPSHOT_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._snapshot, ensure_ascii=False), encoding="utf-8")
            tmp.replace(SNAPSHOT_FILE)
        except Exception:
            logger.exception("保存用量快照失败")

    # ── 凭证 ──

    @staticmethod
    def _expand(path: str) -> Path:
        return Path(path).expanduser()

    def _token_from_dsh(self) -> tuple[str | None, int]:
        """dsh subscriptions 插件的 auth.json —— 首选源（它自己就在保鲜 token）。

        结构：{"claude": {"default": <key>, "accounts": {<key>: {...}}}}。
        认准 default 指向的账号：dsh 的直连路由用的就是它，多账号时跟随同一个，
        用量面板与实际发请求的账号才不会对不上。
        """
        try:
            data = json.loads(self._expand(settings.claude_dsh_auth_file).read_text("utf-8"))
            entry = data.get("claude") or {}
            accounts = entry.get("accounts") or {}
            session = accounts.get(entry.get("default")) or next(iter(accounts.values()), None)
            if not session:
                return None, 0
            return session.get("accessToken"), int(session.get("expiresAt") or 0)
        except Exception:
            return None, 0

    def _token_from_claude_cli(self) -> tuple[str | None, int]:
        """Claude Code CLI 的 ~/.claude/.credentials.json —— 回退源。

        只有本机装了并在用 claude CLI 时，它才会被守护进程轮换；否则一次过期即长期
        过期（此时 _read_token 会继续往下退，最终 no_credentials）。
        """
        try:
            data = json.loads(self._expand(settings.claude_cli_credentials_file).read_text("utf-8"))
            oauth = data.get("claudeAiOauth") or data
            return oauth.get("accessToken"), int(oauth.get("expiresAt") or 0)
        except Exception:
            return None, 0

    def _read_token(self) -> tuple[str | None, int]:
        """按优先级取第一个仍然新鲜的 access token；都不可用返回 (None, 0)。"""
        now_ms = int(time.time() * 1000)
        for source in (self._token_from_dsh, self._token_from_claude_cli):
            token, expires_at = source()
            if token and expires_at - now_ms > TOKEN_MIN_TTL_MS:
                return token, expires_at
        return None, 0

    async def _request_usage(self) -> tuple[dict | None, str | None]:
        """返回 (payload, error)。只读凭证：无新鲜 token 则不请求。"""
        token, _expires_at = self._read_token()
        if not token:
            # _read_token 已排除过期 token，故此处统一为"没有可用凭证"：
            # 要么两个源都不存在，要么都已过期（刷新方停摆）。
            return None, "no_credentials"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    USAGE_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "anthropic-beta": "oauth-2025-04-20",
                        "Content-Type": "application/json",
                        "User-Agent": "myspiders-usage/1.0",
                    },
                )
        except Exception as exc:
            logger.warning("拉取用量请求失败: %s", exc)
            return None, "request_failed"

        if resp.status_code == 200:
            try:
                return resp.json(), None
            except Exception:
                return None, "bad_response"
        if resp.status_code in (401, 403):
            return None, "unauthorized"
        return None, f"http_{resp.status_code}"

    # ── 轮询入口 ──

    async def tick(self) -> None:
        """自适应调度入口：调度器每 5min 调用，到点才真正 poll。"""
        now = time.time()
        due = self._last_fetch_ts == 0 or (
            now - self._last_fetch_ts >= _desired_interval_sec(self._last_max_util)
        )
        if due:
            await self.poll()

    async def poll(self) -> dict:
        """完整一次：拉取 → 更新快照 → 记录历史 → 阈值/重置告警。"""
        payload, error = await self._request_usage()
        self._last_fetch_ts = time.time()

        if error:
            return self._mark_error(error)

        # resets_at 保留原值(秒/亚秒级会随请求漂移);"是否重置"统一用 _same_window 按
        # 跳变幅度判断,见 _process_alerts / _compute_deltas。
        self._snapshot = {
            "ok": True,
            "fetched_at": time.time(),
            "data": payload,
            "last_error": None,
            "last_error_at": None,
        }
        self._save_snapshot()
        self._last_max_util = self._max_util(payload)
        logger.info("账户用量已更新（max_util=%.0f%%）", self._last_max_util)

        # Mongo / 推送出错都不应中断轮询本身
        try:
            self._record_history(payload)
        except Exception:
            logger.exception("记录用量历史失败")
        try:
            self._process_alerts(payload)
        except Exception:
            logger.exception("处理用量告警失败")

        return self.get()

    def _mark_error(self, error: str) -> dict:
        if self._snapshot is None:
            self._snapshot = {"ok": False, "fetched_at": None, "data": None}
        self._snapshot["last_error"] = error
        self._snapshot["last_error_at"] = time.time()
        self._save_snapshot()
        logger.info("用量拉取未成功（%s），沿用上次快照", error)
        return self.get()

    def get(self) -> dict:
        if self._snapshot is None:
            return {"ok": False, "fetched_at": None, "data": None, "last_error": "no_data", "last_error_at": None}
        return self._snapshot

    # ── 历史记录 ──

    @staticmethod
    def _max_util(payload: dict) -> float:
        vals = []
        for key, _ in WINDOWS:
            w = payload.get(key) or {}
            u = w.get("utilization")
            if isinstance(u, (int, float)):
                vals.append(float(u))
        return max(vals) if vals else 0.0

    def _record_history(self, payload: dict) -> None:
        doc: dict = {"ts": time.time()}
        for key, _ in WINDOWS:
            w = payload.get(key) or {}
            doc[key] = {"utilization": w.get("utilization"), "resets_at": w.get("resets_at")}
        claude_usage_repo.insert_sample(doc)

    # ── 阈值预警 + 重置小结 ──

    def _process_alerts(self, payload: dict) -> None:
        state = claude_usage_repo.get_state()
        changed = False

        for key, label in WINDOWS:
            w = payload.get(key) or {}
            util = w.get("utilization")
            resets_at = w.get("resets_at")
            if not isinstance(util, (int, float)):
                continue
            util = float(util)

            ws = state.get(key)
            if ws is None:
                # 首次见到该窗口：建立基线，不补发历史告警（避免部署即刷屏）
                state[key] = {
                    "resets_at": resets_at,
                    "peak": util,
                    "notified_50": util >= 50,
                    "notified_90": util >= 90,
                }
                changed = True
                continue

            if not _same_window(ws.get("resets_at"), resets_at):
                # 窗口已重置 → 上周期有实质用量才推小结（避免空窗刷屏），再以当前值建立新基线
                if float(ws.get("peak", 0.0)) >= RESET_SUMMARY_MIN_PEAK:
                    self._push_reset_summary(label, ws.get("peak", 0.0))
                state[key] = {
                    "resets_at": resets_at,
                    "peak": util,
                    "notified_50": util >= 50,
                    "notified_90": util >= 90,
                }
                changed = True
                continue

            # 同一窗口内：更新峰值 + 检查阈值（每阈值每窗口只推一次）
            ws["peak"] = max(float(ws.get("peak", 0.0)), util)
            if not ws.get("notified_50") and util >= 50:
                self._push_threshold(label, 50, util, resets_at)
                ws["notified_50"] = True
            if not ws.get("notified_90") and util >= 90:
                self._push_threshold(label, 90, util, resets_at)
                ws["notified_90"] = True
            state[key] = ws
            changed = True

        if changed:
            claude_usage_repo.save_state(state)

    def _push_threshold(self, label: str, threshold: int, util: float, resets_at) -> None:
        when = self._fmt_reset(resets_at)
        if threshold >= 90:
            # high：快断供了，值得锁屏 + 响铃打断
            title = f"⚠️ Claude {label} 已达 {int(util)}%"
            tags, priority = ["rotating_light"], "high"
        else:
            # low：50% 只是预警，飘个悬浮横幅就够。本机 default = 响+震但锁屏
            # 看不到内容（高打扰低信息），对这类信息是纯负担。
            title = f"Claude {label} 已过 {threshold}%"
            tags, priority = ["warning"], "low"
        body = f"当前 {int(util)}%" + (f"，{when} 重置" if when else "")
        try:
            push_service.send_note(title, body, topic=NTFY_TOPIC, tags=tags, priority=priority)
        except Exception:
            logger.exception("发送用量阈值告警失败")

    def _push_reset_summary(self, label: str, peak: float) -> None:
        try:
            push_service.send_note(
                f"Claude {label} 已重置",
                f"上一周期峰值用量 {int(peak)}%",
                topic=NTFY_TOPIC,
                tags=["arrows_counterclockwise"],
                # min：纯存档信息、零行动价值，不该有任何提示动静。
                # 想看时主动打开 ntfy 订阅即可。
                priority="min",
            )
        except Exception:
            logger.exception("发送重置小结失败")

    def _fmt_reset(self, iso) -> str:
        if not iso:
            return ""
        try:
            dt = datetime.fromisoformat(iso).astimezone(self._tz)
            now = datetime.now(self._tz)
            return dt.strftime("%H:%M") if dt.date() == now.date() else dt.strftime("%m-%d %H:%M")
        except Exception:
            return ""

    # ── 消耗聚合（供图表/热力图）──

    def _compute_deltas(self, samples: list[dict], window_key: str) -> list[tuple[datetime, float]]:
        """把滚动窗口利用率序列还原成"消耗"差值列表 [(本地时间, Δ%)]。

        消耗 = 同一窗口（resets_at 相同）内相邻样本的 utilization 差值；
        窗口重置时，新窗口首个样本的 utilization 即为"重置以来的消耗"。
        """
        deltas: list[tuple[datetime, float]] = []
        prev: tuple[float, object] | None = None
        for s in samples:
            w = s.get(window_key) or {}
            util = w.get("utilization")
            resets_at = w.get("resets_at")
            ts = s.get("ts")
            if not isinstance(util, (int, float)) or ts is None:
                prev = None  # 链断开，下一个样本不计差值
                continue
            util = float(util)
            if prev is not None:
                p_util, p_resets = prev
                d = (util - p_util) if _same_window(resets_at, p_resets) else util  # 新窗口从 0 累积
                if d > 0:
                    deltas.append((datetime.fromtimestamp(ts, self._tz), d))
            prev = (util, resets_at)
        return deltas

    def history_aggregate(self, view: str, window: str = "5h", days: int = 30) -> dict:
        """还原消耗并按 小时/周几/日期/热力图 聚合（单位：占窗口额度 %）。"""
        window_key = WINDOW_KEYS.get(window, "five_hour")
        samples = claude_usage_repo.samples_since(time.time() - days * 86400)
        return self._bucket(self._compute_deltas(samples, window_key), view, days)

    def _bucket(self, deltas: list[tuple[datetime, float]], view: str, days: int) -> dict:
        if view == "weekday":
            totals = [0.0] * 7
            for dt, d in deltas:
                totals[dt.weekday()] += d
            return {
                "view": "weekday",
                "unit": "%",
                "buckets": [{"label": WEEKDAY_NAMES[i], "value": round(totals[i], 1)} for i in range(7)],
            }

        if view == "date":
            agg: dict[str, float] = {}
            for dt, d in deltas:
                agg[dt.strftime("%Y-%m-%d")] = agg.get(dt.strftime("%Y-%m-%d"), 0.0) + d
            today = datetime.now(self._tz).date()
            buckets = []
            for i in range(days - 1, -1, -1):
                day = today - timedelta(days=i)
                buckets.append({
                    "label": day.strftime("%m-%d"),
                    "value": round(agg.get(day.strftime("%Y-%m-%d"), 0.0), 1),
                })
            return {"view": "date", "unit": "%", "buckets": buckets}

        if view == "heatmap":
            grid = [[0.0] * 24 for _ in range(7)]
            for dt, d in deltas:
                grid[dt.weekday()][dt.hour] += d
            return {
                "view": "heatmap",
                "unit": "%",
                "weekdays": WEEKDAY_NAMES,
                "grid": [[round(v, 1) for v in row] for row in grid],
            }

        # 默认 hour（一天 24 小时）
        totals = [0.0] * 24
        for dt, d in deltas:
            totals[dt.hour] += d
        return {
            "view": "hour",
            "unit": "%",
            "buckets": [{"label": f"{h:02d}", "value": round(totals[h], 1)} for h in range(24)],
        }


    # ── 窗口结束利用率预测（线性 ↔ 历史 自动混合）──

    def predict(self) -> dict:
        """预测 5h / 7d 窗口在 resets_at 时的利用率。

        预测 = (1-w)·线性外推 + w·历史画像，w = min(1, 观测天数/目标天数)。
        数据为 0 → 纯线性；攒满 → 纯历史；中间平滑过渡。
        """
        data = (self.get() or {}).get("data")
        if not data:
            return {"ok": False, "windows": []}

        now = time.time()
        specs = [
            ("5h", "five_hour", "5 小时", 5 * 3600, "hour", 14),
            ("7d", "seven_day", "7 天", 7 * 86400, "weekday", 28),
        ]
        out = []
        for win, key, label, length, view, target_days in specs:
            w = data.get(key) or {}
            u_now = w.get("utilization")
            resets_at = w.get("resets_at")
            if not isinstance(u_now, (int, float)) or not resets_at:
                continue
            u_now = float(u_now)
            try:
                reset_ts = datetime.fromisoformat(resets_at).timestamp()
            except Exception:
                continue
            elapsed = now - (reset_ts - length)
            remaining = reset_ts - now
            if remaining <= 0 or elapsed <= 600:  # 刚重置(<10min) 线性不稳，跳过
                continue

            linear_end = u_now + (u_now / elapsed) * remaining
            hist_end, data_days = self._predict_historical(key, view, now, reset_ts, u_now)

            if hist_end is None:
                pred_end, method, w_hist = linear_end, "linear", 0.0
            else:
                w_hist = max(0.0, min(1.0, data_days / target_days))
                pred_end = (1 - w_hist) * linear_end + w_hist * hist_end
                method = "historical" if w_hist >= 1 else ("blended" if w_hist > 0 else "linear")

            pred_end = max(u_now, min(pred_end, 200.0))

            eta = None
            if pred_end > 100:
                eff_rate = (pred_end - u_now) / remaining
                if eff_rate > 0:
                    secs = (100 - u_now) / eff_rate
                    if 0 < secs < remaining:
                        eta = now + secs

            out.append({
                "window": win,
                "label": label,
                "current": round(u_now, 1),
                "predicted_end": round(pred_end),
                "method": method,
                "confidence": round(w_hist, 2),
                "data_days": data_days,
                "severity": "high" if pred_end >= 100 else ("warning" if pred_end >= 85 else "normal"),
                "eta_100": eta,
                "resets_at": resets_at,
            })
        return {"ok": True, "windows": out}

    def _predict_historical(self, window_key, view, now_ts, reset_ts, u_now):
        """用历史画像预测窗口结束利用率。返回 (predicted_end | None, 观测天数)。"""
        samples = claude_usage_repo.samples_since(now_ts - 30 * 86400)
        deltas = self._compute_deltas(samples, window_key)
        if not deltas:
            return None, 0
        data_days = len({dt.date() for dt, _ in deltas})

        if view == "hour":
            totals = [0.0] * 24
            for dt, d in deltas:
                totals[dt.hour] += d
            avg = [t / data_days for t in totals]            # 每个钟点的日均消耗
            add = self._sum_profile(avg, now_ts, reset_ts, "hour")
        else:
            totals = [0.0] * 7
            for dt, d in deltas:
                totals[dt.weekday()] += d
            weeks = max(1.0, data_days / 7.0)
            avg = [t / weeks for t in totals]                # 每个周几的周均消耗
            add = self._sum_profile(avg, now_ts, reset_ts, "weekday")
        return u_now + add, data_days

    def _sum_profile(self, avg: list[float], start_ts: float, end_ts: float, mode: str) -> float:
        """把 [start, end] 区间按 钟点/周几 拆分，累加对应画像的期望消耗（含部分时段比例）。"""
        total = 0.0
        t = start_ts
        guard = 0
        while t < end_ts and guard < 1000:
            guard += 1
            dt = datetime.fromtimestamp(t, self._tz)
            if mode == "hour":
                nxt = dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                unit, idx = 3600.0, dt.hour
            else:
                nxt = dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                unit, idx = 86400.0, dt.weekday()
            seg_end = min(end_ts, nxt.timestamp())
            total += avg[idx] * ((seg_end - t) / unit)
            t = seg_end
        return total


# 全局单例
claude_usage_manager = ClaudeUsageManager()
