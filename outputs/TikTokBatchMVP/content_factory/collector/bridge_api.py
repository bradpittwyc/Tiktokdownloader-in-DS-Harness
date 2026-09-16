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
    content_collect_tick / content_collector_status / content_collection_jobs
    content_collection_runs / content_collection_failures / content_collect_download
    content_collector_start / content_collector_stop
"""

import threading

from content_factory.creator_monitor import CreatorMonitorService

from .handoff import download_jobs
from .runner import CollectorRunner
from .service import build_collector


class CollectorApi:
    """采集相关的对外接口。内部仍然是「Collector 负责发现/判断/排队，
    下载器负责下载」这条分工，这里只做参数整形与后台线程包装。"""

    def __init__(self, store, settings=None, downloader=None, emit=None, source=None):
        self.store = store
        self.settings = settings
        self.downloader = downloader
        self.monitor = CreatorMonitorService(store, settings=settings)
        self.collector = build_collector(store, downloader=downloader, settings=settings,
                                         source=source, emit=emit)
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
            enabled=None if enabled in (None, "", "all", "全部") else bool(enabled),
            search=search or "", due_only=bool(due_only), limit=int(limit or 200))
        return {"ok": True, "creators": creators, "stats": self.monitor.stats()}

    def content_creator_save(self, values=None):
        """新增或修改一个 Creator：带 id 是改，不带是新增（handle 唯一）。"""
        values = dict(values or {})
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
        return self.monitor.set_enabled(creator_id, bool(enabled))

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
        """
        if not background:
            return download_jobs(self.collector, self.downloader, limit=int(limit or 10),
                                 folder=folder or "", quality=quality or "")
        pending = len(self.collector.pending_jobs(limit=int(limit or 10)))
        if not pending:
            return {"ok": True, "started": False, "count": 0, "message": "没有待下载的采集任务"}
        threading.Thread(
            target=download_jobs, args=(self.collector, self.downloader),
            kwargs={"limit": int(limit or 10), "folder": folder or "", "quality": quality or ""},
            daemon=True, name="CollectorDownload").start()
        return {"ok": True, "started": True, "count": pending, "message": f"已开始下载 {pending} 条"}

    # ---- 24/7 常驻（可选）----------------------------------------------
    def content_collector_start(self, interval=None, limit=None):
        with self._lock:
            if self._runner is None:
                self._runner = CollectorRunner.from_settings(
                    self.collector, self.settings, limit=limit)
            if interval:
                self._runner.interval = max(5, int(interval))
            result = self._runner.start()
        result["interval"] = self._runner.interval
        return result

    def content_collector_stop(self):
        with self._lock:
            if self._runner is None:
                return {"ok": True, "running": False, "message": "常驻采集没有在运行"}
            return self._runner.stop()
