"""内容工厂的设置项：本地 JSON 持久化。

参考图的「设置」有 7 个子页（基础 / 采集 / AI 加工 / 发布 / 存储 / 通知 / 账号）。
今天这些页面的表单全部真实可编辑、真实落盘（这也是用户明确要求的：
「设置页面先做成本地可编辑表单和本地持久化」），但其中只有一小部分会被
真实逻辑读取（保存目录、画质、并发、AI 服务商 / 模型 / Key / token / 温度 /
输出语言 / prompt 模板）。其余字段先落盘、留接口，等后续接真实能力时直接读。
"""

import copy
import json
import os
import threading
from pathlib import Path

from .ai_enrichment import DEFAULT_PROMPT_TEMPLATE


def app_data_root():
    """应用数据目录，与现有下载器保持一致（LOCALAPPDATA/TikTokBatchMVP）。"""
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP"


DEFAULTS = {
    "general": {
        "app_language": "zh-CN",
        "theme": "system",
        "auto_start": True,
        "start_minimized": False,
        "run_in_background": False,
    },
    "work_mode": {
        # manual / timed / auto24  —— 今天不实现真实定时，只存偏好
        "mode": "auto24",
        "concurrent_tasks": 3,
        "task_interval_sec": 60,
    },
    "logging": {
        "level": "INFO",
        "save_detail": True,
        "auto_clean_days": 30,
        "notify_on_error": True,
        "daily_report": False,
    },
    "collect": {
        "group": "默认分组",
        "auto_collect": True,
        "interval_minutes": 30,
        "priority_english": True,
        "new_video_auto_join": True,
        "duration_min_sec": 15,
        "duration_max_sec": 600,
        "low_quality_filter": True,
        "filter_rules": ["广告营销", "纯图文", "低播放量", "重复内容", "无实质内容"],
        "dedupe_method": "视频指纹（推荐）",
        "similarity_threshold": 90,
        "check_fingerprint": True,
        "check_title": True,
        "check_author_time": True,
        "check_audio": False,
        "history_enabled": True,
        "history_days": 90,
        "history_per_creator": 200,
        "history_speed_per_hour": 100,
        "download_quality": "1080p",
        "subtitle_priority": "原生字幕 > 自动生成 > 不抓取",
        "extract_audio": True,
        "subtitle_format": "SRT",
        "keywords_allow": ["tutorial", "how to", "tips", "tricks"],
        "keywords_block": ["ad", "sponsored", "promo", "giveaway"],
        "source_scope": "仅创作者主页",
        "include_types": ["公开视频", "图文帖子"],
        "exclude_keywords": "",
        "fail_retry": 3,
        "window_start": "00:00",
        "window_end": "23:59",
        "daily_limit": 1000,
        "per_creator_limit": 50,
    },
    "ai": {
        "provider": "DeepSeek",
        "model": "deepseek-chat",
        "fallback_model": "deepseek-coder",
        "embedding_model": "text-embedding-v3",
        "asr_provider": "OpenAI Whisper（本地）",
        "output_language": "双语（中英）",
        "api_base": "https://api.deepseek.com/v1",
        "api_key": "",
        "max_tokens": 4000,
        "temperature": 0.7,
        "batch_size": 5,
        "extract_key_points": True,
        "detect_grammar": True,
        "generate_examples": True,
        "generate_exercises": True,
        "generate_cards": True,
        "quality_threshold": 0.6,
        "learning_value_min": 0.7,
        "prompt_template": DEFAULT_PROMPT_TEMPLATE,
    },
    "publish": {
        "targets": ["抖音", "小红书", "B站", "视频号", "YouTube", "Tony Learning OS"],
        "sync_to_learning_os": True,
        "publish_frequency": "每天固定数量",
        "daily_count": 5,
        "slots": ["09:00 – 12:00", "14:00 – 18:00", "19:00 – 22:00"],
        "category_rule": "按内容主题自动匹配",
        "default_category": "生活技巧",
        "topic_tags": ["AI 推荐", "热门话题"],
        "custom_tags": [],
        "title_rule": "AI 生成（结合关键词）",
        "title_template": "{title} | {theme} | Tony Content",
        "seo_keywords": "自动提取 + 自定义关键词",
        "video_template": "使用视频第 1 帧",
        "review_mode": "auto",
        "max_retry": 3,
        "retry_interval_min": 10,
        "notify_on_fail": True,
        "queue_priority": "高（优先发布）",
        "api_rate_limit": True,
        "queue_concurrency": 3,
    },
    "storage": {
        "video_path": "",
        "cover_path": "",
        "library_path": "",
        "temp_path": "",
        "auto_create_dir": True,
        "disk_warn_gb": 10,
        "object_storage": "阿里云 OSS",
        "bucket": "tce-media",
        "region": "华东1（杭州）",
        "access_key_id": "",
        "access_key_secret": "",
        "custom_domain": "https://img.example.com",
        "storage_prefix": "tce/",
        "save_location": "同时保存到本地和对象存储",
        "upload_timing": "处理完成后立即上传",
        "upload_concurrency": 3,
        "upload_retry": 3,
        "upload_size_limit_mb": 500,
        "file_types": "mp4,mov,avi,jpg,png,webp",
        "resume_upload": True,
        "verify_md5": True,
        "keep_video_days": 180,
        "keep_image_days": 365,
        "keep_temp_days": 7,
        "keep_policy": "自动删除",
        "clean_schedule": True,
        "clean_cycle": "每天",
        "clean_time": "02:00",
        "clean_expired_temp": True,
        "clean_recycle": True,
        "clean_unused": True,
        "clean_failed": True,
        "backup_enabled": True,
        "backup_items": ["配置文件", "发布记录", "重要素材"],
        "backup_cycle": "每周",
        "backup_keep": 4,
    },
    "notify": {
        "desktop_task_start": True,
        "desktop_task_done": True,
        "desktop_task_fail": True,
        "desktop_ai_done": True,
        "desktop_publish_done": True,
        "desktop_storage_low": True,
        "desktop_system_error": True,
        "sound_enabled": True,
        "sound_task_start": True,
        "sound_task_done": True,
        "sound_ai_done": True,
        "sound_task_fail": True,
        "sound_repeat": 1,
        "sound_gap_sec": 3,
        "channel_email": True,
        "channel_wecom": False,
        "channel_feishu": False,
        "channel_dingtalk": False,
        "channel_slack": False,
        "channel_webhook": False,
        "smtp_server": "smtp.example.com",
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_password": "",
        "smtp_to": "",
        "smtp_encryption": "STARTTLS",
        "webhook_url": "",
        "webhook_method": "POST",
        "webhook_format": "JSON（推荐）",
        "webhook_template": '{\n  "text": "【{{app_name}}】{{title}}：{{message}}",\n  "time": "{{time}}"\n}',
        "event_task_start": False,
        "event_task_done": True,
        "event_task_fail": True,
        "event_ai_done": True,
        "event_publish_fail": True,
        "event_storage_low": True,
        "event_system_error": True,
        "min_level": "INFO",
        "repeat_interval_min": 30,
        "max_retry": 3,
        "retry_interval_min": 5,
        "quiet_enabled": True,
        "quiet_start": "23:00",
        "quiet_end": "08:00",
        "quiet_mode": "仅屏蔽紧急通知",
        "preview_type": "任务完成通知",
        "preview_message": "这是一条测试消息，用于验证通知渠道是否正常工作。",
    },
    "account": {
        "display_name": "Tony",
        "email": "tony@example.com",
        "plan": "专业版",
        "node_id": "worker_001",
        "node_name": "本地开发节点",
        "two_factor": True,
        "verify_method": "Google Authenticator",
    },
}


class FactorySettings:
    """设置项读写。deep-merge 保证新增默认字段不会让旧文件缺项。"""

    def __init__(self, root=None):
        self._lock = threading.RLock()
        self.root = Path(root) if root else app_data_root()
        self.path = self.root / "content-factory-settings.json"
        self._cache = None

    # ---- 磁盘 ----------------------------------------------------------
    def load(self):
        with self._lock:
            if self._cache is not None:
                return copy.deepcopy(self._cache)
            data = {}
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
            except Exception:
                data = {}
            self._cache = _merge(copy.deepcopy(DEFAULTS), data)
            return copy.deepcopy(self._cache)

    def save(self, data):
        with self._lock:
            merged = _merge(copy.deepcopy(DEFAULTS), data or {})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
            self._cache = merged
            return copy.deepcopy(merged)

    # ---- 便捷读取 ------------------------------------------------------
    def section(self, name):
        return self.load().get(name, {})

    def get(self, section, key, default=None):
        return self.load().get(section, {}).get(key, default)

    def public(self):
        """给界面用的视图：API Key 只暴露「是否已设置」，不回传明文。"""
        data = self.load()
        view = copy.deepcopy(data)
        ai = view.setdefault("ai", {})
        ai["api_key"] = ""
        ai["apiKeySet"] = bool(data.get("ai", {}).get("api_key"))
        storage = view.setdefault("storage", {})
        secret = str(storage.get("access_key_secret") or "")
        storage["access_key_secret"] = ""
        storage["secretSet"] = bool(secret)
        notify = view.setdefault("notify", {})
        password = str(notify.get("smtp_password") or "")
        notify["smtp_password"] = ""
        notify["passwordSet"] = bool(password)
        return view

    def update(self, section, values):
        """局部更新某个分区；空字符串的密钥字段表示「保持原值」。"""
        with self._lock:
            data = self.load()
            bucket = data.setdefault(section, {})
            for key, value in (values or {}).items():
                if key in {"api_key", "access_key_secret", "smtp_password"} and not str(value or "").strip():
                    continue
                bucket[key] = value
            return self.save(data)

    def save_section(self, section, values):
        """整段替换某个分区（用于「恢复默认」）。"""
        with self._lock:
            data = self.load()
            data[section] = dict(values or {})
            return self.save(data)


def _merge(base, incoming):
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base
