"""ContentCollector：创作者配置 → 定期检查 → 候选发现 → 去重 → 判断 → 排队。

一条完整的链路（每一步都能单独调用，也都能单独测）：

    monitor.creators()/due_creators()      谁该被检查
      └─ poll_creator(creator_id)          检查一个创作者
           ├─ source.discover()            候选内容 metadata（复用下载器 recognize）
           ├─ models.normalize_candidates() 归一化
           ├─ DedupeIndex                 本次批次内去重
           ├─ DownloadPolicy.decide()     要不要下载（库里已有 / 队列已有 / 过滤规则）
           ├─ jobStore.enqueue()          生成标准 collection job（唯一索引兜底）
           └─ monitor.mark_checked()      记录成功 / 失败 + 下次检查时间
      └─ plan()/claim()                    把待办 job 变成下载器能吃的形状
      └─ reconcile()                       依据内容库实际结果收尾 job（成功/失败）

边界（刻意不越界）：
- 不下载：一个字节都不写，只生成任务。
- 不抓取：抓取逻辑全在下载器里，这里只调用它。
- 不新增入库协议：下载完成仍然由下载器走原来的
  `content_register_download`，我们只是**读**内容库来判断任务结果。
"""

import threading
import time
import uuid

from content_factory.creator_monitor import CreatorMonitorService

from . import dedupe, models
from .policy import DownloadPolicy
from .sources import DownloaderCandidateSource, StaticCandidateSource
from .store import CollectionJobStore

DEFAULT_TICK_LIMIT = 20
DEFAULT_LEASE_SECONDS = 1800
MAX_DECISION_DETAIL = 200


class ContentCollector:
    def __init__(self, store, monitor=None, settings=None, source=None, emit=None,
                 policy=None, job_store=None, now=None, owner="", lease_seconds=DEFAULT_LEASE_SECONDS):
        self.store = store
        self.settings = settings
        self._now = now
        self.monitor = monitor or CreatorMonitorService(store, settings=settings, now=now)
        self.jobs = job_store or CollectionJobStore(store, now=now)
        self.source = source or StaticCandidateSource(error="未配置候选项来源")
        self.policy = policy or DownloadPolicy.from_settings(settings)
        self._emit = emit or (lambda *_args, **_kwargs: None)
        self._lease_seconds = int(lease_seconds)
        self._owner = owner or f"collector-{uuid.uuid4().hex[:8]}"
        self._locks = {}
        self._locks_guard = threading.Lock()

    # ---- 事件 ----------------------------------------------------------
    def set_emit(self, emit):
        self._emit = emit or (lambda *_args, **_kwargs: None)

    def _report(self, name, payload):
        try:
            self._emit(name, payload)
        except Exception:
            pass

    def _stamp(self):
        from content_factory.creator_monitor import intervals
        return intervals.stamp(self._now() if self._now else None)

    def _creator_lock(self, creator_id):
        """进程内的每创作者互斥：数据库租约负责跨进程，这个负责同进程并发。

        用完就把空锁删掉：24/7 常驻跑下去，creator_id 会越攒越多，
        不清理就是一个慢性内存泄漏。
        """
        with self._locks_guard:
            lock = self._locks.get(creator_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[creator_id] = lock
            return lock

    def _release_creator_lock(self, creator_id, lock):
        lock.release()
        with self._locks_guard:
            if not lock.locked() and self._locks.get(creator_id) is lock:
                self._locks.pop(creator_id, None)

    # ---- 上下文 --------------------------------------------------------
    def _context(self, creator, active_keys=None, history=None, creator_queued=0):
        library = self.jobs.library_index(creator_id=creator.get("id"),
                                          creator_handle=creator.get("handle"))
        return {
            "library_items": library,
            "active_keys": active_keys if active_keys is not None else self.jobs.active_keys(),
            "job_history": history if history is not None else self.jobs.history(creator.get("id")),
            "creator_queued": creator_queued,
        }

    # ---- 采集主体 ------------------------------------------------------
    def poll_creator(self, creator_id, trigger="poll", newest_only=True, force=False):
        """检查一个 Creator：发现候选 → 去重 → 判断 → 排队。同步执行，不抛异常。

        「不抛异常」是硬约束：桥接层的后台线程没人接异常，抛出去就只剩
        threading 的 excepthook —— 界面看不到、记录里也没有。所以这里连
        「读 Creator」「写 busy/skipped 记录」这些边角路径也一起兜住。
        """
        try:
            return self._poll_creator(creator_id, trigger=trigger, newest_only=newest_only,
                                      force=force)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            run = None
            try:
                run = self.jobs.record_run(creator_id=str(creator_id or ""), trigger=trigger,
                                           state="failed", error=message,
                                           message="采集器内部错误")
            except Exception:
                pass
            self._report("collectProgress", {"creatorId": str(creator_id or ""),
                                             "state": "failed", "message": message})
            return {"ok": False, "error": message, "creatorId": str(creator_id or ""),
                    "state": "failed", "run": run}

    def _poll_creator(self, creator_id, trigger="poll", newest_only=True, force=False):
        started = self._stamp()
        creator = self.monitor.creator(creator_id)
        if not creator:
            return {"ok": False, "error": "创作者不存在", "creatorId": creator_id}
        handle = creator.get("handle") or ""
        batch_id = f"batch_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

        if not creator.get("enabled") and not force:
            run = self.jobs.record_run(creator_id=creator["id"], creator_handle=handle,
                                       trigger=trigger, state="skipped", started_at=started,
                                       error="创作者已停用", batch_id=batch_id)
            return {"ok": False, "skipped": True, "reason": "disabled", "creatorId": creator["id"],
                    "handle": handle, "run": run, "message": "创作者已停用，跳过本次检查"}

        lock = self._creator_lock(creator["id"])
        if not lock.acquire(blocking=False):
            run = self.jobs.record_run(creator_id=creator["id"], creator_handle=handle,
                                       trigger=trigger, state="busy", started_at=started,
                                       error="上一次检查还没结束", batch_id=batch_id)
            return {"ok": False, "busy": True, "reason": "already_running",
                    "creatorId": creator["id"], "handle": handle, "run": run,
                    "message": "上一次检查还没结束，本次跳过"}
        try:
            if not self.monitor.begin_check(creator["id"], owner=self._owner,
                                            lease_seconds=self._lease_seconds):
                run = self.jobs.record_run(creator_id=creator["id"], creator_handle=handle,
                                           trigger=trigger, state="busy", started_at=started,
                                           error="该创作者正在被另一个采集进程检查",
                                           batch_id=batch_id)
                return {"ok": False, "busy": True, "reason": "leased",
                        "creatorId": creator["id"], "handle": handle, "run": run,
                        "message": "该创作者正在被另一个采集进程检查"}
            self._report("collectProgress", {
                "creatorId": creator["id"], "handle": handle, "state": "running",
                "trigger": trigger, "message": f"正在检查 @{handle} 的新内容…"})
            try:
                return self._run_poll(creator, trigger=trigger, newest_only=newest_only,
                                      batch_id=batch_id, started=started)
            except Exception as exc:
                # 采集链路上的意外异常也要留下失败记录，不能只让调用方看到 500。
                message = f"{type(exc).__name__}: {exc}"
                self.monitor.mark_checked(creator["id"], state="failed", error=message)
                run = self.jobs.record_run(
                    creator_id=creator["id"], creator_handle=handle, trigger=trigger,
                    state="failed", started_at=started, error=message, batch_id=batch_id)
                self._report("collectProgress", {
                    "creatorId": creator["id"], "handle": handle, "state": "failed",
                    "message": message})
                return {"ok": False, "error": message, "creatorId": creator["id"],
                        "handle": handle, "run": run}
        finally:
            self.monitor.end_check(creator["id"], owner=self._owner)
            self._release_creator_lock(creator["id"], lock)

    def _run_poll(self, creator, trigger, newest_only, batch_id, started):
        handle = creator.get("handle") or ""
        result = self.source.discover(creator, newest_only=newest_only)
        if not result.ok:
            message = result.error or "读取创作者主页失败"
            self.monitor.mark_checked(creator["id"], state="failed", error=message)
            run = self.jobs.record_run(
                creator_id=creator["id"], creator_handle=handle, trigger=trigger,
                state="failed", started_at=started, error=message,
                needs_verification=result.needs_verification, batch_id=batch_id)
            self._report("collectProgress", {
                "creatorId": creator["id"], "handle": handle, "state": "failed",
                "message": message, "needsVerification": bool(result.needs_verification)})
            return {"ok": False, "error": message, "creatorId": creator["id"], "handle": handle,
                    "needsVerification": bool(result.needs_verification), "run": run}

        candidates = models.normalize_candidates(result.videos, handle=handle)
        active_keys = self.jobs.active_keys()
        history = self.jobs.history(creator["id"])
        context = self._context(creator, active_keys=active_keys, history=history)

        seen = dedupe.DedupeIndex()
        decisions, duplicate_batch, duplicate_queue = [], 0, []
        existing, filtered = [], []
        to_enqueue, accepted = [], []

        for candidate in candidates:
            duplicate_reason = seen.check(candidate.content_key, candidate.source_url)
            if duplicate_reason:
                duplicate_batch += 1
                decisions.append({**candidate.to_dict(), "download": False,
                                  "reason": duplicate_reason})
                continue
            decision = self.policy.decide(candidate, context)
            decisions.append({**candidate.to_dict(), **decision.to_dict()})
            if not decision.download:
                if decision.reason in ("job_active",):
                    duplicate_queue.append(decision)
                elif decision.reason in ("already_downloaded", "in_library", "job_done"):
                    existing.append(decision)
                else:
                    filtered.append(decision)
                continue
            to_enqueue.append(models.job_payload(
                candidate, creator_id=creator["id"], creator_handle=handle, batch_id=batch_id,
                priority=creator.get("priority") or "中",
                max_attempts=self.policy.max_attempts, attempts=decision.attempts))
            accepted.append(candidate)
            # 单次上限按「本次已接受」计数，后面批量入库就算有人抢先了也不会超发
            context["creator_queued"] = context.get("creator_queued", 0) + 1
            context["active_keys"] = set(context["active_keys"]) | {candidate.content_key}

        # 一次事务把这一轮的任务全建好（一条一个事务在慢盘上会把一轮采集拖到几分钟）
        outcome = self.jobs.enqueue_many(to_enqueue)
        created_jobs = outcome["created"]
        created_keys = {row.get("content_key") for row in created_jobs}
        by_key = {row.get("content_key"): row for row in created_jobs}
        fresh = [candidate for candidate in accepted if candidate.content_key in created_keys]
        duplicate_queue.extend(
            {"reason": "job_active", "detail": key} for key in
            (candidate.content_key for candidate in accepted
             if candidate.content_key not in created_keys))
        for candidate in fresh:
            self._report("collectJob", {
                "creatorId": creator["id"], "handle": handle,
                "state": "queued", "job": by_key.get(candidate.content_key),
                "contentKey": candidate.content_key, "title": candidate.title})

        self._sync_creator_profile(creator, result)
        state = result.state
        error = "" if state != "partial" else (result.warning or "本次没有抓完")
        self.monitor.mark_checked(creator["id"], state=state, error=error,
                                  discovered=len(candidates), queued=len(created_jobs))

        summary = {
            "discovered": len(candidates),
            "fresh": len(fresh),
            "queued": len(created_jobs),
            "duplicates": duplicate_batch + len(duplicate_queue),
            "existing": len(existing),
            "filtered": len(filtered),
            "skipReasons": _tally(duplicate_queue, existing, filtered, duplicate_batch),
        }
        run = self.jobs.record_run(
            creator_id=creator["id"], creator_handle=handle, trigger=trigger, state=state,
            started_at=started, discovered=summary["discovered"], fresh=summary["fresh"],
            queued=summary["queued"], duplicates=summary["duplicates"],
            existing=summary["existing"], filtered=summary["filtered"], error=error,
            warning=result.warning, needs_verification=result.needs_verification,
            complete=result.complete, batch_id=batch_id,
            message=_summary_text(handle, summary))
        self._report("collectProgress", {
            "creatorId": creator["id"], "handle": handle,
            "state": "done" if state == "success" else state,
            "message": _summary_text(handle, summary), "summary": summary,
            "runId": run["id"] if run else "", "warning": result.warning,
            "nextCheckAt": (self.monitor.state(creator["id"]) or {}).get("next_check_at", "")})
        return {"ok": True, "creatorId": creator["id"], "handle": handle, "batchId": batch_id,
                "state": state, "summary": summary, "jobs": created_jobs,
                # 逐条判断结果只回最近 MAX_DECISION_DETAIL 条：一个几百条内容的主页
                # 全量回传会在桥接层序列化出上百 KB，界面也展示不了那么多。
                "decisions": decisions[:MAX_DECISION_DETAIL],
                "decisionsTotal": len(decisions), "run": run, "warning": result.warning,
                "complete": result.complete, "needsVerification": result.needs_verification}

    def _sync_creator_profile(self, creator, result):
        """把这次读到的头像 / 粉丝数 / 作品数回写到 Creator contract 上。

        注意 followers 是能同步的（`Api.recognize()` 的 profileStats 里有
        "48.2K" 这种文本，parse_count 负责还原），**作品数同步不了** ——
        下载器的主页读取只返回 following / followers / likes 三个数字，
        没有「作品总数」。所以 videos 只在数据源真的给了计数时才更新，
        其余情况保留用户填的值，绝不拿「本次发现的条数」冒充作品总数。
        """
        fields = {}
        if result.avatar and result.avatar != creator.get("avatar"):
            fields["avatar"] = result.avatar
        stats = result.profile_stats or {}
        for key in ("followers", "followerCount"):
            number = models.parse_count(stats.get(key))
            if number and number != _as_int(creator.get("followers")):
                fields["followers"] = number
                break
        for key in ("videoCount", "videos", "video_count"):
            number = models.parse_count(stats.get(key))
            if number and number != _as_int(creator.get("videos")):
                fields["videos"] = number
                break
        if fields:
            self.monitor.update_creator_fields(creator["id"], **fields)

    # ---- 批量 / 手动 ----------------------------------------------------
    def check_now(self, creator_id, trigger="manual", newest_only=True):
        """「立即检查」：忽略 next_check_at，但仍然受重复采集保护。"""
        return self.poll_creator(creator_id, trigger=trigger, newest_only=newest_only, force=True)

    def tick(self, limit=None, force=False, trigger="tick", newest_only=True):
        """到点该检查的都检查一遍；force=True 时不管到没到点。

        force 只忽略 next_check_at，**不会**去动已停用的 Creator —— 「停用」
        是用户的明确意图，只有 check_now（用户手动点「立即检查」）才越过它。

        刻意做成「被调用一次就干一轮」而不是自己开定时器：调度方式（界面按钮、
        本地线程、以后的常驻服务）留给调用方，Collector 只保证一轮是幂等的。
        """
        limit = int(limit or DEFAULT_TICK_LIMIT)
        creators = (self.monitor.creators(enabled=True, limit=limit) if force
                    else self.monitor.due_creators(limit=limit))
        results, queued = [], 0
        for creator in creators:
            outcome = self.poll_creator(creator["id"], trigger=trigger, newest_only=newest_only,
                                        force=force)
            queued += _as_int((outcome.get("summary") or {}).get("queued"))
            results.append(outcome)
        summary = {
            "checked": len(results),
            "queued": queued,
            "failed": sum(1 for item in results if item.get("error") or item.get("state") == "failed"),
            "busy": sum(1 for item in results if item.get("busy")),
            "skipped": sum(1 for item in results if item.get("skipped")),
            "results": results,
        }
        return {"ok": True, **summary}

    # ---- 任务 -> 下载器 -------------------------------------------------
    def pending_jobs(self, limit=20, creator_id=None):
        return self.jobs.pending(limit=limit, creator_id=creator_id)

    def plan(self, limit=20, creator_id=None):
        """待办任务 -> 下载器能直接吃的视频列表（不改状态，可重复调用）。"""
        jobs = self.pending_jobs(limit=limit, creator_id=creator_id)
        return {"ok": True, "count": len(jobs), "jobs": jobs,
                "videos": [models.job_to_downloader_video(job) for job in jobs],
                "folder": self.download_folder(), "quality": self.download_quality()}

    def claim(self, limit=20, creator_id=None):
        """取出待办任务并**原子地**标记为 running（真正交给下载器之前调用一次）。

        用 CAS（claim_jobs）而不是「先查再改」：两个「开始下载」按钮、或者常驻
        轮询和手动点击撞在一起时，同一个任务只能被一个调用者抢到，
        否则同一条视频会被下载两遍（第一遍的文件还没落盘，去重也救不了）。
        """
        planned = self.plan(limit=limit, creator_id=creator_id)
        claimed_ids = self.jobs.claim_jobs([job["id"] for job in planned["jobs"]])
        claimed = [job for job in (self.jobs.job(job_id) for job_id in claimed_ids) if job]
        return {**planned, "count": len(claimed), "jobs": claimed,
                "claimedIds": claimed_ids,
                "videos": [models.job_to_downloader_video(job) for job in claimed]}

    def mark_running(self, job_id, attempts=None):
        return self.jobs.mark_running(job_id, attempts=attempts)

    def mark_done(self, job_id, content_item_id="", error=""):
        return self.jobs.mark_done(job_id, content_item_id=content_item_id, error=error)

    def mark_failed(self, job_id, error=""):
        return self.jobs.mark_failed(job_id, error=error)

    def recover_stale(self, seconds=3600):
        return self.jobs.requeue_stale(seconds=seconds)

    def reconcile(self, limit=200):
        """用内容库的真实结果收尾任务：任务完成与否，以库里那条内容为准。

        为什么需要它：下载是下载器干的，下载完成后是下载器自己走
        `content_register_download` 入库的 —— Collector 不插手这条链路，
        只在事后读内容库，把已经落库的 job 标成 done、把标注失败的标成 failed。
        """
        jobs = self.jobs.jobs(states=("pending", "running"), limit=limit)
        if not jobs:
            return {"ok": True, "checked": 0, "done": 0, "failed": 0, "pending": 0}
        library = self.jobs.library_index(limit=20000)   # 一次读全量，不做 N 次单条查询
        transitions, done, failed, pending = [], 0, 0, 0
        for job in jobs:
            item = library.get(job.get("content_key"))
            if not item:
                pending += 1
                continue
            status = str(item.get("download_status") or "pending").lower()
            if status == "done":
                transitions.append({"id": job["id"], "state": "done",
                                    "content_item_id": item.get("id") or ""})
                done += 1
            elif status == "failed":
                transitions.append({"id": job["id"], "state": "failed",
                                    "error": item.get("last_error") or "下载失败"})
                failed += 1
            else:
                pending += 1
        self.jobs.mark_many(transitions)          # 一次提交收尾整批
        return {"ok": True, "checked": len(jobs), "done": done, "failed": failed, "pending": pending}

    # ---- 设置读取 ------------------------------------------------------
    def download_folder(self):
        if self.settings is None:
            return ""
        value = self.settings.get("storage", "video_path") or ""
        return str(value).strip()

    def download_quality(self):
        if self.settings is None:
            return "1080p"
        value = self.settings.get("collect", "download_quality") or "1080p"
        return str(value).strip() or "1080p"

    # ---- 视图 ----------------------------------------------------------
    def status(self):
        monitor_stats = self.monitor.stats()
        counts = self.jobs.counts()
        runs = self.jobs.runs(limit=5)
        return {
            "ok": True,
            "source": getattr(self.source, "name", "source"),
            "owner": self._owner,
            "creators": monitor_stats,
            "jobs": counts,
            "runs": self.jobs.run_counts(),
            "recentRuns": runs,
            "lastFailure": (self.jobs.failures(limit=1) or [None])[0],
            "downloadFolder": self.download_folder(),
            "quality": self.download_quality(),
            "policy": {
                "minDuration": self.policy.min_duration,
                "maxDuration": self.policy.max_duration,
                "includeKinds": list(self.policy.include_kinds),
                "perCreatorLimit": self.policy.per_creator_limit,
                "maxAttempts": self.policy.max_attempts,
                "retryFailed": self.policy.retry_failed,
            },
        }

    def jobs_view(self, state=None, creator_id=None, limit=100):
        return {"ok": True, "jobs": self.jobs.jobs(state=state, creator_id=creator_id, limit=limit),
                "counts": self.jobs.counts(creator_id=creator_id)}

    def runs_view(self, creator_id=None, state=None, limit=100):
        return {"ok": True, "runs": self.jobs.runs(creator_id=creator_id, state=state, limit=limit),
                "summary": self.jobs.run_counts()}

    def failures_view(self, limit=50):
        return {"ok": True, "runs": self.jobs.failures(limit=limit),
                "jobs": self.jobs.failed_jobs(limit=limit)}


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _tally(duplicate_queue, existing, filtered, duplicate_batch):
    counts = {}
    if duplicate_batch:
        counts["duplicate_in_batch"] = duplicate_batch
    for group in (duplicate_queue, existing, filtered):
        for decision in group:
            reason = decision.reason if hasattr(decision, "reason") else decision.get("reason")
            reason = reason or "unknown"
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _summary_text(handle, summary):
    parts = [f"@{handle} 发现 {summary['discovered']} 条"]
    if summary["queued"]:
        parts.append(f"新增任务 {summary['queued']} 条")
    if summary["duplicates"]:
        parts.append(f"重复 {summary['duplicates']} 条")
    if summary["existing"]:
        parts.append(f"已在库 {summary['existing']} 条")
    if summary["filtered"]:
        parts.append(f"被规则过滤 {summary['filtered']} 条")
    return "，".join(parts)


def build_collector(store, downloader=None, settings=None, source=None, emit=None, now=None):
    """工厂：默认用下载器作为候选项来源，没有下载器时用一个「明确失败」的来源。

    这样调用方（桥接层 / 测试）只需要给一个下载器实例，不用关心内部装配。
    """
    chosen = source
    if chosen is None:
        chosen = (DownloaderCandidateSource(downloader) if downloader is not None
                  else StaticCandidateSource(error="未接入下载器，无法读取创作者主页"))
    return ContentCollector(store, settings=settings, source=chosen, emit=emit, now=now,
                            monitor=CreatorMonitorService(store, settings=settings, now=now))
