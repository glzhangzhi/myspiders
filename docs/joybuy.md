# Joybuy 降价监控 —— 踩坑

> 从 CLAUDE.md 拆出（2026-11）。

- **Joybuy 的风控只认登录 cookie,而拦截页返回 200**:无 cookie 时**整站(含首页)**307 到 `/user/risk/pic`。**playwright 完全无效**且不是指纹问题——无头/有头(xvfb)/改 UA/预热首页/移动 UA 六种姿势全被拦,分水岭只有「有没有登录态」。逐 cookie 实测:**只需 `mfs_session` 一个**(单独带它就 200;`pin`/`3AB9D23F7A4B3C9B`/`__uj` 单独带全部 307),`h5st` 签名**不需要**(去掉它重放 color-api 照样 `success:true`)。该 cookie **无法匿名签发**(`/login`、`/user/risk/pic`、color-api 的 set-cookie 里只有 `__jda` 埋点 id),过期只能人工从浏览器复制 → 故存 Mongo 走 API 热更新(POST body,不走 query)、失效时推一次 high。⚠️ **风控页与登录页 HTTP 都是 200**,唯一判据是 `<title>` 里的「403 Verboten」/「Melden Sie sich」,只看状态码会把拦截当成功。
- **Joybuy 价格有三个源,必须互证且解析失败绝不兜底成 0**:商品详情的 functionId 猜不出来(`color_trade_item_get` 等一律回 "the current API does not exist"),但**商品页 HTML 一次请求带齐全部数据**(JSON-LD 给名称/评分/评价,Next.js flight data 给价格/变体/库存)。同一价格出现在三处:flight 的 `firprice`、flight 的 `finalPrice`、可见 DOM 的 `priceFloor_skuPriceReal__*`(德语逗号做小数点)。`parse_price` 依次尝试并**返回命中来源列表**,掉到只剩 1 个就告警——降价监控里「静默失效」最坏:不报错、只是永远不触发,等人发现促销早结束了。多源不一致取**最小值**(按低的报,点进去看到更贵不会错过机会,反之会)。**失败一律返回 None**:阈值判断是 `price < 10`,兜底成 0 会立刻推一条假的「降到 €0」;`firprice:"0"` 同理当失败处理(字段存在但没填 ≠ 免费)。推送节流按「比上次推送时**更低**才再推」——不是推过就永不再推(€9.99→€4.99 值得再响),也不是每次低于阈值都推(促销持续几天会每小时响一遍)。
