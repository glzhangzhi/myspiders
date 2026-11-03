"""账单的配置项（环境变量名与字段名相同，大小写不敏感）。"""

from pydantic import BaseModel


class FinanceSettings(BaseModel):
    # 个人专属的商家归类规则（JSON 五元组数组：[正则, 归一商家名, 大类, 二级, 是否固定支出]），
    # 排在 features/finance/parse.py 的通用规则之前。例：
    # [["^(An|Von) Savings$", "储蓄 Space", "非消费", "储蓄划转", false],
    #  ["^Acme GmbH$", "Acme 工资", "收入", "工资", false]]
    finance_personal_rules: str = ""
