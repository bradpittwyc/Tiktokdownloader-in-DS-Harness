"""采集能力的桥接实现：给内容工厂外壳用的 `content_*` 方法（**不改 content_bridge.py**）。

为什么要单独一个文件：内容工厂的桥接层（content_bridge.py）是公共接缝，
本阶段明确不许改。但界面上的「创作者监控 / 自动采集」两个页面需要真的能调用 ——
所以把实现放在这里，桥接层只需要**一行转发**：

    # content_bridge.py（由 Integration Agent 补，共 14 行）
    from content_factory.collector.bridge_api import CollectorApi
    ...
    self.bridge_collector = _Hidden(CollectorApi(
        store=unwrap(self.bridge_store), settings=unwrap(self.bridge_settings),
        downloader=downloader, emit=self._record_event))

    def content_collector_status(self):
        return unwrap(self.bridge_collector).content_collector_status()
    ... （其余同名列一行一个）

⚠️ 一个必须注意的坑：`ContentFactoryApi.__dir__` 只列 `vars(type(self))` 里的名字，
所以**不能**用「mixin 继承」的方式加方法（继承来的方法不会出现在 dir() 里，
pywebview 就发现不了）。必须在 ContentFactoryApi 类体里显式写这些一行方法。

方法清单（名字即对外契约）：
    content_creator_list / content_creator_save / content_creator_delete
    content_creator_toggle / content_creator_set_interval / content_creator_set_priority
    content_creator_check_now
    content_run_one_creator / content_run_one_status
    content_collect_tick / content_collector_status / content_collection_jobs
    content_collection_runs / content_collection_failures / content_collect_download
    content_collector_start / content_collector_stop
"""

import threading
import time
import uuid

from content_factory import errors_feed
from content_factory.creator_monitor import CreatorMonitorService
from content_factory.pipeline import ContentPipeline

from .handoff import download_jobs
from .runner import CollectorRunner
from .service import build_collector

# ---- 「立即跑 1 条」的用户可读文案 ------------------------------------
# 每一层失败都要说清楚是哪一层，否则用户看到的只有「运行失败」，没法自己排查。
MSG_NO_FOLDER = "未配置下载目录：请在「设置 → 存储设置」里填写保存路径"
MSG_CREATOR_MISSING = "创作者不存在"
MSG_CREATOR_DISABLED = "该创作者已暂停监控：请先点「启用」再运行"
MSG_NEEDS_VERIFICATION = "TikTok 需要安全验证：请到「视频库」里完成一次人工验证后重试"
MSG_NO_CONTENT = "没有发现新的可处理内容"
MSG_LIBRARY_MISSING = "下载器报告成功，但内容库里没有这条记录（可能是本地已有文件被跳过）"
MSG_ASR_UNAVAILABLE = "没有字幕，且本机语音识别（ASR）不可用"
MSG_NEEDS_API_KEY = "未配置 AI API Key：请在「设置 → AI 加工设置」里填写后重试"

MAX_RUNS_KEPT = 40

# 最近几次「立即跑 1 条」的状态（进程内，给界面轮询用的薄状态）。
# 刻意不建表、不建任务系统：真正的进度事实来自 collection_jobs / content_items /
# 已有的 enrichProgress 事件，这里只是把「这一步在干什么」串成一行字。
RUNS = {}
RUNS_LOCK = threading.Lock()


def _put_run(run):
    with RUNS_LOCK:
        RUNS[run["runId"]] = run
        if len(RUNS) > MAX_RUNS_KEPT:
            for key in sorted(RUNS, key=lambda item: RUNS[item].get("startedAt") or 0)[
                    :len(RUNS) - MAX_RUNS_KEPT]:
                RUNS.pop(key, None)
    return run


class CollectorApi:
    """采集相关的对外接口。内部仍然是「Collector 负责发现/判断/排队，
    下载器负责下载」这条分工，这里只做参数整形与后台线程包装。"""

    def __init__(self, store, settings=None, downloader=None, emit=None, source=None):
        self.store = store
        self.settings = settings
        self.downloader = downloader
        self._emit = emit or (lambda *_args, **_kwargs: None)
        self.monitor = CreatorMonitorService(store, settings=settings)
        self.collector = build_collector(store, downloader=downloader, settings=settings,
                                         source=source, emit=emit)
        # 「立即跑 1 条」的最后一步要调 ContentPipeline.enrich_one（字幕 → ASR → AI）。
        # 这里新建的是**同一条流水线类型**，不是第二套实现：它只拿 store / settings /
        # downloader / emit，与界面上「AI 加工」页用的是同一段代码。
        self.pipeline = ContentPipeline(store, settings, emit=emit, downloader=downloader)
        self._runner = None
        self._lock = threading.Lock()
        # 进程启动时清掉上一次留下的死租约，否则崩溃前的「正在检查」会一直挡着
        try:
            self.monitor.expire_leases()
        except Exception:
            pass

    # ---- Creator 管理 --------------------------------------------------
    def content_creator_list(self, enabled=None, search="", due_only=False, limit=200):
        creators = self.monitor.creators(
            enabled=None if enabled in (None, "", "all", "全部") else _as_bool(enabled),
            search=search or "", due_only=_as_bool(due_only), limit=int(limit or 200))
        return {"ok": True, "creators": creators, "stats": self.monitor.stats()}

    def content_creator_save(self, values=None):
        """新增或修改一个 Creator：带 id 是改，不带是新增（handle 唯一）。"""
        values = _normalize_fields(values or {})
        creator_id = str(values.pop("id", "") or values.pop("creatorId", "") or "")
        handle = values.pop("handle", "")
        if creator_id:
            if handle:
                values["handle"] = handle          # 允许改名，唯一性由 monitor 再查一次
            result = self.monitor.update_creator(creator_id, **values)
        else:
            result = self.monitor.create_creator(handle, **values)
        if result.get("ok"):
            result["creators"] = self.monitor.creators(limit=200)
        return result

    def content_creator_delete(self, creator_id):
        return self.monitor.delete_creator(creator_id)

    def content_creator_toggle(self, creator_id, enabled=True):
        return self.monitor.set_enabled(creator_id, _as_bool(enabled, default=True))

    def content_creator_set_interval(self, creator_id, value):
        return self.monitor.set_poll_interval(creator_id, value)

    def content_creator_set_priority(self, creator_id, value):
        return self.monitor.set_priority(creator_id, value)

    def content_creator_check_now(self, creator_id, background=True):
        if not background:
            return self.collector.check_now(creator_id)
        threading.Thread(target=self.collector.check_now, args=(creator_id,),
                         daemon=True, name="CollectorCheckNow").start()
        return {"ok": True, "started": True, "message": "已开始检查该创作者"}

    # ---- 单条闭环：一个 Creator 的一条新内容跑到底 ----------------------
    def content_run_one_creator(self, creator_id, background=True):
        """「立即跑 1 条」：检查这个 Creator → 挑 1 条最新未处理内容 → 下载 →
        入库 → 字幕 / ASR → AI 标注。

        只是把已有能力串起来，没有第二套下载 / 队列 / 流水线：
        poll_creator（发现+去重+排队）→ claim(limit=1)（CAS 抢 1 条）→
        download_jobs（下载器 handoff）→ content_register_download（下载器自己入库）→
        ContentPipeline.enrich_one（字幕优先 → ASR → AI）。

        前置检查当场做：下载目录没配、Creator 不存在 / 已暂停，都在**起后台线程之前**
        就返回明确原因 —— 后台线程里的报错用户看不见。
        """
        folder = self.collector.download_folder()
        if not folder:
            return {"ok": False, "started": False, "stage": "preflight",
                    "needsFolder": True, "error": MSG_NO_FOLDER, "message": MSG_NO_FOLDER}
        creator = self.monitor.creator(creator_id)
        if not creator:
            return {"ok": False, "started": False, "stage": "preflight",
                    "error": MSG_CREATOR_MISSING, "message": MSG_CREATOR_MISSING}
        if not creator.get("enabled"):
            return {"ok": False, "started": False, "stage": "preflight",
                    "creatorId": creator.get("id"), "handle": creator.get("handle") or "",
                    "error": MSG_CREATOR_DISABLED, "message": MSG_CREATOR_DISABLED}
        run = _put_run({
            "runId": f"run_{uuid.uuid4().hex[:12]}", "creatorId": creator.get("id") or "",
            "handle": creator.get("handle") or "", "status": "running", "stage": "checking",
            "stageLabel": "检查 Creator 新内容", "steps": [], "startedAt": time.time(),
            "updatedAt": time.time(), "finishedAt": 0, "result": None, "error": "",
        })
        if not background:
            return {**self._run_one_creator(run), "started": True}
        threading.Thread(target=self._run_one_creator, args=(run,), daemon=True,
                         name="RunOneCreator").start()
        return {"ok": True, "started": True, "runId": run["runId"],
                "creatorId": run["creatorId"], "handle": run["handle"],
                "stage": "checking", "message": f"已开始处理 @{run['handle']} 的 1 条内容"}

    def content_run_one_status(self, run_id="", creator_id=""):
        """查「立即跑 1 条」的进度：优先按 runId，其次取该 Creator 最近一次。"""
        with RUNS_LOCK:
            run = RUNS.get(str(run_id or "")) if run_id else None
            if run is None and creator_id:
                candidates = [item for item in RUNS.values()
                              if item.get("creatorId") == str(creator_id)]
                run = max(candidates, key=lambda item: item.get("startedAt") or 0,
                          default=None)
            return {"ok": True, "run": dict(run) if run else None}

    def _run_one_creator(self, run):
        """后台线程主体：每一步都往 run 里写状态，返回最终结果字典。"""
        run_id, creator_id = run["runId"], run["creatorId"]
        handle = run.get("handle") or ""

        def step(stage, stage_label, status="running", **extra):
            """记一步的进度。

            注意这里写的是**这一步**的状态，不是整次运行的状态 ——
            「发现完成」只是这一步 done，运行还在继续。整次运行的 done / failed
            只由 _finish() 写（踩过：把这一步的 done 当成整次的 done，
            界面会以为已经结束，而后面下载 / AI 还在跑）。
            """
            entry = {"stage": stage, "label": stage_label,
                     "state": "running" if status == "running" else status,
                     "at": time.time()}
            entry.update(extra)
            with RUNS_LOCK:
                current = RUNS.get(run_id)
                if current is not None:
                    current["stage"] = stage
                    current["stageLabel"] = stage_label
                    current["steps"] = (current.get("steps") or []) + [entry]
                    current["updatedAt"] = time.time()
            self._emit("runOneProgress", {"runId": run_id, "creatorId": creator_id,
                                          "handle": handle, "stage": stage,
                                          "stageLabel": stage_label, "state": status,
                                          **extra})

        try:
            # STEP A/B —— 检查这个 Creator 的最新内容（沿用现有发现链路）
            step("checking", "检查 Creator 新内容")
            poll = self.collector.check_now(creator_id, newest_only=True)
            if not poll.get("ok"):
                if poll.get("needsVerification"):
                    return self._finish(run, False, "check", MSG_NEEDS_VERIFICATION,
                                        needsVerification=True)
                return self._finish(run, False, "check",
                                   f"读取创作者主页失败：{poll.get('error') or poll.get('message') or '未知原因'}")
            summary = poll.get("summary") or {}
            step("discovered", f"发现 {summary.get('discovered', 0)} 条，"
                               f"新增候选 {summary.get('queued', 0)} 条", "done",
                 discovered=summary.get("discovered", 0), queued=summary.get("queued", 0))

            # STEP C —— 只取 1 条：**本次这一轮新排出来的**任务里的第一条。
            #
            # 候选来自下载器的主页读取，它按作品 id 倒序返回（新的在前），
            # 所以「第一条」就是最新的一条；库里已有 / 队列里已有的都已经被
            # DownloadPolicy 排除掉了，这里拿到的必然是「最新且尚未处理」的那条。
            #
            # 刻意**不**回退到「这个 Creator 队列里已有的 pending 任务」：
            # 按钮的语义是「跑 1 条**新**内容」，连点两次不能变成两条不同的视频，
            # 也不能把积压的旧任务顺手拖出来下。没有新内容就明说没有。
            jobs = poll.get("jobs") or []
            if not jobs:
                # 「没有新内容」不是异常：主页读到了几条，但都在内容库或队列里。
                # 文案必须说清楚是哪一种，否则用户会以为抓取失败了。
                discovered = int(summary.get("discovered") or 0)
                message = (f"{MSG_NO_CONTENT}（主页读到 {discovered} 条，"
                           f"都已在内容库或采集队列里）" if discovered else MSG_NO_CONTENT)
                step("no_content", message, "done")
                return self._finish(run, True, "no_content", message, processed=0,
                                    discovered=discovered)
            job = jobs[0]

            # STEP D —— CAS 抢这一条：抢不到说明另一个入口正在处理它
            claimed = self.collector.jobs.claim_jobs([job["id"]])
            if not claimed:
                message = "这条内容已经在处理中，本次没有重复下载"
                step("skipped", message, "done")
                return self._finish(run, True, "skipped", message, processed=0)
            step("downloading", f"下载 {job.get('title') or job.get('source_video_id') or ''}")
            outcome = download_jobs(self.collector, self.downloader, jobs=[job],
                                    folder=self.collector.download_folder(),
                                    quality=self.collector.download_quality(), limit=1)
            failure = self._download_failure(job, outcome)
            if failure:
                return self._finish(run, False, "download", failure, jobId=job["id"])
            step("downloaded", "下载完成", "done", folder=outcome.get("folder") or "")

            # STEP E —— 内容库那条记录由下载器自己写（content_register_download）
            item = self.collector.jobs.library_item(job.get("content_key"))
            if not item:
                return self._finish(run, False, "library", MSG_LIBRARY_MISSING,
                                    jobId=job["id"], sourceVideoId=job.get("source_video_id") or "")
            # STEP F —— 字幕优先 → 无字幕则本机 ASR → AI 标注（全在 enrich_one 里）
            step("transcribing", "读取字幕 / 本机 ASR")
            enriched = self.pipeline.enrich_one(item["id"])
            final = self.store.item(item["id"]) or {}
            transcript_state = final.get("transcript_status") or "pending"
            ai_state = final.get("ai_status") or "pending"
            step("enriching", "AI 标注", "running" if enriched.get("ok") else "failed")
            if enriched.get("ok"):
                step("done", "完成", "done")
                return self._finish(
                    run, True, "done", f"@{handle} 的 1 条内容已处理完成", processed=1,
                    jobId=job["id"], itemId=item["id"],
                    sourceVideoId=job.get("source_video_id") or "",
                    downloadState=final.get("download_status") or "",
                    transcriptState=transcript_state, aiState=ai_state,
                    subtitle=bool(final.get("local_subtitle_path")))
            # 没配 AI Key 时 enrich_one 会**先**判 Key 再判字幕，所以它返回的错误
            # 不是转写失败 —— 按 enricher 的 needsApiKey 走 enrich 这一层，
            # 否则用户会看到「字幕失败」而被引去查一个根本没问题的环节。
            if enriched.get("needsApiKey"):
                return self._finish(run, False, "enrich", enriched.get("error") or MSG_NEEDS_API_KEY,
                                    jobId=job["id"], itemId=item["id"],
                                    sourceVideoId=job.get("source_video_id") or "",
                                    downloadState=final.get("download_status") or "",
                                    transcriptState=transcript_state, aiState=ai_state,
                                    needsApiKey=True)
            if transcript_state != "done":
                # 下载和内容库记录都必须留着：ASR 不可用不是「内容丢了」的理由
                return self._finish(run, False, "transcript",
                                    f"字幕 / 转写失败：{enriched.get('error') or MSG_ASR_UNAVAILABLE}",
                                    jobId=job["id"], itemId=item["id"],
                                    sourceVideoId=job.get("source_video_id") or "",
                                    downloadState=final.get("download_status") or "",
                                    transcriptState=transcript_state, aiState=ai_state,
                                    needsAsr=not final.get("local_subtitle_path"))
            return self._finish(run, False, "enrich", enriched.get("error") or "AI 标注失败",
                                jobId=job["id"], itemId=item["id"],
                                sourceVideoId=job.get("source_video_id") or "",
                                downloadState=final.get("download_status") or "",
                                transcriptState=transcript_state, aiState=ai_state,
                                needsApiKey=bool(enriched.get("needsApiKey")))
        except Exception as exc:                       # 后台线程里的异常必须落到 run 状态里
            message = f"{type(exc).__name__}: {exc}"
            try:
                errors_feed.append_error(f"立即跑 1 条失败：{message}")
            except Exception:
                pass
            return self._finish(run, False, "error", message)

    def _download_failure(self, job, outcome):
        """从 download_jobs 的结果里提炼出「失败在哪一层」的人话原因。"""
        if outcome.get("ok") and not outcome.get("failedCount"):
            return ""
        video_id = str(job.get("source_video_id") or "")
        for entry in outcome.get("failed") or []:
            if str(entry.get("id")) == video_id:
                return f"下载失败：{entry.get('error') or '未知原因'}"
        return f"下载失败：{outcome.get('error') or '下载器没有返回成功'}"

    def _finish(self, run, ok, stage, message, **extra):
        result = {"ok": bool(ok), "creatorId": run["creatorId"], "handle": run.get("handle") or "",
                  "stage": stage, "message": message, "error": "" if ok else message,
                  "runId": run["runId"], "processed": extra.pop("processed", 1 if ok else 0)}
        result.update(extra)
        with RUNS_LOCK:
            current = RUNS.get(run["runId"])
            if current is not None:
                current["status"] = "done" if ok else "failed"
                current["finishedAt"] = time.time()
                current["updatedAt"] = time.time()
                current["result"] = result
                if not ok:
                    current["error"] = message
                result["steps"] = list(current.get("steps") or [])
        self._emit("runOneDone", {key: value for key, value in result.items() if key != "steps"})
        return result

    # ---- 采集调度 ------------------------------------------------------
    def content_collect_tick(self, force=False, limit=None):
        return self.collector.tick(limit=limit, force=bool(force), trigger="manual")

    def content_collector_status(self):
        status = self.collector.status()
        status["runner"] = self._runner.status() if self._runner else {"running": False}
        return status

    def content_collection_jobs(self, state="all", creator_id="", limit=100):
        return self.collector.jobs_view(state=None if state in ("all", "", None) else state,
                                        creator_id=creator_id or "", limit=int(limit or 100))

    def content_collection_runs(self, creator_id="", state="all", limit=100):
        return self.collector.runs_view(creator_id=creator_id or "",
                                        state=None if state in ("all", "", None) else state,
                                        limit=int(limit or 100))

    def content_collection_failures(self, limit=50):
        return self.collector.failures_view(limit=int(limit or 50))

    # ---- 排队内容 → 已有下载器 -----------------------------------------
    def content_collect_download(self, limit=10, folder="", quality="", background=True):
        """把排好队的采集任务交给下载器。

        默认后台执行：下载是分钟级的长任务，界面不能被它卡住。
        但**能不能开始**要当场判断（下载器在不在、下载目录配没配、有没有待办），
        否则界面会收到「已开始下载 N 条」，而后台线程里其实什么都没发生。
        """
        limit = int(limit or 10)
        error = self._download_preflight(folder)
        if error:
            self._record_download_failure(error, limit)
            return {"ok": False, "started": False, "count": 0, "error": error}
        pending = len(self.collector.pending_jobs(limit=limit))
        if not pending:
            return {"ok": True, "started": False, "count": 0, "message": "没有待下载的采集任务"}
        if not background:
            return download_jobs(self.collector, self.downloader, limit=limit,
                                 folder=folder or "", quality=quality or "")
        threading.Thread(
            target=self._download_worker,
            args=(limit, folder or "", quality or ""),
            daemon=True, name="CollectorDownload").start()
        return {"ok": True, "started": True, "count": pending, "message": f"已开始下载 {pending} 条"}

    def _download_preflight(self, folder=""):
        """后台任务开始之前能查的错，一律当场查 —— 后台线程里的报错没人看得见。"""
        if self.downloader is None or not callable(getattr(self.downloader, "download", None)):
            return "当前环境没有可用的下载器"
        if not (str(folder or "").strip() or self.collector.download_folder()):
            return "未配置下载目录：请在「设置 → 存储设置」里填写保存路径"
        return ""

    def _download_worker(self, limit, folder, quality):
        """后台下载线程：异常也要落到采集记录里，不能只进 threading 的 excepthook。"""
        try:
            result = download_jobs(self.collector, self.downloader, limit=limit,
                                   folder=folder, quality=quality)
        except Exception as exc:
            self._record_download_failure(f"{type(exc).__name__}: {exc}", limit)
            return
        if not result.get("ok"):
            self._record_download_failure(str(result.get("error") or "下载失败"), limit)
        return result

    def _record_download_failure(self, error, limit):
        try:
            self.collector.jobs.record_run(trigger="download", state="failed", error=error,
                                           message="采集任务下发下载失败")
        except Exception:
            pass
        try:
            errors_feed.append_error(f"采集任务下发下载失败：{error}")
        except Exception:
            pass

    # ---- 24/7 常驻（可选）----------------------------------------------
    def content_collector_start(self, interval=None, limit=None):
        with self._lock:
            if self._runner is None:
                self._runner = CollectorRunner.from_settings(
                    self.collector, self.settings, limit=limit, emit=self._emit)
            if interval:
                self._runner.interval = max(5, int(interval))
            result = self._runner.start()
            result["interval"] = self._runner.interval
        return result

    def content_collector_stop(self):
        # 先把 runner 取出来再解锁：stop() 会等这一轮跑完（最多 30 秒），
        # 握着锁等会把其它采集调用一起堵死。
        with self._lock:
            runner = self._runner
        if runner is None:
            return {"ok": True, "running": False, "message": "常驻采集没有在运行"}
        return runner.stop()


def _as_bool(value, default=False):
    """界面传过来的布尔值可能是字符串（"false" / "0"），不能直接当 True 用。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("0", "false", "no", "off", "否", "停用"):
        return False
    if text in ("1", "true", "yes", "on", "是", "启用"):
        return True
    return default


# 前端习惯用的驼峰名 -> 服务层的下划线名。
# 不做这层翻译的话，界面上写 pollIntervalSeconds 会「成功但什么都没改」——
# 这种静默无效最难查。服务层同样认这些别名（见 CreatorMonitorService.FIELD_ALIASES）。
FIELD_ALIASES = {
    "displayName": "display_name",
    "pollInterval": "poll_interval",
    "pollIntervalSeconds": "poll_interval_seconds",
    "followerCount": "followers",
    "videoCount": "videos",
}


def _normalize_fields(values):
    return {FIELD_ALIASES.get(key, key): value for key, value in dict(values or {}).items()}
