"""AI 教学内容标注的质量评分。

设计前提（很重要）：评分必须**确定性、可重复、不靠模型自评**。
如果让模型给自己打分，那"质量提升"就只是模型换了个说法而已，无法证伪。
所以七个维度全部基于可比对的客观事实：

- 是否真的出现在 transcript 里（用 n-gram 覆盖判定 → 幻觉/接地）
- 是否落在人工标注的参考答案里（topic / CEFR / learning_value）
- 是否命中了真正值得教的表达（对照 must_expressions）
- 是否提了明确不该提的东西（普通词、噪声、占位文字 → forbidden）

分数统一落在 0–1，越高越好。报告里会把这个方法讲清楚，
避免把"启发式评分"当成绝对真理。
"""

import json
import re
from pathlib import Path

# 仓库根 = benchmark.py 往上第三层（content_factory → TikTokBatchMVP → outputs → 仓库根）
FIXTURE_DIR = (Path(__file__).resolve().parents[3]
               / "tests" / "fixtures" / "ai_benchmark")
ITEMS_FILE = "items.json"

SCORE_FIELDS = (
    "topic_accuracy",
    "cefr_quality",
    "expression_quality",
    "grammar_quality",
    "key_sentence_quality",
    "learning_value_quality",
    "hallucination",
)

# 不参与总分、但解释"为什么是这个分"的辅助指标。
# 光看 expression_quality 一个数字，说不清"是提得更准了，还是只是提得更多"。
EXTRA_METRICS = {
    "expression_precision": "提出的表达里真正值得教的比例",
    "expression_recall": "人工标注的必提表达被提到了多少",
    "grammar_filler_ratio": "语法点里纯术语凑数的比例（越低越好）",
}

# 幻觉维度是"越大越好"：1.0 = 全部有出处，0.0 = 全在编
SCORE_LABELS = {
    "topic_accuracy": "主题判断准确度",
    "cefr_quality": "CEFR 难度合理性",
    "expression_quality": "重点表达质量",
    "grammar_quality": "语法点质量",
    "key_sentence_quality": "重点句质量",
    "learning_value_quality": "学习价值打分准确度",
    "hallucination": "没有编造、有据可依",
}

# 判定"普通词/无教学价值"用的高频词表。
# 不追求语言学上的完备 —— 目的是拦住实测里真实出现过的那类提取结果
# （work / thing / people / good / very 这种），而不是做词性标注。
COMMON_WORDS = {
    "a", "an", "the", "and", "or", "but", "if", "of", "to", "in", "on", "at", "for", "with",
    "from", "by", "as", "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
    "did", "have", "has", "had", "will", "would", "can", "could", "should", "may", "might",
    "must", "this", "that", "these", "those", "it", "its", "he", "she", "they", "them", "we",
    "you", "i", "my", "your", "his", "her", "our", "their", "me", "us", "so", "very", "really",
    "just", "also", "too", "not", "no", "yes", "okay", "ok", "well", "now", "then", "here",
    "there", "what", "which", "who", "when", "where", "why", "how", "all", "any", "some",
    "more", "most", "much", "many", "one", "two", "three", "thing", "things", "people",
    "person", "time", "times", "day", "days", "way", "ways", "work", "works", "working",
    "good", "bad", "big", "small", "new", "old", "get", "got", "getting", "make", "made",
    "making", "take", "took", "taking", "go", "goes", "going", "went", "come", "came",
    "coming", "see", "saw", "seeing", "know", "knew", "knowing", "think", "thought", "want",
    "wanted", "need", "needed", "use", "used", "using", "say", "said", "saying", "tell",
    "told", "look", "looked", "lot", "lots", "kind", "sort", "stuff", "like", "actually",
    "basically", "honestly", "literally", "maybe", "probably", "about", "into", "over",
    "after", "before", "because", "than", "only", "even", "still", "back", "down", "up",
    "out", "off", "again", "around", "every", "each", "other", "another", "same", "own",
    "video", "content", "channel", "subscribe", "guys", "today",
    # 话题相关但同样没有教学价值的常见名词（实测里"model"被当成重点表达提出来过）
    "model", "models", "ai", "app", "phone", "company", "business", "market", "product",
    "customer", "customers", "team", "project", "idea", "problem", "question", "answer",
    "story", "movie", "music", "food", "money", "job", "school", "student", "teacher",
}

# 字幕噪声 / 占位文字：实测里真的被当成"地道表达"提取出来过
NOISE_PATTERN = re.compile(
    r"^\s*[\[(（【].*?[\])）】]\s*$"          # [Music] (laughs) 【音乐】
    r"|demo transcript|placeholder|字幕|示例文本|lorem ipsum",
    re.I)

# 教科书式、讲了等于没讲的"语法点"
GENERIC_GRAMMAR = {
    "一般现在时", "一般过去时", "一般将来时", "现在进行时", "现在完成时", "过去式",
    "主谓宾", "主系表", "简单句", "陈述句", "疑问句", "祈使句", "名词单复数",
    "simple present", "simple past", "present tense", "past tense", "sentence structure",
}

# 只在特定领域成立、换个场景用不上的词（可迁移价值低）
DOMAIN_TERMS = {
    "pipeline", "pipelines", "latency", "feature", "features", "deploy", "deployment",
    "weights", "embedding", "embeddings", "fine-tuning", "token", "tokens", "prompt",
    "inference", "runtime", "framework", "database", "schema", "endpoint", "api",
    "runway", "seed", "churn", "funnel", "conversion", "retention", "synergy",
    "headset", "metaverse", "algorithm", "backend", "frontend", "repository", "commit",
}

# 表达里的"可迁移信号"：出现这些词/结构，说明它更像固定搭配而不是随手拼的几个词
COLLOCATION_MARKERS = (
    "take", "get", "give", "make", "come", "go", "put", "turn", "bring", "break", "break",
    "carry", "hold", "keep", "let", "look", "pick", "point", "pull", "run", "set", "stand",
    "work", "end", "figure", "fill", "find", "move", "pass", "play", "show", "sort", "stick",
    "wear", "wrap", "write", "back", "off", "out", "up", "down", "through", "around", "over",
    "into", "away", "along", "apart", "aside", "by", "for", "in", "on", "to", "with",
)

DISCOURSE_MARKERS = (
    "to be fair", "here's the thing", "here is the thing", "that said", "on the other hand",
    "by the way", "you know", "i mean", "as it usually does", "at the end of the day",
    "rule of thumb", "nine times out of ten", "more often than not", "in other words",
    "having said that", "the thing is", "one more thing", "that is why", "so to speak",
    "when it comes to", "as far as", "if anything", "let alone", "much less", "not to mention",
)


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------
def load_benchmark(path=None):
    """读取固定 Benchmark。纯本地文件，不需要网络。"""
    target = Path(path) if path else Path(ITEMS_FILE)
    if not target.is_absolute():
        target = FIXTURE_DIR / target
    if not target.is_file():
        raise FileNotFoundError(f"Benchmark 数据不存在：{target}")
    data = json.loads(target.read_text(encoding="utf-8"))
    items = data.get("items") or []
    if not items:
        raise ValueError(f"Benchmark 数据为空：{target}")
    seen = set()
    for item in items:
        for field in ("id", "title", "transcript", "expect"):
            if not item.get(field):
                raise ValueError(f"Benchmark 条目缺少字段 {field}：{item.get('id') or item}")
        if item["id"] in seen:
            raise ValueError(f"Benchmark id 重复：{item['id']}")
        seen.add(item["id"])
    return {"items": items, "path": str(target), "about": data.get("_about", "")}


def item_inputs(item):
    """把 fixture 条目转成管线需要的输入 dict。"""
    return {
        "id": item["id"],
        "title": item["title"],
        "creator_handle": item.get("creator", ""),
        "description": item.get("description", ""),
        "duration": item.get("duration", 0),
    }


# --------------------------------------------------------------------------
# 接地判定（幻觉的核心）
# --------------------------------------------------------------------------
def _normalise(text):
    """归一化文本用于比对。

    **必须同时保留中文与英文**：topic、subtopic、grammar_points 都是中文，
    而 expressions / key_sentences 是英文。早先这里的字符集写成 `a-z0-9'`，
    结果所有中文串都被压成空字符串 —— 于是"主题是否准确""语法点是否教科书式"
    这两类判断全部静默失效，测试却全绿（因为空串 vs 空串比的是"不等"，
    在只验证"垃圾分低"的地方看不出来）。自检脚本正是靠主题满分才暴露了它。
    """
    text = str(text or "").lower()
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    # 保留：字母数字、下划线、连字符、单引号、CJK 统一表意文字
    text = re.sub(r"[^\w\-'\u4e00-\u9fff]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text):
    return [tok for tok in _normalise(text).split() if tok]


def transcript_ngrams(transcript, max_n=5):
    """transcript 的所有 1..n 元组，用来判断一句话是否真的出自原文。"""
    tokens = _tokens(transcript)
    grams = set()
    for size in range(1, max_n + 1):
        for index in range(len(tokens) - size + 1):
            grams.add(tuple(tokens[index:index + size]))
    return grams


def coverage(fragment, grams, transcript_tokens):
    """fragment 里有多少比例的连续片段能在 transcript 中找到出处（0–1）。

    用最长匹配：从每个位置开始，尝试最长的、出现在 transcript 里的连续片段，
    命中就跳过这一段。这样既容忍插入语、也容忍轻微改写，
    但整句编造（比如把中文翻译回译成英文）会立刻掉到很低。
    """
    tokens = _tokens(fragment)
    if not tokens:
        return 0.0
    if len(tokens) == 1:
        return 1.0 if tuple(tokens) in grams else 0.0
    matched = 0
    index = 0
    while index < len(tokens):
        hit = 0
        for size in range(min(3, len(tokens) - index), 0, -1):
            if tuple(tokens[index:index + size]) in grams:
                hit = size
                break
        if hit:
            matched += hit
            index += hit
        else:
            index += 1
    return matched / len(tokens)


def is_noise(text):
    return bool(NOISE_PATTERN.search(str(text or "")))


def is_plain_word(text):
    """只有一个词，且是高频普通词 → 没有教学价值。"""
    tokens = _tokens(text)
    return len(tokens) == 1 and tokens[0] in COMMON_WORDS


def is_domain_restricted(text):
    """只在这个领域成立、换个场景就用不上的表达 → 可迁移价值低。

    实测对比里出现过这类：
        A(v1) 提 "feature pipelines"（特征管线）—— 除了 ML 场景没人这么说
        B(v2) 提 "half the time" / "the easy part" —— 任何口语场景都能用
    不区分这两者，两版就会被打成同一个分数，"更值得学"就体现不出来。
    """
    tokens = [token for token in _tokens(text) if token not in COMMON_WORDS]
    if not tokens:
        return False
    hard = sum(1 for token in tokens if token in DOMAIN_TERMS)
    return hard > 0 and len(tokens) <= 3


def is_worth_teaching(text):
    """这条表达是否值得教。

    判定顺序（前四条是"不合格"，最后一条是"合格的样子"）：
    1. 空 / 噪声（[Music]、(demo transcript) 这类）
    2. 单个普通词（work、thing、people…）
    3. 只在本领域成立、换个场景用不上（feature pipelines、latency budgets）
    4. 只有一个词且不是话语标记（单独一个 "cave" 不行，"cave in" 行）
    5. 其余算值得教：多词短语、短语动词、固定搭配、话语标记都落在这里

    **这里刻意不要求"两个以上内容词"**：早先那样写会把 to be fair / out of business /
    lead with / cave in / get rid of / See you around / rather than 这类
    高频短语动词与话语标记全判成"内容词不足" —— 而那恰恰是 Prompt v2 要求优先提取的东西。
    结果就是"越按新要求提，分越低"，评分器和优化方向互相打架（实测踩过）。
    """
    text = str(text or "").strip()
    if not text or is_noise(text) or is_plain_word(text):
        return False
    if is_domain_restricted(text):
        return False
    tokens = _tokens(text)
    if len(tokens) == 1:
        return _match_any(text, DISCOURSE_MARKERS)
    return True


def is_contextual_grammar(point):
    """语法点是否结合了具体语境，而不只是丢一个术语名。

    实测里两版的差别全在这里：
        A(v1): "一般现在时" / "祈使句" / "条件状语从句"      → 只说术语，讲了等于没讲
        B(v2): "Nobody tells you that... 引出别人通常不会告诉你的信息"
              "分裂句强调结构：'that's where things fall apart'"
                                                            → 引用原文 + 说明作用

    **不能用"长度够长"当判据**：像"条件状语从句"这种纯术语也能凑够字数，
    实测就被它蒙混过关过。真正能区分的是"有没有把语法落到这条字幕的具体内容上"，
    所以只认三种信号：引用原文（引号里是英文）、冒号引出解释、句子里夹着英文片段。
    """
    text = str(point or "").strip()
    if not text:
        return False
    if re.search(r"['\"“”「」‘’][^'\"“”「」‘’]{2,}['\"“”「」‘’]", text):
        return True                      # 引用了原文/例句
    if re.search(r"[：:]", text):
        return True                      # "术语：具体说明" 这种结构
    body = text
    for term in GENERIC_GRAMMAR:
        body = body.replace(term, "")
    return bool(re.search(r"[A-Za-z]{2,}", body))   # 句子里夹了英文原文片段


def content_words(text):
    return [tok for tok in _tokens(text) if tok not in COMMON_WORDS]


def _match_any(text, candidates):
    """宽松匹配：忽略大小写/标点，命中任一候选即算。"""
    haystack = _normalise(text)
    if not haystack:
        return False
    for candidate in candidates or ():
        needle = _normalise(candidate)
        if needle and needle in haystack:
            return True
    return False


def _level_distance(level, band):
    order = {"A1": 0, "A2": 1, "B1": 2, "B2": 3, "C1": 4, "C2": 5}
    low, high = band
    value = order.get(str(level or "").upper())
    if value is None:
        return None
    if order[low] <= value <= order[high]:
        return 0
    return min(abs(value - order[low]), abs(value - order[high]))


# --------------------------------------------------------------------------
# 七个维度
# --------------------------------------------------------------------------
def score_topic(result, expect):
    """主题判断：命中可接受说法给满分；明确不该出现的主题直接 0。"""
    topic = str(result.get("topic") or "")
    if expect.get("topic_forbidden") and _match_any(topic, expect["topic_forbidden"]):
        return 0.0
    if _match_any(topic, expect.get("topic_any")):
        return 1.0
    # 主题没命中但子主题说到了要点 → 给一半，避免只因用词不同就归零
    subtopic = str(result.get("subtopic") or "")
    if _match_any(subtopic, expect.get("subtopic_keywords")):
        return 0.5
    return 0.0


def score_cefr(result, expect):
    """CEFR：落在区间内满分，差一档 0.5，差两档及以上 0.2，跑出 A1–C2 记 0。"""
    distance = _level_distance(result.get("cefr_level"), expect["cefr_band"])
    if distance is None:
        return 0.0
    return {0: 1.0, 1: 0.5}.get(distance, 0.2)


def score_expressions(result, expect, grams, transcript):
    """重点表达：既要真有出处，也要真的值得教、且换场景用得上。"""
    expressions = result.get("expressions") or []
    if not expressions:
        # 参考答案本来就没表达可提 → 空数组是正确答案；否则说明漏掉了
        return 1.0 if not expect.get("must_expressions") else 0.0

    total = len(expressions)
    useful = sum(1 for entry in expressions
                 if coverage(str((entry or {}).get("text") or ""), grams, transcript) >= 0.75
                 and is_worth_teaching((entry or {}).get("text")))
    must = expect.get("must_expressions") or []
    joined = " || ".join(str((entry or {}).get("text") or "") for entry in expressions)
    must_hits = sum(1 for candidate in must if _match_any(joined, [candidate]))
    recall = (must_hits / len(must)) if must else useful / total
    # 表达给得越多越容易蒙中，认对了要相应打折：宁可少而准
    precision = useful / total
    return max(0.0, min(1.0, 0.6 * precision + 0.4 * recall))


def score_grammar(result, expect):
    """语法点：宁可少而准，而且要结合语境讲，不能只丢术语名。

    实测的两版差别就在这里：
        A(v1): "一般现在时" / "祈使句"                     → 术语枚举，讲了等于没讲
        B(v2): "Nobody tells you that... 引出别人不会告诉你的信息"  → 结构 + 作用
    只数"有没有教科书术语"是不够的 —— 那会让两版都拿满分，把差别抹平。
    """
    points = [str(p).strip() for p in (result.get("grammar_points") or []) if str(p).strip()]
    ok_empty = bool(expect.get("grammar_ok_empty"))
    if not points:
        return 1.0 if ok_empty else 0.45
    generic = sum(1 for point in points
                  if _match_any(point, GENERIC_GRAMMAR)
                  or _match_any(point, expect.get("generic_grammar_forbidden") or []))
    generic_ratio = generic / len(points)
    contextual_ratio = sum(1 for point in points if is_contextual_grammar(point)) / len(points)
    if ok_empty:
        # 本来不该有语法点的内容上硬给 → 给得越多越糟
        return max(0.0, 0.5 - 0.4 * generic_ratio - 0.1 * min(1, len(points) / 3))
    count_fit = 1.0 if 1 <= len(points) <= 3 else 0.7
    specificity = 0.5 * (1.0 - generic_ratio) + 0.5 * contextual_ratio
    return max(0.0, min(1.0, 0.75 * specificity + 0.25 * count_fit))


def score_key_sentences(result, expect, grams, transcript):
    """重点句：必须逐字来自 transcript，且长度适中、值得背诵。"""
    sentences = result.get("key_sentences") or []
    if not sentences:
        return 0.0
    scores = []
    for entry in sentences:
        text = str((entry or {}).get("text") or "").strip()
        if not text or is_noise(text):
            scores.append(0.0)
            continue
        cover = coverage(text, grams, transcript)
        if cover < 0.9:                      # 允许截取连续片段，不允许改写
            scores.append(cover * 0.5)
            continue
        words = len(_tokens(text))
        length_fit = 1.0 if 6 <= words <= 30 else (0.6 if 4 <= words < 6 else 0.5)
        has_translation = bool(str((entry or {}).get("translation_zh") or "").strip())
        scores.append(min(1.0, 0.7 * length_fit + (0.3 if has_translation else 0.0)))
    return sum(scores) / len(scores)


def score_learning_value(result, expect):
    """learning_value：落在人工给的区间内算准，偏离越远越低。"""
    try:
        value = float(result.get("learning_value"))
    except (TypeError, ValueError):
        return 0.0
    value = max(0.0, min(1.0, value))
    low, high = expect["learning_value_band"]
    if low <= value <= high:
        return 1.0
    distance = (low - value) if value < low else (value - high)
    return max(0.0, 1.0 - distance / 0.35)


def score_hallucination(result, expect, grams, transcript):
    """有据可依程度：expressions / key_sentences / summary 里有多少是原文没有的。

    summary_zh 是中文，不能照字面比对英文原文 —— 这里只在"字幕极短却写出
    一大段具体摘要"时才扣分（实测过：17 字符的占位字幕被写出一份像模像样的分析）。
    """
    checks = []
    for entry in (result.get("expressions") or []):
        text = str((entry or {}).get("text") or "").strip()
        if text:
            checks.append(1.0 if coverage(text, grams, transcript) >= 0.75 else 0.0)
    for entry in (result.get("key_sentences") or []):
        text = str((entry or {}).get("text") or "").strip()
        if text:
            checks.append(1.0 if coverage(text, grams, transcript) >= 0.9 else 0.0)

    transcript_words = len(_tokens(transcript))
    summary = str(result.get("summary_zh") or "")
    if transcript_words < 25 and len(summary) > 60:
        checks.append(0.0)                   # 短字幕写长摘要 → 基本是编的
    if not checks:
        return 1.0
    return sum(checks) / len(checks)


def expression_metrics(result, expect, grams, transcript):
    """把"表达质量"拆成两个能解释清楚的比例。

    只看一个综合分说不清改进来自哪里：
    - precision：提出来的里面有多少是真值得教的（提得准不准）
    - recall   ：人工认为必提的表达里，提到了多少（有没有漏）
    v2 的设计目标是"宁可少而准"，所以它很可能拉高 precision 而压低 recall ——
    这正是需要摆到报告里让用户自己判断的地方。
    """
    expressions = result.get("expressions") or []
    total = len(expressions)
    useful = sum(1 for entry in expressions
                 if coverage(str((entry or {}).get("text") or ""), grams, transcript) >= 0.75
                 and is_worth_teaching((entry or {}).get("text")))
    joined = " || ".join(str((entry or {}).get("text") or "") for entry in expressions)
    must = expect.get("must_expressions") or []
    hits = sum(1 for candidate in must if _match_any(joined, [candidate]))
    return {
        "expression_precision": round(useful / total, 4) if total else 0.0,
        "expression_total": total,
        "expression_useful": useful,
        "expression_recall": round(hits / len(must), 4) if must else None,
        "expression_must_total": len(must),
        "expression_must_hit": hits,
    }


def grammar_metrics(result, expect):
    """语法点里"纯术语凑数"的比例。"""
    points = [str(point).strip() for point in (result.get("grammar_points") or [])
              if str(point).strip()]
    if not points:
        return {"grammar_filler_ratio": None, "grammar_total": 0, "grammar_contextual": 0}
    filler = sum(1 for point in points if not is_contextual_grammar(point))
    return {"grammar_filler_ratio": round(filler / len(points), 4),
            "grammar_total": len(points),
            "grammar_contextual": len(points) - filler}


def score_result(result, expect, transcript):
    """对一条标注结果打分，返回 {维度: 分数} 与补充统计。"""
    grams = transcript_ngrams(transcript)
    scores = {
        "topic_accuracy": score_topic(result, expect),
        "cefr_quality": score_cefr(result, expect),
        "expression_quality": score_expressions(result, expect, grams, transcript),
        "grammar_quality": score_grammar(result, expect),
        "key_sentence_quality": score_key_sentences(result, expect, grams, transcript),
        "learning_value_quality": score_learning_value(result, expect),
        "hallucination": score_hallucination(result, expect, grams, transcript),
    }
    scores = {key: round(float(value), 4) for key, value in scores.items()}
    scores["overall"] = round(sum(scores[field] for field in SCORE_FIELDS) / len(SCORE_FIELDS), 4)
    scores.update(expression_metrics(result, expect, grams, transcript))
    scores.update(grammar_metrics(result, expect))
    return scores


def aggregate(rows):
    """把多条评分汇总成每个维度的平均值，并给出总体均分。"""
    if not rows:
        return {}
    summary = {}
    for field in SCORE_FIELDS:
        values = [row["scores"][field] for row in rows if row.get("scores")]
        summary[field] = round(sum(values) / len(values), 4) if values else 0.0
    overall = [row["scores"]["overall"] for row in rows if row.get("scores")]
    summary["overall"] = round(sum(overall) / len(overall), 4) if overall else 0.0
    summary["count"] = len(rows)
    return summary
