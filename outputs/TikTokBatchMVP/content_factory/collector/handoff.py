"""把采集任务交给**已有下载器**执行 —— 这里就是 Collector 与 Downloader 的接缝。

职责分工（不要越界）：
- Collector：发现 + 判断 + 排队（本模块的输入）。
- Downloader：下载。`Api.download(videos, folder, quality, ...)` 一个字都不用改，
  它下完还是会自己走 `_notify_content_factory` → `content_register_download`
  入库 —— 我们不新增第二套入库协议，也不重复实现下载。
- 本模块只做两件小事：把 job 翻译成 `Api.download` 认得的视频形状；
  下载回来之后按结果把 job 标成 done / failed。

失败处理原则：
- 下载器抛异常不能把采集器带崩 —— 任务全部标 failed 并写明原因。
- **交不出去的任务必须写明原因**，不能悄悄跳过又标成完成（url-only 的候选
  就是这种情况：下载器只认 id + url）。
- 只有内容库里真的出现了那条记录，才算「确认完成」；没有记录时仍然算完成
  （下载器可能因为本地已有文件而跳过），但会把这一点写进任务的备注里，
  不让它变成无声的成功。
"""

import inspect

from . import models


def default_folder(settings):
    if settings is None:
        return ""
    return str(settings.get("storage", "video_path") or "").strip()


def default_quality(settings):
    if settings is None:
        return "1080p"
    return str(settings.get("collect", "download_quality") or "1080p").strip() or "1080p"


def _download_kwargs(downloader, retry_count, concurrency):
    """只传调用方真的指定了的参数。

    默认**不传** retry_count / concurrency：`Api.download` 自己有默认值
    （retry_count=3），采集器再传一个 0 进去等于悄悄把重试关掉 ——
    网络抖一下任务就直接失败。跨轮次的重试由 DownloadPolicy.max_attempts 管。
    """
    kwargs = {}
    try:
        parameters = inspect.signature(downloader.download).parameters
    except (TypeError, ValueError):
        return kwargs
    if retry_count is not None and "retry_count" in parameters:
        kwargs["retry_count"] = int(retry_count)
    if concurrency is not None and "concurrency" in parameters:
        kwargs["concurrency"] = max(1, int(concurrency))
    return kwargs


def download_jobs(collector, downloader, jobs=None, folder="", quality="", limit=20,
                  retry_count=None, concurrency=None, reconcile=True):
    """取待办任务 → 调已有下载器 → 按结果收尾任务。

    jobs 为 None 时自动从队列里 claim（CAS 标记 running，抢不到的不会重复下载）；
    显式传入的 job 由调用方自己负责状态流转。

    返回 {"ok", "downloaded", "failedCount", "jobs", "failed", "folder", "quality"}。
    """
    folder = str(folder or "").strip() or collector.download_folder()
    quality = str(quality or "").strip() or collector.download_quality()
    if downloader is None or not callable(getattr(downloader, "download", None)):
        return {"ok": False, "error": "当前环境没有可用的下载器", "downloaded": 0, "failedCount": 0,
                "jobs": [], "failed": [], "folder": folder, "quality": quality}
    if not folder:
        return {"ok": False, "error": "未配置下载目录：请在「设置 → 存储设置」里填写保存路径",
                "downloaded": 0, "failedCount": 0, "jobs": [], "failed": [],
                "folder": folder, "quality": quality}

    if jobs is None:
        jobs = collector.claim(limit=limit)["jobs"]
    jobs = [job for job in (jobs or []) if job]
    if not jobs:
        return {"ok": True, "downloaded": 0, "failedCount": 0, "jobs": [], "failed": [],
                "folder": folder, "quality": quality, "message": "没有待下载的采集任务"}

    videos, undeliverable = [], []
    for job in jobs:
        video = models.job_to_downloader_video(job)
        if video["id"] and video["url"]:
            videos.append(video)
        else:
            undeliverable.append(job)
    undeliverable_ids = {job.get("id") for job in undeliverable}
    # 交不出去就不能算完成：写清楚原因，让它在界面上看得见、也不会被当成已下载
    for job in undeliverable:
        collector.mark_failed(job["id"], "任务缺少作品 id 或链接，下载器无法处理")
    if not videos:
        return {"ok": False, "downloaded": 0, "failedCount": len(undeliverable), "jobs": jobs,
                "failed": [{"id": job.get("source_video_id") or job.get("content_key"),
                            "error": "任务缺少作品 id 或链接，下载器无法处理"}
                           for job in undeliverable],
                "folder": folder, "quality": quality,
                "error": "待办任务缺少可下载的链接"}

    try:
        kwargs = _download_kwargs(downloader, retry_count, concurrency)
        outcome = downloader.download(videos, folder, quality, **kwargs)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        for job in jobs:
            collector.mark_failed(job["id"], message)
        return {"ok": False, "error": message, "downloaded": 0, "failedCount": len(jobs),
                "jobs": jobs, "failed": [{"id": job.get("source_video_id"), "error": message}
                                         for job in jobs],
                "folder": folder, "quality": quality}

    outcome = outcome if isinstance(outcome, dict) else {}
    failures = {str(entry.get("id")): str(entry.get("error") or "下载失败")
                for entry in (outcome.get("failed") or []) if isinstance(entry, dict)}
    downloaded = _as_int(outcome.get("ok"))
    transitions = []
    for job in jobs:
        if job.get("id") in undeliverable_ids:
            continue                      # 上面已经按失败收尾了
        video_id = str(job.get("source_video_id") or "")
        if video_id in failures:
            transitions.append({"id": job["id"], "state": "failed", "error": failures[video_id]})
            continue
        # 下载器没报这条失败 = 它下好了，或者它发现本地已经有文件而跳过
        # （Api.download 的 skipped 分支）—— 两种都算任务达成。
        # 内容库里有对应的记录才算「确认完成」；没有记录时仍然置为完成，
        # 但把这一点写进备注，避免出现「界面显示已完成、内容库却是空的」这种无声成功。
        item = collector.jobs.library_item(job.get("content_key"))
        transitions.append({
            "id": job["id"], "state": "done",
            "content_item_id": (item or {}).get("id") or "",
            "error": "" if item else "下载器报告成功，但内容库暂无对应记录（可能是本地已有文件被跳过）"})
    collector.jobs.mark_many(transitions)          # 一次提交收尾整批

    result = {"ok": True, "downloaded": downloaded, "failedCount": len(failures) + len(undeliverable),
              "jobs": jobs, "folder": str(outcome.get("folder") or folder), "quality": quality,
              "failed": [{"id": key, "error": value} for key, value in failures.items()]}
    if reconcile:
        result["reconcile"] = collector.reconcile()
    return result


def _as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
