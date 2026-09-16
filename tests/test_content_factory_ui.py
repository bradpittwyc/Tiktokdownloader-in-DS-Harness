"""内容工厂外壳（ui/app.html）的界面回归网。

沿用仓库既有做法：用真实浏览器加载页面、注入假的 window.pywebview.api、
断言零 JS 运行时错误（那 7 个 UI 测试就是这么守住下载器界面的）。
这一份守的是新的内容工厂外壳：

- 9 个主页面 + 7 个设置子页都能切换，且切换过程中不报错
- 首页在「没有内容」和「有内容」两种状态下都能渲染
- AI 加工页能把一条标注结果完整显示出来（主题/难度/表达/语法点/重点句）
- 设置页表单可编辑，点保存会带着 data-key 组装好的值调 content_save_settings
- 桥接缺失时给的是明确提示，不是白屏

这些断言对应的都是用户的验收项（页面全部可切换 / AI 标注结果可查看 /
设置本地可编辑），所以它们失败就说明验收项没达成。
"""

import json
import os
from pathlib import Path
import shutil
import unittest

from playwright.sync_api import sync_playwright

UI_DIR = Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP/ui"
APP_PATH = UI_DIR / "app.html"

ENRICHMENT = {
    "id": "enrich_1", "content_item_id": "item_1", "topic": "AI 科技", "subtopic": "AI 与就业",
    "cefr_level": "B2", "accent": "美音", "speech_speed": "偏快", "learning_value": 0.92,
    "keywords": ["collapse", "shift"], "grammar_points": ["whether 引导宾语从句"],
    "expressions": [{"text": "the cost of thinking is collapsing", "meaning_zh": "思考成本崩塌式下降"}],
    "key_sentences": [{"text": "I see a tool that lets one person do the work of a small team.",
                       "translation_zh": "我看到的是一个让一个人干完小团队工作的工具。"}],
    "summary_zh": "AI 的关键不是取代工作，而是思考成本急剧下降。",
    "recommended_task": "复述", "raw_json": "{}", "attempts": 1, "model": "deepseek-chat",
    "analyzed_at": "2026-01-15 14:30:00",
}

ITEM = {
    "id": "item_1", "source_type": "tiktok", "source_video_id": "7312", "creator_handle": "techwithtim",
    "title": "The real reason AI will change everything", "description": "", "duration": 72,
    "download_status": "done", "transcript_status": "done", "ai_status": "done",
    "transcript_text": "Everyone asks me whether AI is going to take our jobs.",
    "transcript_chars": 52, "has_transcript": True, "thumbnail_path": "",
    "local_video_path": "D:/videos/a.mp4", "local_subtitle_path": "D:/videos/a.en.srt",
    "last_error": "", "created_at": "2026-01-15 14:20:00", "updated_at": "2026-01-15 14:30:00",
    "enrichment": ENRICHMENT,
}

FAILED_ITEM = {
    "id": "item_2", "source_type": "tiktok", "source_video_id": "7313", "creator_handle": "moonvy",
    "title": "Design system walkthrough", "description": "", "duration": 60,
    "download_status": "done", "transcript_status": "failed", "ai_status": "failed",
    "transcript_text": "", "transcript_chars": 0, "has_transcript": False, "thumbnail_path": "",
    "local_video_path": "", "local_subtitle_path": "",
    "last_error": "未配置 API Key：请在「设置 → AI 加工设置」里填写后重试",
    "created_at": "2026-01-15 14:10:00", "updated_at": "2026-01-15 14:12:00", "enrichment": None,
}

SETTINGS = {
    "general": {"app_language": "zh-CN", "theme": "system", "auto_start": True,
                "start_minimized": False, "run_in_background": False},
    "work_mode": {"mode": "auto24", "concurrent_tasks": 3, "task_interval_sec": 60},
    "logging": {"level": "INFO", "save_detail": True, "auto_clean_days": 30,
                "notify_on_error": True, "daily_report": False},
    "ai": {"provider": "DeepSeek", "model": "deepseek-chat", "fallback_model": "deepseek-coder",
           "embedding_model": "text-embedding-v3", "asr_provider": "OpenAI Whisper（本地）",
           "output_language": "双语（中英）", "api_base": "https://api.deepseek.com/v1",
           "api_key": "", "apiKeySet": True, "max_tokens": 4000, "temperature": 0.7,
           "batch_size": 5, "extract_key_points": True, "detect_grammar": True,
           "generate_examples": True, "generate_exercises": True, "generate_cards": True,
           "quality_threshold": 0.6, "learning_value_min": 0.7,
           "prompt_template": "标题：{title}\n作者：{author}\n字幕：{transcript}"},
    "storage": {"video_path": "D:/TikTok/videos", "cover_path": "", "library_path": "",
                "temp_path": "", "auto_create_dir": True, "disk_warn_gb": 10,
                "object_storage": "阿里云 OSS", "bucket": "tce-media", "region": "华东1（杭州）",
                "access_key_id": "", "access_key_secret": "", "secretSet": False,
                "custom_domain": "https://img.example.com", "storage_prefix": "tce/",
                "save_location": "同时保存到本地和对象存储", "upload_timing": "处理完成后立即上传",
                "upload_concurrency": 3, "upload_retry": 3, "upload_size_limit_mb": 500,
                "file_types": ["mp4", "jpg"], "resume_upload": True, "verify_md5": True,
                "keep_video_days": 180, "keep_image_days": 365, "keep_temp_days": 7,
                "keep_policy": "自动删除", "clean_schedule": True, "clean_cycle": "每天",
                "clean_time": "02:00", "clean_expired_temp": True, "clean_recycle": True,
                "clean_unused": True, "clean_failed": True, "backup_enabled": True,
                "backup_items": ["配置文件"], "backup_cycle": "每周", "backup_keep": 4},
    "publish": {"targets": ["抖音", "小红书"], "sync_to_learning_os": True,
                "publish_frequency": "每天固定数量", "daily_count": 5,
                "slots": ["09:00 – 12:00"], "category_rule": "按内容主题自动匹配",
                "default_category": "生活技巧", "topic_tags": [], "custom_tags": [],
                "title_rule": "AI 生成（结合关键词）", "title_template": "{title} | {theme}",
                "seo_keywords": "自动提取 + 自定义关键词", "video_template": "使用视频第 1 帧",
                "review_mode": "auto", "max_retry": 3, "retry_interval_min": 10,
                "notify_on_fail": True, "queue_priority": "高（优先发布）",
                "api_rate_limit": True, "queue_concurrency": 3},
    "notify": {"desktop_task_start": True, "desktop_task_done": True, "desktop_task_fail": True,
               "desktop_ai_done": True, "desktop_publish_done": True, "desktop_storage_low": True,
               "desktop_system_error": True, "sound_enabled": True, "sound_task_start": True,
               "sound_task_done": True, "sound_ai_done": True, "sound_task_fail": True,
               "sound_repeat": 1, "sound_gap_sec": 3, "channel_email": True, "channel_wecom": False,
               "channel_feishu": False, "channel_dingtalk": False, "channel_slack": False,
               "channel_webhook": False, "smtp_server": "smtp.example.com", "smtp_port": 587,
               "smtp_user": "", "smtp_password": "", "passwordSet": False, "smtp_to": "",
               "smtp_encryption": "STARTTLS", "webhook_url": "", "webhook_method": "POST",
               "webhook_format": "JSON（推荐）", "webhook_template": "{}",
               "event_task_start": False, "event_task_done": True, "event_task_fail": True,
               "event_ai_done": True, "event_publish_fail": True, "event_storage_low": True,
               "event_system_error": True, "min_level": "INFO", "repeat_interval_min": 30,
               "max_retry": 3, "retry_interval_min": 5, "quiet_enabled": True,
               "quiet_start": "23:00", "quiet_end": "08:00", "quiet_mode": "仅屏蔽紧急通知",
               "preview_type": "任务完成通知", "preview_message": "这是一条测试消息。"},
    "collect": {"group": "默认分组", "auto_collect": True, "interval_minutes": 30,
                "priority_english": True, "new_video_auto_join": True, "duration_min_sec": 15,
                "duration_max_sec": 600, "low_quality_filter": True,
                "filter_rules": ["广告营销"], "dedupe_method": "视频指纹（推荐）",
                "similarity_threshold": 90, "check_fingerprint": True, "check_title": True,
                "check_author_time": True, "check_audio": False, "history_enabled": True,
                "history_days": 90, "history_per_creator": 200, "history_speed_per_hour": 100,
                "download_quality": "1080p", "subtitle_priority": "原生字幕 > 自动生成 > 不抓取",
                "extract_audio": True, "subtitle_format": "SRT", "keywords_allow": ["tips"],
                "keywords_block": ["ad"], "source_scope": "仅创作者主页",
                "include_types": ["公开视频"], "exclude_keywords": "", "fail_retry": 3,
                "window_start": "00:00", "window_end": "23:59", "daily_limit": 1000,
                "per_creator_limit": 50},
    "account": {"display_name": "Tony", "email": "tony@example.com", "plan": "专业版",
                "node_id": "worker_001", "node_name": "本地开发节点", "two_factor": True,
                "verify_method": "Google Authenticator"},
}

# 假桥接：形态必须和真实桥接**完全一致**。
#
# 真实 pywebview 注入的是 window.pywebview.api.content_factory.content_xxx
# （内容工厂方法统一挂在 content_factory 命名空间下）。这里如果图省事写成顶层的
# api.content_xxx，测试会一路绿，而真实应用永远等不到桥接 —— 这个坑实测踩过：
# 页面一直停在「正在启动内容工厂…」，最后跳「没有检测到应用桥接」。
# 所以下面的 iframe 那套（下载器用的平铺方法）保持平铺，内容工厂这套必须嵌套。
BRIDGE = """
window.calls = [];
window.savedSettings = null;
const factory = {
  content_stats: async()=>({ok:true, counts:{total:2, enriched:1, pending:0, running:0,
      failed:1, transcribed:1, downloaded:2, creators:2}}),
  content_bootstrap: async()=>({
    ok:true,
    stats:{ok:true, counts:{total:2, enriched:1, pending:0, running:0, failed:1,
      transcribed:1, downloaded:2, creators:2}, topicDistribution:[], aiConfigured:true,
      model:'deepseek-chat', provider:'DeepSeek', asrAvailable:false, dbPath:'C:/db.sqlite'},
    items: __ITEMS__,
    creators:[{id:'c1', handle:'techwithtim', display_name:'Tim', category:'AI 科技',
      status:'active', priority:'高', poll_interval:'1 小时', item_count:1},
      {id:'c2', handle:'moonvy', display_name:'Moonvy', category:'设计', status:'error',
      priority:'中', poll_interval:'30 分钟', item_count:1}],
    settings: __SETTINGS__,
    errors:{ok:true, logExists:true, logPath:'C:/logs/scrape.log',
      summary:{total:2, retryable:1, pending:2, fromLog:1, fromStore:1},
      entries:[
        {id:'item_2', source:'store', contentId:'item_2', time:'2026-01-15 14:12:00',
         kind:'配置缺失', advice:'人工处理', detail:'未配置 API Key', title:'Design system walkthrough',
         handle:'moonvy', status:'待处理', retryable:true},
        {id:'log-0', source:'log', time:'2026-01-15 14:28:32', kind:'网络请求超时',
         advice:'自动重试', detail:'下载失败: 请求超时', status:'待处理', retryable:true}]},
    demoCount:0,
    stages:['发现视频','下载视频','转写字幕','AI 分析','生成学习内容','上传云端','发布完成']}),
  content_worker_status: async()=>({ok:true, online:true, pid:1234, python:'3.11.9',
    uptimeText:'2 天 14 小时', startedAt:'2026-01-13 10:00:00', checkedAt:'2026-01-15 14:32:18',
    dbPath:'C:/db.sqlite', dbSize:20480, settingsPath:'C:/settings.json',
    disk:{total:1099511627776, used:360777252864, free:738734374912}}),
  content_save_settings: async(section, values)=>{
    window.calls.push({name:'content_save_settings', section:section, values:values});
    window.savedSettings = values;
    return {ok:true, section:section, settings:window.settingsView || {}};
  },
  content_seed_demo: async()=>{window.calls.push({name:'content_seed_demo'});return {ok:true, created:10}},
  content_clear_demo: async()=>({ok:true, removed:10}),
  content_enrich_pending: async()=>{window.calls.push({name:'content_enrich_pending'});return {ok:true, queued:3}},
  content_enrich: async(id)=>{window.calls.push({name:'content_enrich', id:id});return {ok:true}},
  content_reanalyze: async(id)=>{window.calls.push({name:'content_reanalyze', id:id});return {ok:true}},
  content_choose_folder: async()=>({ok:true, path:'D:/picked'}),
  content_import_folder: async(folder)=>{window.calls.push({name:'content_import_folder', folder:folder});return {ok:true, imported:2}},
  content_ingest_downloader: async()=>({ok:true, created:1}),
  content_ingest_videos: async()=>({ok:true, created:0}),
  content_register_download: async()=>({ok:true}),
  content_test_ai: async()=>({ok:true, model:'deepseek-chat', message:'OK'}),
  content_transcript: async()=>({ok:true, text:'hello world transcript'}),
  content_set_transcript: async(id,text)=>{window.calls.push({name:'content_set_transcript', id:id, text:text});return {ok:true, chars:text.length}},
  content_open_folder: async()=>({ok:true}),
  content_errors: async()=>({ok:true, entries:[], summary:{}, logExists:false}),
  content_item: async(id)=>({ok:true, item:null}),
  content_items: async()=>({ok:true, items:[]}),
  content_creators: async()=>({ok:true, creators:[]}),
  content_topic_distribution: async()=>({ok:true, topics:[]}),
  content_settings: async()=>({ok:true}),
  content_reset_settings: async()=>({ok:true, settings:window.settingsView || {}}),
  content_events: async()=>({ok:true, events:[]}),
};
window.pywebview = {api:{
  content_factory: factory,
  // 内嵌的下载器在 iframe 里，这里给它最小可用的一套，避免它初始化时报错
  recent_profiles: async()=>[],
  refresh_recent_profiles: async()=>({ok:true, profiles:[]}),
  get_filename_template: async()=>'%(title)s',
  get_learning_options: async()=>({enabled:false, apiKeySet:false}),
  get_cookie_status: async()=>({ok:true, count:0, hasSession:false, busy:false}),
  set_cookie_options: async()=>({ok:true, count:0, hasSession:false, busy:false}),
  set_filename_template: async()=>({ok:true}),
  set_learning_options: async()=>({ok:true}),
  get_update_info: async()=>({ok:true, current:'2.0.0', tokenSet:false}),
  load_task_state: async()=>null,
}};
"""


def chrome_path():
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("chrome") or shutil.which("chromium")


class ShellUITestCase(unittest.TestCase):
    items = [ITEM, FAILED_ITEM]

    def setUp(self):
        self.assert_bridge = None
        self._playwright = sync_playwright().start()
        browser = self._playwright.chromium.launch(executable_path=chrome_path(), headless=True)
        self.browser = browser
        self.page = browser.new_page(viewport={"width": 1400, "height": 940})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("https://**/*", lambda route: route.abort())
        script = (BRIDGE.replace("__ITEMS__", json.dumps(self.items, ensure_ascii=False))
                        .replace("__SETTINGS__", json.dumps(SETTINGS, ensure_ascii=False)))
        self.page.add_init_script(script)
        self.page.goto(APP_PATH.as_uri())
        self.page.wait_for_selector(".nav-item")

    def tearDown(self):
        try:
            self.browser.close()
        finally:
            self._playwright.stop()

    def nav(self, page_id):
        self.page.locator(f'.nav-item[data-page="{page_id}"]').click()
        self.page.wait_for_timeout(120)

    def heading(self):
        return self.page.locator("#content h1").inner_text()


class NavigationTests(ShellUITestCase):
    def test_every_main_page_switches_without_errors(self):
        expectations = {
            "dashboard": "首页", "creators": "创作者监控", "collect": "自动采集",
            "pipeline": "内容流水线", "ai": "AI 加工", "publish": "云端发布",
            "errors": "异常处理", "settings": "系统设置", "library": "视频库",
        }
        for page_id, title in expectations.items():
            self.nav(page_id)
            self.assertEqual(self.heading(), title, f"{page_id} 页标题不对")
        self.assertEqual(self.errors, [], "切换页面过程中不该有 JS 报错")

    def test_every_settings_sub_page_switches(self):
        self.nav("settings")
        expectations = {
            "basic": "基础设置", "collect": "采集设置", "ai": "AI 加工设置",
            "publish": "发布设置", "storage": "存储设置", "notify": "通知设置",
            "account": "账号管理",
        }
        for sub, text in expectations.items():
            self.page.locator(f'.tab[data-sub="{sub}"]').click()
            self.page.wait_for_timeout(100)
            self.assertTrue(self.page.locator(f'.tab[data-sub="{sub}"]').get_attribute("class").find("active") >= 0)
            self.assertIn(text, self.page.locator("#content").inner_text())
        self.assertEqual(self.errors, [])

    def test_active_nav_item_is_marked(self):
        self.nav("ai")
        classes = self.page.locator('.nav-item[data-page="ai"]').get_attribute("class")
        self.assertIn("active", classes)

    def test_hash_routing_survives_a_direct_link(self):
        self.page.goto(APP_PATH.as_uri() + "#/settings/storage")
        self.page.wait_for_selector(".nav-item")
        self.page.wait_for_timeout(160)
        self.assertIn("存储设置", self.page.locator("#content").inner_text())


class DashboardTests(ShellUITestCase):
    def test_dashboard_shows_real_statistics(self):
        self.nav("dashboard")
        text = self.page.locator("#content").inner_text()
        self.assertIn("内容总数", text)
        self.assertIn("AI 标注完成", text)
        self.assertIn("The real reason AI will change everything", text)

    def test_dashboard_tells_you_when_nothing_is_collected_yet(self):
        self.items = []
        self.tearDown()
        self.setUp()
        self.nav("dashboard")
        text = self.page.locator("#content").inner_text()
        self.assertIn("还没有内容", text)

    def test_seed_demo_button_calls_the_bridge(self):
        self.nav("dashboard")
        self.page.locator('[data-act="seed-demo"]').first.click()
        self.page.wait_for_timeout(160)
        names = self.page.evaluate("window.calls.map(c=>c.name)")
        self.assertIn("content_seed_demo", names)


class AiPageTests(ShellUITestCase):
    def test_enrichment_result_is_rendered(self):
        self.nav("ai")
        text = self.page.locator("#content").inner_text()
        for expected in ["AI 标注结果", "AI 科技", "B2", "美音", "偏快",
                         "the cost of thinking is collapsing", "重点句", "whether 引导宾语从句",
                         "复述", "deepseek-chat"]:
            self.assertIn(expected, text, f"AI 标注结果里缺少：{expected}")

    def test_failed_item_shows_the_reason_and_a_retry(self):
        self.nav("ai")
        self.page.locator('[data-act="ai-filter"][data-filter="failed"]').click()
        self.page.wait_for_timeout(140)
        text = self.page.locator("#content").inner_text()
        self.assertIn("Design system walkthrough", text)
        self.assertIn("未配置 API Key", text)

    def test_reanalyze_triggers_the_bridge(self):
        self.nav("ai")
        self.page.locator('[data-act="reanalyze"]').first.click()
        self.page.wait_for_timeout(160)
        calls = self.page.evaluate("window.calls.filter(c=>c.name==='content_reanalyze')")
        self.assertTrue(calls, "重新分析必须真的调用桥接")

    def test_transcript_editor_saves_and_reanalyzes(self):
        self.nav("ai")
        self.page.locator('[data-act="edit-transcript"]').first.click()
        self.page.wait_for_selector("#transcriptBox")
        self.page.fill("#transcriptBox", "手工粘贴的字幕文本")
        self.page.locator('[data-act="save-transcript"]').click()
        self.page.wait_for_timeout(200)
        calls = self.page.evaluate("window.calls.map(c=>c.name)")
        self.assertIn("content_set_transcript", calls)
        self.assertIn("content_reanalyze", calls)

    def test_status_filter_narrows_the_queue(self):
        self.nav("ai")
        self.page.locator('[data-act="ai-filter"][data-filter="done"]').click()
        self.page.wait_for_timeout(140)
        text = self.page.locator("#content").inner_text()
        self.assertIn("The real reason AI", text)
        self.assertNotIn("Design system walkthrough", text)


class PipelineAndErrorsTests(ShellUITestCase):
    def test_pipeline_lists_content_with_stage_progress(self):
        self.nav("pipeline")
        text = self.page.locator("#content").inner_text()
        self.assertIn("全自动内容工厂流水线", text)
        self.assertIn("The real reason AI will change everything", text)
        self.assertEqual(self.page.locator(".steps").count() >= 1, True)

    def test_errors_page_shows_store_and_log_entries(self):
        self.nav("errors")
        text = self.page.locator("#content").inner_text()
        self.assertIn("未配置 API Key", text)
        self.assertIn("下载失败: 请求超时", text)
        self.assertIn("scrape.log", text)

    def test_errors_badge_counts_entries(self):
        badge = self.page.locator('.nav-item[data-page="errors"] .nav-badge')
        self.assertEqual(badge.inner_text(), "2")

    def test_publish_page_is_complete_but_honest(self):
        self.nav("publish")
        text = self.page.locator("#content").inner_text()
        self.assertIn("本阶段为界面演示", text)
        self.assertIn("发布任务队列", text)


class SettingsTests(ShellUITestCase):
    def test_storage_form_is_editable_and_saves_with_data_keys(self):
        self.nav("settings")
        self.page.locator('.tab[data-sub="storage"]').click()
        self.page.wait_for_timeout(120)
        self.page.fill('[data-key="bucket"]', "my-new-bucket")
        self.page.fill('[data-key="keep_video_days"]', "30")
        self.page.locator('[data-act="settings-save"][data-section="storage"]').click()
        self.page.wait_for_timeout(200)
        call = self.page.evaluate("window.calls.find(c=>c.name==='content_save_settings')")
        self.assertIsNotNone(call, "保存必须调用 content_save_settings")
        self.assertEqual(call["section"], "storage")
        self.assertEqual(call["values"]["bucket"], "my-new-bucket")
        self.assertEqual(call["values"]["keep_video_days"], 30, "数字字段要以数字类型提交")

    def test_ai_settings_expose_provider_model_key_and_prompt(self):
        self.nav("settings")
        self.page.locator('.tab[data-sub="ai"]').click()
        self.page.wait_for_timeout(120)
        text = self.page.locator("#content").inner_text()
        for expected in ["AI 服务商", "主模型", "API Key", "最大 Token 数", "温度", "输出语言", "Prompt 模板"]:
            self.assertIn(expected, text)
        self.assertTrue(self.page.locator('[data-key="prompt_template"]').is_visible())

    def test_switches_and_multiselects_serialize_correctly(self):
        self.nav("settings")
        self.page.locator('.tab[data-sub="collect"]').click()
        self.page.wait_for_timeout(120)
        self.page.locator('[data-key="auto_collect"]').uncheck()
        self.page.locator('[data-act="settings-save"][data-section="collect"]').click()
        self.page.wait_for_timeout(200)
        call = self.page.evaluate("window.calls.find(c=>c.name==='content_save_settings')")
        self.assertEqual(call["values"]["auto_collect"], False)
        self.assertIsInstance(call["values"]["filter_rules"], list)

    def test_prompt_preview_renders_the_template(self):
        self.nav("settings")
        self.page.locator('.tab[data-sub="ai"]').click()
        self.page.wait_for_timeout(120)
        self.page.locator('[data-act="prompt-preview"]').click()
        self.page.wait_for_selector(".modal")
        text = self.page.locator(".modal").inner_text()
        self.assertIn("3 habits that changed my life", text)
        self.assertNotIn("{title}", text)

    def test_account_page_shows_real_runtime_facts(self):
        self.nav("settings")
        self.page.locator('.tab[data-sub="account"]').click()
        self.page.wait_for_timeout(120)
        text = self.page.locator("#content").inner_text()
        self.assertIn("3.11.9", text)
        self.assertIn("2 天 14 小时", text)


class LibraryTests(ShellUITestCase):
    def test_library_embeds_the_downloader(self):
        self.nav("library")
        frame = self.page.frame_locator("#libraryFrame")
        frame.locator("#url").wait_for(timeout=8000)
        self.assertTrue(frame.locator("#recognize").is_visible(), "内嵌的下载器必须完整可用")


class DegradedModeTests(unittest.TestCase):
    def test_missing_bridge_says_so_instead_of_blank_page(self):
        """没有桥接时（例如直接在浏览器里打开这个文件）必须给出明确说明。

        注意别写成"启动完成就断言" —— 页面初次绘制时本来就写着「正在启动内容工厂」，
        那样断言会在等待窗口内就通过，等于什么都没测。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome_path(), headless=True)
            try:
                page = browser.new_page(viewport={"width": 1200, "height": 800})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(APP_PATH.as_uri())
                page.wait_for_function(
                    "document.getElementById('content').textContent.includes('没有检测到应用桥接')",
                    timeout=20000)
                self.assertEqual(errors, [], "等待降级提示的过程中不该有 JS 报错")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
