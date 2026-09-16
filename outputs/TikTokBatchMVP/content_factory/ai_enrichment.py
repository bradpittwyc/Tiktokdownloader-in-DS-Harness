"""固定 schema 的 AI 内容标注服务。

对外只有一个核心函数 `parse_enrichment`（把模型返回的任意文本变成固定结构）
和一个调用入口 `EnrichmentService.enrich`。

为什么解析要这么小心：模型返回的"JSON"经常带 ``` 围栏、带解释性前后缀、
或者某个字段名写成近义词。今天的闭环里这直接决定成败 —— 解析失败就是一条
内容标不上，用户看到的是「失败」而不是结果。所以这里做三层容错：
    1. 剥掉 ```json 围栏 / 截取第一个 { 到最后一个 }
    2. 直接失败时把原文回灌给模型要求"只输出 JSON"再试一次
    3. 字段级归一化：缺字段补默认值、字符串与数组互转、枚举值纠正

schema（与用户要求一致）：
    topic              主题，中文，如「AI 科技」
    subtopic           子主题，中文
    cefr_level         A1 / A2 / B1 / B2 / C1 / C2
    accent             口音，如「美音」「英音」
    speech_speed       语速，慢速 / 中等 / 偏快
    learning_value     学习价值，0–1 之间的小数
    keywords           关键词数组（英文词条，可带中文释义）
    expressions        重点表达数组，元素为 {text, meaning_zh, example}
    grammar_points     语法点数组
    key_sentences      重点句数组，元素为 {text, translation_zh}
    summary_zh         中文摘要
    recommended_task   推荐练习任务，如「跟读」「复述」
"""

import json
import re
import threading

ENRICHMENT_FIELDS = (
    "topic", "subtopic", "cefr_level", "accent", "speech_speed", "learning_value",
    "keywords", "expressions", "grammar_points", "key_sentences", "summary_zh",
    "recommended_task",
)

CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
SPEECH_SPEEDS = ("慢速", "中等", "偏快")

DEFAULT_PROMPT_TEMPLATE = """你是英语教学内容分析专家。下面是一条 TikTok 视频的元信息与字幕文本，
请分析它作为英语学习材料的价值，并**只输出一个 JSON 对象**，不要任何解释、不要 Markdown 围栏。

输出 JSON 必须严格包含以下字段：
{
  "topic": "主题（中文，如：AI 科技 / 职场成长 / 生活技巧）",
  "subtopic": "更细的子主题（中文）",
  "cefr_level": "难度等级，只能是 A1/A2/B1/B2/C1/C2 之一",
  "accent": "口音，如：美音 / 英音 / 澳音 / 不确定",
  "speech_speed": "语速，只能是 慢速/中等/偏快 之一",
  "learning_value": 0.0 到 1.0 之间的小数，越高越适合学习,
  "keywords": ["关键词，英文原词，最多 10 个"],
  "expressions": [
    {"text": "地道表达或短语", "meaning_zh": "中文意思", "example": "例句（可选）"}
  ],
  "grammar_points": ["语法点，如：现在完成时"],
  "key_sentences": [
    {"text": "值得背诵的英文原句", "translation_zh": "中文翻译"}
  ],
  "summary_zh": "一到两句中文摘要",
  "recommended_task": "推荐练习任务，如：跟读 / 复述 / 影子跟读 / 填空练习"
}

要求：
- expressions 给 3–6 条，key_sentences 给 3–5 条，keywords 给 5–10 个，grammar_points 给 2–4 条。
- 所有中文说明用简体中文；keywords / expressions.text / key_sentences.text 保留英文原文。
- 如果字幕过短或信息不足，仍然照常输出 JSON，把不确定的字段填「不确定」，learning_value 给偏低的值。

视频信息：
- 标题：{title}
- 作者：@{author}
- 简介：{description}
- 时长：{duration} 秒
- 字幕文本：
{transcript}
"""


class EnrichmentError(RuntimeError):
    """模型调用或解析最终失败。"""


def _clip(value, limit=4000):
    text = str(value if value is not None else "").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit]


def build_prompt(template, item, transcript, max_chars=12000):
    """按模板拼 prompt。模板里缺哪个占位符都不会炸。"""
    body = _clip(transcript, max_chars) or "（该视频没有可用字幕文本）"
    values = {
        "title": _clip(item.get("title"), 300) or "（无标题）",
        "author": _clip(item.get("creator_handle") or item.get("author"), 80) or "unknown",
        "description": _clip(item.get("description"), 1200) or "（无）",
        "duration": item.get("duration") or "未知",
        "transcript": body,
    }
    text = template or DEFAULT_PROMPT_TEMPLATE
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def strip_code_fence(text):
    """去掉 ```json ... ``` 围栏，返回内部内容。"""
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:json|JSON)?\s*(.+?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    return raw


def extract_json_object(text):
    """从任意文本里抠出第一个完整的 JSON 对象。

    先试严格解析；不行就扫描花括号配对（跳过字符串内的括号），
    这样"这是分析结果：{...} 希望有帮助"这种也能救回来。
    """
    raw = strip_code_fence(text)
    if not raw:
        raise EnrichmentError("模型返回为空")
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    start = raw.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    chunk = raw[start:index + 1]
                    try:
                        value = json.loads(chunk)
                        if isinstance(value, dict):
                            return value
                    except Exception:
                        break
        start = raw.find("{", start + 1)
    raise EnrichmentError("模型返回里找不到合法 JSON 对象")


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [entry for entry in value if entry not in (None, "")]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        # 模型有时把数组写成 "a, b, c" 或 "a\nb"
        parts = re.split(r"[,，\n;；]+", text) if re.search(r"[,，\n;；]", text) else [text]
        return [part.strip() for part in parts if part.strip()]
    return [value]


def _as_text(value, limit=600):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "；".join(str(entry) for entry in value if entry)
    if isinstance(value, dict):
        value = value.get("text") or value.get("value") or json.dumps(value, ensure_ascii=False)
    return _clip(value, limit)


def _pairs(value, text_keys, extra_keys=()):
    """把数组元素统一成 dict。字符串元素 -> {'text': s}。"""
    result = []
    for entry in _as_list(value):
        if isinstance(entry, dict):
            record = {}
            text = ""
            for key in text_keys:
                if entry.get(key):
                    text = _as_text(entry[key])
                    break
            if not text:
                continue
            record["text"] = text
            for key in extra_keys:
                if entry.get(key):
                    record[key] = _as_text(entry[key], 400)
            result.append(record)
        else:
            text = _as_text(entry)
            if text:
                result.append({"text": text})
    return result


def normalize_enrichment(payload):
    """字段级归一化：缺的补空、类型错的纠正。绝不抛异常。"""
    data = payload if isinstance(payload, dict) else {}
    lower = {str(key).strip().lower(): value for key, value in data.items()}

    def pick(*names):
        for name in names:
            if name in data and data[name] not in (None, ""):
                return data[name]
            if name.lower() in lower and lower[name.lower()] not in (None, ""):
                return lower[name.lower()]
        return None

    level = _as_text(pick("cefr_level", "cefr", "level"), 8).upper()
    level = re.sub(r"[^A-Z0-9]", "", level)
    if level not in CEFR_LEVELS:
        level = level if level in CEFR_LEVELS else "B1"

    speed = _as_text(pick("speech_speed", "speed", "语速"), 20)
    if speed not in SPEECH_SPEEDS:
        speed = next((candidate for candidate in SPEECH_SPEEDS if candidate in speed), "中等")

    value = pick("learning_value", "learningValue", "value")
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.0
    if score > 1:                      # 模型偶尔给 0–100
        score = score / 100
    score = round(max(0.0, min(1.0, score)), 2)

    keywords = []
    for entry in _as_list(pick("keywords", "key_words", "关键词")):
        text = _as_text(entry, 120)
        if text and text not in keywords:
            keywords.append(text)

    grammar = []
    for entry in _as_list(pick("grammar_points", "grammar", "语法点")):
        text = _as_text(entry, 200)
        if text and text not in grammar:
            grammar.append(text)

    return {
        "topic": _as_text(pick("topic", "主题"), 120) or "未分类",
        "subtopic": _as_text(pick("subtopic", "sub_topic", "子主题"), 120) or "未分类",
        "cefr_level": level,
        "accent": _as_text(pick("accent", "口音"), 40) or "不确定",
        "speech_speed": speed,
        "learning_value": score,
        "keywords": keywords[:10],
        "expressions": _pairs(pick("expressions", "重点表达", "phrases"),
                              ("text", "expression", "phrase", "en"),
                              ("meaning_zh", "meaning", "zh", "translation_zh", "example"))[:8],
        "grammar_points": grammar[:6],
        "key_sentences": _pairs(pick("key_sentences", "sentences", "重点句"),
                                ("text", "sentence", "en"),
                                ("translation_zh", "translation", "zh", "meaning_zh"))[:6],
        "summary_zh": _as_text(pick("summary_zh", "summary", "摘要"), 1200),
        "recommended_task": _as_text(pick("recommended_task", "task", "推荐任务"), 200) or "跟读",
    }


def parse_enrichment(text):
    """模型输出 -> (归一化结果, 原始 dict)。失败抛 EnrichmentError。"""
    payload = extract_json_object(text)
    return normalize_enrichment(payload), payload


class EnrichmentService:
    """调用 OpenAI 兼容的 chat/completions 接口做内容标注。

    api_base / model / api_key 等全部来自设置页（与现有学习文档功能同构），
    所以用户在「AI 加工设置」里填的东西立刻生效。
    """

    def __init__(self, settings=None, http=None):
        self._settings = settings
        self._http = http                # 测试可注入假 requests
        self._lock = threading.RLock()

    # ---- 配置 ----------------------------------------------------------
    def config(self):
        section = self._settings.section("ai") if self._settings else {}
        return {
            "provider": section.get("provider") or "DeepSeek",
            "model": (section.get("model") or "deepseek-chat").strip(),
            "api_base": (section.get("api_base") or "https://api.deepseek.com/v1").strip().rstrip("/"),
            "api_key": str(section.get("api_key") or "").strip(),
            "max_tokens": int(section.get("max_tokens") or 4000),
            "temperature": float(section.get("temperature", 0.7)),
            "prompt_template": section.get("prompt_template") or DEFAULT_PROMPT_TEMPLATE,
        }

    def configured(self):
        return bool(self.config()["api_key"])

    # ---- HTTP ----------------------------------------------------------
    def _post(self, config, messages, max_tokens=None):
        http = self._http
        if http is None:
            import requests as http
        base = config["api_base"]
        endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload = {
            "model": config["model"],
            "temperature": config["temperature"],
            "max_tokens": int(max_tokens or config["max_tokens"]),
            "messages": messages,
        }
        response = http.post(
            endpoint,
            headers={"Authorization": f"Bearer {config['api_key']}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=240,
        )
        response.raise_for_status()
        data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EnrichmentError(f"模型返回结构异常：{str(data)[:200]}") from exc

    def test_connection(self):
        config = self.config()
        if not config["api_key"]:
            return {"ok": False, "error": "请先填写 API Key"}
        try:
            text = self._post(config, [{"role": "user", "content": "Reply with OK"}], max_tokens=8)
            return {"ok": True, "message": str(text)[:80], "model": config["model"]}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ---- 主入口 --------------------------------------------------------
    def enrich(self, item, transcript):
        """返回 (归一化结果, raw_text, raw_json, 重试次数)。失败抛 EnrichmentError。"""
        config = self.config()
        if not config["api_key"]:
            raise EnrichmentError("未配置 API Key：请在「设置 → AI 加工设置」中填写后重试")
        prompt = build_prompt(config["prompt_template"], item, transcript)
        system = "You are a precise English-learning content analyst. Always answer with a single JSON object."
        attempts, last_error, raw_text = 0, None, ""
        with self._lock:
            for attempt in range(2):
                attempts = attempt + 1
                try:
                    user = prompt if attempt == 0 else (
                        "上一次的输出无法解析为 JSON。请重新分析，**只输出一个 JSON 对象**，"
                        "不要任何解释与 Markdown 围栏。\n\n" + prompt)
                    raw_text = self._post(config, [{"role": "system", "content": system},
                                                   {"role": "user", "content": user}])
                    normalized, payload = parse_enrichment(raw_text)
                    return normalized, raw_text, payload, attempts
                except Exception as exc:
                    last_error = exc
        raise EnrichmentError(f"AI 标注失败：{last_error}")
