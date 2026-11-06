# 酒馆资料（tavern）—— 约定与踩坑

> 从 CLAUDE.md 拆出（2026-11）。改 `app/features/tavern/` 前先读。

- **酒馆资料(tavern)分两半:读酒馆 = 只读扫文件,写酒馆 = 一律走 tavern_sync**。`features/tavern/catalog.py` 只扫 `/root/sillytavern/data/<user>/` 下的 `characters/`、`worlds/`、`OpenAI Settings/`,从不写;增删改一律由 `features/tavern/sync.py` 改 `tavern_sync.yaml` + 调 `/root/local_lorebook/ts.mjs`。酒馆运行时持有这些文件,越过它写等于和它抢。
- **角色卡的「名称」是 PNG 内嵌的 `name`,不是文件名**:`tavern_sync.yaml` 的 `酒馆中的名称` 认前者。实测 376 张卡里 8 张不一致(`default_Seraphina.png` → `Seraphina`)。故必须解 PNG 的 tEXt 块(`ccv3` 优先于 `chara`)。世界书/预设相反 —— JSON 里不存名字,就按去掉 `.json` 的文件名认。
- **聊天记录的目录名认 PNG 文件名,与上一条相反**:`features/tavern/chats.py`(只读,导出 md/txt/jsonl)。酒馆源码 `src/endpoints/chats.js` 一律用 `chats/<avatar_url 去掉 .png>/`,所以那 8 张名字不一致的卡按卡名找目录会得到「没有聊天记录」——入口收卡名,内部先经 catalog 换成文件名。正文取 `mes`(它恒等于当前选中的 swipe),不倒 `swipes` 里未采用的重roll 分支;jsonl 导出走 `read_bytes()` 按字节搬运,`errors="replace"` 会把非法字节静默换成 U+FFFD,而 jsonl 的用途正是导回酒馆。聊天记录不归 tavern_sync 管,不抢那把同步锁。
- **改 tavern_sync.yaml 与跑 pull/push 必须共用一把锁**:`ts.mjs` 执行时会把展开后的 YAML **临时写回原文件**、结束再还原,期间的任何写入都会被那次还原抹掉。且 tavern_sync 固定占 6620 端口,并发第二个进程必然 EADDRINUSE,故用**非阻塞抢锁**直接回报「已有任务在跑」。
- **杀同步进程必须整组杀**:`ts.mjs` 只是包装器,占着 6620 的是它 spawn 的 `tavern_sync.mjs` —— 只杀父进程会留下孤儿霸占端口,之后每次同步都失败。用 `start_new_session=True` + `os.killpg`。同时**服务端要自己备份 YAML 并在超时后还原**:被强杀时 ts.mjs 的还原钩子不会执行,用户的分组简写会永久停在展开态。
- **「酒馆网页没连上来」不一定是网页没开**:6620 的客户端是浏览器里的「实时编写」脚本,它连的是脚本设置里的 url。酒馆迁到 free-arm 后必须填酒馆的对外地址 `https://st.<你的域名>`(经 cloudflared 转发;实际地址见 `docs/private/deploy.md`),留着默认的 `http://localhost:6620` 就会连到机主自己的电脑上,永远连不上。
- **「酒馆网页没开」要在 15 秒内断定**:这是同步失败里最常见的一种,等满 180 秒总超时对人太难受。判据是输出里有没有「成功连接到酒馆网页」;**读输出必须另起线程**,脚本等连接时一个字节都不输出,主线程卡在 `readline` 上永远走不到超时判断。
