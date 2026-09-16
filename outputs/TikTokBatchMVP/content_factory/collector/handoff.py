"""把采集任务交给**已有下载器**执行 —— 这里就是 Collector 与 Downloader 的接缝。

职责分工（不要越界）：
- Collector：发现 + 判断 + 排队（本模块的输入）。
- Downloader：下载。`Api.download(videos, folder, quality, ...)` 一个字都不用改，
  它下完还是会自己走 `_notify_content_factory` → `content_register_download`
  入库 —— 我们不新增第二套入库协议，也不重复实现下载。
- 本模块只做两件小事：把 job 翻译成 `Api.download` 认得的视频形状；
  下载回来之后按结果把 job 标成 done / failed。

失败处理原则：下载器抛异常不能把采集器带崩 —— 任务全部标 failed 并写明原因，
调用方拿到的是一个结构化的结果，而不是一个异常。
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
    """只传下载器签名里真的有的参数（测试替身往往只实现最小签名）。"""
    kwargs = {}
    try:
        parameters = inspect.signature(downloader.download).parameters
    except (TypeError, ValueError):
        return kwargs
    if "retry_count" in parameters:
        kwargs["retry_count"] = int(retry_count)
    if "concurrency" in parameters:
        kwargs["concurrency"] = max(1, int(concurrency))
    return kwargs


def download_jobs(collector, downloader, jobs=None, folder="", quality="", limit=20,
                  retry_count=0, concurrency=1, reconcile=True):
    """取待办任务 → 调已有下载器 → 按结果收尾任务。

    jobs 为 None 时自动从队列里 claim（标记 running）；显式传入的 job 由调用方
    自己负责状态流转。返回 {"ok", "downloaded", "failedCount", "jobs", "failed",
    "folder", "quality"}。
    """
    folder = str(folder or "").strip() or collector.download_folder()
    quality = str(quality or "").strip() or collector.download_quality()
    if downloader is None or not callable(getattr(downloader, "download", None)):
        return {"ok": False, "error": "当前环境没有可用的下载器", "downloaded": 0, "failedCount": 0}
    if not folder:
        return {"ok": False, "error": "未配置下载目录：请在「设置 → 存储设置」里填写保存路径",
                "downloaded": 0, "failedCount": 0}

    if jobs is None:
        jobs = collector.claim(limit=limit)["jobs"]
    jobs = [job for job in (jobs or []) if job]
    if not jobs:
        return {"ok": True, "downloaded": 0, "failedCount": 0, "jobs": [], "failed": [],
                "folder": folder, "quality": quality, "message": "没有待下载的采集任务"}

    videos = [models.job_to_downloader_video(job) for job in jobs]
    videos = [video for video in videos if video["id"] and video["url"]]
    if not videos:
        return {"ok": True, "downloaded": 0, "failedCount": 0, "jobs": jobs, "failed": [],
                "folder": folder, "quality": quality, "message": "任务里没有可下载的链接"}

    try:
        kwargs = _download_kwargs(downloader, retry_count, concurrency)
        outcome = downloader.download(videos, folder, quality, **kwargs)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        for job in jobs:
            collector.mark_failed(job["id"], message)
        return {"ok": False, "error": message, "downloaded": 0, "failedCount": len(jobs),
                "jobs": jobs, "failed": [{"id": job["source_video_id"], "error": message}
                                         for job in jobs],
                "folder": folder, "quality": quality}

    outcome = outcome if isinstance(outcome, dict) else {}
    failures = {str(entry.get("id")): str(entry.get("error") or "下载失败")
                for entry in (outcome.get("failed") or []) if isinstance(entry, dict)}
    downloaded = _as_int(outcome.get("ok"))
    for job in jobs:
        video_id = str(job.get("source_video_id") or "")
        if video_id in failures:
            collector.mark_failed(job["id"], failures[video_id])
            continue
        # 下载器没报这条失败 = 它下好了，或者它发现本地已经有文件而跳过
        # （Api.download 的 skipped 分支）—— 两种都算任务达成。
        # 真正算不算完成，最终由 reconcile() 读内容库那条记录来定。
        item = collector.jobs.library_item(job.get("content_key"))
        collector.mark_done(job["id"], content_item_id=(item or {}).get("id") or "")

    result = {"ok": True, "downloaded": downloaded, "failedCount": len(failures),
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
