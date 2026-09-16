"""编排层：把「任务队列」接到真实的流水线阶段上。

职责边界（重要）：
- 队列（queue.py）只管「什么时候轮到谁」；
- 本模块管「轮到谁的时候调用哪个阶段、跑完怎么改内容状态、失败要不要重试、跑完接哪一步」;
- **不实现**下载 / ASR / AI 调用本身。这些通过注入的适配器调用：
    download   <- downloader 适配器（真实下载器，由集成方注入）
    transcript <- ContentPipeline.ensure_transcript（复用既有字幕 / ASR 逻辑）
    enrich     <- ContentPipeline.enrich_one（复用既有 AI 标注逻辑）

目标状态链（与既有列一一对应，不另造状态机）：

    discovered ──download──▶ transcript ──▶ ai enrichment ──▶ ready
    download_status:   pending -> running -> done | failed
    transcript_status: pending -> running -> done | failed
    ai_status:         pending -> queued  -> running -> done | failed

内容状态的写入**由事件驱动**：队列写库成功后才发事件，编排层在事件里同步内容状态。
这样「任务表」和「内容表」不会出现互相矛盾的状态（例如任务被取消后
又被 handler 写成 done）。
"""

import threading
import time

from .queue import (EVENT_CANCELLED, EVENT_FAILED, EVENT_PROGRESS, EVENT_QUEUED,
                    EVENT_RECOVERED, EVENT_RETRYING, EVENT_STARTED, EVENT_SUCCEEDED,
                    JobQueue, WorkerPool)
from .retry import NonRetryableError, policies_from_settings
from .jobs import STATE_CANCELLED, STATE_DONE, STATE_FAILED, STATE_PENDING, STATE_QUEUED, \
    STATE_RETRYING, STATE_RUNNING

# 流水线阶段（顺序即默认链路顺序）
STAGES = ("download", "transcript", "enrich")

STAGE_LABELS = {
    "download": "下载",
    "transcript": "转写",
    "enrich": "AI 标注",
}

# 阶段 -> 内容表里的状态列
STAGE_STATUS_FIELD = {
    "download": "download_status",
    "transcript": "transcript_status",
    "enrich": "ai_status",
}

# 任务状态 -> 内容状态的中间语义（"queued" 表示「已排队但没在跑」）
JOB_STATE_GROUP = {
    STATE_PENDING: "queued",
    STATE_QUEUED: "queued",
    STATE_RETRYING: "queued",
    STATE_RUNNING: "running",
    STATE_DONE: "done",
    STATE_FAILED: "failed",
    STATE_CANCELLED: "cancelled",
}

# 中间语义 -> 各阶段内容列里的真实取值。
# 为什么 download / transcript 没有 queued：既有数据模型里
# 它们只有 pending -> running -> done | failed；AI 才有 queued。
# 这里必须尊重现状，否则界面上的筛选立刻错位。
STAGE_STATUS_MAP = {
    "download": {"queued": "pending", "running": "running", "done": "done",
                 "failed": "failed", "cancelled": "pending"},
    "transcript": {"queued": "pending", "running": "running", "done": "done",
                   "failed": "failed", "cancelled": "pending"},
    "enrich": {"queued": "queued", "running": "running", "done": "done",
               "failed": "failed", "cancelled": "pending"},
}

# 「重试也不会变好」的失败原因片段。这些原因来自既有 pipeline / 设置校验的文案，
# 命中就判成永久失败，直接进 failed，不空耗重试次数。
PERMANENT_MARKERS = (
    "API Key", "api key", "needsApiKey", "内容不存在", "找不到内容",
    "未安装语音识别后端", "尚未下载到本地", "已关闭语音识别", "没有可用于转写",
    "目录不存在", "未接入", "未注册", "not configured",
)

# 阶段结果里可以直接回写内容表的字段（camelCase 与 snake_case 都认）
CONTENT_RESULT_FIELDS = {
    "localVideoPath": "local_video_path",
    "local_video_path": "local_video_path",
    "localAudioPath": "local_audio_path",
    "local_audio_path": "local_audio_path",
    "localSubtitlePath": "local_subtitle_path",
    "local_subtitle_path": "local_subtitle_path",
    "thumbnailPath": "thumbnail_path",
    "thumbnail_path": "thumbnail_path",
    "transcriptText": "transcript_text",
    "transcript_text": "transcript_text",
    "duration": "duration",
    "title": "title",
    "sourceUrl": "source_url",
    "source_url": "source_url",
}


def is_permanent_reason(reason):
    """这条失败原因值不值得重试？"""
    text = str(reason or "")
    return any(marker in text for marker in PERMANENT_MARKERS)


class PipelineOrchestrator:
    """流水线编排器。

    典型用法（也是集成方要接的那两行）：

        orchestrator = build_orchestrator(store, settings, pipeline, downloader=api)
        orchestrator.start()          # 恢复上次中断的任务 + 起 worker
        orchestrator.submit_item(item_id)
        ...
        orchestrator.stop()
    """

    def __init__(self, store=None, settings=None, pipeline=None, downloader=None, queue=None,
                 emit=None, events=None, concurrency=2, policies=None, lease_seconds=900.0,
                 poll_interval=0.05, name="ContentJobs", clock=None, on_error=None,
                 legacy_events=False):
        self._store = store
        self._settings = settings
        self._pipeline = pipeline
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._stages = {}
        self._download_adapter = downloader
        self.legacy_events = bool(legacy_events)

        self.queue = queue or JobQueue(
            path=getattr(store, "path", None),
            policies=policies or policies_from_settings(settings),
            emit=emit, events=events, lease_seconds=lease_seconds, clock=self._clock)
        self.events = self.queue.events
        self.pool = WorkerPool(self.queue, self.execute, concurrency=concurrency,
                               poll_interval=poll_interval, name=name, on_error=on_error,
                               clock=self._clock)

        # 默认阶段：转写 / 标注复用既有 ContentPipeline；下载必须由集成方注入适配器
        self.register_handler("transcript", self._stage_transcript)
        self.register_handler("enrich", self._stage_enrich)
        if downloader is not None:
            self.register_handler("download", self._stage_download)

        # 内容状态同步与链路续跑都挂在事件上：只有任务表真的写成功了才动内容表
        self._sync_lock = threading.RLock()
        self.events.subscribe(self._on_event)

    # ---- 适配器注册 ----------------------------------------------------
    def register_handler(self, kind, handler):
        """注册某个阶段的处理器：handler(job, item) -> dict。

        返回 {"ok": True, ...} 表示成功；{"ok": False, "error": ...} 表示失败，
        可带 "permanent": True（不重试）或 "retryable": False（同上）。
        抛 NonRetryableError 等价于 permanent。
        """
        kind = str(kind or "").strip()
        if not kind:
            raise ValueError("kind 不能为空")
        if handler is not None and not callable(handler):
            raise TypeError("handler 必须可调用")
        with self._lock:
            if handler is None:
                self._stages.pop(kind, None)
            else:
                self._stages[kind] = handler
        return handler

    def set_downloader(self, downloader):
        """注入 / 替换下载适配器（可调用对象，或带 download_job(job, item) 的对象）。"""
        self._download_adapter = downloader
        if downloader is not None:
            self.register_handler("download", self._stage_download)
        else:
            self.register_handler("download", None)
        return downloader

    def registered_stages(self):
        with self._lock:
            return sorted(self._stages)

    # ---- 入队 ----------------------------------------------------------
    def submit(self, content_id, kind, priority=0, payload=None, chain=None, delay=0.0,
               max_attempts=None):
        """给一条内容排一个阶段。同阶段已有活跃任务时不重复入队。"""
        kind = str(kind or "").strip()
        if kind not in self.registered_stages():
            ready = ", ".join(self.registered_stages()) or "无"
            return {"ok": False, "created": False, "duplicate": False, "job": None,
                    "error": f"阶段未接入：{kind or '(空)'}（已接入：{ready}）"}
        body = dict(payload or {})
        if chain:
            body["chain"] = [str(step) for step in chain]
        return self.queue.enqueue(kind, content_id=content_id, payload=body, priority=priority,
                                  delay=delay, max_attempts=max_attempts)

    def submit_item(self, content_id, stages=None, priority=0, payload=None, restart=False):
        """按内容当前状态，排出「还差哪些阶段」，只入队第一步，跑完自动接下一步。"""
        item = self._item(content_id)
        if item is None:
            return {"ok": False, "created": 0, "error": f"内容不存在：{content_id}", "jobs": []}
        wanted = [str(step) for step in (stages or STAGES)]
        todo = [step for step in wanted
                if step in self.registered_stages()
                and (restart or not self._stage_done(item, step))]
        if not todo:
            return {"ok": True, "created": 0, "duplicate": 0, "message": "该内容没有待执行的阶段",
                    "jobs": [], "stages": []}
        result = self.submit(content_id, todo[0], priority=priority, payload=payload,
                             chain=todo[1:])
        result["stages"] = todo
        return result

    def submit_many(self, content_ids, stages=None, priority=0, payload=None, restart=False):
        """批量：一条内容排不进去不影响其它内容。"""
        jobs, failures, duplicates = [], [], []
        for content_id in content_ids or []:
            result = self.submit_item(content_id, stages=stages, priority=priority,
                                      payload=payload, restart=restart)
            if not result.get("ok"):
                failures.append({"contentId": content_id, "error": result.get("error")})
            elif result.get("created"):
                jobs.append(result.get("job"))
            else:
                duplicates.append({"contentId": content_id,
                                   "reason": result.get("reason") or result.get("message")})
        return {"ok": True, "created": len(jobs), "duplicates": len(duplicates),
                "failed": len(failures), "jobs": jobs, "skipped": duplicates,
                "errors": failures}

    def resubmit_failed(self, stages=("enrich",), limit=50, priority=0):
        """把失败的内容重新排队 —— 异常处理页的「重试」按钮用这个。

        只挑「指定阶段里任意一个失败」的内容，并且只重排这几个阶段。
        """
        store = self._store
        if store is None or not hasattr(store, "items"):
            return {"ok": False, "created": 0, "error": "没有接入内容库"}
        stages = [str(step) for step in (stages or ()) if step in STAGE_STATUS_FIELD]
        if not stages:
            return {"ok": False, "created": 0, "error": "没有可重排的阶段"}
        rows = store.items(limit=500)
        targets = [row["id"] for row in rows
                   if any(str(row.get(STAGE_STATUS_FIELD[step]) or "") == "failed"
                          for step in stages)][:max(0, int(limit or 0))]
        if not targets:
            return {"ok": True, "created": 0, "duplicates": 0, "failed": 0, "jobs": [],
                    "skipped": [], "errors": [], "message": "没有失败的内容需要重试"}
        return self.submit_many(targets, stages=stages, priority=priority, restart=True)

    # ---- 执行（WorkerPool 的 handler） ---------------------------------
    def execute(self, job):
        """跑一条任务并把结果翻译成队列能理解的返回值。"""
        kind = job.kind
        with self._lock:
            handler = self._stages.get(kind)
        if handler is None:
            return {"ok": False, "error": f"阶段未接入：{kind}", "permanent": True}
        item = self._item(job.content_id) if job.content_id else None
        if job.content_id and item is None:
            return {"ok": False, "error": f"内容不存在：{job.content_id}", "permanent": True}

        started = self._clock()
        self._emit_legacy(job, "running", f"{STAGE_LABELS.get(kind, kind)}中…")
        try:
            outcome = handler(job, item)
        except NonRetryableError as exc:
            return {"ok": False, "error": str(exc), "permanent": True}
        outcome = dict(outcome or {}) if isinstance(outcome, dict) else {"ok": True,
                                                                        "value": outcome}
        elapsed = round(float(self._clock()) - started, 3)
        outcome.setdefault("elapsed", elapsed)
        if outcome.get("ok", True) is False:
            reason = str(outcome.get("error") or f"{STAGE_LABELS.get(kind, kind)}失败")
            outcome["error"] = reason
            if not outcome.get("permanent"):
                outcome["permanent"] = is_permanent_reason(reason)
            self._emit_legacy(job, "failed", reason)
            return outcome

        if job.content_id and kind == "download":
            self._apply_content_fields(job.content_id, outcome)
        outcome.setdefault("message", f"{STAGE_LABELS.get(kind, kind)}完成（{elapsed}s）")
        self._emit_legacy(job, "done", str(outcome.get("message")))
        return outcome

    def run_once(self, worker_id=None):
        """在当前线程跑一条任务（手动步进 / 单任务同步执行）。"""
        return self.pool.run_once(worker_id=worker_id)

    def run_until_idle(self, timeout=30.0, poll=0.02):
        return self.pool.run_until_idle(timeout=timeout, poll=poll)

    # ---- 生命周期 ------------------------------------------------------
    def start(self, recover=True):
        """启动：先把上次进程留下的中断任务收回来，再起 worker。"""
        summary = self.recover() if recover else None
        self.pool.start()
        return summary or {"ok": True, "recovered": 0, "abandoned": 0, "jobs": []}

    def stop(self, wait=True, timeout=5.0):
        return self.pool.stop(wait=wait, timeout=timeout)

    def close(self):
        """停 worker 并释放任务表的连接（程序退出 / 测试清理）。"""
        self.pool.stop()
        return self.queue.close()

    def recover(self, now=None):
        """程序重启后的恢复：把 running 的孤儿任务拉回队列，并同步内容状态。"""
        return self.queue.recover(now=now)

    # ---- 状态 ----------------------------------------------------------
    def status(self):
        return {
            "ok": True,
            "runToken": self.queue.run_token,
            "queue": self.queue.status(),
            "workers": self.pool.stats(),
            "stages": [{"kind": kind, "label": STAGE_LABELS.get(kind, kind),
                        "field": STAGE_STATUS_FIELD.get(kind, ""),
                        "ready": kind in self.registered_stages()}
                       for kind in STAGES],
        }

    def counts(self):
        return self.queue.counts()

    def job(self, job_id):
        return self.queue.job_dict(job_id)

    def content_jobs(self, content_id, active_only=False):
        rows = self.queue.jobs(content_id=content_id, limit=200)
        if active_only:
            rows = [row for row in rows if row.is_active]
        return [row.to_dict() for row in rows]

    # ---- 阶段实现（薄封装，真正的活由既有模块干） ----------------------
    def _stage_download(self, job, item):
        adapter = self._download_adapter
        if adapter is None:
            raise NonRetryableError("未接入下载器：请通过 set_downloader 注入下载适配器")
        call = getattr(adapter, "download_job", None) or adapter
        result = call(job, item) if callable(call) else None
        if not isinstance(result, dict):
            return {"ok": False, "error": "下载适配器没有返回结果字典"}
        return result

    def _stage_transcript(self, job, item):
        pipeline = self._pipeline
        if pipeline is None:
            raise NonRetryableError("未接入 ContentPipeline：转写阶段无法执行")
        allow_asr = bool(job.payload.get("allowAsr", job.payload.get("allow_asr", True)))
        ok, text, reason = pipeline.ensure_transcript(job.content_id, allow_asr=allow_asr)
        if not ok:
            return {"ok": False, "error": reason or "转写失败"}
        return {"ok": True, "transcriptChars": len(text or ""),
                "message": f"字幕/转写就绪（{len(text or '')} 字符）"}

    def _stage_enrich(self, job, item):
        pipeline = self._pipeline
        if pipeline is None:
            raise NonRetryableError("未接入 ContentPipeline：AI 标注阶段无法执行")
        allow_asr = bool(job.payload.get("allowAsr", job.payload.get("allow_asr", True)))
        result = pipeline.enrich_one(job.content_id, allow_asr=allow_asr)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error") or "AI 标注失败",
                    "permanent": bool(result.get("needsApiKey"))}
        return {"ok": True, "enrichment": result.get("enrichment"),
                "attempts": result.get("attempts"), "elapsed": result.get("elapsed"),
                "message": "AI 标注完成"}

    # ---- 内容表同步（事件驱动） ----------------------------------------
    def _on_event(self, name, payload):
        try:
            self._handle_event(name, payload)
        except Exception:
            # 同步失败不能反过来影响任务执行：任务表才是事实来源
            pass

    def _handle_event(self, name, payload):
        if name == EVENT_RECOVERED:
            for job in list(payload.get("jobs") or []) + list(payload.get("abandonedJobs") or []):
                self._sync_job(dict(job, message=payload.get("message", "")))
            return
        if name == EVENT_PROGRESS:
            return                       # 只是播报，不改状态
        if name in (EVENT_QUEUED, EVENT_STARTED, EVENT_RETRYING, EVENT_FAILED, EVENT_SUCCEEDED,
                    EVENT_CANCELLED):
            self._sync_job(payload)
        if name == EVENT_SUCCEEDED:
            self._continue_chain(payload)

    def _sync_job(self, payload):
        kind = payload.get("kind") or payload.get("stage") or ""
        content_id = payload.get("contentId") or payload.get("id") or ""
        state = payload.get("state") or ""
        if not content_id or kind not in STAGE_STATUS_FIELD:
            return
        group = JOB_STATE_GROUP.get(state)
        if group is None:
            return
        value = STAGE_STATUS_MAP.get(kind, {}).get(group)
        if value is None:
            return
        error = str(payload.get("error") or "")
        field = STAGE_STATUS_FIELD[kind]
        fields = {field: value}
        if group == "failed":
            fields["last_error"] = error[:1000]
        elif group == "queued" and error:
            fields["last_error"] = error[:1000]
        elif group == "done":
            fields["last_error"] = ""
        # 只在真的会变化时写库：内容表的写入在慢盘上很贵，而且没必要的写
        # 会把 updated_at 刷新一遍，界面上的「最近更新」排序就乱了。
        item = self._item(content_id)
        if item is None:
            return
        changed = {key: val for key, val in fields.items() if str(item.get(key) or "") != str(val)}
        if changed:
            self._update_item(content_id, **changed)

    def _continue_chain(self, payload):
        """一步成功后自动接下一步（链路写在 payload.chain 里，重启后依然有效）。"""
        body = payload.get("payload")
        chain = body.get("chain") if isinstance(body, dict) else None
        content_id = payload.get("contentId") or payload.get("id") or ""
        if not chain or not content_id:
            return
        item = self._item(content_id)
        if item is None:
            return
        remaining = [str(step) for step in chain]
        guard = 0
        while remaining and guard < len(STAGES) + 1:
            guard += 1
            kind = remaining.pop(0)
            if kind not in self.registered_stages() or self._stage_done(item, kind):
                continue
            self.submit(content_id, kind, priority=int(payload.get("priority") or 0),
                        payload={"chain": remaining})
            return

    def _apply_content_fields(self, content_id, result):
        fields = {}
        for key, column in CONTENT_RESULT_FIELDS.items():
            if key in result and result[key] not in (None, ""):
                fields[column] = result[key]
        if fields:
            self._update_item(content_id, **fields)

    def _update_item(self, content_id, **fields):
        store = self._store
        if store is None or not hasattr(store, "update_item"):
            return 0
        with self._sync_lock:
            return store.update_item(content_id, **fields)

    def _item(self, content_id):
        store = self._store
        if store is None or not hasattr(store, "item"):
            return {"id": content_id} if content_id else None
        return store.item(content_id)

    @staticmethod
    def _stage_done(item, kind):
        field = STAGE_STATUS_FIELD.get(kind)
        if not field or not item:
            return False
        return str(item.get(field) or "") == "done"

    # ---- 可选的兼容事件（给现有界面用） --------------------------------
    def _emit_legacy(self, job, state, message):
        """把任务事件翻译成既有界面的 enrichProgress 形状（默认关闭）。

        为什么默认关闭：现有 ContentPipeline 自己已经在发 enrichProgress，
        两边一起发会让界面收到重复状态。集成方二选一即可。
        """
        if not self.legacy_events or job.kind != "enrich":
            return
        self.events.emit(EVENT_PROGRESS, {
            "id": job.content_id, "contentId": job.content_id, "jobId": job.id,
            "state": state, "kind": job.kind, "stage": job.kind,
            "stageLabel": STAGE_LABELS.get(job.kind, job.kind),
            "message": message, "attempt": job.attempts, "maxAttempts": job.max_attempts,
            "legacy": True, "queue": self.queue.counts(),
        })


def build_orchestrator(store=None, settings=None, pipeline=None, downloader=None, emit=None,
                       concurrency=None, lease_seconds=900.0, autostart=False, legacy_events=False,
                       policies=None):
    """按现有组件拼一个可直接用的编排器（集成方只需要这一行）。

    - concurrency 默认取设置里的 `work_mode.concurrent_tasks`（默认 3），
      与既有设置页的「并发任务数」保持一致；
    - 事件 sink 直接给 web_app 的 _record_event 即可（签名是 (name, payload)）。
    """
    if concurrency is None:
        concurrency = 3
        if settings is not None:
            try:
                concurrency = int(settings.section("work_mode").get("concurrent_tasks") or 3)
            except Exception:
                concurrency = 3
    orchestrator = PipelineOrchestrator(
        store=store, settings=settings, pipeline=pipeline, downloader=downloader, emit=emit,
        concurrency=concurrency, lease_seconds=lease_seconds, policies=policies,
        legacy_events=legacy_events)
    if autostart:
        orchestrator.start()
    return orchestrator
