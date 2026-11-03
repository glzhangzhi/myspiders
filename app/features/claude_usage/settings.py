"""Claude 用量的配置项（环境变量名与字段名相同，大小写不敏感）。

由 app.core.settings 自动合并进全局 `settings`，读取方式不变：`settings.<字段>`。
"""

from pydantic import BaseModel


class ClaudeUsageSettings(BaseModel):
    # Claude 用量：凭证源路径（见 services/claude_usage.py 的凭证源说明）
    # dsh 的 subscriptions 插件在同机持续轮换 token，是首选源；~/.claude 为回退源。
    # 两者都只读，本服务永不刷新、不写回，故不与任何刷新方争抢 refresh token。
    #
    # 迁到 WSL2 后：dsh 跑在 Windows 侧，凭证文件在 Windows 用户目录下，用 .env 覆盖成
    # 挂载路径即可（WSL 直接读 /mnt/c，同机文件、无需 SSH，凭证读取逻辑一行不用改）:
    #   claude_dsh_auth_file=/mnt/c/Users/<你>/.dsh/plugins/subscriptions/auth.json
    # 注意只有这个几百字节的小文件走 /mnt/c；项目本体必须放在 WSL 内部（9p 对
    # downloads/av_images 的三万多个小文件慢到不可用）。
    claude_dsh_auth_file: str = "~/.dsh/plugins/subscriptions/auth.json"
    claude_cli_credentials_file: str = "~/.claude/.credentials.json"
