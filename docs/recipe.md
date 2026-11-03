# 菜谱（recipe）—— 约定与踩坑

> 从 CLAUDE.md 拆出（2026-11）。改 `app/features/recipe/` 前先读。

## 约定

- **菜谱 tag**:词表存 Mongo `recipe_tag_vocab`(四组:做法/口味/主材/分类),**可在仪表盘「管理标签」增删改名**,改名/删除会同步改写所有引用的菜谱。`app/features/recipe/tags.py` 里的常量只是**出厂默认值**(首次 `$setOnInsert` 播种),不是运行时词表 —— 加标签要走 API,别改常量。三类维度由代码从标题+分类+配料+步骤**派生**,人工可覆盖;`custom_tags` 纯人工、代码永不碰。派生只在入库/转换时跑一次,重算是显式操作(`POST /api/recipes/{slug}/retag`)。**DeepSeek 转换器不让模型自由发挥标签**:候选词表(读库)写进提示词,返回值再用 `_sanitize_tags` 过一道,否则会冒出「家常菜」这类同义词把筛选维度稀释掉。
- **AI 转换会回答用户夹带的提问**:用户用 **`[]`** 在任意字段里写问题(盐的用量栏写 `[对500g鸡腿用多少]`、小贴士写 `[能提前做好冷藏吗?怎么复热]`),模型在 `answers` 里逐条作答(`{question, answer, where}`,`where` 如 `配料:盐`),**并把答案落回对应字段**(算出的克数填进 `amount`、依据写进 `note`);`build_recipe_from_payload` 再把 answers 合并到 `tips` 开头(带 ❓ 前缀),并用 `_strip_questions` 剥掉各字段残留的 `[]`。旧式圆括号带问号(`味极鲜(怎么代替?)`)仍然识别。**不留快照、没有还原**(2026-10 机主定:一直保留最新版),旧数据的 `prev_snapshot` 已由 repo 启动迁移摘掉。
- **重转要喂当前 doc,不是 `raw_text`**:`raw_text` 是导入时的原始 markdown,**不含用户后来的编辑** —— 拿它重转就是「AI 把我存好的用量覆盖掉」的根因(改提示词没用,模型压根看不见)。故 `reconvert` 走 `render_doc_for_convert(doc)`,并在 upsert 前把 `raw_text` 显式写回,别让这次拼的输入覆盖掉源数据。空用量在输入里写成「未填」而非留白:模型对「字段存在但空着」的反应积极得多。
- **用量绝不留空,但用户填的绝不改**:没量的料让模型按份量推算(`5g(约1小勺)`)并在 `note` 标「推算」;用户自己填的一律保留,**口语量(`两指捏一撮`)也算用户填的**,只可补括号估值不可整个替换 —— 不写死这条模型会自作主张「规范化」。模型漏答是静默失败,用 `extract_questions()` 数原文 `[]` 个数与 answers 比对,少了进 warnings;前端 `countPendingQuestions()` 用同一套正则在详情页提示「还有 N 处提问没答」。
- **`last_pushed_at` 必须在 `_USER_OWNED` 里**:upsert 写完整模型、该字段默认 None —— 不保护的话一次 AI 重转换就把「推送过」标记抹掉(原先 `prev_snapshot` 踩过的同一个坑)。它取代了 `cooked_count`(那是推送时 +1,从来不是真的做过几次)。
- **菜谱主键**:`slug = "来源:标题"`(如 `howtocook:回锅肉`)。同名菜在不同来源是不同菜谱;AI 重转换时**必须保持原 slug**,否则模型改写标题会凭空多出一条,旧的还留在菜单里指向空记录。
- **菜谱源用 HowToCook,不用 CookLikeHOC**:`Anduin2017/HowToCook` 面向家庭厨房,370 道格式 100% 统一(必备原料 / 计算 / 操作 / 附加内容四段),自带难度星级与热量。CookLikeHOC(老乡鸡)是**商用后厨口径**,实测 340 道里 83% 有硬伤(单项克数≥500g 的 172 道、蒸柜等商用设备 127 道、依赖中央厨房半成品 126 道、根本没写步骤 49 道),真正能直接家用的只有 9 道 —— 2026-09 已整体清库换源。解析器 `features/recipe/hoc_import.py`(HOC)与 `features/recipe/howtocook.py`(HTC)并存,后者是默认。
- **HowToCook 的「必备原料」与「计算」必须合并**:前者只有名字、后者只有用量公式,分开存会得到"一堆没有量的料 + 一堆没有主语的量"。按名字前缀对齐(两段写法不完全一致,如「糖(可选)」vs「糖 = 0-2g * 份数」)。
- **分类 = 菜品类型,不是做法**:`荤菜/素菜/水产/汤/主食/早餐/甜点/饮品/配料/半成品`。早期沿用 HOC 的目录名(炒菜/蒸菜/…)其实是做法,与 `method_tags` 语义重复,且把 370 道里 201 道挤进「炒菜」一格、筛选器失效。两个维度正交后才能叠加筛选。
- **推送列表(原「当前菜单」)不按日期**:`meal_plans` 集合只有**一条**文档(`key="current"`,接口路径仍是 `/menu`)。做饭是"现在要做这几道菜",不是日程管理;前端任何改动都防抖自动落库。**推送 = 一道菜一个文件、文件名就是菜名**(`回锅肉.txt`),同名覆盖、批量也不合并 —— Kindle 上每道菜只留最新一份(2026-10 推翻了早先「按推送时间命名留历史」的设计:实测 Kindle 上堆满同名不同时间的版本,找最新的反而费劲)。同一批里两道同名菜(不同来源)由 `recipe_filename(taken=)` 加来源后缀,否则后推的静默盖掉先推的。远端走 `cat > x.part && mv`,断线不会把旧版截成半截。**「我的」菜谱**= source 不在 `IMPORTED_SOURCES`(howtocook/cooklikehoc)里,用排除法:新录入入口自动算「我的」,新增导入源才需要显式加进去。

## Kindle 推送的坑

- **Kindle 推送不能用 scp**:KOReader 自带的 dropbear **没有 sftp-server**,新版 scp 走 SFTP 协议会直接失败。用 `ssh host 'cat > file'` 管道写入,只依赖 shell 重定向。
- **Kindle 的 ping 不通是正常的**:设备丢 ICMP,但 TCP/SSH 畅通。判断在线只认 SSH 探测(`check_kindle()`),别拿 ping 当依据。
- **KOReader 的「免密登录」是空密码而非 none 认证**:服务端 banner 仍报 `publickey,password`,所以 `BatchMode=yes` 反而连不上。本服务只走**公钥**(`kindle_ssh_key`),公钥要放进 Kindle 的 `/mnt/us/koreader/settings/SSH/authorized_keys`(dropbear 补丁读的是这个相对路径,不是 `~/.ssh/`)。推送一律带 `BatchMode=yes` —— 服务进程卡在密码提示的 stdin 上就是永久挂起。
