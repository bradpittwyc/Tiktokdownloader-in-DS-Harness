"""下载判断：这条候选内容到底要不要进采集任务队列。

Collector 的职责是「发现 + 判断 + 排队」。判断这一半全在这里，规则明确、
可单测、不碰数据库也不碰网络 —— 上下文（库里已有什么、队列里已有什么）
由调用方一次性查好传进来。

判断顺序是有讲究的（先排除、后过滤）：
1. 拿不到 id/链接 → 建不出任务
2. 批次内重复 → 同一次采集里出现两遍
3. 队列里已有未完成的同一个 job → 重复采集保护（最重要的一条）
4. 内容库里已有 → 不重复进入任务；已下载的不再下，失败过的按次数重试
5. 硬过滤：时长 / 类型 / 屏蔽词
6. 单次单个创作者的上限（防止一次抓回几千条把队列灌爆）
"""

from dataclasses import dataclass

# 跳过原因 -> 中文说明（界面/日志直接用，别在别处再写一套文案）
REASON_LABELS = {
    "new": "新内容，进入采集队列",
    "retry_after_failure": "上次采集失败，按策略重试",
    "retry_failed_item": "内容库里标记为下载失败，按策略重试",
    "missing_source": "缺少作品 id 与链接，无法建立任务",
    "duplicate_in_batch": "本次采集里重复出现",
    "duplicate_key": "与本次采集的另一条内容重复（content_key 相同）",
    "duplicate_url": "与本次采集的另一条内容重复（链接相同）",
    "job_active": "队列里已有未完成的同一个任务",
    "job_done": "已经采集完成过，且内容库中已不存在",
    "attempts_exhausted": "重试次数已用尽",
    "already_downloaded": "内容库中已下载",
    "in_library": "内容库中已有该内容",
    "too_short": "时长低于下限",
    "too_long": "时长超过上限",
    "type_excluded": "内容类型不在采集范围内",
    "keyword_blocked": "标题命中屏蔽词",
    "keyword_not_allowed": "标题未命中允许词",
    "per_creator_limit": "达到单个创作者的单次采集上限",
    "disabled": "该创作者已停用",
    "cancelled": "任务已取消",
}


def reason_label(reason):
    return REASON_LABELS.get(reason, reason or "")


@dataclass
class Decision:
    download: bool
    reason: str
    detail: str = ""
    content_item_id: str = ""
    attempts: int = 0

    def to_dict(self):
        return {"download": self.download, "reason": self.reason,
                "label": reason_label(self.reason), "detail": self.detail,
                "contentItemId": self.content_item_id, "attempts": self.attempts}


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


class DownloadPolicy:
    """采集策略。默认值偏保守：宁可少下一个，也不要重复下一个。"""

    def __init__(self, min_duration=0, max_duration=0, include_kinds=("video", "photo"),
                 keywords_block=(), keywords_allow=(), keywords_allow_enabled=False,
                 per_creator_limit=0, max_attempts=3, retry_failed=True,
                 download_library_pending=False):
        self.min_duration = _as_int(min_duration)
        self.max_duration = _as_int(max_duration)
        self.include_kinds = tuple(kind for kind in (include_kinds or ()) if kind) or ("video", "photo")
        self.keywords_block = tuple(str(word).strip().lower() for word in (keywords_block or ()) if str(word).strip())
        self.keywords_allow = tuple(str(word).strip().lower() for word in (keywords_allow or ()) if str(word).strip())
        self.keywords_allow_enabled = bool(keywords_allow_enabled)
        self.per_creator_limit = _as_int(per_creator_limit)
        self.max_attempts = max(1, _as_int(max_attempts) or 3)
        self.retry_failed = bool(retry_failed)
        self.download_library_pending = bool(download_library_pending)

    # ---- 构造 ----------------------------------------------------------
    @classmethod
    def from_settings(cls, settings, **overrides):
        """从「设置 → 采集」读出策略。

        注意 `keywords_allow`：设置里默认就填着 tutorial / how to 这类词，
        如果默认当成白名单，绝大多数真实内容都会被挡掉（等于采集失效）。
        所以白名单默认**关闭**，要显式打开 keywords_allow_enabled 才生效。
        """
        section = {}
        if settings is not None:
            try:
                section = settings.section("collect") or {}
            except Exception:
                section = {}
        include = section.get("include_types")
        kinds = ["video", "photo"]
        if isinstance(include, list) and include:
            kinds = []
            for item in include:
                text = str(item)
                if "图" in text or "photo" in text.lower():
                    kinds.append("photo")
                if "视频" in text or "video" in text.lower():
                    kinds.append("video")
            kinds = kinds or ["video", "photo"]
        config = {
            "min_duration": section.get("duration_min_sec") or 0,
            "max_duration": section.get("duration_max_sec") or 0,
            "include_kinds": kinds,
            "keywords_block": section.get("keywords_block") or (),
            "keywords_allow": section.get("keywords_allow") or (),
            "keywords_allow_enabled": section.get("keywords_allow_enabled", False),
            "per_creator_limit": section.get("per_creator_limit") or 0,
            "max_attempts": section.get("fail_retry") or 3,
        }
        config.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**config)

    # ---- 判断 ----------------------------------------------------------
    def decide(self, candidate, context=None):
        """candidate: CandidateVideo；context 里认得这些 key（都可省略）：

        - library_items：content_key -> 内容库那一条（判断「已经有了」）
        - active_keys：未完成任务的 content_key 集合（判断「队列里已有」）
        - job_history：content_key -> 最近一次任务（判断「要不要重试」）
        - creator_queued：本次采集已经接受了几条（单次上限）
        - disabled：该创作者是否停用（ContentCollector 在进这里之前就挡掉了，
          这个分支留给直接调用 policy 的地方）
        - batch_duplicate：批次内重复原因（ContentCollector 用 DedupeIndex
          先一步挡住，这个分支同样留给直接调用方）
        """
        context = context or {}
        key = candidate.content_key
        if not key:
            return Decision(False, "missing_source")

        if context.get("disabled"):
            return Decision(False, "disabled")

        if context.get("batch_duplicate"):
            return Decision(False, context["batch_duplicate"],
                            detail=candidate.source_url or candidate.source_video_id)

        active = context.get("active_keys") or set()
        if key in active:
            return Decision(False, "job_active", detail=key)

        history = (context.get("job_history") or {}).get(key) or {}
        previous_attempts = _as_int(history.get("attempts"))
        if history:
            state = str(history.get("state") or "")
            if state == "running":
                return Decision(False, "job_active", detail=key)
            if state == "failed" and not (self.retry_failed and previous_attempts < self.max_attempts):
                return Decision(False, "attempts_exhausted",
                                detail=f"已失败 {previous_attempts} 次")
            if state == "done":
                return Decision(False, "job_done", detail=key)

        item = (context.get("library_items") or {}).get(key)
        retry_library = False
        if item:
            status = str(item.get("download_status") or "pending").lower()
            if status == "done":
                return Decision(False, "already_downloaded", detail=key,
                                content_item_id=item.get("id") or "")
            if status == "failed":
                if not self.retry_failed:
                    return Decision(False, "attempts_exhausted", detail=key,
                                    content_item_id=item.get("id") or "")
                retry_library = True
            elif not self.download_library_pending:
                return Decision(False, "in_library", detail=key,
                                content_item_id=item.get("id") or "")

        duration = _as_int(candidate.duration)
        if duration:
            if self.min_duration and duration < self.min_duration:
                return Decision(False, "too_short", detail=f"{duration}s < {self.min_duration}s")
            if self.max_duration and duration > self.max_duration:
                return Decision(False, "too_long", detail=f"{duration}s > {self.max_duration}s")

        kind = candidate.kind if candidate.kind in ("video", "photo") else "video"
        if kind not in self.include_kinds:
            return Decision(False, "type_excluded", detail=kind)

        title = str(candidate.title or "").lower()
        for word in self.keywords_block:
            if word in title:
                return Decision(False, "keyword_blocked", detail=word)
        if self.keywords_allow_enabled and self.keywords_allow:
            if not any(word in title for word in self.keywords_allow):
                return Decision(False, "keyword_not_allowed")

        limit = self.per_creator_limit
        if limit and _as_int(context.get("creator_queued")) >= limit:
            return Decision(False, "per_creator_limit", detail=f"上限 {limit} 条/次")

        reason = "retry_after_failure" if history.get("state") == "failed" else (
            "retry_failed_item" if retry_library else "new")
        return Decision(True, reason, attempts=previous_attempts)
