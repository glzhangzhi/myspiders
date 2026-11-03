# 计时 · 时间记录与估算训练

个人自用：手机记录、Web 复盘。原始需求文档由机主提供（2026-11），这里只记**需求文档之外定下来的事**和实现约定。

## 进度

| 步骤 | 内容 | 状态 |
|---|---|---|
| 1 | 手机端界面原型（假数据、不接接口） | 完成（2026-11 机主确认） |
| 2 | 数据模型 + FastAPI 接口 | 完成：`/api/timelog/*` |
| 3 | 手机端接真实数据：IndexedDB + 同步 + ntfy 提醒 | 完成：可以日常使用 |
| 4 | Web 端：时间线、补记、统计 | — |

## 已定的决策（2026-11 机主确认）

- **Web 端不计时**，只查看和编辑。手机是唯一的计时端，所以「同一时刻只有一个进行中」由手机自己保证，不用跨端裁决。
- **一键切换默认「暂停」**当前任务（设置里可改成「结束」）。因此手机端要有「已暂停」列表：在卡片下方，点一条就恢复它，当前在跑的会同时自动暂停。
- **记录允许时间重叠**。主任务一直挂着的时候补记一段「打游戏」，两条都保留，时间线分层画，不去切断原记录。
- **提醒走 ntfy，由服务端判断**（第 3 步做）。HyperOS 会杀 PWA 后台，手机自己没法定时提醒。

## 评审时给出、机主没反对的默认做法

- **先猜后看**（2026-11 试用后改）：点按钮**直接开始计时，不弹窗**。计时卡片里出现一排时长「估一下 10 分 / 15 分 / …」，点一个就记为预估、这排随即消失；不点就是不估。**不揭晓历史**：初版是弹窗猜完马上显示「通常 40 分–1 小时」，机主试用后说「很奇怪」——计时已经开始了，再插一个问答环节是打断。按钮上仍不显示参考时长（避免先看答案）。预估和实际的对比只在结束时出现。
- **参考时长分档**：n=0 显示按钮的初始预估，或者「还没有记录」；n=1 显示「上次 X」；n=2–3 显示最短到最长；n≥4 显示四分位区间「通常 X–Y」。10 分钟以上的取整到 5 分钟。
- **快捷按钮默认手动排序**（长按 → 往前/往后）。自动按频率排序会让按钮换位置，破坏肌肉记忆。
- **只有用户自己填的数才算「预估」**（`estimateSource: 'guess'`）。按钮的初始预估、关闭先猜后看时显示的参考时长，都不计入偏差统计。
- **偏差统计**用「实际 ÷ 预估」倍数的中位数，不平均百分比：-50% 和 +100% 的误差程度相当，平均下来却是 +25%。判断「是否变准」看误差绝对值的趋势。
- **文案中性**：「实际比预估多 12 分（40%），预估偏乐观」。偏乐观 = 估少了，偏悲观 = 估多了。全页面不用 `--red` / `--green`（这两个是全站的错误/成功状态色）。
- **取消**：点了先挂起 5 秒，可以撤销；之后才真正写入操作。挂起期间做任何其他操作、或者页面被切到后台，都会立刻落定，不能让已作废的记录因为关了页面又活过来。
- **「今天不再提醒」**做成 ntfy 通知上的按钮（见下文「提醒」）。
- **时间存 UTC**，按 Europe/Berlin 划分每一天（处理跨午夜和夏令时）。
- **删除类型**改成归档（`archived`），历史记录保留原类型。类型只在网页上改，不走操作日志（不需要离线）。
- **桌面快捷方式**只放三个固定项（立即开始 / 暂停恢复 / 结束）。manifest 的 shortcuts 在安装时写死，WebAPK 按天级周期才更新，「最常用的几个」放进去也不会跟着变。**必须用 Chrome 安装**：HyperOS 自带浏览器不支持 shortcuts。

## 数据模型与同步（第 2 步，已实现）

代码：规则在 `app/features/timelog/replay.py`（纯函数，模块 docstring 是规则的权威说明）；
仓储 `app/features/timelog/repo.py`；业务 `app/features/timelog/service.py`；路由 `app/features/timelog/router.py`。

### 只存操作，记录是重放出来的

```
{ id: uuid, ts: 毫秒整数, kind, rid: 记录或按钮的 uuid, data? }
```

| 来源 | kind |
|---|---|
| 手机 | `start` `pause` `resume` `end` `cancel` `edit` |
| 网页 | `create`（补记、拆分出的后半） `set_segments` `delete` `edit` |
| 按钮（手机长按编辑要能离线） | `button.put` `button.delete` |

- **`timelog_ops` 是唯一事实来源**，`_id` = 操作 id。重复上传撞唯一索引就算「重复」，结果与第一次完全一样。
- `timelog_records` / `timelog_buttons` 是**物化视图**：每批操作入库后，重放受影响的记录全部操作再覆盖。改了重放规则后跑 `get_timelog_repo().rebuild_all()` 整体重建。
- 重放按 `(ts, seq, id)` 排序。`seq` 是服务端收到的先后，只用于同一 ts 的网页操作定序（拆分时同一刻写两条）。
- `ts` 存手机给的原值，不换算、不改写。物化记录另存 UTC datetime 的 `span_start`/`span_end`，只用于按日期查询。
- **墓碑**：`cancel`、`delete` 之后对这条记录的任何操作都忽略，所以晚到的重复 `start` 救不活它。
- **冲突不报错**：时间上自相矛盾的操作（比如网页把开始时间改到手机暂停之后）留在操作日志里，记录的 `conflicts` 里标出来，给网页看。
- 一键切换由手机**显式**写 `pause`（与新 `start` 同 ts），服务端不做跨记录裁决。

### 预估的来源

`estimate_source`：`guess` = 用户事前自己填的，**只有它进偏差统计**；`late` = 网页上事后补/改的，留着看但不计入。

### 网页编辑的语义

- **改时间段**：每段 s<e、按顺序不重叠、不晚于现在；计时中的记录最后一段保持开放。网页编辑先按当前状态校验，不合法直接 400；手机的操作不做这种校验，交给重放（可能只是乱序）。
- **拆分**：前半保留原 id 和全部字段；后半是新记录，复制名称/类型/备注，**不带按钮和预估**。预估是对整件事的，按钮历史若两半都算，参考时长会被拉低一半。
- **合并**：保留最早开始的那条（按钮、预估随它），时间段取并集（重叠只算一次），名称/类型缺的从别的补，备注按时间拼接，其余删除。
- **补记**：直接生成已完成的记录，允许和已有记录重叠。

### 接口（全部要 `x-token`）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/timelog/sync` | 手机上传 `{ops:[...]}`（≤500 条），回 `accepted`/`duplicate`/`rejected` + 最新 `state` |
| GET | `/api/timelog/state` | 手机启动：进行中的记录、按钮（带参考时长）、类型、设置 |
| GET | `/api/timelog/day/{YYYY-MM-DD}` | 本地某天与之有交集的记录（按设置的时区划分） |
| GET | `/api/timelog/unnamed` | 待补填的未命名记录 |
| GET | `/api/timelog/records/{id}?ops=true` | 单条记录（可带操作日志） |
| POST | `/api/timelog/records` | 补记 |
| PATCH | `/api/timelog/records/{id}` | 改名称/类型/备注/预估 |
| PUT | `/api/timelog/records/{id}/segments` | 改时间段 |
| DELETE | `/api/timelog/records/{id}` | 删除 |
| POST | `/api/timelog/records/{id}/split` | `{at}` 处拆成两条 |
| POST | `/api/timelog/merge` | `{ids}` 合并 |
| GET/POST/PATCH | `/api/timelog/types[/{id}]` | 类型（`archived` 即删除） |
| GET/PATCH | `/api/timelog/settings` | 切换行为、空闲/超时阈值、静音时段、时区 |
| POST | `/api/timelog/mute-today` | 今天不再提醒（到本地次日 0 点） |

手机端客户端收到 `accepted` 和 `duplicate` 都把操作移出同步队列；`rejected` 是形状错误，重试也不会成功，丢弃并留日志。

### 两处容易踩的

- **计时中被取消的记录，最后一段仍开放**（取消不补结束时刻）。凡是落库算耗时的地方都走 `closed_actual_ms()`，不能只看 status 是不是 running。开发时实测踩过：整批同步 500。
- **参考时长的取整两端必须一致**：Python 的 `round()` 是银行家舍入（`round(2.5)==2`），JS 的 `Math.round(2.5)==3`。`round_ref` 用 floor(x+0.5)，测试钉着。

## 手机端（第 3 步）

- 文件：页面 `app/dashboard/timelog.html`；存储与同步 `timelog-core.js`（路由 `/timelog/core.js`，页面与 service worker 共用）；service worker `timelog-sw.js`（路由 `/timelog/sw.js`）。
- **屏幕上的状态 = 上次同步拿到的服务端 state + 本机还没同步的操作**，按与服务端相同的规则叠上去（页面里的 `applyRecOp` 对应 `replay_record`，只需处理进行中的记录）。离线时打开，用 IndexedDB 里缓存的 state。
- IndexedDB 的 `queue` 只按 id 增删，从不整体覆盖：页面和 service worker 可能同时在同步。服务端回的 `accepted` / `duplicate` / `rejected` 都移出队列；被拒收的在 `kv.rejected` 里留最近 50 条，设置页显示条数。
- 何时同步：每次操作后 400ms（连点合成一批）；回到前台；`online` 事件；有待同步时每 30 秒重试，平时每 2 分钟拉一次 state。另外登记 Background Sync：App 被切走后联网，由 service worker 推上去。否则服务端看不到离线时开始的计时，会误发空闲提醒。
- **令牌**：首次打开弹设置页让输入，存在 IndexedDB（service worker 读得到，localStorage 它读不到）。同源的面板存过 `myspiders-token` 就直接沿用。
- **所有请求 `redirect: 'manual'`**：Access 会话过期时接口被 302 到 cloudflareaccess.com，跟随的话跨域无 CORS，fetch 抛的错和断网一模一样，会被当成离线永远重试。manual 时拿到的是 `opaqueredirect`，顶栏显示「需要重新登录」，点一下整页跳转去登录。`tests/test_timelog_page.py` 钉着「全站只有 core.js 一处 fetch，且带 manual」。
- service worker：页面和静态资源**网络优先**，失败才用缓存（改了页面刷新就能看到）；`/api/` 不缓存。作用域放宽到 `/timelog` 要靠响应头 `Service-Worker-Allowed`。
- 快捷按钮的增删改、排序也是操作（`button.put` / `button.delete`），离线长按改按钮同样记得下。排序时整列重新编号。
- 一键切换按设置写 `pause` 或 `end`（设置页可改，存服务端）。

## 提醒（第 3 步）

规则在 `app/features/timelog/remind.py`（纯函数，测试 `tests/test_timelog_remind.py`）；`timelog_service.check_reminders` 由定时任务 `timelog_remind` 每 5 分钟跑一次。

- **手机要订阅 ntfy topic `<前缀>timelog`**，没订阅就收不到，而且不报错。设置页会显示这个名字。
- 空闲：没有计时中的记录，且距最后一次动静超过 30 分钟。最后动静取所有记录里最晚的结束/暂停时刻，和手机最后一条操作的 ts，取较晚者。**每段空闲只提醒一次**，`low`（不响不震）。
- 超时：计时中的记录超过「预估 × 1.5」，或者没有预估时超过 180 分钟。**每条记录只提醒一次**，`high`（忘了暂停会把数据写坏，要锁屏就能看见）。文案问「是不是忘了暂停或结束」，不说「超时」。
- 静音：20:00–08:00 和周末。**空闲时长从静音结束才开始算**，否则早上 8 点一过，前一晚 19 点起的「空闲 13 小时」会立刻推出来，那是点名不是提醒。
- 通知上的按钮是 ntfy 的 **view** 动作，跳到 `/timelog?action=pause` / `?action=mute-today`，由页面执行。不用 http 动作：那是 ntfy App 直接发请求，带不上 Access 的 cookie，会被 302。
- 去重键和 `muted_until` 存在 `timelog_settings` 的 `key="remind"` 文档里（不是进程内变量：服务一重启，进程内的去重就没了，会把刚推过的再推一遍）。推送失败不记键，下一轮重试。
- 已知局限：服务端只知道已同步的状态。手机离线时开始的计时它看不到，可能误报空闲，所以文案写成提问。

## 原型（第 1 步）的实现

- 页面：`app/dashboard/timelog.html`，单文件。路由在 `app/features/system/spiders_router.py` 的「计时 PWA」段：`/timelog`、`/timelog/manifest.webmanifest`、`/timelog/icon-{192,512}.png`。
- 挂在 `/timelog`，不放在 `/dashboard/` 下：manifest 的 scope 就是这个前缀，装到桌面的应用只覆盖这一页。
- `<link rel="manifest" crossorigin="use-credentials">` 不能省：panel. 在 Cloudflare Access 后面，不带 cookie 拿到的是登录页。
- （第 3 步已删除）原型的假数据存在 `localStorage['timelog-proto-v1']`，现在页面启动时会顺手删掉它。
- 图标由 `scripts/timelog_make_icons.py` 用纯标准库生成，颜色读自 theme.css。改了配色要重跑这个脚本。
- `tests/test_timelog_page.py` 钉住这几点：manifest 形状；页面能识别每个 shortcut action 和提醒按钮的 action；不写死 hex 色值；请求不跟随重定向；sw 的作用域头。
