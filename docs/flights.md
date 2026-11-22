# 机票（flights）

Trip.com 机票查询面板：选航线 + 日期区间 + 硬条件，后端**逐天**查当天全部行程，按条件筛完取最低价。
单程出**日历**，往返出**矩阵**（行 = 去程日，列 = 回程日）。点格子看当天全部航班（含被排除的与原因）。
没有定时任务、没有推送 —— 只是一个「我现在想查」的工具。

```
app/features/flights/
  client.py    Trip 接口：请求体、发请求、解析成 dataclass（无业务逻辑）
  filters.py   筛选/排序硬规则（纯函数，tests/test_flights.py 钉住）
  service.py   全局串行限速、25 条补查、缓存、日历/矩阵组装、真往返、DeepSeek 点评
  repo.py      Mongo 缓存 flights_cache（原始结果，TTL 索引）
  router.py    /api/flights/*（search 是 POST + SSE）
web/src/pages/Flights.tsx + pages/flights/*
```

## 配置（`.env`，名字 = 字段名）

| 变量 | 默认 | 说明 |
|---|---|---|
| `FLIGHTS_REQUEST_INTERVAL_MIN` / `_MAX` | 1 / 2 | 两次 Trip 请求的间隔（秒，随机） |
| `FLIGHTS_CACHE_TTL_MINUTES` | 30 | 单日原始结果缓存时长 |
| `FLIGHTS_MAX_DAYS_ONEWAY` | 31 | 单程一次最多查几天 |
| `FLIGHTS_MAX_DAYS_ROUNDTRIP` | 15 | 往返去程、回程区间各自最多几天 |
| `FLIGHTS_CURRENCY` | EUR | 请求头、请求体、Trip 链接三处一起用 |
| `FLIGHTS_PAGE_CAP` | 25 | Trip 单次返回上限；顶到就补查 |
| `FLIGHTS_USER_AGENT` | 桌面 Chrome | **不能带 HeadlessChrome** |

DeepSeek 点评复用全站的 `DEEPSEEK_API_KEY`；没配就不显示点评区。

## 设计决定

- **纯 HTTP，不用 Playwright。** `FlightListSearchSSE` 不要 cookie、不要登录、不要签名（2026-11 实测），
  一次请求 2–8 秒。浏览器方案慢一个数量级还占内存。
- **不用 `GetLowPriceInCalender` 拿价格。** 那是「这天最低价」，不管直飞/中转/行李，和筛选后的最低价对不上，
  会让日历显示一个点进去找不到的价格。
- **往返矩阵 = 两张单程相加。** 11×11 的矩阵若每格都查真往返要 121 次请求；按天查单程只要 22 次。
  点开一格时再查一次真往返（`tripType: 2`）并排显示「两张单程 €X / 真往返 €Y」，便宜的那个高亮。
  真往返返回的行程只有**去程段**（价格是往返总价）—— Trip 网页也是先选去程、再单独请求回程段，一期不复刻，
  所以真往返的回程段**没按条件筛**，页面上写明了。
- **筛选全是硬代码**（`filters.py`），DeepSeek 只在查完后写一段中文点评，看不到也改不了筛选结果。
- **被排除的行程不丢**，带逐条原因（「在 HKG 中转 7h20，超过 6h」「自助中转，需重新托运」）折叠在明细末尾 ——
  否则分不清「条件排除了」和「漏查了」。
- **缓存存原始结果，不存过滤结果。** 换一组开关不必重查。键里带「下发给 Trip 的最多中转次数」，
  因为服务端筛选改变了原始结果本身；更宽的缓存可以给更窄的查询用（存的是 ≤1 次中转，现在只看直飞照样命中）。
- **全局串行。** 所有 Trip 搜索请求过一把锁、间隔 1–2 秒；同一时刻只允许一个长查询（第二个直接 409）。
  31 天单程约 1.5–4 分钟，往返 15+15 天类似。点格子看明细多半命中缓存，不额外请求。
- **查询要 admin token**（会替人打外部站点）。SSE 走 POST + fetch 读流：EventSource 只能 GET、带不了 x-token，
  而且断线自动重连 —— 对一次性长查询，重连 = 把几十次 Trip 请求再发一遍。

## 筛选规则

| 开关 | 默认 | 含义 |
|---|---|---|
| 只看直飞 | 关 | 只有 1 个航段；同时服务端下发 `TRANSFER_COUNT=0` |
| 最多中转次数 | 1 | 航段数 − 1 ≤ N；同时服务端下发 `TRANSFER_COUNT="0\|1"`（不限则不下发） |
| 接受长时间中转 | 关 | 关闭时任一次中转 > X 小时（默认 6）就排除 |
| 接受需重新托运 | 关 | 关闭时只留「确认直挂」「推断直挂」；打开时第三态也留，但每次中转都要 ≥ Y 小时（默认 3） |
| 总时长上限 | 无 | 可选，小时 |

**行李三态**：
- **确认直挂**：行程带 `LUGGAGE_DIRECT`，或直飞。
- **推断直挂**：没有标记，但所有航段 `transSplit` 都是 false（同一张票、联程）。页面标「推断」，下单前去详情页确认。
- **需重新托运**：任一航段 `transSplit` 为 true（自助中转、分开出票）。

**中转时长** = 后一段 `departDateTime` − 前一段 `arriveDateTime`。两个时间都是**中转机场的当地时间**，同一个机场，
直接相减就对，不用管时区。`transSectionList[i].transferDuration`（分钟）实测与它一致，但不是每段都有，没用它。
前后两段机场不同（SHA 落地、PVG 起飞）时显示「SHA→PVG（换机场）」，`sectionFlags` 里也有 `DIFFERENT_AIRPORT`。

## 接口（非官方，网页自己在用的）

`POST https://www.trip.com/restapi/soa2/27015/FlightListSearchSSE`，头只要 `content-type`、`currency`、`locale`、`user-agent`。
请求体见 `client.build_body`。关键字段：

- `searchCriteria.tripType` 1 单程 / 2 往返（`journeyInfoTypes` 两段）；`grade: 1` 经济舱；1 位成人。
- `sortInfoType.orderBy`：`Direct`（网页默认）/ `Price` / `Duration`。
- `filterType.queryItemSettings`：`{"type":1,"key":"TRANSFER_COUNT","value":"0|1","journeyNo":1}`，往返时每个 journeyNo 一条。
  `"0"` = 只要直飞（实测返回 1 条 CI061）。

响应是 `data:` + 一个 JSON（不是多条 SSE 事件）。有用的部分：

- `head.retCode` / `retSubCode`
- `basicInfo.recordCount`、`basicInfo.flags`（`HAS_DIRECT_FLIGHT` / `NO_DIRECT_FLIGHT`）
- `itineraryList[].journeyList[0]`：`duration`（分钟）、`journeyFlags`（`DIRECT` / `ONE_STOP` / `MULTI_STOPS` / `LUGGAGE_DIRECT` / `SHARE_FLIGHT`）、
  `transSectionList[]`（航段：`flightInfo.flightNo`、`departPoint/arrivePoint.airportCode`、`departDateTime` / `arriveDateTime` 当地时间、`transSplit`）
- `itineraryList[].policies[]`：多个价格方案，`price.totalPrice`（整数，币种由头决定）；取最小的。
  `tagList` 里 `key == "FREE_CHECKED_BAGGAGE"` 的 `extendInfo[extendKey=bagFormat]` 是托运额度文案（如 `Checked baggage: 2 x 23 kg`）。

**失败和无航班必须分开**（这是整个功能最要紧的一条）：

| 情况 | 表现 | 当作 |
|---|---|---|
| 正常 | `retCode SUCCESS`，有 `itineraryList` | 有结果 |
| 这天没航班 / 日期已过 | `SUCCESS` + `retSubCode AGG_NO_RESULT`，0 条 | 无航班 |
| 服务端筛选后为空 | `SUCCESS` + `FILTER_NO_RESULT`，0 条 | 无航班 |
| 机场代码不对 | `retCode FAILURE` + `AGG_SEARCH_FAIL` | **查询失败** |
| 请求体不对 / 被风控 | HTTP 432，body 像二进制 | **查询失败** |
| 返回不是 JSON、recordCount>0 却没列表、全部行程解析失败 | — | **查询失败** |

查询失败的格子是红的，点开会重查；补查（见下）失败不算整天失败，只在明细里给警告。

### 自动补全

`POST https://www.trip.com/restapi/soa2/14427/poiSearch`，body
`{"key":"上海","mode":"0","tripType":"OW","Head":{"Group":"Trip","Source":"ONLINE","Version":"3","Currency":"EUR","Locale":"en-XX"}}`。
中文、英文、代码都能搜；`results[]` 是城市（`cityCode`）带 `childResults[]` 机场（`airportCode`）。

### 下单链接

`https://www.trip.com/flights/showfarefirst?dcity=tpe&acity=fra&ddate=2026-12-26&triptype=ow&class=y&quantity=1&curr=EUR`，
往返 `triptype=rt&rdate=…`。代码要小写。

## 踩坑

- **有时单次只给 25 条。** `recordCount` 正好 25 说明被截断了（没有翻页参数）。对策：同一天再按 `Price`、`Duration`
  各查一次，按「航班号组合 + 首段起飞时间」去重合并，同一条取较低价（2027-01-03 TPE→FRA：25 → 合并后 30 条）。
  但上限**不总是 25**：同一接口同一天也见过一次返回 43、57、129 条（`recordCount` 与条数一致），规律没摸清 ——
  所以只在「正好 25」时补查，补查也只是多捞一些、不保证全。每天的请求数如实计入进度和统计。
- **`LUGGAGE_DIRECT` 时有时无。** 同一天几分钟内能从 8 条变 0 条；2026-11-22 那次 CI061 带着它。
  所以「没有标记」只能推断，不能当「不能直挂」—— 这就是三态里「推断直挂」存在的理由。
  `transSplit: true` 在 2026-11 的样本（TPE/PVG/BKK/HKG → FRA/BER/MUC，数百条）里**一次都没出现过**：
  Trip 的默认结果可能根本不给自助中转。规则留着，测试用改过的 fixture 覆盖。
- **价格会浮动几十欧。** 同一航班同一天，隔几分钟、换个排序方式查，`totalPrice` 能差 20–40 €。页面写明
  「价格以 Trip 详情页为准」并显示查询时间；补查合并时取较低价。
- **城市展开。** 填 `PVG` 也会返回从 `SHA`（虹桥）出发的行程 —— Trip 按城市查。这里不过滤，明细里每段都写了实际机场，
  换机场的中转会标出来。
- **`TRANSFER_COUNT` 往返时要每个 journeyNo 各一条**，否则只筛去程。
- **非官方接口随时可能变。** 网页 JS 里有 `isOpenRequestBodySign`、`isOpenSSEAntiBotCheck` 之类的开关，哪天开了，
  纯 HTTP 就会全部 432。表现会是「所有格子查询失败」，**不会**是「全部无航班」—— 解析层任何看不懂的情况都抛 `TripError`。
  2026-11 实测 HeadlessChrome UA 也没被拦，但别依赖它，UA 一律用桌面 Chrome。
- **学生票没做。** 网页上有「Student tickets」勾选（前端 key `studentTicket`），点了**不发新请求**；
  请求体 `filterFlagTypes: [{"flag":"Students","allow":true,"journeyNoList":[1]}]` 在 TPE↔FRA、PVG↔FRA 四个方向都返回
  `FILTER_NO_RESULT`；`STUDENT`、`IntlStudents` 被忽略（结果与不筛相同）；正常响应里没有任何含 `STUD` 的 policyFlags。
  结论：这几条航线 Trip 没有学生票产品，开关不加。
- **点评要的是查完的结果。** 最近 10 次查询的摘要放在进程内存里（`service._recent`），服务重启后点评会提示「重新查一次」。
- **请求耗时比想象长。** 每次 2–8 秒 + 1–2 秒间隔；11 天单程约 1–1.5 分钟，往返 5+5 天约 1 分钟。

## 测试

`.venv/bin/pytest tests/test_flights.py -v`：解析（脱敏 fixture `tests/fixtures/flights/tpe_fra_20261226.txt`，
真实响应删掉了 token / uniqueId 等，留 6 条代表性行程）、行李三态、中转时长（跨午夜、换机场）、筛选原因、
25 条补查合并去重（MockTransport）、矩阵组装（失败盖过无航班）、失败 ≠ 无航班、区间上限。
