"""ASR / Transcript Core：本地视频 / 音频 → transcript，并写回内容库。

对外主入口是 `TranscriptService`：

    service = TranscriptService(store, settings, emit=...)
    result  = service.transcribe(item_id)      # 字幕优先 → 本机 ASR → 外部 provider
    result  = service.retry(item_id)           # 失败后重试
    state   = service.status(item_id)          # 文本 / 状态 / 结构化错误（重启后仍在）

四种来源在结果里明确区分（`result.source` / `TranscriptSource`）：
    1. subtitle      已有字幕文件直接读取
    2. local_asr     本机 faster-whisper / openai-whisper
    3. external_asr  外部 ASR 服务（接口就绪；本阶段没有可用 API，默认不可用）
    4. failed + retry 结构化错误（错误码 + 人话 + 是否可重试）

底层能力全部复用 `content_factory/transcript.py`（字幕清洗、字幕查找、后端探测、
真实识别调用），这里只补上「编排 + 写回 + 结构化错误」，没有第二套数据模型：
文本与状态仍然只写 `content_items.transcript_text` / `transcript_status`。
"""

from .models import (STATUS_DONE, STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING,  # noqa: F401
                     TRANSCRIPT_STATUSES, ProviderOutput, TranscriptError,
                     TranscriptErrorCode, TranscriptResult, TranscriptSource,
                     choose_error)
from .providers import (ASR_MODE_EXTERNAL, ASR_MODE_LOCAL, ASR_MODE_OFF,  # noqa: F401
                        ExternalAsrProvider, LocalAsrProvider, SubtitleProvider,
                        TranscriptProvider, asr_mode, default_providers,
                        mode_enabled_kinds)
from .runs import TranscriptRunLog  # noqa: F401
from .service import TranscriptService  # noqa: F401

__all__ = [
    "TranscriptService", "TranscriptError", "TranscriptErrorCode", "TranscriptResult",
    "TranscriptSource", "TranscriptProvider", "SubtitleProvider", "LocalAsrProvider",
    "ExternalAsrProvider", "default_providers", "TranscriptRunLog", "ProviderOutput",
    "choose_error", "asr_mode", "mode_enabled_kinds", "ASR_MODE_LOCAL",
    "ASR_MODE_EXTERNAL", "ASR_MODE_OFF", "STATUS_PENDING", "STATUS_RUNNING",
    "STATUS_DONE", "STATUS_FAILED", "TRANSCRIPT_STATUSES",
]
