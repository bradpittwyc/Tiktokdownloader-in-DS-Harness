"""ASR / transcript 核心的数据模型：来源、结构化错误、运行结果。

这一层只描述「发生了什么」，不碰数据库、不碰任何识别实现：

- `TranscriptSource`  转写文本从哪来（已有字幕 / 本地 ASR / 外部 provider / 缓存 / 人工）
- `TranscriptError`   带错误码的失败：人话 message 给人看，机器可判的 code 给程序用
- `TranscriptResult`  一次转写尝试的完整结果（成功与失败共用同一个结构）

为什么失败必须是结构化的：失败原因决定「重试有没有意义」。
没装识别后端 → 提示装后端；没有媒体文件 → 提示先下载；字幕文件坏了 → 提示重新抓字幕；
外部服务没配置 → 提示填 Key。只靠一句中文文案，界面和脚本都没法判断该给用户哪条出路，
也就做不了「一键重试失败项」。

状态机沿用内容库已有的 `content_items.transcript_status`，**不新增状态**：
    pending → running → done | failed
"""

from dataclasses import dataclass, field
from typing import Optional

# ---- 状态机（与 content_items.transcript_status 完全一致）--------------
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
TRANSCRIPT_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED)


class TranscriptSource:
    """转写文本的来源。四种真实来源互相独立，结果里必须能区分。"""

    SUBTITLE = "subtitle"            # 1) 已有字幕文件直接读取（零成本、最准）
    LOCAL_ASR = "local_asr"          # 2) 本机 faster-whisper / openai-whisper
    EXTERNAL_ASR = "external_asr"    # 3) 外部 ASR 服务（接口就绪，见 providers.py）
    CACHED = "cached"                # 库里已经有文本，直接复用，没有重跑
    MANUAL = "manual"                # 人工粘贴 / 编辑（pipeline.set_transcript）

    ALL = (SUBTITLE, LOCAL_ASR, EXTERNAL_ASR, CACHED, MANUAL)


class TranscriptErrorCode:
    """失败原因的分类。界面 / 脚本据此决定「给什么出路」。"""

    CANCELLED = "cancelled"                          # 用户取消
    ASR_DISABLED = "asr_disabled"                    # 调用方明确关掉了语音识别
    BACKEND_UNAVAILABLE = "backend_unavailable"      # 本机没装识别后端
    PROVIDER_UNCONFIGURED = "provider_unconfigured"  # 外部 ASR 没配置（今天没有可用 API）
    SUBTITLE_UNREADABLE = "subtitle_unreadable"      # 有字幕文件，但读不出文本
    MEDIA_MISSING = "media_missing"                  # 没有可用的本地媒体文件
    NO_INPUT = "no_input"                            # 既没字幕也没媒体
    ASR_FAILED = "asr_failed"                        # 识别过程本身失败
    EMPTY_RESULT = "empty_result"                    # 识别回来是空的
    ITEM_MISSING = "item_missing"                    # 内容不存在
    UNKNOWN = "unknown"                              # 兜底（含 provider 自己的异常）

    ALL = (CANCELLED, ASR_DISABLED, BACKEND_UNAVAILABLE, PROVIDER_UNCONFIGURED,
           SUBTITLE_UNREADABLE, MEDIA_MISSING, NO_INPUT, ASR_FAILED, EMPTY_RESULT,
           ITEM_MISSING, UNKNOWN)


# 「重试有没有意义」：默认按错误码判断，个别情况可以在构造时覆盖。
# 不 retryable 的两种都是「重试也不会变」：内容被删了、调用方自己关掉了 ASR。
RETRYABLE_CODES = {
    TranscriptErrorCode.CANCELLED: True,
    TranscriptErrorCode.ASR_DISABLED: False,
    TranscriptErrorCode.BACKEND_UNAVAILABLE: True,      # 装完后端再重试
    TranscriptErrorCode.PROVIDER_UNCONFIGURED: True,    # 配好外部服务再重试
    TranscriptErrorCode.SUBTITLE_UNREADABLE: True,      # 重新抓字幕 / 手工粘贴后重试
    TranscriptErrorCode.MEDIA_MISSING: True,            # 下载完成后重试
    TranscriptErrorCode.NO_INPUT: False,                # 没有输入，重试还是没输入
    TranscriptErrorCode.ASR_FAILED: True,
    TranscriptErrorCode.EMPTY_RESULT: True,
    TranscriptErrorCode.ITEM_MISSING: False,
    TranscriptErrorCode.UNKNOWN: True,
}

# 多条错误同时出现时（例如「没装后端」+「没有媒体文件」+「外部服务没配置」），
# 挑最能说明问题、最可操作的那一条报给用户。
# 顺序 = 优先级，越靠前越优先。几条真实取舍：
# - 字幕文件坏了排在「没装后端」前面：前者能靠重新抓字幕 / 手工粘贴解决，更具体；
# - 「没装后端」排在「没有媒体文件」前面：两者都缺时前者更根本（没后端＝这条路走不通，
#   没下载＝还能先补下载）；顺序与旧实现一致，界面文案不变；
# - 「没有媒体文件」排在「外部服务没配置」前面：连素材都没有，报外部服务没配置是误导；
# - 真的试过并且失败（ASR_FAILED / EMPTY_RESULT）排在「外部服务没配置」前面：
#   试过之后的具体失败，比「有个来源没配置」有用得多。
ERROR_PRIORITY = (
    TranscriptErrorCode.CANCELLED,
    TranscriptErrorCode.ASR_DISABLED,
    TranscriptErrorCode.ITEM_MISSING,
    TranscriptErrorCode.SUBTITLE_UNREADABLE,
    TranscriptErrorCode.BACKEND_UNAVAILABLE,
    TranscriptErrorCode.MEDIA_MISSING,
    TranscriptErrorCode.ASR_FAILED,
    TranscriptErrorCode.EMPTY_RESULT,
    TranscriptErrorCode.PROVIDER_UNCONFIGURED,
    TranscriptErrorCode.NO_INPUT,
    TranscriptErrorCode.UNKNOWN,
)


class TranscriptError(RuntimeError):
    """一次转写失败：人话文案 + 机器可判的错误码。

    `str(error)` 就是给人看的那句话，可以直接写进 `content_items.last_error`
    （界面把它当纯文本渲染）；`to_dict()` 给需要分支处理的调用方。
    """

    def __init__(self, code, message, provider="", retryable=None, hint="", detail=""):
        super().__init__(str(message))
        self.code = str(code or TranscriptErrorCode.UNKNOWN)
        self.message = str(message or "")
        self.provider = str(provider or "")
        self.hint = str(hint or "")
        self.detail = str(detail or "")
        if retryable is None:
            retryable = RETRYABLE_CODES.get(self.code, True)
        self.retryable = bool(retryable)

    @property
    def kind(self):
        """给异常页 / 日志用的粗分类，沿用 errors_feed 的口径。"""
        return {
            TranscriptErrorCode.BACKEND_UNAVAILABLE: "语音识别不可用",
            TranscriptErrorCode.PROVIDER_UNCONFIGURED: "语音识别不可用",
            TranscriptErrorCode.ASR_FAILED: "语音识别失败",
            TranscriptErrorCode.EMPTY_RESULT: "语音识别失败",
            TranscriptErrorCode.SUBTITLE_UNREADABLE: "字幕文件读取失败",
            TranscriptErrorCode.MEDIA_MISSING: "缺少本地媒体文件",
            TranscriptErrorCode.NO_INPUT: "无字幕且无媒体",
            TranscriptErrorCode.ASR_DISABLED: "语音识别已关闭",
            TranscriptErrorCode.CANCELLED: "任务被取消",
            TranscriptErrorCode.ITEM_MISSING: "内容不存在",
        }.get(self.code, "转写失败")

    def to_dict(self):
        return {"code": self.code, "message": self.message, "provider": self.provider,
                "retryable": self.retryable, "hint": self.hint, "detail": self.detail,
                "kind": self.kind}

    def __repr__(self):
        return f"<TranscriptError {self.code}: {self.message}>"


def choose_error(errors, default=None):
    """从多次尝试的错误里挑一条最该报给用户的。errors 为空时返回 default。"""
    found = [error for error in (errors or []) if isinstance(error, TranscriptError)]
    if not found:
        if isinstance(default, TranscriptError):
            return default
        return TranscriptError(TranscriptErrorCode.NO_INPUT, "没有可用的转写来源",
                               hint="该内容既没有字幕文件，也没有可转写的本地媒体")
    for code in ERROR_PRIORITY:
        for error in found:
            if error.code == code:
                return error
    return found[0]


@dataclass
class ProviderOutput:
    """provider 成功拿到文本时的返回：文本 + 命中的素材路径。"""

    text: str = ""
    asset: str = ""          # 命中的文件（字幕文件 / 媒体文件），由 provider 声明写回哪个字段
    asset_field: str = ""    # 写回 content_items 的列名，空 = 不需要写回


@dataclass
class TranscriptResult:
    """一次转写尝试的结果（成功与失败共用）。

    to_dict() 是给桥接层 / 脚本用的 JSON 形状；库内部请直接读属性。
    """

    item_id: str = ""
    ok: bool = False
    text: str = ""
    source: str = ""
    status: str = STATUS_PENDING
    provider: str = ""
    asset: str = ""
    chars: int = 0
    attempts: int = 0
    cached: bool = False
    retried: bool = False
    elapsed: float = 0.0
    error: Optional[TranscriptError] = None
    notes: list = field(default_factory=list)

    def to_dict(self):
        return {
            "ok": self.ok,
            "itemId": self.item_id,
            "text": self.text,
            "chars": self.chars,
            "source": self.source,
            "status": self.status,
            "provider": self.provider,
            "asset": self.asset,
            "attempts": self.attempts,
            "cached": self.cached,
            "retried": self.retried,
            "elapsed": self.elapsed,
            "error": self.error.to_dict() if self.error else None,
            "notes": list(self.notes),
        }

    def __bool__(self):
        return bool(self.ok)
