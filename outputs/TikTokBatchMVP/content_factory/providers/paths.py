"""Provider 层与 Settings Core 之间唯一的一点耦合：应用数据目录在哪。

目录的权威定义者是 Settings Core（`settings_store.app_data_root`），
这里只做一次最小适配 —— 优先用它的实现，接口万一变了也能自己兜住，
不复制整套设置系统、也不另立第二个数据目录。

`LOCALAPPDATA/TikTokBatchMVP` 与现有下载器、sqlite、设置 JSON 完全一致，
所以「程序重启后配置仍可恢复」不需要任何额外约定。
"""

import os
from pathlib import Path

APP_DIR_NAME = "TikTokBatchMVP"


def default_data_root():
    """应用数据根目录。"""
    try:
        from ..settings_store import app_data_root      # Settings Core 的权威实现
        return Path(app_data_root())
    except Exception:                                   # pragma: no cover - 兜底路径
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_DIR_NAME
