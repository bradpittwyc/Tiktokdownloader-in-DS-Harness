"""单个密钥的加密文件（Windows DPAPI）。

存在的理由很具体：`learning.json` 里**曾经明文存着 API Key**（实测确认过）。
这个模块把密钥搬到一个单独加密的文件里，设置文件从此只放非机密的配置。

接口照搬 Tony Dev Kit 的凭据纪律（`docs/AI_RUNTIME_STANDARD.md`）：

* **前端读不回明文** —— `get_learning_options()` 只回答"配没配"（`apiKeySet`）
* **留空 = 不修改**，不是清空；要清除得显式调 `remove()`
* 加密复用 `session_store.dpapi()`，不另写一套 —— 两份实现迟早有一份忘了加保护

**刻意做小**：这是一个"一个文件一个密钥"的工具，不是凭据框架。
如果以后要管多种凭据、多种后端（环境变量 / 明文 / 测试替身）和多字段脱敏，
那份东西已经存在于 `Tony-Content-Factory` 的
`content_factory/providers/credentials.py`，应该去复用它而不是在这里长出来。
"""

import os
from pathlib import Path

from session_store import dpapi


APP_DATA_FOLDER = "TikTokBatchMVP"


class DpapiSecret:
    """一个 DPAPI 加密的密钥文件。"""

    def __init__(self, filename="ai-key.dpapi", root=None):
        base = Path(root) if root else \
            Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_DATA_FOLDER
        self.path = base / filename

    def has(self):
        """配没配。文件不存在、为空、读不了都算没配。"""
        try:
            return self.path.is_file() and self.path.stat().st_size > 0
        except OSError:
            return False

    def get(self):
        """读出明文。

        **只给后端自己用**（拼 Authorization 头那一步）。任何 js_api 方法都不许
        把它交出去 —— 交出去就等于把密钥送到了前端和它可能留下的任何日志里。
        换了机器、换了用户、文件损坏都会返回 None，让调用方提示"重新填一次"。
        """
        if not self.has():
            return None
        try:
            return dpapi(self.path.read_bytes(), decrypt=True).decode("utf-8")
        except Exception:
            return None

    def set(self, value):
        """写入（原子写）。空值抛错而不是静默删掉。"""
        text = "" if value is None else str(value)
        if not text:
            raise ValueError("密钥不能为空；要清除请用 remove()")
        encrypted = dpapi(text.encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, self.path)
        return self.path

    def remove(self):
        """删除。返回是否真的删掉了东西。"""
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False
