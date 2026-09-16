"""字幕 / 转写文本获取。

今天的优先级（与用户要求一致）：
    1. 复用已有能力：yt-dlp 下载时已经把字幕轨写到了本地（writesubtitles +
       writeautomaticsub），所以「字幕文件 → 纯文本」是主路径。
    2. 没有字幕时再接 ASR：这里只留接口与真实实现骨架 —— 本机没装
       faster-whisper / openai-whisper 时**明确返回"ASR 不可用"**，
       而不是静默假装成功（那样界面会显示一条空的标注结果，更难排查）。
    3. 纯文本清洗与 web_app.Api._subtitle_text 保持一致：去掉序号行、时间轴、
       HTML 标签、重复行，并保留去重后的正文顺序。
"""

import re
from pathlib import Path

SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ttml", ".srv1", ".srv2", ".srv3", ".json")
PLAIN_EXTS = (".txt", ".md")

# 视觉/音效标记，TikTok 自动字幕里很常见，留着会污染给模型的输入
NOISE_PATTERN = re.compile(r"\[(?:music|applause|laughter|sound|音乐|笑声|掌声)[^\]]*\]",
                           re.I)


class ASRUnavailable(RuntimeError):
    """本机没有可用的语音识别后端。"""


def clean_transcript(raw):
    """字幕 / 转写原文 -> 干净的可读段落。"""
    lines, seen = [], set()
    for line in str(raw or "").replace("\r\n", "\n").split("\n"):
        text = line.strip()
        if not text:
            continue
        if text.isdigit():
            continue
        if "-->" in text or text.upper().startswith(("WEBVTT", "NOTE", "KIND:", "LANGUAGE:")):
            continue
        if re.match(r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*$", text):
            continue
        text = re.sub(r"<[^>]+>", "", text)                 # HTML 标签
        text = re.sub(r"\{\\[^}]*\}", "", text)             # ASS 样式码
        text = NOISE_PATTERN.sub("", text)
        text = re.sub(r"[ \t]+", " ", text).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)
    # 逐行去重后按句子重排，让模型看到的是连续文本而不是碎行
    joined = " ".join(lines)
    joined = re.sub(r"\s+([,.!?;:])", r"\1", joined)
    return joined.strip()


def read_subtitle_file(path):
    """读一个字幕文件并转成纯文本；不是字幕文件或读不到则返回空串。"""
    target = Path(path)
    if not target.is_file():
        return ""
    try:
        if target.suffix.lower() == ".json":
            # TikTok 的 json 字幕是 {"body":[{"content":"..."}]} 结构
            import json
            payload = json.loads(target.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(payload, dict) and isinstance(payload.get("body"), list):
                return clean_transcript(" ".join(
                    str(entry.get("content", "")) for entry in payload["body"]
                    if isinstance(entry, dict)))
            return ""
        return clean_transcript(target.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return ""


def pick_subtitle_path(paths):
    """从候选里挑最合适的一条字幕：优先原生 .srt / 英文轨。"""
    candidates = [Path(path) for path in (paths or []) if str(path or "").strip()]
    candidates = [path for path in candidates
                  if path.suffix.lower() in SUBTITLE_EXTS and path.is_file()]
    if not candidates:
        return None
    candidates.sort(key=lambda path: (
        0 if path.suffix.lower() == ".srt" else 1,
        0 if ".en" in path.name.lower() else 1,
        len(path.name),
    ))
    return candidates[0]


def transcript_from_subtitle_file(path):
    """主路径：字幕文件 -> transcript 文本。"""
    target = Path(path) if path else None
    if not target:
        return ""
    if target.is_dir():
        inside = sorted(target.glob("*"))
        picked = pick_subtitle_path(inside)
        return read_subtitle_file(picked) if picked else ""
    return read_subtitle_file(target)


def find_subtitle_for(video_path):
    """给定已下载的媒体文件，找它旁边的字幕（同名字幕在 yt-dlp 下很常见）。"""
    media = Path(video_path)
    if not media.is_file():
        return None
    stem = media.stem
    siblings = [path for path in media.parent.glob(stem + "*")
                if path.suffix.lower() in SUBTITLE_EXTS]
    if not siblings:
        # yt-dlp 有时把语言后缀加在扩展名之前：name.en.srt
        siblings = [path for path in media.parent.glob("*")
                    if path.suffix.lower() in SUBTITLE_EXTS and path.stem.startswith(stem)]
    return pick_subtitle_path(siblings)


def asr_backend():
    """探测本机 ASR 后端：优先 faster-whisper，其次 openai-whisper。"""
    try:
        import faster_whisper  # noqa: F401
        return "faster-whisper"
    except Exception:
        pass
    try:
        import whisper  # noqa: F401
        return "whisper"
    except Exception:
        return ""


def asr_available():
    return bool(asr_backend())


def transcribe_media(media_path, model_size="base", language=None, progress=None):
    """ASR 转写。本机没有后端时抛 ASRUnavailable，界面据此显示明确原因。

    这条链路今天不做强保证（用户允许："如果暂时不能稳定做 ASR，可先对已有字幕的
    视频跑通主流程，并为 ASR 留出接口"），但代码是真的：装了 faster-whisper 就能用。
    """
    target = Path(media_path)
    if not target.is_file():
        raise ASRUnavailable(f"媒体文件不存在：{media_path}")
    backend = asr_backend()
    if not backend:
        raise ASRUnavailable(
            "本机未安装语音识别后端（faster-whisper / openai-whisper），"
            "该视频也没有字幕轨，无法生成转写文本")
    if progress:
        progress("正在加载语音识别模型…")
    try:
        if backend == "faster-whisper":
            from faster_whisper import WhisperModel
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            segments, _info = model.transcribe(str(target), language=language, vad_filter=True)
            text = " ".join(segment.text.strip() for segment in segments)
        else:
            import whisper
            model = whisper.load_model(model_size)
            result = model.transcribe(str(target), language=language)
            text = result.get("text", "")
    except ASRUnavailable:
        raise
    except Exception as exc:
        raise ASRUnavailable(f"语音识别失败：{type(exc).__name__}: {exc}") from exc
    return clean_transcript(text)
