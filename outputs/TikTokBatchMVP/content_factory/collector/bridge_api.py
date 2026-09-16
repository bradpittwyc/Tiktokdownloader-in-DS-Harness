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

from content_factory import errors_feed
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
        self._emit = emit or (lambda *_args, **_kwargs: None)
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
