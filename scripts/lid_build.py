#!/usr/bin/env python3
"""构建「Leben in Deutschland」黑森州题库（一次性运行，产物提交进仓库）。

产物：
    app/features/lid/data/questions.json
    app/features/lid/data/img/*.png

步骤（详细背景见 docs/lid.md）：
    1. 题干与选项：leben-in-deutschland/leben-in-deutschland-app 的 bamf-catalogue.json（MIT），
       URL 钉在 CATALOGUE_SHA。只取通用题 1..300 与 HE-1..HE-10。
    2. 标准答案：BAMF 官方在线工具（Oracle APEX，Playwright 驱动）。正确选项的 radio 带
       id="FARBE"，不用作答就能读。题干是图片，靠选项文字与 PDF 对齐。每题间隔约 1 秒。
    3. 校验：310 题不重不漏；工具第 n 题的 4 个选项按顺序与 PDF 一致；锚点题答案；
       与 abdullahbutt/leben-in-deutschland-test 的答案交叉核对（只比对，不取其内容）。
    4. 图片：13 道图片题，取自 abdullahbutt 仓库 images/（都是官方 PDF 的页面截图）。
    5. 中文翻译与解析：DeepSeek（deepseek-chat，JSON mode），每批约 10 题，逐批写缓存。

用法：
    python scripts/lid_build.py                 # 全量（官方答案有缓存则复用；LLM 全部重跑）
    python scripts/lid_build.py --only-missing  # LLM 只补缓存里缺的题
    python scripts/lid_build.py --refresh-official  # 重新抓官方答案
    python scripts/lid_build.py --skip-llm      # 只跑抓取与校验（不写产物）

中间缓存在 downloads/lid_build/（已被 .gitignore 忽略）。任何一条校验不过 → 报错退出、不写产物。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

OUT_DIR = REPO_ROOT / "app" / "features" / "lid" / "data"
IMG_DIR = OUT_DIR / "img"
CACHE_DIR = REPO_ROOT / "downloads" / "lid_build"

CATALOGUE_REPO = "leben-in-deutschland/leben-in-deutschland-app"
CATALOGUE_SHA = "2d0a200ec5c0746afdbfcf83d41229f6e225b4c5"
CATALOGUE_PATH = "src/catalogue/data/bamf-catalogue.json"
CROSSCHECK_REPO = "abdullahbutt/leben-in-deutschland-test"
CROSSCHECK_SHA = "877812a65d5edae9a8b24e21bce531c32ce65613"

OFFICIAL_URL = "https://oet.bamf.de/ords/oetut/f?p=514:1:0"
HESSEN_BUL_ID = "7"
STATE = "HE"
LLM_MODEL = "deepseek-chat"
BATCH = 10

# 图片题 → abdullahbutt 仓库 images/ 下的文件名。2026-11 逐张目视核对过，与题目一致。
IMAGE_SOURCES = {
    "21": "21.png", "55": "55.png", "70": "70.png", "130": "130.png", "176": "176.png",
    "181": "181.png", "187": "187.png", "209": "209.png", "216": "216.png", "226": "226.png",
    "235": "235.png", "HE-1": "301-hessen.png", "HE-8": "308-hessen.png",
}

# 图片内容的文字描述 —— 只喂给 LLM 写解析用（模型看不到图）。按目视核对的结果写。
IMAGE_HINTS = {
    "21": "Bild 1: schwarzer einköpfiger Adler auf Schild (Bundeswappen); Bild 2: Christusmonogramm (Chi-Rho); "
          "Bild 3: Eisernes Kreuz (Bundeswehr); Bild 4: DDR-Wappen mit Hammer, Zirkel und Ährenkranz.",
    "55": "Foto des Reichstagsgebäudes in Berlin mit Glaskuppel.",
    "70": "Foto: Bundespräsident Gustav Heinemann überreicht Helmut Schmidt 1974 die Ernennungsurkunde zum Bundeskanzler.",
    "130": "Vier Stimmzettel zur Bundestagswahl. 1: je ein Kreuz bei Erststimme (Kandidat X) und Zweitstimme (Partei B); "
           "2: kein Kreuz bei der Erststimme, zwei Kreuze bei der Zweitstimme; 3: zwei Kreuze bei der Erststimme, "
           "keins bei der Zweitstimme; 4: drei Kreuze bei der Erststimme.",
    "176": "Karte Deutschlands mit vier Zonen: 1 Nordwesten, 2 Osten, 3 Süden/Südosten (Bayern, Hessen), 4 Südwesten.",
    "181": "Foto: Willy Brandt kniet 1970 vor dem Denkmal für die Helden des Warschauer Ghettos.",
    "187": "Flagge schwarz-rot-gold mit Hammer, Zirkel und Ährenkranz in der Mitte.",
    "209": "Bild 1: Bundesadler-Wappen; Bild 2: Christusmonogramm (Chi-Rho); Bild 3: Eisernes Kreuz (Bundeswehr); "
           "Bild 4: DDR-Wappen mit Hammer, Zirkel und Ährenkranz.",
    "216": "Foto des Plenarsaals des Bundestages; an der Stirnwand hängt ein großer Adler.",
    "226": "Bild 1: Flagge der USA; Bild 2: Kreis aus zwölf Sternen auf dunklem Grund; Bild 3: Flagge der Vereinten Nationen; "
           "Bild 4: Flagge einer anderen Organisation.",
    "235": "Foto: François Mitterrand und Helmut Kohl halten sich 1984 in Verdun an den Händen.",
    "HE-1": "Bild 1: gestreifter, gekrönter Löwe (Hessen); Bild 2: Bär (Berlin); Bild 3 und 4: Wappen anderer Länder.",
    "HE-8": "Deutschlandkarte, markiert: 1 Nordrhein-Westfalen, 2 Rheinland-Pfalz, 3 Hessen, 4 Thüringen.",
}

ANCHORS = {"1": "hier Meinungsfreiheit gilt.", "14": "meine Meinung im Internet äußern kann."}
EXPECTED_IDS = [str(i) for i in range(1, 301)] + [f"HE-{i}" for i in range(1, 11)]


class BuildError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def ws(s: str) -> str:
    """空白归一：折叠连续空白、去首尾。"""
    return re.sub(r"\s+", " ", s or "").strip()


def loose(s: str) -> str:
    """宽松比较（只用于识别「同一选项」，差异照样进报告）：
    去掉全部空白、句末句点与引号样式差异。工具里的选项常缺句末句点、斜杠后多空格。"""
    s = re.sub(r"\s+", "", s or "")
    s = s.replace("„", '"').replace("“", '"').replace("”", '"').replace("‚", "'").replace("‘", "'").replace("’", "'")
    return s.rstrip(".")


_STOP = {"der", "die", "das", "den", "dem", "des", "von", "vom", "zum", "zur", "und", "eine", "ein"}


def gender_bag(s: str) -> frozenset[str]:
    """「性别词序 / 冠词」归一后的词集合。工具里写「Einwohner / Einwohnerinnen」，PDF 写
    「Einwohnerinnen/Einwohner」；工具写「vom Bundespräsidenten / von der …」，PDF 反过来。"""
    out = set()
    for w in re.findall(r"[0-9a-zäöüßáéí]+", s.lower()):
        if len(w) > 5:
            w = re.sub(r"(innen|in)$", "", w)
        w = w.replace("ss", "s").replace("í", "i")
        if w not in _STOP:
            out.add(w)
    return frozenset(out)


# 工具与 PDF 措辞真不一样、但确属同一选项的地方（2026-11 逐条人工核对）。
# {(题号, 选项下标): 工具里的原文}。新出现的差异不在这里 → 构建失败，人工看过再加。
KNOWN_VARIANTS = {
    ("23", 2): "selbständig mit einer eigenen Firma tätig.",                  # 旧正字法
    ("35", 0): "Kirchensteuern",
    ("45", 2): "Hausratversicherung",
    ("135", 2): "Selbständige",
    ("138", 0): "weiter arbeiten und freundlich zum Chef / zur Chefin sein",
    ("231", 0): "Damit sind amerikanische Einwanderer in Europa gemeint.",    # 工具只有阳性
    ("231", 2): "Damit sind europäische Auswanderer in den USA gemeint.",
}


def is_image_option(s: str) -> bool:
    return re.fullmatch(r"(Bild )?[1-4]", ws(s)) is not None


# ── 1. 下载开源数据（钉 commit） ──


def fetch(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"  下载 {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "myspiders-lid-build"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest


def raw_url(repo: str, sha: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"


def load_catalogue() -> dict[str, dict]:
    p = fetch(raw_url(CATALOGUE_REPO, CATALOGUE_SHA, CATALOGUE_PATH), CACHE_DIR / f"catalogue-{CATALOGUE_SHA[:12]}.json")
    data = json.loads(p.read_text("utf-8"))
    stand = data.get("source", {}).get("stand")
    if stand != "07.05.2025":
        raise BuildError(f"题库 Stand 不是 07.05.2025：{stand!r}（PDF 换版了？先人工核对再改脚本）")
    out: dict[str, dict] = {}
    for q in data["questions"]:
        num = q["num"]
        if q["part"] == "general" or (q.get("stateCode") == STATE and num.startswith(f"{STATE}-")):
            if num in out:
                raise BuildError(f"题库里 {num} 重复")
            out[num] = q
    return out


def load_crosscheck() -> dict[str, int]:
    """abdullahbutt 的答案 {id: 官方顺序下标}。只用于比对，内容不进产物。"""
    p = fetch(raw_url(CROSSCHECK_REPO, CROSSCHECK_SHA, "quiz-data.json"), CACHE_DIR / f"crosscheck-{CROSSCHECK_SHA[:12]}.json")
    data = json.loads(p.read_text("utf-8"))
    out: dict[str, tuple[int, list[str]]] = {}
    for q in data["general"]:
        out[str(q["id"])] = (q["correct"], [o["de"] for o in q["options"]])
    for q in data["states"]["hessen"]["questions"]:
        m = re.fullmatch(r"hessen-(\d+)", str(q["id"]))
        if not m:
            raise BuildError(f"交叉核对数据里有意外的 id：{q['id']}")
        out[f"HE-{int(m.group(1)) - 300}"] = (q["correct"], [o["de"] for o in q["options"]])
    return out  # type: ignore[return-value]


# ── 2. 官方答案（Playwright） ──

_READ_JS = """() => {
  const head = document.body.innerText.match(/Aufgabe\\s+(\\d+)\\s+von\\s+(\\d+)/);
  const rows = [...document.querySelectorAll('input[type=radio][name=f20]')].map(r => {
    const tr = r.closest('tr');
    const a = tr ? tr.querySelector('td[headers=ANTWORT]') : null;
    return {farbe: r.id === 'FARBE', text: a ? a.innerText : ''};
  });
  return {n: head ? +head[1] : null, total: head ? +head[2] : null, rows};
}"""


async def _scrape(numbers: list[int], cache: dict, cache_path: Path) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(OFFICIAL_URL, wait_until="networkidle")
        await page.select_option("select#P1_BUL_ID", HESSEN_BUL_ID)
        await page.wait_for_load_state("networkidle")
        await page.click('input[value="Zum Fragenkatalog"]')
        await page.wait_for_selector("select#P30_ROWNUM")
        await page.wait_for_load_state("networkidle")
        for i, n in enumerate(numbers):
            for attempt in range(3):
                try:
                    async with page.expect_navigation(wait_until="networkidle", timeout=30000):
                        await page.select_option("select#P30_ROWNUM", label=str(n))
                    got = await page.evaluate(_READ_JS)
                    if got["n"] != n:
                        raise BuildError(f"选了第 {n} 题，页面显示的是第 {got['n']} 题")
                    if got["total"] != 310:
                        raise BuildError(f"工具题目总数是 {got['total']}，不是 310（州没选对？）")
                    break
                except BuildError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    log(f"  第 {n} 题读取失败（{type(exc).__name__}），5 秒后重试")
                    await asyncio.sleep(5)
            rows = got["rows"]
            farbe = [k for k, r in enumerate(rows) if r["farbe"]]
            if len(rows) != 4 or len(farbe) != 1:
                raise BuildError(f"第 {n} 题：{len(rows)} 个选项、{len(farbe)} 个 FARBE")
            cache[str(n)] = {"answer": farbe[0], "options": [ws(r["text"]) for r in rows],
                             "answer_text": ws(rows[farbe[0]]["text"])}
            if (i + 1) % 10 == 0 or i == len(numbers) - 1:
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), "utf-8")
                log(f"  官方答案 {len(cache)}/310")
            await asyncio.sleep(1.0)   # 别给官方服务器压力
        await browser.close()


def load_official(refresh: bool) -> dict[str, dict]:
    path = CACHE_DIR / "official.json"
    cache: dict = {} if refresh or not path.exists() else json.loads(path.read_text("utf-8"))
    missing = [n for n in range(1, 311) if str(n) not in cache]
    if missing:
        log(f"▶ 抓官方答案：{len(missing)} 题（约 {len(missing) * 2.5 / 60:.0f} 分钟）")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        asyncio.run(_scrape(missing, cache, path))
    return cache


# ── 3. 对齐与校验 ──


def tool_no(qid: str) -> int:
    return 300 + int(qid[3:]) if qid.startswith("HE-") else int(qid)


def align(catalogue: dict, official: dict) -> tuple[dict[str, int], dict[str, list[str]]]:
    """返回 ({id: 官方答案下标}, {差异类别: [说明]})。

    工具与 PDF 的选项文字有三类容忍的差异（都要求位置一一对应）：
      punct  —— 只差空白、句末句点、引号样式（工具普遍不带句点）；
      gender —— 性别词序与冠词（「Einwohner / Einwohnerinnen」vs「Einwohnerinnen/Einwohner」），
                且该词集合在本题四个选项里唯一；
      known  —— KNOWN_VARIANTS 里人工核对过的措辞差异（逐字匹配）。
    其余一律报错。"""
    ids = sorted(catalogue, key=lambda x: EXPECTED_IDS.index(x) if x in EXPECTED_IDS else 9999)
    if ids != EXPECTED_IDS:
        extra = set(ids) - set(EXPECTED_IDS)
        lack = set(EXPECTED_IDS) - set(ids)
        raise BuildError(f"题目 id 不对：多 {sorted(extra)} 缺 {sorted(lack)}")
    answers: dict[str, int] = {}
    notes: dict[str, list[str]] = {"punct": [], "gender": [], "known": []}
    for qid in EXPECTED_IDS:
        q, o = catalogue[qid], official.get(str(tool_no(qid)))
        if not o:
            raise BuildError(f"{qid}：没有官方答案")
        pdf_opts = [ws(a) for a in q["answers"]]
        if len(pdf_opts) != 4:
            raise BuildError(f"{qid}：PDF 选项不是 4 个")
        if all(is_image_option(a) for a in pdf_opts) and all(is_image_option(a) for a in o["options"]):
            if not q.get("images"):
                raise BuildError(f"{qid}：选项是「Bild n」却没有图片")
            if [re.sub(r"\D", "", a) for a in pdf_opts] != [re.sub(r"\D", "", a) for a in o["options"]]:
                raise BuildError(f"{qid}：图片选项编号顺序不一致 {pdf_opts} vs {o['options']}")
        else:
            bags = [gender_bag(a) for a in pdf_opts]
            for k, (a, b) in enumerate(zip(pdf_opts, o["options"])):
                if a == b:
                    continue
                if loose(a) == loose(b):
                    notes["punct"].append(f"{qid} 选项{k + 1}：PDF「{a}」/ 工具「{b}」")
                    continue
                gb = gender_bag(b)
                if gb == bags[k] and bags.count(gb) == 1:
                    notes["gender"].append(f"{qid} 选项{k + 1}：PDF「{a}」/ 工具「{b}」")
                    continue
                known = KNOWN_VARIANTS.get((qid, k))
                if known is not None and ws(known) == b:
                    notes["known"].append(f"{qid} 选项{k + 1}：PDF「{a}」/ 工具「{b}」")
                    continue
                raise BuildError(f"{qid}（工具第 {tool_no(qid)} 题）选项 {k + 1} 对不上：\n  PDF : {a}\n  工具: {b}")
        if o["answer"] not in (0, 1, 2, 3):
            raise BuildError(f"{qid}：官方答案下标 {o['answer']}")
        answers[qid] = o["answer"]
    for qid, text in ANCHORS.items():
        got = ws(catalogue[qid]["answers"][answers[qid]])
        if got != text:
            raise BuildError(f"锚点失败：第 {qid} 题答案应为「{text}」，实际「{got}」")
    return answers, notes


def map_option(pdf_opts: list[str], text: str) -> int | None:
    """把开源数据里的一个选项文字换算成官方顺序下标。

    abdullahbutt 有些题把选项打乱了（如 57、82、247、282、291），图片题的「1..4」也是乱序
    （226 存的是 ['2','1','4','3']），所以只能按内容找：图片题按数字，文字题按归一后的词集合
    取最相近且唯一的那个。"""
    if is_image_option(text):
        d = re.sub(r"\D", "", text)
        hits = [i for i, a in enumerate(pdf_opts) if re.sub(r"\D", "", a) == d]
        return hits[0] if len(hits) == 1 else None
    exact = [i for i, a in enumerate(pdf_opts) if loose(a) == loose(text)]
    if len(exact) == 1:
        return exact[0]
    t = gender_bag(text)
    # 重合度用「较小集合被覆盖的比例」：247 的开源选项多了括号注释「Mutterschutz (8 Wochen …)」
    scores = [len(t & gender_bag(a)) / max(1, min(len(t), len(gender_bag(a)))) for a in pdf_opts]
    best = max(scores)
    if best < 0.5 or scores.count(best) > 1:
        return None
    return scores.index(best)


def crosscheck(catalogue: dict, answers: dict[str, int], other: dict) -> list[dict]:
    diffs = []
    for qid in EXPECTED_IDS:
        if qid not in other:
            diffs.append({"id": qid, "official": answers[qid], "other": None, "note": "开源数据缺这题"})
            continue
        idx, opts = other[qid]
        mapped = map_option(catalogue[qid]["answers"], opts[idx]) if 0 <= idx < len(opts) else None
        if mapped is None:
            diffs.append({"id": qid, "official": answers[qid], "other": None, "note": f"对不上开源选项「{opts[idx]}」"})
            continue
        if mapped != answers[qid]:
            diffs.append({"id": qid, "official": answers[qid], "other": mapped,
                          "official_text": ws(catalogue[qid]["answers"][answers[qid]]),
                          "other_text": ws(catalogue[qid]["answers"][mapped]) if 0 <= mapped < 4 else None})
    return diffs


# ── 4. 图片 ──


def build_images() -> dict[str, str]:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for qid, src in IMAGE_SOURCES.items():
        cached = fetch(raw_url(CROSSCHECK_REPO, CROSSCHECK_SHA, f"images/{src}"), CACHE_DIR / "img" / src)
        data = cached.read_bytes()
        if not data.startswith(b"\x89PNG"):
            raise BuildError(f"{src} 不是 PNG")
        out[qid] = f"{qid}.png"
    return out


def write_images(names: dict[str, str]) -> None:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    for old in IMG_DIR.glob("*.png"):
        if old.name not in names.values():
            old.unlink()
    for qid, name in names.items():
        (IMG_DIR / name).write_bytes((CACHE_DIR / "img" / IMAGE_SOURCES[qid]).read_bytes())


# ── 5. 中文翻译与解析（DeepSeek） ──

SYSTEM_PROMPT = """Du bereitest Lernmaterial für den deutschen Einbürgerungstest („Leben in Deutschland“, Bundesland Hessen) vor.
Der Lernende spricht Deutsch auf C1-Niveau, seine Muttersprache ist Chinesisch.

Du bekommst einen JSON-Block mit Fragen. Jede Frage hat: id, question (Fragetext), options (4 Antworten in
offizieller Reihenfolge, Index 0–3), answer (Index der OFFIZIELL richtigen Antwort — diese ist verbindlich,
auch wenn du anderer Meinung bist), optional image_hint (Beschreibung eines Bildes, das du nicht siehst).

Gib ein JSON-Objekt zurück: {"items": [ ... ]} mit genau einem Eintrag pro Frage, gleiche Reihenfolge:
{
  "id": "<id unverändert>",
  "answer": <denselben answer-Index unverändert zurückgeben>,
  "zh": {"q": "<Fragetext auf Chinesisch>", "options": ["<Option 1 chinesisch>", "<2>", "<3>", "<4>"]},
  "exp": {"de": "<Erklärung Deutsch>", "zh": "<dieselbe Erklärung auf Chinesisch>"}
}

Regeln:
- zh.q / zh.options: treue, natürliche Übersetzung ins vereinfachte Chinesisch; Reihenfolge der Optionen exakt
  wie im Original. „Bild 1“ → „图 1“, reine Ziffern bleiben Ziffern. Fachbegriffe beim ersten Vorkommen gern mit
  dem deutschen Begriff in Klammern, z. B. 联邦议院（Bundestag）.
- exp.de: 1–2 Sätze, die erklären, WARUM die offizielle Antwort stimmt — möglichst an einem konkreten Wissensanker
  festgemacht (Artikel des Grundgesetzes, Jahreszahl, Aufgabe einer Institution, Merkregel). NICHT schreiben
  „Die richtige Antwort ist …“ und die Antwort nicht bloß wiederholen. Die Erklärung muss zur offiziellen
  Antwort passen und darf ihr nie widersprechen.
- exp.zh: inhaltlich gleich wie exp.de, auf Chinesisch.
- Bei Fragen zu aktuellen Amtsinhabern oder aktuellen Zahlen (Bundeskanzler/in, Bundespräsident/in,
  Ministerpräsident/in, Landesregierung, Parteien in Regierung usw.) im exp.de den Satz ergänzen
  „Maßgeblich ist der Stand zum Prüfungszeitpunkt.“ und im exp.zh „以考试时的现任为准。“
- Keine Markdown-Formatierung, keine Zeilenumbrüche in den Strings.
"""


def _llm_input(q: dict, answer: int) -> dict:
    item = {"id": q["num"], "question": ws(q["question"]), "options": [ws(a) for a in q["answers"]], "answer": answer}
    if q["num"] in IMAGE_HINTS:
        item["image_hint"] = IMAGE_HINTS[q["num"]]
    return item


_BAD_EXP = re.compile(r"^\s*(die\s+)?richtige\s+antwort\s+(ist|lautet)", re.I)


def validate_llm_item(item: dict, expected_id: str, answer: int) -> str | None:
    """返回错误描述；合格返回 None。"""
    if not isinstance(item, dict) or str(item.get("id")) != expected_id:
        return f"id 不符（{item.get('id') if isinstance(item, dict) else item!r}）"
    if item.get("answer") != answer:
        return f"answer 回显 {item.get('answer')} ≠ 官方 {answer}"
    zh, exp = item.get("zh") or {}, item.get("exp") or {}
    if not isinstance(zh.get("q"), str) or not zh["q"].strip():
        return "zh.q 为空"
    opts = zh.get("options")
    if not isinstance(opts, list) or len(opts) != 4 or not all(isinstance(o, str) and o.strip() for o in opts):
        return "zh.options 不是 4 个非空字符串"
    for k in ("de", "zh"):
        if not isinstance(exp.get(k), str) or len(exp[k].strip()) < 8:
            return f"exp.{k} 为空或太短"
    if _BAD_EXP.match(exp["de"]):
        return "exp.de 只是「Die richtige Antwort ist …」"
    return None


def _call_llm(client, batch: list[dict]) -> list[dict]:
    from app.core.llm_json import extract_json_object
    from app.core.settings import settings

    resp = client.post(
        f"{settings.deepseek_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}", "Content-Type": "application/json"},
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"questions": batch}, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 8000,
            "temperature": 0.2,
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"DeepSeek HTTP {resp.status_code}: {resp.text[:200]}")
    payload = extract_json_object(resp.json()["choices"][0]["message"]["content"])
    if payload is None or not isinstance(payload.get("items"), list):
        raise RuntimeError("回复不是 {items: [...]}")
    return payload["items"]


def run_llm(catalogue: dict, answers: dict[str, int], only_missing: bool, stats: dict) -> dict[str, dict]:
    import httpx

    from app.core.settings import settings

    path = CACHE_DIR / "llm.json"
    cache: dict = json.loads(path.read_text("utf-8")) if path.exists() else {}
    # 缓存里答案和当前官方答案不一致的条目作废（官方改答案时解析必须重写）
    cache = {k: v for k, v in cache.items() if k in answers and v.get("answer") == answers[k]}
    todo = [qid for qid in EXPECTED_IDS if not (only_missing and qid in cache)]
    if not todo:
        return cache
    if not settings.deepseek_api_key:
        raise BuildError("deepseek_api_key 未配置（.env）")
    log(f"▶ DeepSeek 翻译与解析：{len(todo)} 题，每批 {BATCH}")

    def save():
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), "utf-8")

    with httpx.Client(timeout=180) as client:
        queue = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
        while queue:
            ids = queue.pop(0)
            batch = [_llm_input(catalogue[q], answers[q]) for q in ids]
            ok: dict[str, dict] = {}
            err = ""
            for attempt in range(3):
                try:
                    items = _call_llm(client, batch)
                    by_id = {str(it.get("id")): it for it in items if isinstance(it, dict)}
                    for q in ids:
                        if q in ok:
                            continue
                        e = validate_llm_item(by_id.get(q, {"id": None}), q, answers[q])
                        if e is None:
                            ok[q] = by_id[q]
                        else:
                            err = f"{q}: {e}"
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                if len(ok) == len(ids):
                    break
                stats["llm_retries"] += 1
                log(f"  批 {ids[0]}..{ids[-1]} 第 {attempt + 1} 次不完整（{err}），重试缺的 {len(ids) - len(ok)} 题")
                batch = [_llm_input(catalogue[q], answers[q]) for q in ids if q not in ok]
                time.sleep(2)
            for q, it in ok.items():
                cache[q] = {"answer": answers[q], "zh": {"q": ws(it["zh"]["q"]), "options": [ws(o) for o in it["zh"]["options"]]},
                            "exp": {"de": ws(it["exp"]["de"]), "zh": ws(it["exp"]["zh"])}}
            save()
            failed = [q for q in ids if q not in ok]
            if failed:
                if len(ids) == 1:
                    raise BuildError(f"{ids[0]} 连续 3 次 LLM 输出不合格：{err}")
                stats["llm_splits"] += 1
                queue = [[q] for q in failed] + queue   # 拆成单题再试
            log(f"  LLM 进度 {sum(1 for q in EXPECTED_IDS if q in cache)}/310")
    return cache


# ── 6. 组装 ──


def assemble(catalogue: dict, answers: dict, llm: dict, images: dict) -> dict:
    questions = []
    for qid in EXPECTED_IDS:
        q, t = catalogue[qid], llm.get(qid)
        if not t:
            raise BuildError(f"{qid} 缺中文/解析（用 --only-missing 补）")
        questions.append({
            "id": qid,
            "n": tool_no(qid),
            "part": "general" if q["part"] == "general" else "state",
            "de": {"q": ws(q["question"]), "options": [ws(a) for a in q["answers"]]},
            "zh": t["zh"],
            "answer": answers[qid],
            "exp": t["exp"],
            "images": [images[qid]] if qid in images else [],
            "credit": q.get("credit"),
        })
    bank = {
        "meta": {
            "stand": "07.05.2025",
            "state": STATE,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": {
                "catalogue": f"{CATALOGUE_REPO}@{CATALOGUE_SHA}",
                "answers": "oet.bamf.de",
                "crosscheck": f"{CROSSCHECK_REPO}@{CROSSCHECK_SHA}",
                "images": f"{CROSSCHECK_REPO}@{CROSSCHECK_SHA} images/ (Ausschnitte aus dem BAMF-Gesamtfragenkatalog)",
            },
            "llm": LLM_MODEL,
        },
        "questions": questions,
    }
    final_check(bank)
    return bank


def final_check(bank: dict) -> None:
    qs = bank["questions"]
    if [q["id"] for q in qs] != EXPECTED_IDS:
        raise BuildError("产物题号序列不对")
    for q in qs:
        if q["answer"] not in (0, 1, 2, 3):
            raise BuildError(f"{q['id']} answer 越界")
        if len(q["de"]["options"]) != 4 or len(q["zh"]["options"]) != 4:
            raise BuildError(f"{q['id']} 选项不是 4 个")
        if not all(s for s in [q["de"]["q"], q["zh"]["q"], q["exp"]["de"], q["exp"]["zh"], *q["zh"]["options"]]):
            raise BuildError(f"{q['id']} 有空字段")
    for qid, text in ANCHORS.items():
        q = next(x for x in qs if x["id"] == qid)
        if q["de"]["options"][q["answer"]] != text:
            raise BuildError(f"锚点 {qid} 不对")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only-missing", action="store_true", help="LLM 只补缓存里缺的题")
    ap.add_argument("--refresh-official", action="store_true", help="忽略缓存，重新抓官方答案")
    ap.add_argument("--skip-llm", action="store_true", help="只抓取与校验，不调 LLM、不写产物")
    args = ap.parse_args()
    stats = {"llm_retries": 0, "llm_splits": 0}
    try:
        log("▶ 题库（MIT）与交叉核对数据")
        catalogue = load_catalogue()
        other = load_crosscheck()
        official = load_official(args.refresh_official)
        log("▶ 对齐校验")
        answers, notes = align(catalogue, official)
        diffs = crosscheck(catalogue, answers, other)
        images = build_images()
        if args.skip_llm:
            llm = {}
        else:
            llm = run_llm(catalogue, answers, args.only_missing, stats)
            bank = assemble(catalogue, answers, llm, images)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            tmp = OUT_DIR / "questions.json.part"
            tmp.write_text(json.dumps(bank, ensure_ascii=False, indent=1) + "\n", "utf-8")
            write_images(images)
            tmp.replace(OUT_DIR / "questions.json")
    except BuildError as exc:
        log(f"\n✗ 构建失败：{exc}")
        return 1

    log("\n══════ 构建报告 ══════")
    log(f"题数：{len(answers)}（通用 300 + HE 10）")
    log(f"图片：{len(images)} 道题 / {len(images)} 个文件")
    log(f"工具与 PDF 的选项文字差异（已容忍，位置一一对应）：句点/空白 {len(notes['punct'])} 处、"
        f"性别词序 {len(notes['gender'])} 处、人工核对过的措辞 {len(notes['known'])} 处")
    for n in notes["known"]:
        log(f"  {n}")
    log(f"与开源答案（{CROSSCHECK_REPO}）不一致：{len(diffs)} 题（以官方为准）")
    for d in diffs:
        if d["other"] is None:
            log(f"  {d['id']}: {d['note']}")
        else:
            log(f"  {d['id']}: 官方「{d['official_text']}」 / 开源「{d['other_text']}」")
    log(f"LLM：重试 {stats['llm_retries']} 次，拆单题 {stats['llm_splits']} 次")
    if args.skip_llm:
        log("（--skip-llm：未写产物）")
    else:
        log(f"产物：{(OUT_DIR / 'questions.json').relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
