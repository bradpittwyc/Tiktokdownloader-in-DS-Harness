"""异常处理页的数据源。

用户要求：「异常处理页面可先读取本地 mock / 本地错误日志」。
这里读的是**真实存在的本地日志**（下载器一直在写
%LOCALAPPDATA%/TikTokBatchMVP/logs/scrape.log），再叠加内容库里
失败的任务，组成异常列表。日志不存在时返回空列表 + 说明，不编假数据。
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

from .settings_store import app_data_root

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "scrape.log"

# 日志里的失败关键词 -> 异常类型
PATTERNS = (
    (re.compile(r"超时|timeout|timed out", re.I), "网络请求超时", "自动重试"),
    (re.compile(r"未生成目标文件|No such file|文件不存在", re.I), "下载失败", "重新下载"),
    (re.compile(r"WAF|challenge|验证|拦截", re.I), "安全验证拦截", "人工处理"),
    (re.compile(r"Cookie|cookie|登录", re.I), "登录态失效", "重新登录"),
    (re.compile(r"磁盘|空间不足|disk", re.I), "磁盘空间不足", "清理空间"),
    (re.compile(r"取消|cancel", re.I), "任务被取消", "已忽略"),
    (re.compile(r"失败|error|Error|Exception", re.I), "任务失败", "自动重试"),
)

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)$")

# 只有带这些标记的日志行才算「异常」。
# 为什么不直接把所有日志行都列出来：scrape.log 里绝大多数是「开始下载」
# 这类正常进度记录，全塞进异常页会让真正的故障被淹掉。
NEWSWORTHY = re.compile(
    r"失败|错误|异常|超时|无法|不能|不存在|拒绝|中断|error|exception|timeout|refused|denied|"
    r"not found|failed|traceback", re.I)


def log_path(root=None):
    base = Path(root) if root else app_data_root()
    return base / LOG_DIR_NAME / LOG_FILE_NAME


def _classify(message):
    for pattern, kind, advice in PATTERNS:
        if pattern.search(message):
            return kind, advice
    return "未知异常", "人工处理"


def recent_errors(store=None, limit=20, root=None, days=7):
    """返回最近的异常条目（日志 + 内容库失败任务）。"""
    entries = []
    path = log_path(root)
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            lines = []
        cutoff = datetime.now() - timedelta(days=days)
        for line in reversed(lines):
            match = STAMP.match(line.strip())
            if not match:
                continue
            stamp, message = match.group(1), match.group(2)
            if not NEWSWORTHY.search(message):
                continue
            kind, advice = _classify(message)
            try:
                when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                when = None
            entries.append({
                "id": f"log-{len(entries)}",
                "source": "log",
                "time": stamp,
                "occurred_at": stamp,
                "kind": kind,
                "advice": advice,
                "detail": message[:300],
                "title": message[:80],
                "status": "待处理",
                "severity": "error" if kind != "任务被取消" else "info",
                "retryable": kind not in ("任务被取消", "登录态失效"),
                "stale": bool(when and when < cutoff),
            })
            if len(entries) >= int(limit):
                break

    if store is not None:
        for row in store.items(limit=500):
            failed_ai = row.get("ai_status") == "failed"
            failed_download = row.get("download_status") == "failed"
            if not (failed_ai or failed_download):
                continue
            reason = row.get("last_error") or ("AI 标注失败" if failed_ai else "下载失败")
            kind, advice = _classify(reason)
            entries.append({
                "id": row["id"],
                "source": "store",
                "contentId": row["id"],
                "time": row.get("updated_at") or row.get("created_at") or "",
                "occurred_at": row.get("updated_at") or row.get("created_at") or "",
                "kind": kind,
                "advice": advice,
                "detail": reason,
                "title": row.get("title") or "",
                "handle": row.get("creator_handle") or "",
                "status": "待处理",
                "severity": "error",
                "retryable": True,
                "demo": row.get("source_type") == "demo",
            })

    entries.sort(key=lambda entry: str(entry.get("occurred_at") or ""), reverse=True)
    summary = {
        "total": len(entries),
        "retryable": sum(1 for entry in entries if entry.get("retryable")),
        "pending": sum(1 for entry in entries if entry.get("status") == "待处理"),
        "fromLog": sum(1 for entry in entries if entry.get("source") == "log"),
        "fromStore": sum(1 for entry in entries if entry.get("source") == "store"),
    }
    return {"ok": True, "entries": entries[:int(limit)], "summary": summary,
            "logPath": str(path), "logExists": path.is_file()}


def append_error(message, root=None):
    """把一次异常写进日志（内容工厂自己的失败也留痕）。"""
    path = log_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        return True
    except Exception:
        return False
