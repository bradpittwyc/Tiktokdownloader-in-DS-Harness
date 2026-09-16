"""内容工厂的本地持久化层：sqlite。

为什么用 sqlite 而不是沿用 localStorage / json：
- 「应用重启后下载记录与 AI 标注结果仍在」是硬验收项。原来下载器的记录放在
  webview 的 localStorage 里，与界面耦合；标注结果是结构化数据（数组字段、
  需要按状态筛选、要能重跑），换成表更直接。
- 不引入任何外部依赖：sqlite3 是标准库，单文件、可备份、可直接用工具查看。
- 不接云端数据库（今天的明确约束）。

位置：%LOCALAPPDATA%/TikTokBatchMVP/content-factory.db
表：creators / content_items / ai_enrichments（字段与用户给的数据模型一致）
"""

import datetime
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

from .settings_store import app_data_root

SCHEMA = """
CREATE TABLE IF NOT EXISTS creators (
    id            TEXT PRIMARY KEY,
    handle        TEXT UNIQUE,
    display_name  TEXT DEFAULT '',
    avatar        TEXT DEFAULT '',
    category      TEXT DEFAULT '',
    status        TEXT DEFAULT 'active',
    followers     INTEGER,
    videos        INTEGER,
    priority      TEXT DEFAULT '中',
    poll_interval TEXT DEFAULT '1 小时',
    created_at    TEXT DEFAULT '',
    updated_at    TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS content_items (
    id                   TEXT PRIMARY KEY,
    source_type          TEXT DEFAULT 'tiktok',
    source_video_id      TEXT DEFAULT '',
    source_url           TEXT DEFAULT '',
    creator_id           TEXT DEFAULT '',
    creator_handle       TEXT DEFAULT '',
    title                TEXT DEFAULT '',
    description          TEXT DEFAULT '',
    local_video_path     TEXT DEFAULT '',
    local_audio_path     TEXT DEFAULT '',
    local_subtitle_path  TEXT DEFAULT '',
    thumbnail_path       TEXT DEFAULT '',
    transcript_text      TEXT DEFAULT '',
    duration             INTEGER DEFAULT 0,
    download_status      TEXT DEFAULT 'pending',
    transcript_status    TEXT DEFAULT 'pending',
    ai_status            TEXT DEFAULT 'pending',
    last_error           TEXT DEFAULT '',
    created_at           TEXT DEFAULT '',
    updated_at           TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ai_enrichments (
    id                TEXT PRIMARY KEY,
    content_item_id   TEXT NOT NULL,
    topic             TEXT DEFAULT '',
    subtopic          TEXT DEFAULT '',
    cefr_level        TEXT DEFAULT '',
    accent            TEXT DEFAULT '',
    speech_speed      TEXT DEFAULT '',
    learning_value    REAL DEFAULT 0,
    keywords          TEXT DEFAULT '[]',
    expressions       TEXT DEFAULT '[]',
    grammar_points    TEXT DEFAULT '[]',
    key_sentences     TEXT DEFAULT '[]',
    summary_zh        TEXT DEFAULT '',
    recommended_task  TEXT DEFAULT '',
    classification,
    raw_json          TEXT DEFAULT '{}',
    raw_response      TEXT DEFAULT '',
    attempts          INTEGER DEFAULT 0,
    model             TEXT DEFAULT '',
    provider          TEXT DEFAULT '',
    prompt_version    TEXT DEFAULT '',
    analyzed_at       TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_enrichment_item ON ai_enrichments(content_item_id);
CREATE INDEX IF NOT EXISTS idx_items_created ON content_items(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_ai_status ON content_items(ai_status);
"""

ITEM_FIELDS = (
    "id", "source_type", "source_video_id", "source_url", "creator_id", "creator_handle",
    "title", "description", "local_video_path", "local_audio_path", "local_subtitle_path",
    "thumbnail_path", "transcript_text", "duration", "download_status", "transcript_status",
    "ai_status", "last_error", "created_at", "updated_at",
)

JSON_FIELDS = ("keywords", "expressions", "grammar_points", "key_sentences")

# 按「最近导入 / 最近更新」排序，界面上内容流水线与 AI 加工都要这个顺序
ITEM_ORDER = "COALESCE(NULLIF(updated_at,''), created_at) DESC, created_at DESC"


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def content_key(source_type, source_video_id):
    """同一平台同一条内容只入库一次。"""
    return f"{source_type or 'tiktok'}:{source_video_id}"


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


class FactoryStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else app_data_root() / "content-factory.db"
        self._lock = threading.RLock()
        self._schema_ready = False

    # ---- 基础 ----------------------------------------------------------
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=20, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def init(self):
        with self._lock:
            if self._schema_ready:
                return self
            with closing(self.connect()) as connection, connection:
                connection.executescript(SCHEMA)
                self._migrate(connection)
            self._schema_ready = True
            return self

    @staticmethod
    def _migrate(connection):
        """给已经存在的旧库补新增列。

        为什么必须有：用户本机已经有一份存了标注结果的 content-factory.db，
        `CREATE TABLE IF NOT EXISTS` 对已存在的表**什么都不会做** —— 新加的
        prompt_version / provider 会永远不存在，保存标注时直接报
        "no such column"，而且是在用户已经跑了一段时间之后才炸。
        所以按 SQLite 的 table_info 逐个补列，已存在就跳过（幂等，可反复执行）。
        """
        wanted = {
            "ai_enrichments": (("provider", "TEXT DEFAULT ''"),
                               ("prompt_version", "TEXT DEFAULT ''")),
        }
        for table, columns in wanted.items():
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for name, definition in columns:
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _rows(self, sql, params=()):
        self.init()
        with self._lock, closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def _row(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _write(self, sql, params=()):
        self.init()
        with self._lock, closing(self.connect()) as connection, connection:
            return connection.execute(sql, params).rowcount

    # ---- creator -------------------------------------------------------
    def upsert_creator(self, handle, **fields):
        handle = (handle or "").strip().lstrip("@") or "unknown"
        existing = self._row("SELECT * FROM creators WHERE handle=?", (handle,))
        if existing:
            updates = {key: value for key, value in fields.items()
                       if key in {"display_name", "avatar", "category", "status", "followers",
                                  "videos", "priority", "poll_interval"} and value not in (None, "")}
            if updates:
                updates["updated_at"] = now_text()
                clause = ", ".join(f"{key}=?" for key in updates)
                self._write(f"UPDATE creators SET {clause} WHERE handle=?",
                            (*updates.values(), handle))
            return (self._row("SELECT * FROM creators WHERE handle=?", (handle,)) or existing)["id"]
        creator_id = new_id("creator")
        stamp = now_text()
        self._write(
            "INSERT INTO creators (id,handle,display_name,avatar,category,status,followers,videos,"
            "priority,poll_interval,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (creator_id, handle, fields.get("display_name") or handle, fields.get("avatar") or "",
             fields.get("category") or "", fields.get("status") or "active",
             _as_int(fields.get("followers")), _as_int(fields.get("videos")),
             fields.get("priority") or "中", fields.get("poll_interval") or "1 小时", stamp, stamp))
        return creator_id

    def creators(self, limit=200):
        rows = self._rows(f"SELECT * FROM creators ORDER BY updated_at DESC, created_at DESC LIMIT {int(limit)}")
        for row in rows:
            row["item_count"] = self._row(
                "SELECT COUNT(*) AS total FROM content_items WHERE creator_id=?", (row["id"],))["total"]
        return rows

    def creator(self, creator_id):
        return self._row("SELECT * FROM creators WHERE id=?", (creator_id,))

    # ---- content item --------------------------------------------------
    def find_item_by_source(self, source_type, source_video_id):
        return self._row("SELECT * FROM content_items WHERE source_type=? AND source_video_id=?",
                         (source_type or "tiktok", str(source_video_id)))

    def item(self, item_id):
        return self._row("SELECT * FROM content_items WHERE id=?", (item_id,))

    def upsert_item(self, source_video_id, source_type="tiktok", **fields):
        """按 (source_type, source_video_id) 去重入库。返回 item id。"""
        source_video_id = str(source_video_id or "").strip()
        if not source_video_id:
            raise ValueError("source_video_id 不能为空")
        existing = self.find_item_by_source(source_type, source_video_id)
        allowed = {key for key in ITEM_FIELDS if key not in {"id", "created_at", "updated_at"}}
        payload = {key: value for key, value in fields.items() if key in allowed}
        handle = str(payload.pop("creator_handle", "") or "").strip().lstrip("@")
        if handle:
            creator_id = self.upsert_creator(handle, display_name=payload.get("creator_name") or handle)
            payload["creator_id"] = creator_id
            payload["creator_handle"] = handle
        payload.pop("creator_name", None)
        stamp = now_text()
        if existing:
            if payload:
                clause = ", ".join(f"{key}=?" for key in payload)
                self._write(f"UPDATE content_items SET {clause}, updated_at=? WHERE id=?",
                            (*payload.values(), stamp, existing["id"]))
            return existing["id"]
        item_id = new_id("item")
        payload.update({"source_type": source_type or "tiktok", "source_video_id": source_video_id,
                        "created_at": stamp, "updated_at": stamp})
        columns = ", ".join(payload)
        marks = ", ".join("?" for _ in payload)
        self._write(f"INSERT INTO content_items (id, {columns}) VALUES (?, {marks})",
                    (item_id, *payload.values()))
        return item_id

    def update_item(self, item_id, **fields):
        allowed = {key for key in ITEM_FIELDS if key not in {"id", "created_at"}}
        payload = {key: value for key, value in fields.items() if key in allowed}
        if not payload:
            return 0
        payload["updated_at"] = now_text()
        clause = ", ".join(f"{key}=?" for key in payload)
        return self._write(f"UPDATE content_items SET {clause} WHERE id=?",
                           (*payload.values(), item_id))

    def items(self, status=None, search="", limit=200, creator_handle=None):
        sql = "SELECT * FROM content_items"
        clauses, params = [], []
        if status and status not in ("all", "全部"):
            clauses.append("ai_status=?")
            params.append(status)
        if creator_handle:
            clauses.append("creator_handle=?")
            params.append(str(creator_handle).lstrip("@"))
        if search:
            clauses.append("(title LIKE ? OR creator_handle LIKE ? OR transcript_text LIKE ?)")
            needle = f"%{search}%"
            params.extend([needle, needle, needle])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {ITEM_ORDER} LIMIT {int(limit)}"
        rows = self._rows(sql, tuple(params))
        return [self._decorate(row) for row in rows]

    def _decorate(self, row):
        if not row:
            return row
        enrichment = self.enrichment(row["id"])
        row["enrichment"] = enrichment
        row["has_transcript"] = bool(str(row.get("transcript_text") or "").strip())
        row["transcript_chars"] = len(str(row.get("transcript_text") or ""))
        row["content_key"] = content_key(row.get("source_type"), row.get("source_video_id"))
        # 界面上「资源与状态」要显示模型原始返回的长度（排查解析问题时有用的第一手信息）
        row["raw_length"] = len(str((enrichment or {}).get("raw_response") or ""))
        return row

    def counts(self):
        total = self._row("SELECT COUNT(*) AS total FROM content_items") or {"total": 0}
        by_status = {row["ai_status"] or "pending": row["total"] for row in self._rows(
            "SELECT ai_status, COUNT(*) AS total FROM content_items GROUP BY ai_status")}
        return {
            "total": total["total"],
            "creators": (self._row("SELECT COUNT(*) AS total FROM creators") or {"total": 0})["total"],
            "enriched": by_status.get("done", 0),
            "pending": by_status.get("pending", 0) + by_status.get("queued", 0),
            "running": by_status.get("running", 0),
            "failed": by_status.get("failed", 0),
            "transcribed": (self._row(
                "SELECT COUNT(*) AS total FROM content_items WHERE transcript_status='done'")
                or {"total": 0})["total"],
            "downloaded": (self._row(
                "SELECT COUNT(*) AS total FROM content_items WHERE download_status='done'")
                or {"total": 0})["total"],
            "by_status": by_status,
        }

    def topic_distribution(self, limit=10):
        rows = self._rows(
            "SELECT topic, COUNT(*) AS total FROM ai_enrichments WHERE topic<>'' "
            "GROUP BY topic ORDER BY total DESC LIMIT ?", (int(limit),))
        return rows

    def delete_item(self, item_id):
        self._write("DELETE FROM ai_enrichments WHERE content_item_id=?", (item_id,))
        return self._write("DELETE FROM content_items WHERE id=?", (item_id,))

    # ---- enrichment ----------------------------------------------------
    def save_enrichment(self, item_id, data, raw_json=None, raw_response="", attempts=0,
                        model="", provider="", prompt_version=""):
        """写入 / 覆盖一条标注结果（唯一索引保证一条内容只有一份最新结果）。"""
        self.init()
        values = {
            "topic": data.get("topic", ""),
            "subtopic": data.get("subtopic", ""),
            "cefr_level": data.get("cefr_level", ""),
            "accent": data.get("accent", ""),
            "speech_speed": data.get("speech_speed", ""),
            "learning_value": float(data.get("learning_value") or 0),
            "keywords": json.dumps(data.get("keywords") or [], ensure_ascii=False),
            "expressions": json.dumps(data.get("expressions") or [], ensure_ascii=False),
            "grammar_points": json.dumps(data.get("grammar_points") or [], ensure_ascii=False),
            "key_sentences": json.dumps(data.get("key_sentences") or [], ensure_ascii=False),
            "summary_zh": data.get("summary_zh", ""),
            "recommended_task": data.get("recommended_task", ""),
            "raw_json": json.dumps(raw_json if raw_json is not None else data, ensure_ascii=False),
            "raw_response": str(raw_response or "")[:20000],
            "attempts": int(attempts or 0),
            "model": model or "",
            # 记下"这份标注是谁产的"：以后 A/B 对比、回溯质量问题时唯一能依赖的线索
            "provider": provider or "",
            "prompt_version": prompt_version or "",
            "analyzed_at": now_text(),
        }
        existing = self.enrichment(item_id)
        with self._lock, closing(self.connect()) as connection, connection:
            if existing:
                clause = ", ".join(f"{key}=?" for key in values)
                connection.execute(f"UPDATE ai_enrichments SET {clause} WHERE content_item_id=?",
                                   (*values.values(), item_id))
            else:
                columns = ", ".join(values)
                marks = ", ".join("?" for _ in values)
                connection.execute(
                    f"INSERT INTO ai_enrichments (id, content_item_id, {columns}) VALUES (?, ?, {marks})",
                    (new_id("enrich"), item_id, *values.values()))
        self.update_item(item_id, ai_status="done", last_error="")
        return self.enrichment(item_id)

    def enrichment(self, item_id):
        row = self._row("SELECT * FROM ai_enrichments WHERE content_item_id=?", (item_id,))
        if not row:
            return None
        for field in JSON_FIELDS:
            try:
                parsed = json.loads(row.get(field) or "[]")
            except Exception:
                parsed = []
            row[field] = parsed if isinstance(parsed, list) else []
        return row

    def set_ai_status(self, item_id, status, error=""):
        return self.update_item(item_id, ai_status=status, last_error=str(error or "")[:1000])

    # ---- 便捷：按内容 id 取组合视图 ------------------------------------
    def item_view(self, item_id):
        row = self.item(item_id)
        return self._decorate(row) if row else None

    def recent_items(self, limit=8):
        return self.items(limit=limit)

    def error_items(self, limit=20):
        return [row for row in self.items(limit=500)
                if row["ai_status"] == "failed" or row["download_status"] == "failed"][:limit]

    def export_all(self):
        return {"creators": self.creators(1000), "items": self.items(limit=5000)}


def default_path():
    return app_data_root() / "content-factory.db"


def created_today_prefix():
    return datetime.date.today().strftime("%Y-%m-%d")
