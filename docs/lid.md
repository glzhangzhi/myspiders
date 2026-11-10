# 入籍考试刷题（lid）

「Leben in Deutschland / Einbürgerungstest」黑森州题库刷题。单人自用、手机优先、不做任何推送。

- 考试规则：33 题（30 道通用题 + 3 道本州题），60 分钟，答对 17 题及格。
- 题库：300 道通用题 + HE-1..HE-10，共 310 题，官方 PDF Stand 07.05.2025。
- 页面 `/dashboard/lid`，代码 `app/features/lid/` + `web/src/pages/Lid.tsx`、`web/src/pages/lid/*`。
- 配置（`.env`）：`lid_exam_date=YYYY-MM-DD`（空 = 不显示倒计时）、`lid_daily_new`（默认 55）、`lid_timezone`（默认 Europe/Berlin）。
  考试日期是个人数据，只写进 `.env`。

## 数据来源与出处

题库是**构建期产物**（`app/features/lid/data/questions.json` + `data/img/*.png`），由 `scripts/lid_build.py`
一次性生成并提交进仓库；线上运行时不联网抓题。

| 内容 | 来源 | 协议 / 说明 |
|---|---|---|
| 题干与选项（德语，官方顺序） | [leben-in-deutschland/leben-in-deutschland-app](https://github.com/leben-in-deutschland/leben-in-deutschland-app) 的 `src/catalogue/data/bamf-catalogue.json`，钉 commit（见 `meta.sources.catalogue`） | MIT。数据直接抽自 BAMF 官方《Gesamtfragenkatalog》PDF |
| 标准答案 | BAMF 官方在线工具 [oet.bamf.de](https://oet.bamf.de/ords/oetut/f?p=514:1:0) | 官方 PDF **没有答案**，只能从这里取 |
| 交叉核对 | [abdullahbutt/leben-in-deutschland-test](https://github.com/abdullahbutt/leben-in-deutschland-test) 的 `quiz-data.json` | 内容 CC BY-NC 4.0。**只在构建时比对答案，不分发它的文字**（它的解析 278/310 题只有一句「Die richtige Antwort ist: X」，也没法用） |
| 13 张题图 | 同上仓库的 `images/`（`301-hessen.png` → HE-1，`308-hessen.png` → HE-8） | 图是官方 PDF 页面的截图（题干 + 图），逐张目视核对过与题目一致。照片的版权归属见各题 `credit`（如「© Bundesregierung/Engelbert Reineke」），页面上显示在图下 |
| 中文翻译与解析 | DeepSeek（`deepseek-chat`），构建时生成 | 解析与官方答案一致，按校验规则见下 |

题目文字本身是 BAMF 的官方题库（公共考试材料）；答案取自 BAMF 官方练习工具。

## 重建题库

```
python scripts/lid_build.py                  # 全量：抓官方答案（有缓存复用）→ 校验 → LLM 全部重跑 → 写产物
python scripts/lid_build.py --only-missing   # LLM 只补缓存里缺的题（断点续跑）
python scripts/lid_build.py --refresh-official
python scripts/lid_build.py --skip-llm       # 只抓取 + 校验，不写产物
```

- 缓存在 `downloads/lid_build/`（gitignore）：`official.json`（官方答案）、`llm.json`（每批写一次）、下载的源文件与图片。
- 需要 `.env` 的 `deepseek_api_key`；Playwright + chromium（项目 venv 已装）。抓官方答案约 13 分钟（每题间隔 1 秒，别给官方服务器压力）。
- **任何一条校验不过就报错退出、不写产物**：正好 310 题、id 不重不漏；工具第 n 题与 PDF 对齐（见下）；
  每题官方答案 0..3；锚点：第 1 题答案「hier Meinungsfreiheit gilt.」、第 14 题答案「meine Meinung im Internet äußern kann.」；
  LLM 输出字段齐全、4 个选项、回显的答案下标与官方一致、解析不是「Die richtige Antwort ist …」。
- 官方 PDF 换版（`stand` 不再是 07.05.2025）时脚本直接拒绝，需要人工核对后再改。
- 缓存里答案和当前官方答案不一致的 LLM 条目会自动作废重写（官方改答案时解析必须跟着变）。

### 用 FARBE 读官方答案

工具是 Oracle APEX，纯 requests 提交表单进不去，要用 Playwright：

1. 首页 `select#P1_BUL_ID` 选 `7`（Hessen），点 `input[value="Zum Fragenkatalog"]` → 进入 page 30；
2. `select#P30_ROWNUM` 按 label 选第 n 题（1–310；301–310 = HE-1..HE-10），选项的 onchange 是 `apex.submit` → 整页导航，要 `expect_navigation` 等它；
3. **正确选项的 radio 带 `id="FARBE"`**，不用作答就能读；同行 `td[headers=ANTWORT]` 是选项文字；
4. 页面正文有「Aufgabe n von 310」—— 用它确认真的翻到了第 n 题、且州选对了（总数 310）；
5. 题干是图片（`#P30_AUFGABENSTELLUNG_BILD`），不是文字，所以靠 4 个选项的文字和 PDF 对齐。

### 工具与 PDF 的文字差异（都是同一选项，按位置一一对应）

工具里的选项文字和 PDF 不完全一样，脚本分三类容忍，其余一律报错：

- **句点 / 空白 / 引号**（约 30 处）：工具普遍不带句末句点。
- **性别词序与冠词**（约 120 处）：工具写「Einwohner / Einwohnerinnen」「vom Bundespräsidenten / von der …」，
  PDF 写「Einwohnerinnen/Einwohner」「von der Bundespräsidentin/von dem …」。判据是去掉冠词、`-in/-innen` 后的词集合相同，
  且这个集合在本题 4 个选项里唯一。
- **人工核对过的措辞**（7 处，`KNOWN_VARIANTS` 逐字列出）：旧正字法「selbständig」、「Kirchensteuern」、
  「Hausratversicherung」、231 题工具只有阳性等。新出现的差异不在表里 → 构建失败，人工看过再加。

图片题（选项是「Bild 1..4」或「1..4」）只核对编号顺序。

### 交叉核对的坑

abdullahbutt 的数据有几道题**把选项顺序打乱了**（57、82、247、282、291；226 的「1..4」存成了 `['2','1','4','3']`），
直接比下标会误报 6 题不一致。脚本按选项**内容**把它的答案换算回官方顺序再比，换算后与官方答案 **0 题不一致**
（包括第 14 题：这个仓库已经是新答案）。

## ⚠️ 不要用 lebenindeutschland.org 的数据

流行的 `question.json` / `questions-core.json` 已过期：**36 道题已不在现行 PDF 里**，选项也被打乱了。
例如第 14 题它给的是旧答案「in Leserbriefen」，现行官方答案是「meine Meinung im Internet äußern kann」。

## ⚠️「现任官员」类题目

涉及现任总理、联邦总统、州长、执政党等的题，答案会随选举变化。构建提示词要求这类解析写明
「Maßgeblich ist der Stand zum Prüfungszeitpunkt. / 以考试时的现任为准。」—— 考前要自己再确认一遍现任是谁；
官方工具改了答案后重建题库即可（缓存会自动作废对应解析）。

## 复习规则：压缩版 Leitner（`srs.py`，单测 `tests/test_lid.py`）

| 情形 | 结果 |
|---|---|
| 新题第一次答对 | 等级 1，到期 +1 天 |
| 到期（或逾期）答对 | 1 → 2（+3 天）→ 3（+7 天）→ 4 已掌握。从第一次答对算约 11 天 |
| 提前作答（还没到期就答对） | 只计统计和 streak，**不升级**（随手刷题不打乱间隔） |
| 答错（任何等级，含已掌握） | 等级 1、到期日 = 今天、streak = 0、进错题本 |
| 模拟考里答对 / 答错 | 答对按提前作答；答错同上 |

- 日期按 `lid_timezone` 的日历日，存 `YYYY-MM-DD` 字符串；存储时间戳一律 UTC。
- 错题本：答错进入；streak ≥ 2 自动移出；也可以手动移出。
- 今日新题配额 = `lid_daily_new` − 今天首次引入的题数（`intro_on`，第一次作答那天，答错也算引入）。
- 今日队列：先到期复习（逾期最久在前），再新题（按官方题号，不超过余量）。答错的题本轮里过 3–5 题再出一次
  （到期日是今天，所以那次答对按到期答对升 2 级）。
- 掌握预测：剩余新题按每日上限引入（今天用余量），每题再加 11 天；复习中的题按各自到期日推算，取最晚者，与考试日期比较。

## 中文淡出（前端，按单题）

- 全局开关三档（localStorage `lid-zh-mode`）：自动 / 总是显示 / 从不显示。
- 自动：该题**作答前**的 streak < 2 时，题干、选项、解析的中文显示在德语下面（小一号、淡色）；streak ≥ 2 默认收起，
  卡片角上「中」临时展开（键盘 `z`）。本题刚答错时中文立刻回来；答错让 streak 归零，之后都会显示，直到再连对 2 次。
- 取「作答前」的 streak：否则第二次答对的瞬间中文会在眼前消失。
- 模拟考作答时只有德语、官方选项顺序、交卷前不显示对错；交卷后的错题回顾按淡出规则显示中文。
- 练习时选项打乱显示，提交时换算回官方下标；模拟考不打乱。
  例外：选项是「Bild 1..4」/「1..4」的图片题不打乱——编号指向图里的标号，打乱后屏幕上的序号会和图对不上。

## 接口

读：`GET /api/lid/bank`（ETag；带 `?v=<etag>` 长期缓存）、`/state`、`/stats`、`/exams`、`/img/{name}`（文件名白名单正则）。
写（`x-token`）：`POST /answer {qid, chosen, mode}`、`/star`、`/wrongbook/remove`、`/exam/start`、`/exam/{id}/finish {answers}`。

- 模拟考的题由服务端抽（30 通用 + 3 HE，不重复），判分也在服务端；重复交卷不会把进度写两遍（`finished_at` 为空才写）。
- 模拟考草稿存在前端 localStorage（刷新不丢）；超过 1 天没交的半截考卷在下次开考时清掉。
- Mongo 集合：`lid_progress`（每题一条）、`lid_answers`（作答日志，含德国日 `day`）、`lid_exams`。删功能时一并清掉。
