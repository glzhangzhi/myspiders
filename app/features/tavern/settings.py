"""酒馆资料的配置项（环境变量名与字段名相同，大小写不敏感）。

由 app.core.settings 自动合并进全局 `settings`，读取方式不变：`settings.<字段>`。
"""

from pydantic import BaseModel


class TavernSettings(BaseModel):
    # ── 酒馆资料（SillyTavern + tavern_sync）──
    # 只读扫描 ST 的 data 目录得到「酒馆里有哪些角色卡/世界书/预设」，
    # 写操作一律走 tavern_sync（ts.mjs），本服务不直接碰酒馆的数据文件。
    tavern_data_dir: str = "/root/sillytavern/data"
    tavern_user: str = "default-user"
    # tavern_sync 的工作目录：tavern_sync.yaml 与拉下来的 yaml 都在这里。
    tavern_sync_dir: str = "/root/local_lorebook"
    # 包装脚本。pull/push 都通过它跑，以复用它的 YAML 展开与补丁逻辑。
    tavern_sync_entry: str = "ts.mjs"
    # pull/push 要等酒馆网页连上 6620 端口才动得了；网页没开就是干等，
    # 故给一个明确上限，超时直接回报「请打开酒馆网页」而不是挂住请求。
    tavern_sync_timeout: int = 180

    # 酒馆自身的 HTTP 服务。聊天记录的**写**操作（重命名/删除）必须调它的
    # API 而不是直接动文件：酒馆内存里持有当前会话，直接改文件会被它下一次
    # 自动保存覆盖回去。只读的列表/统计/导出仍然直接扫文件（见 tavern_chats）。
    tavern_http_base: str = "http://127.0.0.1:12313"
    tavern_http_timeout: int = 20
    # 聊天记录异地归档目标。原始 jsonl 原样上传，可直接导回酒馆。
    tavern_chat_remote: str = "google drive:myspiders-backup/sillytavern/chats"
    # 「这份记录可能正开着」的判据：最近多少小时内被写过就拒绝删除/重命名。
    # 判据用**文件 mtime** 而不是卡内嵌的 chat 指针 —— 后者是「下次默认打开
    # 哪一份」的书签，每张聊过的卡都必然有一个（实测 112 份记录里命中 72 份），
    # 拿它当锁会把 7 份该清理的空记录拦掉 5 份。mtime 直接对应「酒馆正在写」。
    # 实测本机记录的新鲜度是断层分布：3 天内 2 份，然后直接跳到 7–30 天前 108 份，
    # 故 72 小时这条线落在天然空隙上，既有余量又零误伤。
    tavern_chat_lock_hours: int = 72
