"""Prompt 版本库。

为什么要有这一层：`prompt_template` 原本只是设置里的一个字符串，改了就覆盖，
于是「这条标注是哪个版本的 prompt 产出的」永远说不清 —— 做 A/B 对比时，
报告里的分数没法回溯到具体的 prompt，质量提升也就无法归因。

所以这里把 prompt 变成**有名字、有版本、不覆盖**的资产：
- `ai-enrichment-v1`（baseline）：与之前跑通的默认模板**逐字一致**，绝不改动，
  历史标注的 prompt_version 都指向它。
- `ai-enrichment-v2`（tuned）：本阶段新增。目标是让模型"敢少给、别硬凑"。
- `custom`：用户在设置页自己改过的模板（沿用 v1 的字段契约）。

`resolve_prompt()` 负责把设置里的值解析成 (版本名, 模板文本)，并兼容三种历史状态：
空值、老的默认模板文本、用户自定义文本。
"""

from dataclasses import dataclass

CUSTOM_VERSION = "custom"


@dataclass(frozen=True)
class PromptSpec:
    version: str
    label: str
    template: str
    notes: str

    def as_dict(self):
        return {"version": self.version, "label": self.label,
                "template": self.template, "notes": self.notes}


# --------------------------------------------------------------------------
# v1 / baseline —— 与仓库里原本的 DEFAULT_PROMPT_TEMPLATE 完全一致，勿改
# --------------------------------------------------------------------------
V1_TEMPLATE = """你是英语教学内容分析专家。下面是一条 TikTok 视频的元信息与字幕文本，
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

V1_NOTES = ("基线版本。硬性要求 expressions 3–6 条、grammar_points 2–4 条，"
            "会逼模型为凑数量提普通词与教科书式语法点。")


# --------------------------------------------------------------------------
# v2 / tuned —— 本阶段新增
# --------------------------------------------------------------------------
V2_TEMPLATE = """你是英语教学内容分析专家，服务对象是**想通过短视频学英语的中文母语者**。
下面是一条 TikTok 视频的元信息与字幕文本，请判断它作为英语学习材料的价值。

**只输出一个 JSON 对象**，不要任何解释、不要 Markdown 围栏。

输出 JSON 必须严格包含以下字段：
{
  "topic": "主题（中文，如：AI 科技 / 职场成长 / 生活技巧）",
  "subtopic": "更细的子主题（中文）",
  "cefr_level": "难度等级，只能是 A1/A2/B1/B2/C1/C2 之一",
  "accent": "口音，如：美音 / 英音 / 澳音 / 不确定",
  "speech_speed": "语速，只能是 慢速/中等/偏快 之一",
  "learning_value": 0.0 到 1.0 之间的小数,
  "keywords": ["关键词，英文原词"],
  "expressions": [
    {"text": "字幕里出现的英文表达", "meaning_zh": "中文意思", "example": "可选例句"}
  ],
  "grammar_points": ["值得讲的语法点"],
  "key_sentences": [
    {"text": "字幕里的英文原句", "translation_zh": "中文翻译"}
  ],
  "summary_zh": "一到两句中文摘要",
  "recommended_task": "跟读 / 复述 / 影子跟读 / 填空练习 之一"
}

【最重要的一条：宁缺勿滥】
数量不设下限。**没有合格的就返回空数组 `[]`**，这比硬凑更好。
- expressions：只收「换到别的场景也能用」的表达，按优先级取：
  高频口语表达 > 固定搭配 > phrasal verbs > 习语 > 话语标记（you know / here's the thing / to be fair）。
  **不要**收：单个普通名词或动词（work / thing / people / good）、
  只在标题里出现的词、字幕噪声或占位文字（如 [Music]、"(demo transcript)"）、
  人名品牌名等专有名词、对 A2 学习者都过于基础的词（very / really / a lot of）。
  0 条也是合法答案；只有真的够好才给到 5–6 条。
- grammar_points：只讲**这条字幕里真实出现、且讲出来对学习者有用**的点。
  不要为了填字段而机械列「一般现在时」「一般过去时」这类没有讲解价值的条目。
  0–3 条都是合法答案；能讲清一个真正有用的点，好过罗列四个没用的。
- key_sentences：**必须逐字来自上面的字幕文本**（可以截取连续片段，但不能改写、不能拼接、
  不能翻译后再回译）。选那些结构漂亮或表达地道、值得背诵的句子。1–5 条。

【必须基于字幕，不许编】
- expressions.text 与 key_sentences.text 只能使用字幕里出现过的英文。
- 字幕里没有的信息不要写进 summary_zh / subtopic。
- 如果字幕太短、只是占位文字或没有实质内容：照常输出 JSON，但
  learning_value 给 0.1–0.3，expressions 与 grammar_points 给空数组，
  subtopic 填「内容不足」，并在 summary_zh 里直说「字幕内容不足，无法有效分析」。

【learning_value 怎么给（必须真的区分开）】
- 0.0–0.3：垃圾或无效内容（纯音乐、占位文本、无实质信息的广告、几乎没说话）。
- 0.3–0.6：普通内容（日常闲聊、信息稀少、表达很基础）。
- 0.6–0.8：有学习价值（表达地道、结构清晰、话题有用）。
- 0.8–1.0：高教学价值（信息密度高且表达丰富，适合作为精读或跟读材料）。

【CEFR 怎么判断（综合判断，不要只看单词）】
同时考虑：词汇难度与抽象程度、句法复杂度（从句 / 虚拟语气 / 被动 / 分词结构）、
语速、信息密度、表达是否依赖背景知识。日常口语不等于简单，学术词汇多也不等于难。
判断依据要能对应到字幕本身。

【accent 与 speech_speed】
只看字幕文本时无法真正听到口音与语速 —— 拿不准就填「不确定」，
不要根据作者国籍或话题猜测。

视频信息：
- 标题：{title}
- 作者：@{author}
- 简介：{description}
- 时长：{duration} 秒
- 字幕文本：
{transcript}
"""

V2_NOTES = ("调优版本。① 数量不设下限、允许空数组，禁止为凑数提普通词与教科书语法；"
            "② expressions 明确优先级与排除项；③ key_sentences 必须逐字来自字幕；"
            "④ learning_value 给出四档锚点；⑤ CEFR 要求综合判断并说明依据；"
            "⑥ 拿不准的口音/语速填「不确定」，不许靠猜。")


PROMPT_LIBRARY = {
    "ai-enrichment-v1": PromptSpec(version="ai-enrichment-v1", label="v1 基线",
                                   template=V1_TEMPLATE, notes=V1_NOTES),
    "ai-enrichment-v2": PromptSpec(version="ai-enrichment-v2", label="v2 调优",
                                   template=V2_TEMPLATE, notes=V2_NOTES),
}

# 默认使用哪个版本。
#
# 刻意保持 v1：v1 就是仓库里原本的默认模板，也就是说「升级到本版本后，
# 现有用户看到的标注行为一个字都没变」。v2 是这一阶段新增的可选版本，
# 在「设置 → AI 加工设置」里显式切换，或由 benchmark 单独调用。
# 等对比报告出来、确认 v2 确实更好之后，再由人决定要不要把默认切过去 ——
# 这种事不该由我替用户静默决定。
ACTIVE_VERSION = "ai-enrichment-v1"

# 版本选择器在界面上的展示顺序
VERSION_ORDER = ("ai-enrichment-v1", "ai-enrichment-v2", CUSTOM_VERSION)


def get_prompt(version):
    return PROMPT_LIBRARY.get(str(version or "").strip())


def all_prompts():
    return [PROMPT_LIBRARY[name].as_dict() for name in VERSION_ORDER if name in PROMPT_LIBRARY]


def resolve_prompt(value):
    """设置里的 prompt_template -> (版本名, 模板文本)。

    三种历史状态都要能正确识别，否则老用户的标注会被错误地标成 custom：
    - 迁移后的新值：直接就是版本名（"ai-enrichment-v1"）
    - 更早的默认值：存的是整段 v1 模板文本
    - 用户改过的：既不是版本名也不是任何已知模板 -> custom
    """
    text = str(value or "").strip()
    if not text:
        return ACTIVE_VERSION, PROMPT_LIBRARY[ACTIVE_VERSION].template
    spec = PROMPT_LIBRARY.get(text)
    if spec:
        return spec.version, spec.template
    for name, candidate in PROMPT_LIBRARY.items():
        if candidate.template.strip() == text:
            return name, candidate.template
    return CUSTOM_VERSION, text


def default_template():
    return PROMPT_LIBRARY[ACTIVE_VERSION].template
