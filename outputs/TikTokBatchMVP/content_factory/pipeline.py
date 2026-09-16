"""内容工厂的分析编排：入库 → 字幕/转写 → AI 标注 → 落库。

这一层是「今天闭环」的主干，刻意做成不依赖 pywebview 的纯服务：
- 输入是普通 dict（下载器给的视频信息）或本地文件路径；
- 输出是 sqlite 里的记录 + 一个可注入的进度回调（界面用它刷新任务队列）。

为什么要「批量入队 + 逐条处理」：AI 标注是网络请求，可能几十秒一条。
界面要能显示「待分析 / 分析中 / 已完成 / 失败」，所以每次状态变化都要回报；
单条失败不能让整批停摆（用户明确要求失败可重试）。

转写那一段（字幕优先 → 没有字幕才 ASR）已经独立成 `content_factory.asr`
（ASR Core）。这里只保留接线与原签名，语义与以前完全一致。
"""

import threading
import time
from pathlib import Path

from .ai_enrichment import EnrichmentError, EnrichmentService
from .asr import TranscriptService
from .transcript import (ASRUnavailable, asr_available, clean_transcript,
                         find_subtitle_for, pick_subtitle_path, transcribe_media,
                         transcript_from_subtitle_file)

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi")

# 分析阶段 -> 界面文案
STAGE_LABELS = {
    "download": "下载",
    "transcript": "转写",
    "enrich": "AI 标注",
}


class ContentPipeline:
    def __init__(self, store, settings, emit=None, downloader=None, transcribe=None):
        self.store = store
        self.settings = settings
        self._emit = emit or (lambda *_args, **_kwargs: None)
        self._downloader = downloader          # web_app.Api，用于复用下载能力
        self._transcribe = transcribe or transcribe_media
        self._enricher = EnrichmentService(settings)
        # 转写阶段已经独立成 content_factory.asr（字幕优先 → 本机 ASR → 外部 provider），
        # 这里只做接线：注入的 transcribe / asr_available 都透传下去，行为与以前一致。
        self._transcripts = TranscriptService(
            store, settings, emit=self._on_transcript_event,
            transcriber=lambda media, **kwargs: self._transcribe(media, **kwargs),
            asr_probe=lambda: asr_available())
        self._cancel = threading.Event()
        self._busy = threading.Lock()
        self._queue = []

    # ---- 进度回报 ------------------------------------------------------
    def set_emit(self, emit):
        self._emit = emit or (lambda *_args, **_kwargs: None)

    def _on_transcript_event(self, event):
        """把 ASR Core 的进度事件转成界面已有的 enrichProgress（stage=transcript）。"""
        extra = {key: value for key, value in (event or {}).items()
                 if key not in {"id", "state", "message", "stage"}}
        self._report((event or {}).get("id") or "", (event or {}).get("state") or "running",
                     (event or {}).get("message") or "", stage="transcript", **extra)

    def _report(self, item_id, state, message="", stage="enrich", **extra):
        payload = {"id": item_id, "state": state, "message": message, "stage": stage,
                   "stageLabel": STAGE_LABELS.get(stage, stage)}
        payload.update(extra)
        try:
            self._emit("enrichProgress", payload)
        except Exception:
            pass

    # ---- 入库 ----------------------------------------------------------
    def ingest_videos(self, videos, handle="", source_type="tiktok"):
        """把下载器识别的作品写进内容库（同一条内容只入一次）。"""
        created, existing = [], []
        for video in videos or []:
            video_id = str(video.get("id") or "").strip()
            if not video_id:
                continue
            url = video.get("url") or ""
            video_handle = handle or _handle_from_url(url)
            before = self.store.find_item_by_source(source_type, video_id)
            item_id = self.store.upsert_item(
                video_id, source_type=source_type,
                source_url=url,
                creator_handle=video_handle,
                creator_name=video.get("author") or video_handle,
                title=video.get("title") or f"作品 {video_id}",
                description=video.get("description") or "",
                duration=int(video.get("duration") or 0),
                thumbnail_path=video.get("cover") or "",
                download_status=video.get("download_status") or "pending",
                transcript_status=video.get("transcript_status") or "pending",
                ai_status=video.get("ai_status") or "pending",
            )
            (existing if before else created).append(item_id)
        return {"ok": True, "created": len(created), "existing": len(existing),
                "ids": created + existing}

    def create_from_local(self, folder, handle="local", limit=50):
        """把本地已有视频（或某个下载目录）当作内容来源纳管。

        这条路径是「没网 / 不想真下载时也能演示闭环」的入口：媒体文件本身就在本机，
        字幕有就用字幕，没有就走 ASR（ASR 不可用时给出明确失败原因）。
        """
        root = Path(folder).expanduser()
        if not root.is_dir():
            return {"ok": False, "error": f"目录不存在：{folder}"}
        files = sorted((path for path in root.rglob("*")
                        if path.suffix.lower() in VIDEO_EXTS and path.is_file()),
                       key=lambda path: path.stat().st_mtime, reverse=True)[:int(limit)]
        ids = []
        for path in files:
            subtitle = find_subtitle_for(path)
            item_id = self.store.upsert_item(
                path.stem, source_type="local",
                source_url=str(path),
                creator_handle=handle,
                title=path.stem,
                local_video_path=str(path),
                thumbnail_path="",
                duration=0,
                download_status="done",
            )
            self.store.update_item(
                item_id,
                local_subtitle_path=str(subtitle) if subtitle else "",
                transcript_status="pending",
                ai_status="pending")
            ids.append(item_id)
        return {"ok": True, "imported": len(ids), "ids": ids, "folder": str(root)}

    # ---- 转写 ----------------------------------------------------------
    def transcribe_item(self, item_id, allow_asr=True, force=False):
        """结构化转写入口：返回 content_factory.asr.TranscriptResult。

        需要错误码 / 是否可重试 / 来源（字幕 or ASR）时用这个；
        只要「成没成 + 文本 + 原因」的旧调用方继续用 ensure_transcript。
        """
        return self._transcripts.transcribe(item_id, allow_asr=allow_asr, force=force)

    def retry_transcript(self, item_id, allow_asr=True, force=False):
        """转写失败后重试（done 的内容默认不重跑，force=True 才重跑）。"""
        return self._transcripts.retry(item_id, allow_asr=allow_asr, force=force)

    def transcribe_status(self, item_id):
        """当前转写状态 + 结构化错误（重启后仍可读，来自内容库与运行记录）。"""
        return self._transcripts.status(item_id)

    def ensure_transcript(self, item_id, allow_asr=True):
        """返回 (是否成功, transcript文本, 失败原因)。

        实现已经搬到 content_factory.asr.TranscriptService（见 asr/service.py）：
        字幕优先 → 没有字幕才 ASR → 外部 provider。这里保留原签名与返回形状，
        调用方（enrich_one / 界面）一行都不用改；失败原因就是结构化错误的人话文案。
        """
        result = self._transcripts.transcribe(item_id, allow_asr=allow_asr)
        reason = result.error.message if result.error else ""
        return result.ok, result.text, reason

    def set_transcript(self, item_id, text):
        """人工粘贴 / 编辑字幕文本，仍然算真实闭环的一部分（内容不丢）。"""
        text = clean_transcript(text)
        self.store.update_item(item_id, transcript_text=text,
                               transcript_status="done" if text else "pending", last_error="")
        return {"ok": True, "chars": len(text)}

    # ---- AI 标注 -------------------------------------------------------
    def enrich_one(self, item_id, allow_asr=True):
        """单条内容跑完转写 + 标注。返回结果字典（不抛异常，失败也返回结构）。"""
        item = self.store.item(item_id)
        if not item:
            return {"ok": False, "error": "内容不存在"}
        if not self._enricher.configured():
            reason = "未配置 API Key：请在「设置 → AI 加工设置」里填写后重试"
            self.store.set_ai_status(item_id, "failed", reason)
            self._report(item_id, "failed", reason, stage="enrich")
            return {"ok": False, "error": reason, "needsApiKey": True}

        ok, transcript, reason = self.ensure_transcript(item_id, allow_asr=allow_asr)
        if not ok:
            self.store.set_ai_status(item_id, "failed", reason)
            return {"ok": False, "error": reason}

        self.store.set_ai_status(item_id, "running")
        self._report(item_id, "running", "AI 正在分析内容…", stage="enrich")
        fresh = self.store.item(item_id)
        started = time.time()
        try:
            normalized, raw_text, payload, attempts = self._enricher.enrich(fresh, transcript)
        except EnrichmentError as exc:
            self.store.set_ai_status(item_id, "failed", str(exc))
            self._report(item_id, "failed", str(exc), stage="enrich")
            return {"ok": False, "error": str(exc)}
        except Exception as exc:                       # 网络异常等
            reason = f"{type(exc).__name__}: {exc}"
            self.store.set_ai_status(item_id, "failed", reason)
            self._report(item_id, "failed", reason, stage="enrich")
            return {"ok": False, "error": reason}

        saved = self.store.save_enrichment(
            item_id, normalized, raw_json=payload, raw_response=raw_text,
            attempts=attempts, model=self._enricher.config()["model"])
        elapsed = round(time.time() - started, 2)
        self._report(item_id, "done", f"标注完成（{elapsed}s）", stage="enrich",
                     enrichment=saved, elapsed=elapsed)
        return {"ok": True, "enrichment": saved, "elapsed": elapsed, "attempts": attempts}

    def enrich_many(self, item_ids=None, limit=20, allow_asr=True, background=True):
        """批量标注。默认后台线程跑，界面不被阻塞。"""
        ids = [str(x) for x in (item_ids or []) if str(x).strip()]
        if not ids:
            ids = [row["id"] for row in self.store.items(limit=int(limit))
                   if row["ai_status"] in ("pending", "failed", "queued")]
        if not ids:
            return {"ok": True, "queued": 0, "message": "没有待分析的内容"}
        if not self._enricher.configured():
            reason = "未配置 API Key：请在「设置 → AI 加工设置」里填写后重试"
            for item_id in ids:
                self.store.set_ai_status(item_id, "failed", reason)
                self._report(item_id, "failed", reason, stage="enrich")
            return {"ok": False, "queued": len(ids), "error": reason, "needsApiKey": True}
        for item_id in ids:
            self.store.set_ai_status(item_id, "queued")
        self._queue = list(ids)
        if not background:
            return {"ok": True, "queued": len(ids), "results": self._drain(ids, allow_asr)}
        threading.Thread(target=self._drain, args=(ids, allow_asr),
                         daemon=True, name="ContentEnrich").start()
        return {"ok": True, "queued": len(ids)}

    def _drain(self, ids, allow_asr=True):
        results = []
        with self._busy:
            for index, item_id in enumerate(ids, 1):
                if self._cancel.is_set():
                    break
                self._report(item_id, "running", f"第 {index}/{len(ids)} 条",
                             stage="enrich", index=index, total=len(ids))
                results.append(self.enrich_one(item_id, allow_asr=allow_asr))
        self._report("", "idle", "队列已处理完", stage="enrich")
        return results

    def cancel(self):
        self._cancel.set()
        return True

    def resume(self):
        self._cancel.clear()
        return True

    # ---- 视图用数据 ----------------------------------------------------
    def ai_queue(self, status="all", search="", limit=200):
        rows = self.store.items(status=None if status in ("all", "", None) else status,
                                search=search, limit=limit)
        counts = self.store.counts()
        return {"ok": True, "items": rows, "counts": counts}

    def stats(self):
        """首页 / AI 加工页共用的统计。全部来自本地库，不编数字。"""
        counts = self.store.counts()
        config = self._enricher.config()
        return {
            "ok": True,
            "counts": counts,
            "topicDistribution": self.store.topic_distribution(),
            "aiConfigured": self._enricher.configured(),
            "model": config["model"],
            "provider": config["provider"],
            "asrAvailable": asr_available(),
            "asrMode": self._transcripts.asr_mode(),
            "asrProviders": self._transcripts.providers_status(),
            "dbPath": str(self.store.path),
        }


def _handle_from_url(url):
    text = str(url or "")
    marker = "/@"
    if marker in text:
        tail = text.split(marker, 1)[1]
        return tail.split("/")[0].split("?")[0].strip() or "unknown"
    return "unknown"
