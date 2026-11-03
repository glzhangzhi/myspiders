"""DeepSeek 菜谱转换器 —— 任意格式的菜谱文本 → 本系统的固定结构。

用途有二：
1. 用户从别处粘一段菜谱（公众号、小红书、随手记的流水账），一键转成结构化条目
2. CookLikeHOC 里规则解析没吃透的（52 份没抽出步骤），补一刀

**用 JSON mode 而不是自由文本**：DeepSeek 支持 `response_format={"type":"json_object"}`，
比"请返回 JSON"的提示词可靠得多。即便如此仍要防御性解析 —— 模型偶尔会用
markdown 代码块把 JSON 包起来。

标签**不让模型自由发挥**：把本系统的候选标签列进提示词，让它从中选。
否则会冒出「家常菜」「下饭」这类同义词，把筛选维度稀释成一团乱麻。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.core.llm_json import extract_json_object
from app.core.logging import get_logger
from app.core.settings import settings
from app.features.recipe.models import Ingredient, Recipe, RecipeStep
from app.features.recipe.tags import (
    DEFAULT_CATEGORIES,
    FLAVOR_TAGS,
    INGREDIENT_TAGS,
    METHOD_TAGS,
    derive_tags,
    make_slug,
)

logger = get_logger("recipe_convert")

def _build_system_prompt(
    methods: list[str], flavors: list[str], ingredients: list[str], categories: list[str]
) -> str:
    """按当前词表动态拼提示词。

    词表现在是用户可编辑的（存 Mongo），不能再写死在模块常量里 ——
    用户新加的「空气炸锅」做法必须让模型也能选到。
    """
    return f"""你是一个菜谱结构化转换器兼家常菜顾问。用户会给你一段任意格式的菜谱文本
（可能是流水账、公众号排版、商用后厨配方、或用户自己编辑过的半成品条目），
你要把它转换成严格的 JSON，并**当面回答用户夹带的提问**。

输出 JSON 结构：
{{
  "title": "菜名",
  "category": "分类，从 {categories} 中选最合适的一个",
  "servings": "几人份，如 2人份，不确定就按 2人份 给",
  "total_minutes": 预估总耗时分钟数（整数，不确定填 null）,
  "ingredients": [{{"name": "主料名", "amount": "用量，必填", "note": "备注可空"}}],
  "steps": ["第一步的完整描述", "第二步…"],
  "method_tags": ["从 {methods} 中选 1-2 个"],
  "flavor_tags": ["从 {flavors} 中选 1-3 个"],
  "ingredient_tags": ["从 {ingredients} 中选 1-3 个主材"],
  "tips": "窍门、注意事项、以及你对这道菜的实用建议",
  "answers": [{{"question": "用户问的原话（可精简）", "answer": "你的回答", "where": "问题出现的位置，如 配料:盐 / 步骤3 / 小贴士"}}]
}}

════ 最高优先级：回答方括号里的提问 ════

用户会用**方括号 []** 把问题包起来，夹在任何位置 —— 配料的用量栏、备注栏、
步骤文字里、小贴士里。例如：

  配料「盐」的用量写成 `[请帮我计算出对于500g鸡腿的使用量]`
  小贴士里写 `[我能提前煮好鸡腿冷藏吗？如何二次加热，微波炉还是水煮？]`
  步骤里写 `大火翻炒[多大火？我家是电磁炉]`

对**每一个** [] 里的问题：

A. 在 "answers" 数组里给一条独立回答。question 写问题要点，answer 写**具体、
   可执行**的答案 —— 给数字、给时间、给判断标准，不要"视个人口味而定"这种废话。
   微波/水煮这类二选一的问题，**明确推荐一个**并说清为什么、怎么操作。
   where 写这个问题出现在哪（`配料:盐`、`步骤3`、`小贴士`），方便用户对照。

B. **把答案落回它该在的字段**，而不是只写在 answers 里：
   - 问题在某个配料的用量栏 → 把算出来的量填进该配料的 `amount`
     （如 `[请帮我计算出对于500g鸡腿的使用量]` → amount 填 `5g（约1小勺）`），
     并在该配料 `note` 里写一句依据（如 `按肉重1%计`）。
   - 问题在配料备注栏（如"家里没有，怎么代替"）→ note 写替代方案，name 保持原样。
   - 问题在步骤里 → 把答案揉进该步骤的文字。
   - 问题在小贴士里（做法/保存/复热类）→ tips 里正常展开回答。

C. **输出里不许残留方括号和问句**。ingredients 的 name/amount/note、steps 的正文
   都必须是干净的菜谱内容。用户的问题只在 answers 里以 question 形式出现。

D. 用户在问题里给的条件（"对于500g鸡腿"、"我家是电磁炉"、"两个人吃"）是**硬约束**，
   算量、给火候、定份量都要按它来，不要按原文的份量。

════ 用量：推算，绝不留空 ════

1. **用户已经填好的用量是权威，原样保留**。他自己写的 `3瓣蒜`、`半勺糖`，
   哪怕你觉得偏多偏少，也不要改成自己的数 —— 只能在 tips 里提一句建议。
   口语化的量（`两指捏一撮`、`一小把`）**也是用户填的**，同样保留原话，
   只可以在后面补个括号估值：`两指捏一撮（约3g）`，不能整个换成 `3g`。
   例外：该用量栏里写着 [] 提问时，才由你重算。

2. **原文没有用量的，你来推算，不要留空字符串**。这是家常菜谱，一味没有量的料
   等于没写。按 servings（没有就按 2人份）和常规配比给一个**可直接下厨的数**：
   - 给具体数量 + 家用度量：`8g（约1.5小勺）`、`30ml（2汤匙）`、`3瓣`
   - 调味料按主料重量的常规比例算：盐约主料 0.8–1%，生抽约 5–8%，糖提鲜 1–2%
   - 实在只能给模糊量的（香油、白胡椒），写 `少许（约1g）` 这种带锚点的写法，
     不要只写「适量」
   - 推算出来的量，在该配料 `note` 里标注 `推算`，让用户知道这不是原文给的

3. 步骤里的火候、时间、用量照原文保留；原文含糊（"炒熟即可"）时补成
   可判断的描述（"中火炒 2-3 分钟至肉色发白"）。

4. 除用量外的**事实**不要编造：原文没提的食材不要加，没说的产地品牌不要编。

════ 主动给建议 ════

即使用户没问，也在 tips 里补实用提示：商用克数如何缩到家庭份、难买半成品的
家常替代、容易翻车的环节与判断标准、哪些部分能提前备好。
tips 简短分行，控制在 6 行以内。

只输出 JSON，不要 markdown 代码块，不要任何解释。"""

# 与 translate_service 一致的计价口径（平峰，USD→¥ 按 6.8）
_INPUT_PRICE = 0.952
_OUTPUT_PRICE = 1.904


@dataclass
class ConvertResult:
    recipe: Optional[Recipe] = None
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw_reply: str = ""
    warnings: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)   # 模型对用户夹带提问的回答

    @property
    def ok(self) -> bool:
        return self.recipe is not None

    @property
    def cost_yuan(self) -> float:
        return (
            self.input_tokens * _INPUT_PRICE / 1_000_000
            + self.output_tokens * _OUTPUT_PRICE / 1_000_000
        )


def _extract_json(text: str) -> Optional[dict]:
    """从模型回复里取出 JSON 对象（实现见 core.llm_json）。"""
    return extract_json_object(text)


#: 用户夹带提问的标记。方括号是主用法（用户约定），圆括号里带问号的也算 ——
#: 早期用法是「味极鲜（家里没有，怎么代替？）」，不能因为改推方括号就漏掉。
_BRACKET_Q = re.compile(r"[\[［]\s*([^\[\]［］]{2,200}?)\s*[\]］]")
_PAREN_Q = re.compile(r"[(（]\s*([^()（）]{2,200}?[?？])\s*[)）]")


def extract_questions(text: str) -> list[str]:
    """从用户原文里抽出所有夹带的提问。

    用途是**核对模型有没有漏答**：模型一次要干转换+计算+答疑三件事，
    实测问题多的时候会漏掉一两个，而漏答在界面上是静默的 —— 用户以为
    AI 看到了，其实没有。抽出来数一遍，少了就进 warnings。
    """
    out: list[str] = []
    for pat in (_BRACKET_Q, _PAREN_Q):
        for m in pat.finditer(text or ""):
            q = m.group(1).strip()
            if q and q not in out:
                out.append(q)
    return out


def _strip_questions(text: str) -> str:
    """剥掉字段里残留的提问标记。

    提示词已经要求模型不要留，但它偶尔会把整句 `[请帮我算用量]` 原样抄进
    amount —— 那会让配料表里出现一句问句当用量，推到 Kindle 上尤其难看。
    这里兜一道底：只删方括号整体，圆括号不动（正常括注是有用信息）。
    """
    cleaned = _BRACKET_Q.sub("", text or "").strip()
    # 删完可能留下孤零零的标点或空串
    return cleaned.strip("，,、;；:： ") if cleaned else ""


def _parse_answers(raw) -> list[dict]:
    """归一化模型返回的 answers。

    新提示词要 `{question, answer, where}` 的对象，但模型偶尔仍退化成纯字符串
    数组（旧格式），历史数据里也全是字符串 —— 两种都得吃下，统一成 dict。
    """
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, dict):
            answer = str(item.get("answer") or "").strip()
            question = str(item.get("question") or "").strip()
            where = str(item.get("where") or "").strip()
            if not (answer or question):
                continue
            out.append({"question": question, "answer": answer, "where": where})
        else:
            s = str(item or "").strip()
            if s:
                out.append({"question": "", "answer": s, "where": ""})
    return out


def format_answer_line(a: dict) -> str:
    """一条回答 → 落进 tips 的一行文本。"""
    where = f"［{a['where']}］" if a.get("where") else ""
    if a.get("question"):
        return f"❓{where}{a['question']} → {a['answer']}"
    return f"❓{where}{a['answer']}"


def render_doc_for_convert(doc: dict) -> str:
    """把库里的**当前**条目还原成一段菜谱文本，喂给 AI 重转。

    为什么不直接用 `raw_text`：raw_text 是导入时的原始 markdown，**不含用户
    后来的任何编辑**。用它重转等于让模型对着旧稿重写一遍 —— 用户改好的用量
    被退回原样、写在 `[]` 里的提问模型压根看不见。这正是「AI 转换把我存好的
    用量覆盖掉」的根因，不是提示词不够好。

    所以输入以当前 doc 为准，并把字段结构明写出来（`用量:` / `备注:`），
    让模型知道哪个 `[]` 提问挂在哪个字段上、答案该落回哪里。
    """
    parts = [f"菜名：{doc.get('title', '')}"]
    if doc.get("category"):
        parts.append(f"分类：{doc['category']}")
    if doc.get("servings"):
        parts.append(f"份量：{doc['servings']}")
    if doc.get("total_minutes"):
        parts.append(f"耗时：约 {doc['total_minutes']} 分钟")

    if doc.get("ingredients"):
        parts.append("\n【配料】")
        for i in doc["ingredients"]:
            # 用量为空时显式写「未填」，而不是留下 "- 盐  " 这种看不出缺什么的行 ——
            # 模型对「字段存在但空着」的反应比对「字段不存在」积极得多。
            bits = [f"- {i.get('name', '')}", f"用量: {i.get('amount') or '未填'}"]
            if i.get("note"):
                bits.append(f"备注: {i['note']}")
            parts.append("  ".join(bits))

    if doc.get("steps"):
        parts.append("\n【步骤】")
        parts.extend(f"{n}. {s.get('text', '')}" for n, s in enumerate(doc["steps"], 1))

    if doc.get("tips"):
        parts.append(f"\n【小贴士】\n{doc['tips']}")

    return "\n".join(p for p in parts if p.strip())


def _sanitize_tags(values, allowed: list[str]) -> list[str]:
    """只保留候选表里的标签，去重保序。

    模型偶尔仍会冒出「家常」这类自创词；放进去会污染筛选维度，直接丢掉。
    """
    out: list[str] = []
    for v in values or []:
        v = str(v).strip()
        if v in allowed and v not in out:
            out.append(v)
    return out


def build_recipe_from_payload(
    payload: dict,
    raw_text: str = "",
    source: str = "deepseek",
    source_url: str = "",
    vocab: Optional[dict] = None,
) -> tuple[Recipe, list[str], list[dict]]:
    """模型返回的 dict → Recipe（含清洗与兜底）。

    Returns:
        (recipe, warnings, answers)  answers 是 {question, answer, where} 的列表
    """
    warnings: list[str] = []
    vocab = vocab or {}
    allowed_method = vocab.get("method") or METHOD_TAGS
    allowed_flavor = vocab.get("flavor") or FLAVOR_TAGS
    allowed_ingredient = vocab.get("ingredient") or INGREDIENT_TAGS

    title = str(payload.get("title") or "").strip() or "未命名菜谱"

    ingredients: list[Ingredient] = []
    for item in payload.get("ingredients") or []:
        if isinstance(item, str):
            ingredients.append(Ingredient(name=item.strip()))
        elif isinstance(item, dict):
            name = _strip_questions(str(item.get("name") or ""))
            if name:
                ingredients.append(Ingredient(
                    name=name,
                    amount=_strip_questions(str(item.get("amount") or "")),
                    note=_strip_questions(str(item.get("note") or "")),
                ))

    steps: list[RecipeStep] = []
    for item in payload.get("steps") or []:
        text = (item.get("text") if isinstance(item, dict) else item)
        text = _strip_questions(str(text or ""))
        if text:
            steps.append(RecipeStep(order=len(steps) + 1, text=text))

    if not steps:
        warnings.append("模型没有返回任何步骤")

    # 用量留空是最常见的退化：模型一忙着答疑就忘了推算。这是家常菜谱，
    # 一味没有量的料等于没写，所以宁可吵一句也不静默放过。
    blank = [i.name for i in ingredients if not i.amount]
    if blank:
        warnings.append(
            f"{len(blank)} 味配料没给用量（{'、'.join(blank[:4])}"
            f"{'…' if len(blank) > 4 else ''}），建议重转或手填"
        )

    method = _sanitize_tags(payload.get("method_tags"), allowed_method)
    flavor = _sanitize_tags(payload.get("flavor_tags"), allowed_flavor)
    ingredient = _sanitize_tags(payload.get("ingredient_tags"), allowed_ingredient)

    # 模型对用户夹带提问的回答 —— 合进 tips，否则用户看不到
    answers = _parse_answers(payload.get("answers"))

    # 数一遍原文里有几个提问：模型漏答是静默的，用户会以为 AI 看到了。
    asked = extract_questions(raw_text)
    if asked and len(answers) < len(asked):
        warnings.append(f"原文有 {len(asked)} 处提问，模型只回答了 {len(answers)} 条")

    # 模型没给或全被过滤掉时，回退到规则派生 —— 宁可标签糙点也不能空着，
    # 空标签的菜谱在筛选器里等于不存在。
    if not (method and flavor and ingredient):
        derived = derive_tags(
            title=title,
            category=str(payload.get("category") or ""),
            ingredients=[f"{i.amount}{i.name}{i.note}" for i in ingredients],
            steps=[s.text for s in steps],
        )
        if not method:
            method = derived["method"]
            warnings.append("做法标签回退到规则派生")
        if not flavor:
            flavor = derived["flavor"]
            warnings.append("口味标签回退到规则派生")
        if not ingredient:
            ingredient = derived["ingredient"]
            warnings.append("主材标签回退到规则派生")

    minutes = payload.get("total_minutes")
    try:
        minutes = int(minutes) if minutes not in (None, "") else None
    except (TypeError, ValueError):
        minutes = None

    tips = _strip_questions(str(payload.get("tips") or ""))
    if answers:
        # 回答放在最前面：用户是带着问题来的，答案比通用建议更要紧
        answer_block = "\n".join(format_answer_line(a) for a in answers)
        tips = f"{answer_block}\n\n{tips}".strip() if tips else answer_block

    recipe = Recipe(
        slug=make_slug(title, source),
        title=title,
        category=str(payload.get("category") or "").strip(),
        ingredients=ingredients,
        steps=steps,
        method_tags=method,
        flavor_tags=flavor,
        ingredient_tags=ingredient,
        servings=str(payload.get("servings") or "").strip(),
        total_minutes=minutes,
        tips=tips,
        source=source,
        source_url=source_url,
        raw_text=raw_text,
        parse_status="converted",
    )
    return recipe, warnings, answers


async def convert_recipe_text(
    text: str,
    source: str = "deepseek",
    source_url: str = "",
    client: Optional[httpx.AsyncClient] = None,
) -> ConvertResult:
    """把一段自由文本菜谱转成结构化 Recipe（不落库，由调用方决定）。"""
    if not text or not text.strip():
        return ConvertResult(error="输入为空")

    if not settings.deepseek_api_key:
        return ConvertResult(error="DeepSeek API key 未配置（.env 的 deepseek_api_key）")

    # 词表以库为准（用户可增删），动态拼进提示词
    try:
        from app.features.recipe.vocab_repo import tag_vocab_repo
        vocab = tag_vocab_repo.all_groups()
    except Exception:
        # 库不可用时退回代码默认值 —— 转换本身不该因为词表读不到就失败
        vocab = {
            "method": METHOD_TAGS, "flavor": FLAVOR_TAGS,
            "ingredient": INGREDIENT_TAGS, "category": DEFAULT_CATEGORIES,
        }
    system_prompt = _build_system_prompt(
        vocab.get("method") or METHOD_TAGS,
        vocab.get("flavor") or FLAVOR_TAGS,
        vocab.get("ingredient") or INGREDIENT_TAGS,
        vocab.get("category") or DEFAULT_CATEGORIES,
    )

    own_client = client is None
    if own_client:
        # 长菜谱 + 大输出，30s 不够；转换是人工触发的单次操作，等得起
        client = httpx.AsyncClient(timeout=120)

    try:
        resp = await client.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text.strip()[:12000]},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 4000,
                "temperature": 0.2,
            },
        )

        if resp.status_code != 200:
            logger.warning("DeepSeek 转换失败 HTTP %d — %s", resp.status_code, resp.text[:300])
            return ConvertResult(error=f"DeepSeek HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        payload = _extract_json(reply)

        if payload is None:
            return ConvertResult(
                error="模型返回的不是合法 JSON",
                raw_reply=reply[:1000],
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            )

        recipe, warnings, answers = build_recipe_from_payload(
            payload, raw_text=text, source=source, source_url=source_url, vocab=vocab,
        )
        logger.info(
            "转换成功: %s (%d 步, %d 条回答)", recipe.title, len(recipe.steps), len(answers)
        )
        return ConvertResult(
            recipe=recipe,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            raw_reply=reply[:1000],
            warnings=warnings,
            answers=answers,
        )

    except httpx.TimeoutException:
        return ConvertResult(error="DeepSeek 请求超时（120s）")
    except Exception as exc:
        logger.warning("转换异常: %s", exc)
        return ConvertResult(error=f"{type(exc).__name__}: {exc}")
    finally:
        if own_client:
            await client.aclose()
