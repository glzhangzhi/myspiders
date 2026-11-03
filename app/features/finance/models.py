"""账单 / 支出数据模型。

## 为什么交易主键是内容哈希而不是自增 id

同一份 PDF 可能被重复导入（换机器、恢复备份、手抖点两次）。主键取
`sha1(月份|账本|序号|日期|金额|描述)` 后重导天然幂等 —— 同一笔永远算出
同一个 id，`upsert` 就是更新而非新增。

**序号必须进哈希**：3 月有两笔同日、同额（-1,79）、同商家（GO ASIA）的交易，
只按「日期+金额+商家」算哈希会把它们合成一条，少算一笔钱。

## 机器字段与人工字段

`raw_*` 与解析结果属于机器，重导 PDF 时覆盖；
分类、标签、备注、确认状态属于人工，**重导时绝不覆盖**（见
`repositories/finance.py` 的 `_USER_OWNED`）。两者放在同一个文档里，
靠字段归属划清界限。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

# 交易性质。expense 之外的三种都不计入「支出」统计：
#   income   收入
#   transfer 非消费（自己两个口袋间搬钱 / 代付往来 / 取现）
#   refund   退款，冲减原类别而不是算作收入
KINDS = ("expense", "income", "transfer", "refund")

# 账本。space = 储蓄 Space，与主账户的划转是同一笔钱的两条腿，
# 统计时只认 main 那条，否则净额恒为 0。
SECTIONS = ("main", "space")


class Transaction(BaseModel):
    """一笔交易，对应 MongoDB `fin_transactions`。"""

    id: str                                    # 主键 = 内容哈希，见模块 docstring
    month: str                                 # YYYY-MM，取自 PDF 账期而非文件名
    section: str = "main"
    seq: int = 0                               # 在该对账单内的出现序号，参与主键

    # ── 机器字段：重导 PDF 时覆盖 ──
    date: str                                  # 记账日 YYYY-MM-DD
    value_date: str = ""                       # 起息日
    amount: float                              # 负=出账，正=入账。德式金额已转 float
    raw_desc: str = ""                         # PDF 上的原始描述，永不改写
    memo: str = ""                             # 用途备注
    channel: str = ""                          # card / Lastschriften / Gutschriften / MoneyBeam…
    bank_cat: str = ""                         # 银行自带的德语类别，仅刷卡消费才有
    counter_iban: str = ""
    statement_id: str = ""                     # 来自哪次导入

    # ── 人工字段：重导 PDF 时保留 ──
    merchant: str = ""                         # 归一后的商家名，规则按它沉淀
    kind: str = "expense"
    l1: str = "未分类"                          # 大类
    l2: str = ""                               # 二级分类
    fixed: bool = False                        # 固定支出（房租/宽带/月票…）
    excluded: bool = False                     # 不计入任何统计
    tags: list[str] = Field(default_factory=list)   # 自定义标签，与类别正交
    note: str = ""
    reviewed: bool = False                     # 人工确认过
    auto: bool = True                          # 分类是否仍是机器给的

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Statement(BaseModel):
    """一次导入，对应 MongoDB `fin_statements`。

    `file_sha256` 唯一：同一个文件重复上传直接认出来，不必重新解析。
    `reconcile` 存下勾稽明细 —— 将来 N26 改版式导致某个月对不上时，
    能立刻看出是哪一段、差多少。
    """

    month: str
    file_name: str = ""
    file_sha256: str = ""
    period_start: str = ""
    period_end: str = ""
    tx_count: int = 0
    reconcile: list[dict] = Field(default_factory=list)
    ok: bool = False                           # 勾稽是否全平
    imported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MerchantRule(BaseModel):
    """商家 → 分类的沉淀规则，对应 `fin_merchant_rules`。

    用户每确认一条交易就可能长出一条规则，下次同名商家自动套用，
    人工只需处理增量。
    """

    merchant: str                              # 主键
    l1: str = ""
    l2: str = ""
    kind: str = "expense"
    fixed: bool = False
    hits: int = 0                              # 被套用过几次，排错时看得出哪条规则在生效
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CategoryVocab(BaseModel):
    """分类词表，对应 `fin_categories`，一个大类一条文档。

    代码里的常量只是**出厂默认值**（首次 `$setOnInsert` 播种），
    运行时一切以库为准 —— 与菜谱 tag 词表同一套约定。
    """

    l1: str                                    # 主键
    subs: list[str] = Field(default_factory=list)
    order: int = 0                             # 显示顺序，同时决定图表配色的取色顺序
    color: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TxPatch(BaseModel):
    """逐条编辑的入参。全部可选 —— 只改传进来的字段，没传的不动。"""

    merchant: Optional[str] = None
    amount: Optional[float] = None
    date: Optional[str] = None
    kind: Optional[str] = None
    l1: Optional[str] = None
    l2: Optional[str] = None
    fixed: Optional[bool] = None
    excluded: Optional[bool] = None
    tags: Optional[list[str]] = None
    note: Optional[str] = None
    reviewed: Optional[bool] = None
