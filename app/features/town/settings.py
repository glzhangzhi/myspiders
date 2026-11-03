"""AI 小镇的配置项（环境变量名与字段名相同，大小写不敏感）。

由 app.core.settings 自动合并进全局 `settings`，读取方式不变：`settings.<字段>`。
"""

from pydantic import BaseModel


class TownSettings(BaseModel):
    # ── AI 小镇 (town)：酒馆角色卡自己过日子，一步 = 一天 ──────────
    # 模型：deepseek-flash（DeepSeek-V4.1-Flash；旧名 deepseek-chat 已被路由到它）。
    # 思考模式对「按设定演戏」值得开：实测 JSON 模式下照常工作，多花的只是输出 token。
    town_model: str = "deepseek-flash"
    town_thinking: bool = True
    # 价格（元 / 百万 token，**高峰价**——北京时间工作日 9-12、14-18；其余时段半价）。
    # 刻意按高峰价估：花费宁可显示偏高，偏低会让人对「点一下多少钱」失去警觉。
    town_price_hit: float = 0.04
    town_price_miss: float = 2.0
    town_price_out: float = 8.0
    # 场景与日记的目标篇幅（字）。思考链也计入 completion，max_tokens 要留得宽，
    # 否则正文被截断、JSON 不完整，表现成「解析失败」而不是「写短了」。
    town_scene_chars_min: int = 1200
    town_scene_chars_max: int = 2500
    town_diary_chars_min: int = 200
    town_diary_chars_max: int = 500
    town_scene_max_tokens: int = 16000
    town_plan_max_tokens: int = 4000
    town_diary_max_tokens: int = 6000
    # 同时进行的调用数（选地点 / 场景 / 日记三段各自并行）
    town_concurrency: int = 6
    town_timeout: int = 300
    # 日记全部带上（机主定：不压缩）。这只是防跑到几百天后撑爆上下文的保险丝：
    # 超过就只带最近 N 篇，并在提示词里写明「更早的略」—— 截断必须显式。
    town_memory_days_cap: int = 60
