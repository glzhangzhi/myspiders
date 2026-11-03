"""LLM 用量的配置项（环境变量名与字段名相同，大小写不敏感）。

由 app.core.settings 自动合并进全局 `settings`，读取方式不变：`settings.<字段>`。
"""

from pydantic import BaseModel


class LlmUsageSettings(BaseModel):
    # DSH 花费按「大类 → 项目」归并的规则（JSON），见 features/llm_usage/projects.py。
    # 空 = 全部归「未分类」。
    llm_project_categories: str = ""
