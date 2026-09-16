"""Creator Monitor：创作者 CRUD + 检查节奏 + 上次/下次检查状态。

对外只需要 `CreatorMonitorService`：

    from content_factory.creator_monitor import CreatorMonitorService

    monitor = CreatorMonitorService(store, settings)
    monitor.create_creator("emilyintech", display_name="Emily", priority="高")
    monitor.disable(creator_id)
    monitor.due_creators()          # 到点该检查的 Creator（按优先级排序）
    monitor.mark_checked(creator_id, state="success", discovered=12, queued=3)

这一层不 import 下载器、不 import webview、不发网络请求 —— 纯本地服务，
方便单测，也方便以后换调度方式。
"""

from . import intervals  # noqa: F401
from .service import CreatorMonitorService  # noqa: F401
from .store import CreatorMonitorStore  # noqa: F401

__all__ = ["CreatorMonitorService", "CreatorMonitorStore", "intervals"]
