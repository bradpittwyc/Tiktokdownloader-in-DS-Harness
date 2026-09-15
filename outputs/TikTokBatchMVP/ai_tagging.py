"""AI 打标签：把一条素材的文字信息变成可检索的标签。

这一层是**纯逻辑** —— 提供方预设、提示词、回复解析、写回 meta ——
不联网。真正的网络调用在 web_app.py 的 `Api.tag_assets`。
拆出来的理由跟 asset_index 一样：能脱离 Api 直接测，跑得还快。

**为什么需要它**：TikTok 的文案常常很短，实测本机 51 条素材里
**文案带话题标签的 0 条**。素材库的检索只能命中文案里**明写**的词，
搜不出"这条视频到底在讲什么"。标签补的就是这一层语义。

**标签写在 meta.json 的 `ai` 段里**（不是单独一个文件）：
素材是会被整体搬走、备份、发给别人的，元数据必须跟素材在一起。
`ai` 是**可选段** —— 老 meta 没有它照样能读，见 ASSET_SCHEMA 的说明。
"""

import re


# --- 提供方 ---------------------------------------------------------------

# 都是 OpenAI 兼容接口，所以只有 base 和默认模型名不同。
PROVIDERS = {
    "openai": {"label": "OpenAI", "api_base": "https://api.openai.com/v1",
               "model": "gpt-4o-mini"},
    "deepseek": {"label": "DeepSeek", "api_base": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat"},
    "custom": {"label": "自定义（OpenAI 兼容）", "api_base": "", "model": ""},
}
DEFAULT_PROVIDER = "openai"


def provider_defaults(provider):
    """某个提供方的默认 base 与模型。认不出来的提供方退回默认，不抛。"""
    return dict(PROVIDERS.get(str(provider or "").strip()) or PROVIDERS[DEFAULT_PROVIDER])


def provider_label(provider):
    return provider_defaults(provider)["label"]


def resolve_endpoint(api_base, path="/chat/completions"):
    """把用户填的 base 拼成完整的接口地址。

    用户可能填 https://api.deepseek.com、https://api.deepseek.com/v1，
    甚至直接把 /chat/completions 一起贴上 —— 三种都要能用。
    """
    base = str(api_base or "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith(path) else base + path


# --- 提示词与解析 ---------------------------------------------------------

MAX_TAGS = 8
MAX_TAG_LENGTH = 12
MAX_SUMMARY = 120
# 字幕截断：一条短视频的字幕远小于这个数，截断只是防止异常长的输入烧钱。
TRANSCRIPT_LIMIT = 6000

SYSTEM_PROMPT = ("You are a precise content librarian. "
                 "You reply with a single JSON object and nothing else.")

TAG_PROMPT = """请为下面这条 TikTok 视频生成检索标签。

要求：
- tags 是 3 到 {max_tags} 个**中文**标签，每个 2 到 {max_tag_length} 个字
- 标签描述的是**内容主题**（在讲什么、属于什么领域），不是情绪词或废话
- 不要带 # 号，不要重复，不要出现"视频""抖音""tiktok"这类没有区分度的词
- summary 是一句不超过 {max_summary} 字的中文概括
- 只输出 JSON，格式：{{"tags": ["标签1", "标签2"], "summary": "一句话概括"}}
- 信息不足时宁少勿多，不要编造素材里没有的内容

标题：{title}

文案：{description}

字幕：
{transcript}
"""


def build_tag_prompt(title="", description="", transcript=""):
    """拼出给模型的 system / user 两条消息。"""
    transcript = str(transcript or "").strip()[:TRANSCRIPT_LIMIT]
    return {
        "system": SYSTEM_PROMPT,
        "user": TAG_PROMPT.format(
            max_tags=MAX_TAGS, max_tag_length=MAX_TAG_LENGTH,
            max_summary=MAX_SUMMARY,
            title=str(title or "").strip() or "（无）",
            description=str(description or "").strip() or "（无）",
            transcript=transcript or "（无字幕）",
        ),
    }


def tag_input_text(title="", description="", transcript=""):
    """一条素材到底有没有可用来打标签的文字。

    只有文件名的话不值得花一次模型调用 —— 让调用方据此跳过。
    """
    return " ".join(str(part or "").strip() for part in (title, description, transcript)).strip()


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _clean_tag(value):
    tag = str(value or "").strip().lstrip("#").strip()
    tag = re.sub(r"\s+", " ", tag)
    return tag[:MAX_TAG_LENGTH]


def normalize_tags(values):
    """去空、去 #、限长、大小写无关去重、截到 MAX_TAGS。"""
    tags, seen = [], set()
    for value in values if isinstance(values, (list, tuple)) else []:
        tag = _clean_tag(value)
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def parse_tag_response(text):
    """从模型回复里抠出 (tags, summary)。

    模型经常裹一层 ```json 代码块、或者前后加一句客套话，所以**不能**直接
    json.loads。先剥代码围栏，再定位第一个 { 到最后一个 }。
    解析不出来就返回空标签 —— 调用方据此报"这次没拿到标签"，而不是把
    整段回复当成标签存进去。
    """
    raw = _FENCE_RE.sub("", str(text or "").strip())
    match = _JSON_OBJECT_RE.search(raw)
    if not match:
        return [], ""
    try:
        import json
        payload = json.loads(match.group(0))
    except Exception:
        return [], ""
    if not isinstance(payload, dict):
        return [], ""
    tags = normalize_tags(payload.get("tags"))
    summary = str(payload.get("summary") or "").strip()[:MAX_SUMMARY]
    return tags, summary


# --- 写回 meta ------------------------------------------------------------

def merge_tags(meta, tags, summary, provider="", model="", tagged_at=""):
    """把标签并进 meta 的 `ai` 段，**保留其它所有字段**。

    必须是"读出来改一处再整体写回"，不能只写 ai 段 —— 那会把 4.19 里
    enrich 抹掉归档字段的同类事故重演一遍（只不过这次抹的是素材元数据）。
    """
    updated = dict(meta or {})
    ai = dict(updated.get("ai") or {})
    ai["tags"] = list(tags)
    if summary:
        ai["summary"] = summary
    else:
        ai.setdefault("summary", "")
    ai["provider"] = provider or ai.get("provider", "")
    ai["model"] = model or ai.get("model", "")
    ai["tagged_at"] = tagged_at or ai.get("tagged_at", "")
    updated["ai"] = ai
    return updated


def meta_tags(meta):
    """读一条 meta 里的标签，缺什么都返回空列表。"""
    if not isinstance(meta, dict):
        return []
    ai = meta.get("ai")
    if not isinstance(ai, dict):
        return []
    tags = ai.get("tags")
    return [str(tag) for tag in tags] if isinstance(tags, list) else []


def meta_summary(meta):
    if not isinstance(meta, dict):
        return ""
    ai = meta.get("ai")
    return str(ai.get("summary") or "") if isinstance(ai, dict) else ""
