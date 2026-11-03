"""菜谱 + Kindle 推送的配置项（环境变量名与字段名相同，大小写不敏感）。

由 app.core.settings 自动合并进全局 `settings`，读取方式不变：`settings.<字段>`。
"""

from pydantic import BaseModel


class RecipeSettings(BaseModel):
    # ── Kindle 菜谱推送（KOReader 自带 dropbear）──
    # 认证只走公钥：服务进程绝不能弹密码提示（会卡死在 stdin 上）。
    # 公钥要放进 Kindle 的 /mnt/us/koreader/settings/SSH/authorized_keys
    # （KOReader 的 dropbear 读的是这个相对路径，不是 ~/.ssh/）。
    kindle_host: str = ""                  # 局域网 IP，如 192.168.1.50
    kindle_user: str = "root"
    kindle_ssh_port: int = 2222
    kindle_ssh_key: str = "~/.ssh/id_ed25519"
    kindle_ssh_timeout: int = 10           # 连接超时秒数；Kindle 休眠时会直接失败而不是干等
    # 跳板（2026-10-31 服务搬到 Oracle 后）：Kindle 只在家里局域网，服务器够不着，
    # 经 Pi（同一局域网、已在 tailnet）ProxyJump 过去。值是 ssh 能解析的目标，
    # 如 "pi@home-gateway"（~/.ssh/config 别名）。空 = 直连（服务在家里时）。
    kindle_ssh_jump: str = ""
    # 推送目标目录。**单独建一个子目录**，不要直接丢进 documents 根 ——
    # 那里是书库，混进一堆菜单文件会把书淹掉。push_text 自带 mkdir -p，
    # 目录不存在会自动建。
    kindle_recipe_dir: str = "/mnt/us/documents/菜谱"

    # ── 菜谱库 ──
    # 「明天吃什么」按这个时区解释日期 —— 排餐是日历概念，跟着人走。
    recipe_timezone: str = "Europe/Berlin"
    # HowToCook：面向家庭厨房的菜谱库，370 道、格式统一、带难度与热量。
    # 这是**推荐的默认源**。
    howtocook_repo_url: str = "https://github.com/Anduin2017/HowToCook.git"
    howtocook_local_dir: str = "downloads/HowToCook"
    # CookLikeHOC（老乡鸡）：商用后厨口径，实测 83% 不适合家用。保留入口
    # 供偶尔单捞，别再全量导入。
    cooklikehoc_repo_url: str = "https://github.com/Gar-b-age/CookLikeHOC.git"
    cooklikehoc_local_dir: str = "downloads/CookLikeHOC"
