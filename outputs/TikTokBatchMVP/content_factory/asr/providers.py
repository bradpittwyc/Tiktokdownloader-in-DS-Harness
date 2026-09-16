"""转写来源的 provider 抽象与三个真实实现。

一次转写的候选来源，按「便宜可靠 → 贵且不一定可用」排序：

1. `SubtitleProvider`   已有字幕文件直接读取 —— yt-dlp 下载时字幕就落盘了，
                        零成本、最准，所以永远排第一。
2. `LocalAsrProvider`   本机 faster-whisper / openai-whisper —— 要装、要算力，
                        没装时明确报「后端不可用」，不静默假装成功。
3. `ExternalAsrProvider` 外部 ASR 服务 —— **接口与真实 HTTP 调用都已写好**，
                        但本阶段没有可用的 API Key，所以默认不可用（available()=False），
                        fetch() 抛 provider_unconfigured。它绝不会伪造转写结果。

契约（provider 只做一件事，不碰数据库、不改状态、不知道自己在哪条链路里）：

- 找不到输入 → `fetch()` 返回 `None`，交给下一个 provider；
- 真的失败 → 抛 `TranscriptError`（带错误码），链路会继续尝试后面的 provider；
- 成功 → 返回 `ProviderOutput`（文本 + 命中的素材路径）。

这样「字幕优先、没有字幕才 ASR、ASR 不行再外部」就是一条 provider 链，
而不是 pipeline 里一串 if。
"""

from pathlib import Path
from typing import Optional

from ..transcript import (ASRUnavailable, clean_transcript, find_subtitle_for,
                          transcribe_media, transcript_from_subtitle_file)
from .models import (ProviderOutput, TranscriptError, TranscriptErrorCode,
                     TranscriptSource)

# 设置页「ASR / 转写」的三个取值（ui/app.js 里的下拉就是这三项）
ASR_MODE_LOCAL = "local"        # OpenAI Whisper（本地）
ASR_MODE_EXTERNAL = "external"  # 外部服务
ASR_MODE_OFF = "off"            # 不启用


def _recorded_media(item):
    """按「音频优先」列出内容里记录的本地媒体路径。

    先音频后视频：设置里「提取音频」打开时，ASR 喂音频比喂视频快得多，
    识别结果完全一样。
    """
    return [str(item.get(key) or "").strip()
            for key in ("local_audio_path", "local_video_path")
            if str(item.get(key) or "").strip()]


def _existing_media(item):
    """返回真实存在的本地媒体文件路径；没有则返回空串。"""
    for path in _recorded_media(item):
        if Path(path).is_file():
            return path
    return ""


def _media_missing_error(item, provider=""):
    recorded = _recorded_media(item)
    detail = (f"记录的文件不存在：{'、'.join(recorded)}" if recorded
              else "内容没有记录任何本地媒体文件")
    return TranscriptError(
        TranscriptErrorCode.MEDIA_MISSING,
        "内容尚未下载到本地，没有可用于转写的媒体文件",
        provider=provider, hint="先把视频下载到本地（或导入本地文件）再重试", detail=detail)


class TranscriptProvider:
    """转写来源的统一接口。子类只需要实现 available() / fetch()。"""

    name = ""            # 内部标识（写进结果与运行记录）
    kind = ""            # TranscriptSource 之一
    label = ""           # 界面上/报告里的中文名
    asset_field = ""     # 命中的素材要写回 content_items 的哪一列（空 = 不写）
    start_message = ""   # 进度事件文案（与旧实现保持一致的措辞）

    def done_message(self, chars):
        """拿到文本后的进度事件文案。"""
        return f"转写完成（{chars} 字符）"

    # ---- 能力探测 ------------------------------------------------------
    def available(self):
        """今天这条来源能不能用。不能用时 fetch() 会抛结构化错误。"""
        return True

    def detail(self):
        """available() 为 False 时，人话说明为什么。"""
        return ""

    def unavailable_error(self):
        return TranscriptError(TranscriptErrorCode.NO_INPUT,
                               f"{self.label}当前不可用", provider=self.name,
                               hint=self.detail())

    def describe(self):
        return {"name": self.name, "kind": self.kind, "label": self.label,
                "available": bool(self.available()), "detail": self.detail()}

    # ---- 主入口 --------------------------------------------------------
    def fetch(self, item):
        raise NotImplementedError


class SubtitleProvider(TranscriptProvider):
    """来源 1：已有字幕文件直接读取。

    两条查找路径，与旧实现一致：
    1. 内容库里已经记着的 `local_subtitle_path`（下载交接时写进去的）；
    2. 媒体文件旁边的同名字幕（yt-dlp 的 `name.en.srt` / `name.srt`）。

    注意「没有字幕」（返回 None，不是错误）与「有字幕但读不出文本」
    （抛 subtitle_unreadable）是两回事：前者该去跑 ASR，后者该提示重新抓字幕。
    """

    name = TranscriptSource.SUBTITLE
    kind = TranscriptSource.SUBTITLE
    label = "已有字幕文件"
    asset_field = "local_subtitle_path"
    start_message = "正在获取字幕文本…"

    def fetch(self, item):
        recorded = str(item.get("local_subtitle_path") or "").strip()
        if recorded:
            text = transcript_from_subtitle_file(recorded)
            if text:
                return ProviderOutput(text=text, asset=recorded, asset_field=self.asset_field)
            raise TranscriptError(
                TranscriptErrorCode.SUBTITLE_UNREADABLE,
                f"字幕文件读不出文本：{recorded}",
                provider=self.name,
                hint="字幕文件可能是空的或格式不受支持；重新抓一次字幕，或手工粘贴字幕文本",
                detail=recorded)

        video = str(item.get("local_video_path") or "").strip()
        sibling = find_subtitle_for(video) if video else None
        if not sibling:
            return None
        text = transcript_from_subtitle_file(sibling)
        if text:
            return ProviderOutput(text=text, asset=str(sibling), asset_field=self.asset_field)
        raise TranscriptError(
            TranscriptErrorCode.SUBTITLE_UNREADABLE,
            f"字幕文件读不出文本：{sibling}",
            provider=self.name,
            hint="字幕文件可能是空的或格式不受支持；重新抓一次字幕，或手工粘贴字幕文本",
            detail=str(sibling))

    def done_message(self, chars):
        return f"字幕文本已就绪（{chars} 字符）"


class LocalAsrProvider(TranscriptProvider):
    """来源 2：本机 faster-whisper / openai-whisper。

    真正的识别逻辑仍然在 `content_factory/transcript.py::transcribe_media`
    （那里已经处理了两种后端的差异），这里只负责：
    探测后端 → 挑媒体文件 → 把异常翻译成结构化错误。
    """

    name = TranscriptSource.LOCAL_ASR
    kind = TranscriptSource.LOCAL_ASR
    label = "本机语音识别"
    start_message = "正在语音识别（ASR）…"

    def __init__(self, transcriber=None, probe=None, backend=None, transcribe_kwargs=None):
        self._transcribe = transcriber or transcribe_media
        self._probe = probe                    # 默认延迟到调用时再探测（测试能手替）
        self._backend = backend
        self.transcribe_kwargs = dict(transcribe_kwargs or {})

    # ---- 探测 ----------------------------------------------------------
    def backend(self):
        if self._backend is not None:
            return str(self._backend or "")
        if self._probe is not None:
            return self.name if self._probe() else ""
        from ..transcript import asr_backend
        return asr_backend()

    def available(self):
        if self._backend is not None:
            return bool(self._backend)
        if self._probe is not None:
            return bool(self._probe())
        from ..transcript import asr_available
        return bool(asr_available())

    def detail(self):
        if self.available():
            return f"已检测到后端：{self.backend() or 'unknown'}"
        return ("本机未安装语音识别后端（faster-whisper / openai-whisper）；"
                "安装后即可识别没有字幕的视频")

    def unavailable_error(self):
        # 措辞与旧实现保持一致：这句话会直接进 content_items.last_error，
        # 界面把它当纯文本显示（异常页也按关键词分类）。
        return TranscriptError(
            TranscriptErrorCode.BACKEND_UNAVAILABLE,
            "本机未安装语音识别后端（faster-whisper / openai-whisper），"
            "该视频也没有字幕轨；请安装识别后端，或改用手工粘贴字幕文本",
            provider=self.name,
            hint="pip install faster-whisper（或 openai-whisper）后重试；也可以手工粘贴字幕文本",
            detail=self.detail())

    # ---- 转写 ----------------------------------------------------------
    def fetch(self, item):
        # 先判后端再判媒体文件：两者都缺时，前者是更根本的原因
        # （没装识别后端 = 这条路走不通；没下载 = 还能先补下载）。
        if not self.available():
            raise self.unavailable_error()
        media = _existing_media(item)
        if not media:
            raise _media_missing_error(item, provider=self.name)
        try:
            text = self._transcribe(media, **self.transcribe_kwargs)
        except ASRUnavailable as exc:
            # transcript.transcribe_media 把「后端报错 / 媒体读不了」都包成 ASRUnavailable，
            # 它的 message 已经是人话，原样保留。
            raise TranscriptError(TranscriptErrorCode.ASR_FAILED, str(exc),
                                  provider=self.name,
                                  hint="确认媒体文件可播放；文件损坏时重新下载再重试") from exc
        except Exception as exc:
            raise TranscriptError(
                TranscriptErrorCode.ASR_FAILED,
                f"语音识别失败：{type(exc).__name__}: {exc}",
                provider=self.name,
                hint="确认媒体文件可播放；文件损坏时重新下载再重试") from exc
        # transcribe_media 已经清洗过；这里再洗一遍是为了兜住外部注入的 transcriber。
        text = clean_transcript(text)
        if not text:
            raise TranscriptError(TranscriptErrorCode.EMPTY_RESULT,
                                  "语音识别没有返回可用文本", provider=self.name,
                                  hint="媒体可能是纯音乐 / 无人声；可手工粘贴字幕文本")
        return ProviderOutput(text=text, asset=media)

    def done_message(self, chars):
        return f"语音识别完成（{chars} 字符）"


class ExternalAsrProvider(TranscriptProvider):
    """来源 3：外部 ASR 服务（OpenAI 兼容的 `/audio/transcriptions`）。

    **本阶段没有可用的外部 API**，所以默认 `available()` 为 False，
    `fetch()` 抛 `provider_unconfigured` —— 宁可明确报「没配置」，
    也不伪造一条转写结果（伪造的数据会一路流进 AI 标注，最后没人知道是假的）。

    但接口与真实 HTTP 调用是写好的：在设置里填上 `asr_api_base` / `asr_api_key`
    （可选 `asr_model` / `asr_language`），或直接构造时传入，它就能真的工作。
    配置项故意不写进 settings_store.DEFAULTS：那是公共 contract，
    需要设置页配合时再由集成阶段统一加。
    """

    name = TranscriptSource.EXTERNAL_ASR
    kind = TranscriptSource.EXTERNAL_ASR
    label = "外部语音识别服务"
    start_message = "正在调用外部语音识别服务…"

    def __init__(self, api_base="", api_key="", model="whisper-1", language="",
                 http=None, timeout=300):
        self.api_base = str(api_base or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "whisper-1").strip() or "whisper-1"
        self.language = str(language or "").strip()
        self._http = http
        self.timeout = int(timeout or 300)

    @classmethod
    def from_settings(cls, settings, http=None):
        section = settings.section("ai") if settings is not None else {}
        return cls(api_base=section.get("asr_api_base") or "",
                   api_key=section.get("asr_api_key") or "",
                   # 外部服务的模型名单独一个键：asr_model 是本机模型大小，两者含义不同
                   model=section.get("asr_external_model") or "whisper-1",
                   language=section.get("asr_language") or "",
                   http=http)

    # ---- 探测 ----------------------------------------------------------
    def configured(self):
        return bool(self.api_base and self.api_key)

    def available(self):
        return self.configured()

    def detail(self):
        if self.configured():
            return f"已配置：{self.api_base}"
        return ("未配置外部语音识别服务（本阶段没有可用的外部 ASR API）；"
                "填好 asr_api_base / asr_api_key 后即可启用")

    def unavailable_error(self):
        return TranscriptError(
            TranscriptErrorCode.PROVIDER_UNCONFIGURED,
            "未配置外部语音识别服务（本阶段没有可用的外部 ASR API）",
            provider=self.name,
            hint="在设置里填写 asr_api_base / asr_api_key，或安装本机识别后端后重试",
            detail=self.detail())

    # ---- 转写 ----------------------------------------------------------
    def endpoint(self):
        base = self.api_base
        if not base:
            return ""
        return base if base.endswith("/audio/transcriptions") else base + "/audio/transcriptions"

    def fetch(self, item):
        if not self.configured():
            raise self.unavailable_error()
        media = _existing_media(item)
        if not media:
            raise _media_missing_error(item, provider=self.name)
        payload = self._post(media)
        text = clean_transcript(payload.get("text") if isinstance(payload, dict) else payload)
        if not text:
            raise TranscriptError(TranscriptErrorCode.EMPTY_RESULT,
                                  "外部语音识别没有返回可用文本", provider=self.name)
        return ProviderOutput(text=text, asset=media)

    def _post(self, media):
        http = self._http
        if http is None:
            import requests as http
        form = {"model": self.model}
        if self.language:
            form["language"] = self.language
        try:
            with open(media, "rb") as handle:
                response = http.post(
                    self.endpoint(),
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files={"file": (Path(media).name, handle, "application/octet-stream")},
                    data=form,
                    timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise TranscriptError(
                TranscriptErrorCode.ASR_FAILED,
                f"外部语音识别失败：{type(exc).__name__}: {exc}",
                provider=self.name,
                hint="确认服务地址 / API Key / 网络可用后重试") from exc

    def done_message(self, chars):
        return f"外部语音识别完成（{chars} 字符）"


def default_providers(settings=None, transcriber=None, probe=None, http=None):
    """默认 provider 链：已有字幕 → 本机 ASR → 外部服务。

    transcriber / probe 是给测试与 pipeline 注入用的（pipeline 会把
    ContentPipeline(transcribe=...) 的注入对象一路传到这里）。
    注意：这里给的是**全部**来源，具体用哪几个由 `asr_mode` + 调用方决定。
    """
    section = settings.section("ai") if settings is not None else {}
    audio_model = str(section.get("asr_model") or "").strip()
    language = str(section.get("asr_language") or "").strip()
    kwargs = {}
    if audio_model:
        kwargs["model_size"] = audio_model
    if language:
        kwargs["language"] = language
    return [
        SubtitleProvider(),
        LocalAsrProvider(transcriber=transcriber, probe=probe, transcribe_kwargs=kwargs),
        ExternalAsrProvider.from_settings(settings, http=http),
    ]


def asr_mode(settings):
    """把设置页「ASR / 转写」那一项翻译成链路模式。

    界面上的选项是固定的三个（ui/app.js:1119）：
        'OpenAI Whisper（本地）' | '不启用' | '外部服务'

    这个键本来就已经存在、也已经在设置页里可选，只是一直没有代码读它 ——
    ASR Core 把它接上，否则用户在设置页里的选择是句空话。
    认不出来的取值按「本机识别」处理（默认值也是它），不报错。
    """
    raw = ""
    if settings is not None:
        raw = str(settings.section("ai").get("asr_provider") or "").strip()
    text = raw.lower()
    if not raw:
        return ASR_MODE_LOCAL
    if "不启用" in raw or "禁用" in raw or text in ("off", "none", "disabled", "false"):
        return ASR_MODE_OFF
    if "外部" in raw or "external" in text or "api" in text:
        return ASR_MODE_EXTERNAL
    return ASR_MODE_LOCAL


def mode_enabled_kinds(mode):
    """某个模式下允许使用的来源（字幕永远允许：关掉 ASR 不等于放弃已有字幕）。"""
    if mode == ASR_MODE_OFF:
        return (TranscriptSource.SUBTITLE,)
    if mode == ASR_MODE_EXTERNAL:
        return (TranscriptSource.SUBTITLE, TranscriptSource.EXTERNAL_ASR)
    return (TranscriptSource.SUBTITLE, TranscriptSource.LOCAL_ASR,
            TranscriptSource.EXTERNAL_ASR)


def asr_provider_status(providers):
    """把 provider 链描述成可 JSON 化的列表（stats() / 诊断用）。"""
    return [provider.describe() for provider in (providers or [])]


def find_in_chain(providers, kind):
    """按 kind 在链里找 provider；找不到返回 None。"""
    for provider in providers or []:
        if getattr(provider, "kind", "") == kind:
            return provider
    return None


def local_asr_available(providers) -> Optional[bool]:
    """本机 ASR 后端是否可用（链里没有本机 provider 时返回 None）。"""
    provider = find_in_chain(providers, TranscriptSource.LOCAL_ASR)
    return None if provider is None else bool(provider.available())
