"""转写运行记录：把「这条内容上一次是怎么转出来的」留痕。

**为什么不往 content_items 表里加列**：那是三条并行开发线（collector / pipeline /
ASR）共用的公共 contract，加列会直接和别人撞车，也会让旧库需要迁移。
所以主链路只写已有的列（transcript_text / transcript_status / last_error /
local_subtitle_path），来源与结构化错误这些「附加信息」写在附属的小 JSON 里：

    %LOCALAPPDATA%/TikTokBatchMVP/transcripts/<item_id>.json

设计上刻意的取舍：
- 主链路永远可用：文本、状态、失败原因都在 content_items 里，不依赖这些文件；
- 这些文件丢了 / 坏了只丢「来源信息」（是字幕读的还是 ASR 识别的、错误码是什么），
  不会影响读回文本，所以所有读写都吞异常；
- 有了它，重启之后仍然知道一条内容是「字幕读出来的」还是「ASR 转出来的」，
  失败项也还带着错误码，界面才能给出正确的重试提示。
"""

import json
import os
import re
import time
from pathlib import Path

from ..settings_store import app_data_root

FOLDER_NAME = "transcripts"
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(item_id):
    """item id 可能带路径分隔符等字符，落盘前先洗干净。"""
    cleaned = SAFE_NAME.sub("_", str(item_id or "").strip())
    return cleaned[:120] or "unknown"


class TranscriptRunLog:
    """一条内容一份运行记录（只保留最近一次）。"""

    def __init__(self, root=None):
        self.root = Path(root) if root else app_data_root() / FOLDER_NAME

    def path(self, item_id):
        return self.root / f"{safe_name(item_id)}.json"

    def load(self, item_id):
        try:
            data = json.loads(self.path(item_id).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, item_id, record):
        payload = dict(record or {})
        payload.setdefault("itemId", str(item_id or ""))
        payload.setdefault("updatedAt", time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            target = self.path(item_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.replace(temporary, target)
            return payload
        except Exception:
            return payload

    def clear(self, item_id):
        try:
            self.path(item_id).unlink()
            return True
        except Exception:
            return False

    def all(self, limit=200):
        """最近若干条运行记录（诊断 / 统计用），读不到就返回空列表。"""
        rows = []
        try:
            files = sorted(self.root.glob("*.json"),
                           key=lambda path: path.stat().st_mtime, reverse=True)
        except Exception:
            return rows
        for path in files[:int(limit)]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                data.setdefault("itemId", path.stem)
                rows.append(data)
        return rows
