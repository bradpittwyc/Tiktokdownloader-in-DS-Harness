"""自动采集核心（Collector）：发现 + 判断 + 排队。

    从 content_factory.collector import build_collector, download_jobs

    collector = build_collector(store, downloader=api, settings=settings)
    collector.monitor.create_creator("emilyintech", priority="高", poll_interval="30 分钟")
    collector.tick()                      # 到点的 Creator 各检查一次
    collector.plan()["videos"]            # 待办任务 -> 下载器能直接吃的形状
    download_jobs(collector, api)         # 交给已有下载器执行，并按结果收尾

这一层不下载、不抓取、不写第二套入库协议：
- 抓取复用下载器的 `Api.recognize()`（见 sources.py）
- 下载复用下载器的 `Api.download()`（见 handoff.py）
- 入库仍然由下载器走原来的 `content_register_download`
"""

from . import dedupe, models, policy  # noqa: F401
from .bridge_api import CollectorApi  # noqa: F401
from .handoff import download_jobs, default_folder, default_quality  # noqa: F401
from .policy import DownloadPolicy  # noqa: F401
from .runner import CollectorRunner  # noqa: F401
from .service import ContentCollector, build_collector  # noqa: F401
from .sources import (CandidateSource, DiscoveryResult,  # noqa: F401
                      DownloaderCandidateSource, StaticCandidateSource)
from .store import CollectionJobStore  # noqa: F401

__all__ = [
    "ContentCollector", "build_collector", "CollectionJobStore", "CollectorRunner",
    "CollectorApi", "DownloadPolicy", "download_jobs", "default_folder", "default_quality",
    "CandidateSource", "DownloaderCandidateSource", "StaticCandidateSource",
    "DiscoveryResult", "models", "dedupe", "policy",
]
