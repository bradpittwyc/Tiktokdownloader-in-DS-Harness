"""内容工厂 ↔ 界面的桥接层。

放在 content_factory 包外，是因为这里是**唯一**允许同时碰「下载器实例」
（web_app.Api）和「内容工厂服务」的地方 —— 服务层保持纯净、可单测，
下载器保持原样不动，两边在这里对接。

界面（ui/app.html）只认 `window.pywebview.api.content_*` 这些方法，
不关心背后是 sqlite 还是别的实现。
"""

import threading
import time
from pathlib import Path

from content_factory import errors_feed
from content_factory.collector.bridge_api import CollectorApi
from content_factory.factory_store import FactoryStore
from content_factory.mock_data import clear_demo_items, seed_demo_items
from content_factory.pipeline import ContentPipeline
from content_factory.settings_store import DEFAULTS, FactorySettings

# 内容流水线的 7 个环节（与参考图一致）
PIPELINE_STAGES = ("发现视频", "下载视频", "转写字幕", "AI 分析", "生成学习内容", "上传云端", "发布完成")


class _Hidden:
    """把服务对象包成「只放行 content_*、其余一律不认」。

    pywebview 暴露 js_api 的方式是 dir() 递归 + getattr 链
    （util.py:180-211 与 :268），所以两种天真的写法都不行：

    - 裸挂服务对象：桥接对象上会多出上百个 `store.upsert_item` 之类的伪接口，
      内部实现被迫变成对外契约。
    - 完全透明地转发：`content_factory.value.enrich_one`
      这种绕过封装的暗道就通了。
    - 用 `_serializable = False` 藏：pywebview 是**整个属性跳过**，
      连 content_factory 本身都不暴露（实测 exposed 里一个 content_* 都没有）。
    - 用双下划线槽位藏：包装对象对外一个公开属性都没有，pywebview 同样发现不了方法。

    最终方案是显式白名单：只有 `content_*` 开头的名字转发，别的名字直接
    AttributeError。内部代码要取真实对象请用模块级 `unwrap()`（走
    object.__getattribute__，不受这里限制）。
    """

    __slots__ = ("_target",)

    def __init__(self, target):
        object.__setattr__(self, "_target", target)

    def _allowed(self, name):
        return name.startswith("content_") and callable(getattr(unwrap(self), name, None))

    def __dir__(self):
        return [name for name in dir(unwrap(self)) if self._allowed(name)]

    def __getattr__(self, name):
        if not name.startswith("content_"):
            raise AttributeError(
                f"{type(self).__name__} 只放行 content_* 方法，不转发 {name!r}，"
                "内部取真实对象请使用 unwrap()")
        return getattr(unwrap(self), name)

    def __repr__(self):
        return f"<bridge {unwrap(self)!r}>"


def unwrap(value):
    """取出 _Hidden 里的真实对象；普通对象原样返回。"""
    return (object.__getattribute__(value, "_target")
            if isinstance(value, _Hidden) else value)


class ContentFactoryApi:
    """所有 content_* 桥接方法的实现。

    downloader：web_app.Api 实例（可为 None，此时下载相关动作返回明确提示）。
    """

    def __init__(self, downloader=None, store=None, settings=None):
        self.bridge_downloader = None if downloader is None else _Hidden(downloader)
        self.bridge_settings = _Hidden(settings or FactorySettings())
        self.bridge_store = _Hidden(store or FactoryStore())
        self.bridge_events = []               # 最近的桥接事件，供界面轮询（可选）
        self.bridge_lock = threading.Lock()
        self.bridge_started = time.time()
        self.bridge_pipeline = _Hidden(ContentPipeline(
            unwrap(self.bridge_store), unwrap(self.bridge_settings),
            emit=self._record_event, downloader=downloader))
        # 采集/创作者监控：复用同一个 store / settings / downloader / 事件出口。
        # 这里**不能**另建一套 FactoryStore 或 FactorySettings —— 创作者写在
        # creators 表里，两份 store 会指向同一个 sqlite 文件却各自缓存建表状态，
        # 打开的是「两个世界」的内容库。downloader 同理：采集排队后的下载必须
        # 落到界面上那个真实下载器，而不是另一个实例。
        self.bridge_collector = _Hidden(CollectorApi(
            store=unwrap(self.bridge_store), settings=unwrap(self.bridge_settings),
            downloader=downloader, emit=self._record_event))

    # ---- 内部引用（属性名带下划线，pywebview 会跳过）-------------------
    @property
    def _downloader(self):
        return unwrap(self.bridge_downloader)

    @property
    def _settings(self):
        return unwrap(self.bridge_settings)

    @property
    def _store(self):
        return unwrap(self.bridge_store)

    @property
    def _pipeline(self):
        return unwrap(self.bridge_pipeline)

    @property
    def _collector(self):
        return unwrap(self.bridge_collector)

    @property
    def _started_at(self):
        return self.bridge_started

    def __dir__(self):
        """只把 content_* 方法交给 pywebview（_Hidden 那边还有一层白名单）。"""
        return sorted(name for name in vars(type(self)) if name.startswith("content_"))

    # ---- 事件 ----------------------------------------------------------
    def _record_event(self, name, payload):
        with self.bridge_lock:
            self.bridge_events.append({"name": name, "payload": payload, "at": time.time()})
            if len(self.bridge_events) > 200:
                del self.bridge_events[:-200]
        push = getattr(self._downloader, "_emit", None)
        if callable(push):
            try:
                push(name, payload)
            except Exception:
                pass

    def content_events(self, since=0):
        with self.bridge_lock:
            fresh = [entry for entry in self.bridge_events if entry["at"] > float(since or 0)]
        return {"ok": True, "events": fresh, "now": time.time()}

    # ---- 概览 ----------------------------------------------------------
    def content_bootstrap(self):
        """界面启动时一次性取回：统计 + 内容列表 + 创作者 + 设置。

        一次给全而不是每页单独请求：内容量在千条级别，一次几千字节，
        省掉一堆异步竞态（界面刚切页时数据还没回来的空窗）。
        """
        items = self._store.items(limit=500)
        creators = self._store.creators(limit=200)
        return {
            "ok": True,
            "stats": self.content_stats(),
            "items": items,
            "creators": creators,
            "settings": self._settings.public(),
            "errors": errors_feed.recent_errors(store=self._store, limit=40),
            "demoCount": sum(1 for row in items if row.get("source_type") == "demo"),
            "stages": list(PIPELINE_STAGES),
        }

    def content_stats(self):
        return self._pipeline.stats()

    def content_items(self, status="all", search="", limit=200):
        return {"ok": True, "items": self._store.items(
            status=None if status in ("all", "", None) else status,
            search=search or "", limit=int(limit or 200))}

    def content_item(self, item_id):
        view = self._store.item_view(item_id)
        return {"ok": bool(view), "item": view} if view else {"ok": False, "error": "内容不存在"}

    def content_creators(self):
        return {"ok": True, "creators": self._store.creators(limit=200)}

    # ---- 创作者监控（转发给 CollectorApi）-------------------------------
    # 为什么要一行一行手写，而不能继承 CollectorApi：`__dir__` 只列
    # `vars(type(self))` 里的名字，继承来的方法不会出现在 dir() 里，
    # pywebview 是 dir() 递归 + getattr 链暴露 js_api 的 —— 继承的写法
    # 界面上会直接 undefined，而且单元测试直连 Python 时完全看不出来
    # （直连能调到，只有真实窗口里调不到）。所以这里必须是显式类体方法。
    def content_creator_list(self, enabled=None, search="", due_only=False, limit=200):
        return self._collector.content_creator_list(enabled=enabled, search=search,
                                                    due_only=due_only, limit=limit)

    def content_creator_save(self, values=None):
        return self._collector.content_creator_save(values)

    def content_creator_delete(self, creator_id):
        return self._collector.content_creator_delete(creator_id)

    def content_creator_toggle(self, creator_id, enabled=True):
        return self._collector.content_creator_toggle(creator_id, enabled)

    def content_creator_set_interval(self, creator_id, value):
        return self._collector.content_creator_set_interval(creator_id, value)

    def content_creator_set_priority(self, creator_id, value):
        return self._collector.content_creator_set_priority(creator_id, value)

    def content_creator_check_now(self, creator_id, background=True):
        return self._collector.content_creator_check_now(creator_id, background)

    def content_errors(self, limit=40):
        return errors_feed.recent_errors(store=self._store, limit=int(limit or 40))

    def content_topic_distribution(self):
        return {"ok": True, "topics": self._store.topic_distribution()}

    # ---- 设置 ----------------------------------------------------------
    def content_settings(self):
        return self._settings.public()

    def content_prompts(self):
        """给「AI 加工设置」用的 Prompt 版本清单。

        返回每个版本的名称、说明与模板文本，以及当前生效的版本。
        UI 靠它渲染下拉框 —— 加新版本时不需要改前端代码。
        """
        from content_factory.prompts import all_prompts, resolve_prompt

        current, _template = resolve_prompt(self._settings.section("ai").get("prompt_template"))
        return {"ok": True, "current": current, "prompts": all_prompts()}

    def content_save_settings(self, section, values):
        section = str(section or "").strip()
        if section not in ("general", "work_mode", "logging", "collect", "ai", "publish",
                           "storage", "notify", "account"):
            return {"ok": False, "error": f"未知的设置分区：{section}"}
        saved = self._settings.update(section, values or {})
        return {"ok": True, "section": section,
                "settings": self._settings.public(), "saved": bool(saved)}

    def content_set_prompt_version(self, version):
        """切换 Prompt 版本。

        - 传已知版本名 → 存版本名（模板由版本库统一提供，永不覆盖旧版）
        - 传 `custom` 且带 template → 存用户自己写的文本
        - 传 `reset` → 回到默认版本
        """
        from content_factory.prompts import ACTIVE_VERSION, get_prompt, resolve_prompt

        version = str(version or "").strip()
        if version == "reset":
            version = ACTIVE_VERSION
        spec = get_prompt(version)
        if spec:
            self._settings.update("ai", {"prompt_template": spec.version})
        elif version == "custom":
            return {"ok": False, "error": "切到自定义版本需要同时提供模板文本"}
        else:
            return {"ok": False, "error": f"未知的 Prompt 版本：{version}"}
        current, template = resolve_prompt(self._settings.section("ai").get("prompt_template"))
        return {"ok": True, "current": current, "template": template,
                "settings": self._settings.public()}

    def content_save_prompt_template(self, template):
        """保存用户编辑过的模板文本。

        文本与某个已知版本**完全一致**时存版本名（这样历史标注仍能正确归因），
        否则存为自定义模板。
        """
        from content_factory.prompts import CUSTOM_VERSION, PROMPT_LIBRARY, resolve_prompt

        text = str(template or "").strip()
        if not text:
            return {"ok": False, "error": "模板不能为空"}
        if "{transcript}" not in text:
            return {"ok": False, "error": "模板里必须保留 {transcript} 占位符，否则模型看不到字幕"}
        matched = next((spec.version for spec in PROMPT_LIBRARY.values()
                        if spec.template.strip() == text), None)
        self._settings.update("ai", {"prompt_template": matched or text})
        current, resolved = resolve_prompt(self._settings.section("ai").get("prompt_template"))
        return {"ok": True, "current": current, "matched_version": matched,
                "template": resolved, "settings": self._settings.public()}

    def content_reset_settings(self, section=""):
        """把某个分区恢复到出厂默认值（只覆盖该分区的键，不动别的分区）。"""
        section = str(section or "").strip()
        if not section:
            return {"ok": False, "error": "未指定要恢复的分区"}
        defaults = DEFAULTS.get(section)
        if defaults is None:
            return {"ok": False, "error": f"未知的设置分区：{section}"}
        self._settings.save_section(section, defaults)
        return {"ok": True, "section": section, "settings": self._settings.public()}

    def content_test_ai(self, values=None):
        """「测试模型」按钮：先用界面上的值临时覆盖设置，再发一次真实请求。"""
        if values:
            staged = dict(values)
            if str(staged.get("api_key") or "").strip():
                self._settings.update("ai", staged)
        return self._pipeline._enricher.test_connection()

    def content_import_legacy_ai(self):
        """把下载器「学习文档」里已配好的 API 凭据导入内容工厂。

        很多用户（包括本机）早就在 learning.json 里配过 DeepSeek，没必要为了
        AI 标注再手填一遍 Key。只导入 api_key / api_base / model 三项，
        其余参数保持内容工厂自己的默认值；导入后仍可在设置页覆盖。
        """
        import json as _json
        import os as _os
        from pathlib import Path as _Path

        path = (_Path(_os.environ.get("LOCALAPPDATA", str(_Path.home())))
                / "TikTokBatchMVP" / "learning.json")
        if not path.is_file():
            return {"ok": False, "error": "没有找到已有的学习文档配置（learning.json）"}
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "error": f"读取 learning.json 失败：{exc}"}
        key = str(data.get("api_key") or "").strip()
        if not key:
            return {"ok": False, "error": "已有的学习文档配置里没有 API Key"}
        values = {"api_key": key}
        if str(data.get("api_base") or "").strip():
            values["api_base"] = str(data["api_base"]).strip().rstrip("/")
        if str(data.get("model") or "").strip():
            values["model"] = str(data["model"]).strip()
        self._settings.update("ai", values)
        return {"ok": True, "api_base": values.get("api_base", ""),
                "model": values.get("model", ""), "settings": self._settings.public()}

    def content_choose_folder(self, kind="video"):
        """复用下载器的目录选择对话框。"""
        chooser = getattr(self._downloader, "choose_folder", None)
        if not callable(chooser):
            return {"ok": False, "error": "当前环境不支持目录选择"}
        path = chooser()
        if not path:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": path, "kind": kind}

    def content_open_folder(self, path):
        opener = getattr(self._downloader, "open_folder", None)
        if callable(opener):
            return opener(path)
        return {"ok": False, "error": "当前环境不支持打开目录"}

    # ---- AI 标注闭环 ---------------------------------------------------
    def content_enrich(self, item_id, background=True):
        if not background:
            return self._pipeline.enrich_one(item_id)
        threading.Thread(target=self._pipeline.enrich_one, args=(item_id,),
                         daemon=True, name="ContentEnrichOne").start()
        return {"ok": True, "queued": 1, "message": "已开始分析"}

    def content_reanalyze(self, item_id):
        """重新分析：清掉旧结果后重跑，失败的内容也能靠它救回来。"""
        item = self._store.item(item_id)
        if not item:
            return {"ok": False, "error": "内容不存在"}
        self._store.set_ai_status(item_id, "pending", "")
        threading.Thread(target=self._pipeline.enrich_one, args=(item_id,),
                         daemon=True, name="ContentReanalyze").start()
        return {"ok": True, "message": "已开始重新分析"}

    def content_enrich_pending(self, ids=None, limit=20):
        return self._pipeline.enrich_many(ids, limit=int(limit or 20), background=True)

    def content_set_transcript(self, item_id, text):
        result = self._pipeline.set_transcript(item_id, text)
        return result

    def content_transcript(self, item_id):
        item = self._store.item(item_id)
        if not item:
            return {"ok": False, "error": "内容不存在"}
        return {"ok": True, "text": item.get("transcript_text") or "",
                "status": item.get("transcript_status") or "pending",
                "path": item.get("local_subtitle_path") or ""}

    # ---- 内容入库（真实下载链路）---------------------------------------
    def content_ingest_videos(self, videos, handle=""):
        """把界面（下载器）里勾选的作品纳管进内容库。"""
        return self._pipeline.ingest_videos(videos or [], handle=handle)

    def content_import_folder(self, folder, handle="local"):
        return self._pipeline.create_from_local(folder or "", handle=handle or "local")

    def content_register_download(self, payload):
        """下载完成回调：把落盘信息写进内容库。

        web_app.Api.download 在每条作品下完（或跳过）之后调它，
        这样"下载 → 内容库"不需要用户在界面上再点一次。
        """
        payload = payload or {}
        video_id = str(payload.get("id") or "").strip()
        if not video_id:
            return {"ok": False, "error": "缺少作品 id"}
        folder = str(payload.get("folder") or "")
        media = str(payload.get("media") or "")
        subtitles = [str(path) for path in (payload.get("subtitles") or []) if path]
        url = str(payload.get("url") or "")
        handle = payload.get("handle") or _handle_from_url(url)
        item_id = self._store.upsert_item(
            video_id, source_type="tiktok", source_url=url,
            creator_handle=handle, creator_name=payload.get("title") or handle,
            title=payload.get("title") or f"作品 {video_id}",
            description=payload.get("description") or "",
            duration=int(payload.get("duration") or 0),
            thumbnail_path=payload.get("cover") or "",
            local_video_path=media,
            local_subtitle_path=subtitles[0] if subtitles else "",
            download_status=payload.get("state") or "done",
        )
        self._store.update_item(item_id, transcript_status="pending")
        return {"ok": True, "id": item_id,
                "subtitle": subtitles[0] if subtitles else "",
                "hasSubtitle": bool(subtitles)}

    def content_ingest_downloader(self):
        """把下载器已有记录（localStorage 里的下载记录）扫进内容库。

        这是"内容发现 → 本地内容"的兜底入口：老用户升级上来，本机已经有
        下好的作品，不必重新抓一遍。
        """
        videos = []
        records = getattr(self._downloader, "_ui_records", None)
        if isinstance(records, dict):
            for handle, bucket in records.items():
                if not isinstance(bucket, dict):
                    continue
                for video_id, record in bucket.items():
                    item = (record or {}).get("item") or {}
                    if not item:
                        continue
                    videos.append({
                        "id": video_id,
                        "title": item.get("title") or "",
                        "url": item.get("url") or "",
                        "cover": item.get("cover") or "",
                        "duration": item.get("duration") or 0,
                        "description": item.get("description") or "",
                        "_handle": handle,
                    })
        if not videos:
            return {"ok": True, "created": 0, "message": "本机还没有下载记录"}
        created = 0
        for video in videos:
            result = self.content_register_download({
                "id": video["id"], "title": video["title"], "url": video["url"],
                "cover": video["cover"], "duration": video["duration"],
                "description": video["description"], "folder": "",
                "media": "", "subtitles": [], "state": "done",
                "handle": video.get("_handle") or "",
            })
            created += 1 if result.get("ok") else 0
        return {"ok": True, "created": created}

    # ---- 演示数据 ------------------------------------------------------
    def content_seed_demo(self):
        return seed_demo_items(self._store, reset=False)

    def content_clear_demo(self):
        return clear_demo_items(self._store)

    # ---- 其他页面的数据 ------------------------------------------------
    def content_worker_status(self):
        """顶部状态栏 / 首页 / 账号管理用的运行状态。全部取自本机真实进程与磁盘信息。"""
        import os
        import shutil
        import sys

        elapsed = int(time.time() - self._started_at)
        disk = {}
        try:
            root = self._store.path.parent
            usage = shutil.disk_usage(str(root if root.exists() else Path.home()))
            disk = {"total": usage.total, "used": usage.used, "free": usage.free}
        except Exception:
            disk = {}
        try:
            db_size = self._store.path.stat().st_size if self._store.path.exists() else 0
        except Exception:
            db_size = 0
        return {
            "ok": True,
            "online": True,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "uptimeSeconds": elapsed,
            "uptimeText": _duration_text(elapsed),
            "startedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._started_at)),
            "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "dbPath": str(self._store.path),
            "dbSize": db_size,
            "settingsPath": str(self._settings.path),
            "disk": disk,
        }


def _handle_from_url(url):
    text = str(url or "")
    if "/@" in text:
        return text.split("/@", 1)[1].split("/")[0].split("?")[0].strip() or "unknown"
    return "unknown"


def _duration_text(seconds):
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"
