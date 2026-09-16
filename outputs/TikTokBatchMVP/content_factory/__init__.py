"""Tony Content Engine —— 内容工厂服务层。

设计原则（今天这一版）：
- UI 与下载 / 分析逻辑解耦：本包不 import webview，也不碰 pywebview 的 window 对象，
  只提供纯数据的服务函数，方便单测与以后换成别的界面。
- 只用本地存储：sqlite（结构化数据）+ json（设置项），不接任何云端数据库。
- 复杂能力（云端发布、对象存储、自动调度）今天只留接口，不实现。
"""

from .factory_store import FactoryStore, content_key, new_id  # noqa: F401
from .settings_store import FactorySettings  # noqa: F401
from .ai_enrichment import (  # noqa: F401
    EnrichmentError,
    ENRICHMENT_FIELDS,
    DEFAULT_PROMPT_TEMPLATE,
    parse_enrichment,
    build_prompt,
    normalize_enrichment,
)
from .transcript import transcript_from_subtitle_file, transcribe_media, ASRUnavailable  # noqa: F401
from .asr import (  # noqa: F401
    TranscriptError,
    TranscriptErrorCode,
    TranscriptResult,
    TranscriptService,
    TranscriptSource,
)
from .pipeline import ContentPipeline  # noqa: F401
from .mock_data import seed_demo_items  # noqa: F401
from .errors_feed import recent_errors  # noqa: F401

# 任务编排内核（队列 / 重试 / 编排），与上面的服务层互不依赖，可单独使用
from .jobs import Job, JobStore  # noqa: F401
from .retry import RetryPolicy, RetryDecision, NonRetryableError, DEFAULT_POLICIES  # noqa: F401
from .queue import JobQueue, WorkerPool, JobEvents  # noqa: F401
from .orchestrator import PipelineOrchestrator, build_orchestrator  # noqa: F401
