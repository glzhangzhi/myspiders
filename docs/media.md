# 媒体下载（视频号作者 + YouTube 列表）

面板「收藏 → 📥 下载」。手动添加视频号作者 / YouTube 播放列表 → 检测到新条目进「待下载」→
电脑开着时点「开始下载」→ 文件落到 3060ti 的 D 盘。

## 架构

```
面板 ─> free-arm myspider ─ssh wsl-3060ti─> /root/media-agent/agent.py（WSL，纯标准库）
                                           ├─ http://127.0.0.1:2022  ltaoo/wx_channels_download（Windows，视频号）
                                           └─ yt-dlp(venv) + ffmpeg + deno → /mnt/d/Downloads/YouTube
```

| 组件 | 在哪 | 说明 |
|---|---|---|
| `app/features/media/service.py` | free-arm | 编排：检测 / 下载 / 停止，全局非阻塞锁 |
| `app/features/media/agent_client.py` | free-arm | ssh 传输；每次长任务前把 agent **推过去覆盖** |
| `app/features/media/rules.py` | free-arm | 纯规则：来源输入识别、新条目分档 |
| `app/features/media/repo.py` | free-arm | `media_sources` / `media_items` / `media_runs` |
| `deploy/win-media/media_agent.py` | 仓库 → 3060ti | 唯一一份。那边的 `/root/media-agent/agent.py` 只是缓存 |
| `deploy/win-media/setup_media_agent.sh` | 3060ti | 一次性环境准备（幂等） |

**为什么检测只在电脑开着时发生**：视频号没有对外接口能列出某个作者的作品。
wechat-mp-tools 和它的上游 ltaoo 都是让电脑版微信打开视频号页面，再借那个页面的登录态调
`finderUserPage`。服务器自己做不到，所以 free-arm 每 30 分钟 ssh 过去问一次，
**连不上就静默跳过**（关机是常态，不是故障，不推送、不报红）。

**为什么用 ltaoo 而不是 x554960766/wechat-mp-tools**：后者是前者的移植，但把程序化调用的
WebSocket 客户端注释掉了（`utils.js` 末尾 `// ws_client$.methods.connect_local_ws();`），
只能在微信页面里手动点「同步作者」。ltaoo 有完整的本地 HTTP API（`/api/channels/*`、
`/api/v1/download_task/*`，OpenAPI 在其仓库 `docs/public/openapi/`），解密也由它做。

**为什么 YouTube 用 WSL 里的 yt-dlp**：Windows 侧那个 `yt-dlp.exe`（YtGui 用的）旁边没有
ffmpeg，最高只能拿到 ~720p 的合流格式。WSL 里 apt 装 ffmpeg 一行完事，路径编码也可控。
下载时碎片写在 ext4 的 `/root/media-agent/tmp/yt`，合并完才搬到 `/mnt/d`（9p 上大量小写入很慢）。

## 一次性准备

1. 环境（我已跑过；重跑无副作用）：
   `ssh wsl-3060ti 'bash -s' < deploy/win-media/setup_media_agent.sh`
2. **机主手动**：在 3060ti 上双击
   `%LOCALAPPDATA%\wx_video_download\wx_video_download.exe`
   （脚本已校验 sha256 后放好 safe 版，版本见脚本里的 `VER`）。首次会装根证书、设系统代理（弹 UAC）。
   建议把它的快捷方式放进 `shell:startup` 开机自启。
3. **机主手动**：YouTube 私人列表要 cookies。当前的
   `C:\Users\Administrator\Downloads\cookies.txt` **没有登录 cookie**（缺 `SID`/`LOGIN_INFO`，
   2026-11-01 实测连「喜欢的视频」都读不到）。用浏览器**无痕窗口**登录 YouTube →
   扩展「Get cookies.txt LOCALLY」导出 → 覆盖该文件 → 关掉无痕窗口
   （不关的话浏览器会轮换 cookie，导出的那份很快失效）。面板顶栏会显示「cookies 未登录」直到修好。
4. 手机订阅 ntfy topic `<前缀>media`（只有自动检测发现新东西、下载批次结束会推，都是 low）。

## 每次开机

- YouTube：什么都不用做。
- 视频号：确认 wx_video_download 在跑，并**在电脑微信里打开视频号**（任意页面）。
  面板顶栏「视频号页面已连接」变绿才能检测/下载视频号；没连上时 YouTube 部分照常进行。

## 规则

- **来源模式**：YouTube 列表默认「全部」（列表本身就是下载清单）；视频号作者默认「仅新作」——
  首次检测只看第一页并把已有作品记为「已跳过」，之后出现的才进待下载。来源上的
  「补下历史」可把跳过的全部加回队列；「切换模式」改成「全部」后下次检测会翻最多 50 页。
- **翻页判据不能是「遇到已知就停」**：作者置顶的老视频排在第一页最前面，碰到它就停会让新作品
  永远检测不到。判据是「本页里已知的 ≤3 个才继续翻」（`should_continue_paging`）。
- **昵称只在完全一致且唯一时自动绑定**：搜「龙虾」会回来一串相似昵称，模糊匹配会静默盯错人。
  否则来源进 `needs_pick`，面板里点选。
- **检测不改已有条目的状态**（`$setOnInsert`）；**删来源不删已完成记录**（那是「下过了」的凭据）。
- **队列「清空」= 移到已跳过，不删**：删掉的条目下次检测会被当成新条目重新插入成待下载，清空等于没清。
  有筛选词时只清筛选出来的。「最近运行」的清空是真删，但保留进行中的那条（进度与停止靠它）。
- **视频号下载前先重新取 feed/profile**：检测时拿到的 CDN 链接带 token，会过期。
- YouTube 另有 `/mnt/d/Downloads/YouTube/.archive.txt`（yt-dlp 的下载存档）兜底防重复。
  面板上「重下」一个已完成的 YouTube 条目时，要先删掉 archive 里那一行才会真的重下。
- 画质：分辨率优先，同分辨率偏好 H.264+AAC（Windows 自带播放器对 AV1/Opus 支持参差）；
  4K 只有 VP9/AV1，照样拿最高。

## YouTube 登录凭据（2026-11-02）

Chrome 导出的 cookies.txt **约半小时就失效**（06:57 导出 → 07:15 还能下年龄限制视频 → 07:35
"cookies are no longer valid"），Chrome 新版频繁轮换 Google 登录 cookie，导出的只是一张快照。
agent 现在**优先直接读 Firefox 的 cookie 库**（`--cookies-from-browser firefox:<profile>`，
从 `/mnt/c/Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*/cookies.sqlite` 自动找登录过 YouTube 的那个）；
Firefox 的库不加密，WSL 里能直接读（Chrome 是 App-Bound 加密，读不了）。找不到才退回 cookies.txt，
且**每次从原件重拷**——持久保留副本会被失效时 yt-dlp 写回的「删掉 SID」永久弄残。
面板的登录状态看「最近一次真实请求的结果」（`yt_cookie_state.json`），不再只看文件里有没有登录字段。

## 停止 / 断线

- 「停止」= 远端 `agent.py stop`（SIGTERM → 杀 yt-dlp **进程组** + 暂停进行中的 ltaoo 任务）
  + 本地断 ssh。没下完的条目退回「待下载」。
- ssh 断了（电脑睡眠、服务器重启）：不带 tty 的 ssh 会话断开时远端**不一定**收到 SIGHUP，
  所以 agent 另有两道：stdout 断管即停、父进程（sshd 会话）消失即停。2026-11-01 实测
  断 ssh 后远端无残留进程。
- myspider 重启：`recover_on_startup()` 把 downloading 打回 pending、running 的运行记 interrupted。

## 排查

| 现象 | 原因 / 处理 |
|---|---|
| 顶栏「离线」 | 3060ti 关机/睡眠，或 WSL 没被 `\DSH-WSL-Boot` 钉住。`ssh wsl-3060ti true` 试试 |
| 「下载器未运行」 | wx_video_download.exe 没开 |
| 「视频号页面未连接」 | 电脑微信里打开视频号；还不行就重启 wx_video_download 并确认系统代理是它的 2023 端口 |
| YouTube「The playlist does not exist」 | 私人列表 + cookies 无效（见准备第 3 步），或 list id 抄错 |
| YouTube「Sign in to confirm you're not a bot」 | 同上，换新 cookies；或点「更新 yt-dlp」 |
| 视频号窗口一直载入不出来 / 拉关注报 `WXU.API4.finderGetFollowList is not a function` | 微信更新后 ltaoo 注入脚本失配（2026-11-06 那次由 v261106 修复）。去 [ltaoo releases](https://github.com/ltaoo/wx_channels_download/releases) 换新 exe：把正在跑的 exe 改名备份、放新的、重启它并重开视频号窗口；同步改 `setup_media_agent.sh` 的 `VER` |
| 启动报 `bind: An attempt was made to access a socket in a way forbidden` | 2022/2023 落进 winnat 的保留段（`netsh int ipv4 show excludedportrange protocol=tcp`）。根因是 TCP 动态端口范围被改到 1024 起，管理员下改回 `start=49152 num=16384` 并重启 winnat |
| 视频号任务长时间没进展 | ltaoo 任务 30 分钟无进展会被判失败，面板「全部重试」即可 |

## 真机校准（2026-11-01 已做）

用 ltaoo v260907 + 机主的微信实测（2026-11-09 已升到 v261106，API 无破坏性变化），样本存为 `tests/fixtures/media/real_*.json`：

- **微信搜索（`contact/search`）不可用**：一直回 `-70003 JSAPI_JSONPARSE_FAILED`
  （`H5ExtTransfer:fail` / `resp mmpkg invalid`），是微信那边的问题、换关键词也一样。
  所以 agent 在搜索失败时**退回到「在我关注的作者里按昵称找」**（`follow/list`，每页 20，
  实测 215 个约 15 秒）。面板另有「从关注里选」按钮直接列出全部关注。
  推论：**要下载的作者得先在微信里关注**。
- `feed/list`：`data.data.object` + `lastBuffer` + **`continueFlag`**（以它判断还有没有下一页）。
- 下载任务的 `files[].kind` 是 MIME（`video/mp4`），不是文档示例里的 `video`；
  下载中 `progress` 口径不稳，进度按 `downloaded/size` 自己算。
- **必须传 `config.spec = "original"`**：默认下的是 `xWT111`（720p 转码版），
  original 才是原片（实测同一条 720×1280 → 1080×1920）。
- 不传 `filename` 时文件名是「feed id_规格.mp4」；现在传 `YYYYMMDD_描述前 50 字`（去掉 #话题），
  ltaoo 会在后面补 `_<规格>`，original 时规格为空，所以文件名以 `_` 结尾，属正常。
