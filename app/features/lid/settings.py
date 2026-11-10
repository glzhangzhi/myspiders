"""入籍考试刷题（Leben in Deutschland）的配置项（环境变量名与字段名相同，大小写不敏感）。"""

from pydantic import BaseModel


class LidSettings(BaseModel):
    # 考试日期，ISO 格式（如 2027-01-15）。空 = 不显示倒计时、不做「考前能否掌握」的比较。
    # 个人数据，只写在 .env 里。
    lid_exam_date: str = ""
    # 每天最多引入的新题数（今日新题配额）。
    lid_daily_new: int = 55
    # 「今天」「到期」都按这个时区的日历日算。
    lid_timezone: str = "Europe/Berlin"
