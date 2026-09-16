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
// 创作者监控：假桥接要能被「添加」改变，否则测不出「保存后列表刷新」。
// 字段形态与真实 content_creator_list() 一致（含 enabled / is_due / checking 等监控状态）。
window.creators = [
  {id:'c1', handle:'techwithtim', display_name:'Tim', category:'AI 科技',
   status:'active', priority:'高', poll_interval:'1 小时', item_count:1,
   enabled:true, poll_interval_seconds:3600, last_state:'', checking:false,
   is_due:false, due_in_seconds:1200, next_check_at:'2026-01-15 15:00:00'},
  {id:'c2', handle:'moonvy', display_name:'Moonvy', category:'设计', status:'error',
   priority:'中', poll_interval:'30 分钟', item_count:1,
   enabled:true, poll_interval_seconds:1800, last_state:'failed', checking:false,
   is_due:true, due_in_seconds:0, next_check_at:''},
];
window.creatorStats = {total:2, enabled:2, disabled:0, due:1, checking:0};
// 「立即跑 1 条」的假后端：按测试配置一次性给出最终状态。
window.runOnePlan = {ok:true, processed:1, stage:'done', message:'@nasa 的 1 条内容已处理完成'};
window.runOneRuns = {};
window.runOneStarted = (creatorId)=>{
  const creator = (window.creators.find(c=>c.id===creatorId) || {handle:'nasa'});
  const plan = window.runOnePlan || {};
  window.runOneRuns[creatorId] = {
    runId:'run_test_1', creatorId:creatorId, handle:creator.handle,
    status: plan.ok === false ? 'failed' : 'done', stage: plan.stage || 'done',
    stageLabel:'完成', finishedAt:Date.now()/1000,
    steps:[{stage:'checking',label:'检查 Creator 新内容',state:'done'},
           {stage:'discovered',label:'发现最新内容',state:'done'},
           {stage:'downloading',label:'下载',state:'done'},
           {stage:'downloaded',label:'下载完成',state:'done'},
           {stage:'transcribing',label:'读取字幕 / 本机 ASR',state:'done'},
           {stage:'enriching',label:'AI 标注',state:'done'},
           {stage:'done',label:'完成',state:'done'}],
    error: plan.ok === false ? (plan.error || '失败') : '',
    result: Object.assign({ok:plan.ok !== false, creatorId:creatorId, handle:creator.handle,
                           processed:plan.processed===undefined?1:plan.processed,
                           stage:plan.stage||'done', message:plan.message||'',
                           itemId:plan.itemId||'', error:plan.error||''}, plan),
  };
};
window.currentRun = ()=>Object.values(window.runOneRuns)[0] || null;
const factory = {
  content_stats: async()=>({ok:true, counts:{total:2, enriched:1, pending:0, running:0,
      failed:1, transcribed:1, downloaded:2, creators:2}}),
  content_bootstrap: async()=>({
    ok:true,
    stats:{ok:true, counts:{total:2, enriched:1, pending:0, running:0, failed:1,
      transcribed:1, downloaded:2, creators:2}, topicDistribution:[], aiConfigured:true,
      model:'deepseek-chat', provider:'DeepSeek', asrAvailable:false, dbPath:'C:/db.sqlite'},
    items: __ITEMS__,
    creators:window.creators,
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
  content_creators: async()=>({ok:true, creators:window.creators}),
  // 创作者监控页的真实数据源（Creator Monitor）。语义与后端一致：
  // 空 handle / 非法 handle / 重复 handle 都返回 ok:false + 真实 error。
  content_creator_list: async()=>{
    window.calls.push({name:'content_creator_list'});
    return {ok:true, creators:window.creators, stats:window.creatorStats};
  },
  content_creator_save: async(values)=>{
    window.calls.push({name:'content_creator_save', values:values});
    const raw = String((values&&values.handle)||'').trim();
    const handle = raw.replace(/^@+/, '');
    if (!handle) return {ok:false, error:'handle 不能为空'};
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(handle)) {
      return {ok:false, error:'handle 不合法：'+handle+'（只允许字母、数字、. _ -）'};
    }
    if (window.creators.some(c=>c.handle.toLowerCase()===handle.toLowerCase())) {
      return {ok:false, duplicate:true, error:'创作者 @'+handle+' 已存在'};
    }
    const creator = {id:'creator_'+(window.creators.length+1), handle:handle,
      display_name:(values&&values.display_name)||handle, category:(values&&values.category)||'',
      status:'active', priority:(values&&values.priority)||'中',
      poll_interval:(values&&values.poll_interval)||'1 小时', item_count:0,
      enabled:(values&&values.enabled)===false?false:true, poll_interval_seconds:1800,
      last_state:'', checking:false, is_due:true, due_in_seconds:0, next_check_at:''};
    window.creators = window.creators.concat([creator]);
    window.creatorStats = Object.assign({}, window.creatorStats, {total:window.creators.length});
    return {ok:true, created:true, creatorId:creator.id, creator:creator, creators:window.creators};
  },
  content_creator_toggle: async(id, enabled)=>{
    window.calls.push({name:'content_creator_toggle', id:id, enabled:enabled});
    return {ok:true, enabled:enabled!==false};
  },
  // 「立即跑 1 条」：默认直接给一个已完成的 run（界面轮询一次就能拿到结果）。
  // 真实后端是后台线程 + 进度状态，这里保留同样的字段形状，界面代码不用分叉。
  content_run_one_creator: async(creatorId)=>{
    window.calls.push({name:'content_run_one_creator', creatorId:creatorId});
    if (!window.runOneStarted) return {ok:false, error:'测试里没有配置这个 run'};
    window.runOneStarted(creatorId);
    return {ok:true, started:true, runId:window.currentRun().runId, creatorId:creatorId,
            handle:window.currentRun().handle, stage:'checking'};
  },
  content_run_one_status: async(runId, creatorId)=>{
    window.calls.push({name:'content_run_one_status', runId:runId, creatorId:creatorId});
    return {ok:true, run:window.currentRun()};
  },
  content_topic_distribution: async()=>({ok:true, topics:[]}),
  content_settings: async()=>({ok:true}),
  content_reset_settings: async()=>({ok:true, settings:window.settingsView || {}}),
  content_import_legacy_ai: async()=>{
    window.calls.push({name:'content_import_legacy_ai'});
    return {ok:true, model:'deepseek-chat', api_base:'https://api.deepseek.com/v1'};
  },
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
        for expected in ["本次 AI 加工内容", "内容分析结果", "AI 科技", "B2", "美音", "偏快",
                         "the cost of thinking is collapsing", "重点句", "whether 引导宾语从句",
                         "复述", "deepseek-chat", "资源与状态"]:
            self.assertIn(expected, text, f"AI 标注结果里缺少：{expected}")

    def test_queue_shows_the_annotation_columns(self):
        """队列列结构对齐参考图：转写 / AI 标签 / 表达提取 / 生成学习内容。"""
        self.nav("ai")
        header = self.page.locator("#content table thead").inner_text()
        for expected in ["视频信息", "创作者", "转写", "AI 标签", "表达 / 语法",
                         "标注结果", "状态", "操作"]:
            self.assertIn(expected, header, f"队列表头缺少：{expected}")

    def test_long_lists_can_be_expanded(self):
        """重点表达默认只露 3 条，点「查看全部」要能展开（参考图里的折叠行为）。"""
        self.tearDown()
        many = dict(ITEM)
        many["enrichment"] = dict(ENRICHMENT)
        many["enrichment"]["expressions"] = [
            {"text": f"expression number {i}", "meaning_zh": f"表达 {i}"} for i in range(6)]
        self.items = [many]
        self.setUp()
        self.nav("ai")
        text = self.page.locator("#content").inner_text()
        self.assertIn("expression number 0", text)
        self.assertNotIn("expression number 5", text, "默认应收起超出的条目")
        self.page.locator('[data-act="ai-expand"][data-expand="expressions"]').click()
        self.page.wait_for_timeout(140)
        self.assertIn("expression number 5", self.page.locator("#content").inner_text())

    def test_processing_log_and_model_panel_are_present(self):
        self.nav("ai")
        text = self.page.locator("#content").inner_text()
        for expected in ["Prompt / 模型处理日志", "组装 Prompt", "写入内容库", "模型与资源状态"]:
            self.assertIn(expected, text)

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

    def test_browse_button_is_not_collected_as_a_setting_field(self):
        """目录「浏览」按钮不能被 collectSettings 当成设置字段。

        它以前也带 data-key（和旁边的 input 同名），于是保存时：
        input 先写入路径 → button 后写入 "" （button.value 默认空串）→ 路径被清空，
        用户选好目录、点了保存，落盘的却是空字符串，采集器一直报「未配置下载目录」。
        """
        self.nav("settings")
        self.page.locator('.tab[data-sub="storage"]').click()
        self.page.wait_for_timeout(120)
        self.assertTrue(self.page.locator('[data-act="pick-folder"]').first.is_visible(),
                        "存储设置里必须有「浏览」按钮")
        collected = self.page.evaluate("""(() => {
          const keeps = [];
          document.querySelectorAll('#content [data-key]').forEach((node) => {
            const tag = String(node.tagName || '').toLowerCase();
            if (!['input', 'select', 'textarea'].includes(tag)) {
              keeps.push(tag + ':' + node.dataset.key);
            }
          });
          return keeps;
        })()""")
        self.assertEqual(collected, [],
                         f"只有 input/select/textarea 能带 data-key，实际还有：{collected}")

    def test_browse_button_does_not_clear_the_path_on_save(self):
        """选好目录再保存，发出去的必须是那个目录，不能被「浏览」按钮覆盖成空。"""
        self.nav("settings")
        self.page.locator('.tab[data-sub="storage"]').click()
        self.page.wait_for_timeout(120)
        self.page.fill('[data-key="video_path"]', r"E:\ContentFactory\Videos")
        self.page.locator('[data-act="settings-save"][data-section="storage"]').click()
        self.page.wait_for_timeout(200)
        call = self.page.evaluate(
            "window.calls.filter(c=>c.name==='content_save_settings').pop()")
        self.assertEqual(call["values"]["video_path"], r"E:\ContentFactory\Videos",
                         "保存时 video_path 不能被「浏览」按钮覆盖成空字符串")

    def test_all_four_local_paths_survive_a_save(self):
        """四个本地路径字段（视频 / 封面 / 素材 / 临时）都要正确提交。"""
        paths = {
            "video_path": r"E:\ContentFactory\Videos",
            "cover_path": r"E:\ContentFactory\Covers",
            "library_path": r"E:\ContentFactory\Library",
            "temp_path": r"E:\ContentFactory\Temp",
        }
        self.nav("settings")
        self.page.locator('.tab[data-sub="storage"]').click()
        self.page.wait_for_timeout(120)
        for key, value in paths.items():
            self.page.fill(f'[data-key="{key}"]', value)
        self.page.locator('[data-act="settings-save"][data-section="storage"]').click()
        self.page.wait_for_timeout(200)
        call = self.page.evaluate(
            "window.calls.filter(c=>c.name==='content_save_settings').pop()")
        for key, value in paths.items():
            self.assertEqual(call["values"][key], value, f"{key} 没有正确提交")

    def test_pick_folder_writes_the_chosen_path_into_the_input(self):
        """桥接返回的目录要写进对应的 input（不是写进按钮，也不是丢掉）。"""
        self.nav("settings")
        self.page.locator('.tab[data-sub="storage"]').click()
        self.page.wait_for_timeout(120)
        self.page.evaluate("""window.pywebview.api.content_factory.content_choose_folder =
          async(kind)=>({ok:true, path:'E:\\\\Picked\\\\By\\\\Dialog', kind:kind})""")
        row = self.page.locator('[data-act="pick-folder"]').first
        row.click()
        self.page.wait_for_timeout(250)
        value = self.page.eval_on_selector('[data-key="video_path"]', "node => node.value")
        self.assertEqual(value, r"E:\Picked\By\Dialog")
        self.page.locator('[data-act="settings-save"][data-section="storage"]').click()
        self.page.wait_for_timeout(200)
        call = self.page.evaluate(
            "window.calls.filter(c=>c.name==='content_save_settings').pop()")
        self.assertEqual(call["values"]["video_path"], r"E:\Picked\By\Dialog",
                         "浏览选中的目录要能原样保存")

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

    def test_ai_settings_can_import_the_legacy_credentials(self):
        """「导入已有配置」按钮：复用下载器学习文档里配好的 Key，别让用户再填一遍。"""
        self.nav("settings")
        self.page.locator('.tab[data-sub="ai"]').click()
        self.page.wait_for_timeout(120)
        self.page.locator('[data-act="import-legacy-ai"]').click()
        self.page.wait_for_timeout(200)
        calls = self.page.evaluate("window.calls.map(c=>c.name)")
        self.assertIn("content_import_legacy_ai", calls)

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


class CreatorMonitorTests(ShellUITestCase):
    """创作者监控页：真正能添加创作者（回归 bug：按钮以前只是跳视频库）。"""

    def open_form(self):
        self.nav("creators")
        self.page.locator('[data-act="creator-add"]').first.click()
        self.page.wait_for_selector("#creatorHandle")

    def test_add_button_opens_the_creator_modal_instead_of_jumping_to_library(self):
        self.nav("creators")
        button = self.page.locator('[data-act="creator-add"]').first
        self.assertNotEqual((button.get_attribute("data-act") or ""), "nav",
                            "「＋ 添加创作者」不能再是跳转视频库的导航按钮")
        button.click()
        self.page.wait_for_selector(".modal")
        self.assertIn("添加创作者", self.page.locator(".modal").inner_text())
        self.assertTrue(self.page.locator("#creatorHandle").is_visible())
        self.assertEqual(self.page.evaluate("location.hash"), "#/creators",
                         "点添加创作者不应该离开创作者监控页")

    def test_modal_submits_the_creator_and_refreshes_the_list(self):
        self.open_form()
        self.page.fill("#creatorHandle", "@nasa")
        self.page.fill("#creatorName", "NASA")
        self.page.select_option("#creatorPriority", "高")
        self.page.locator('[data-act="creator-save"]').click()
        self.page.wait_for_timeout(250)

        call = self.page.evaluate("window.calls.find(c=>c.name==='content_creator_save')")
        self.assertIsNotNone(call, "保存必须调用 content_creator_save")
        self.assertEqual(call["values"]["handle"], "nasa", "保存时要统一去掉 @")
        self.assertEqual(call["values"]["display_name"], "NASA")
        self.assertEqual(call["values"]["priority"], "高")
        self.assertTrue(call["values"]["enabled"])
        self.assertEqual(self.page.locator(".modal").count(), 0, "保存成功后要关闭 modal")
        self.assertIn("@nasa", self.page.locator("#content").inner_text(),
                      "保存成功后要重新拉列表并渲染出 @nasa")
        self.assertIn("已添加 @nasa", self.page.locator("#toasts").inner_text())

    def test_empty_handle_is_reported_and_nothing_is_saved(self):
        self.open_form()
        self.page.locator('[data-act="creator-save"]').click()
        self.page.wait_for_timeout(200)
        self.assertTrue(self.page.locator(".modal").is_visible(), "失败时 modal 要留着")
        self.assertIn("Handle", self.page.locator("#creatorFormMsg").inner_text())
        self.assertEqual(
            self.page.evaluate("window.calls.filter(c=>c.name==='content_creator_save').length"), 0,
            "空 handle 不该发到桥接")

    def test_invalid_handle_shows_the_real_error_and_keeps_the_modal(self):
        self.open_form()
        self.page.fill("#creatorHandle", "bad handle!")
        self.page.locator('[data-act="creator-save"]').click()
        self.page.wait_for_timeout(250)
        self.assertTrue(self.page.locator(".modal").is_visible(), "失败时不能关 modal")
        self.assertIn("handle 不合法", self.page.locator("#creatorFormMsg").inner_text())
        self.assertNotIn("@bad handle!", self.page.locator("#content").inner_text())

    def test_duplicate_creator_is_reported_and_not_added_twice(self):
        self.open_form()
        self.page.fill("#creatorHandle", "techwithtim")
        self.page.locator('[data-act="creator-save"]').click()
        self.page.wait_for_timeout(250)
        self.assertTrue(self.page.locator(".modal").is_visible())
        self.assertIn("已存在", self.page.locator("#creatorFormMsg").inner_text())
        self.assertEqual(
            self.page.evaluate("window.creators.filter(c=>c.handle==='techwithtim').length"), 1,
            "重复添加不能产生第二条记录")

    def test_bare_handle_without_at_is_accepted(self):
        self.open_form()
        self.page.fill("#creatorHandle", "nasa")
        self.page.locator('[data-act="creator-save"]').click()
        self.page.wait_for_timeout(250)
        call = self.page.evaluate("window.calls.find(c=>c.name==='content_creator_save')")
        self.assertEqual(call["values"]["handle"], "nasa")

    def test_registered_creators_show_their_monitor_state(self):
        """列表来自 content_creator_list()：状态与下次检查时间是监控器算出来的。"""
        self.nav("creators")
        text = self.page.locator("#content").inner_text()
        self.assertIn("@techwithtim", text)
        self.assertIn("20 分钟后", text, "下次检查要按 due_in_seconds 显示")
        self.assertIn("即将检查", text, "已到点的创作者要显示即将检查")
        self.assertIn("异常", text, "上次检查失败的创作者要显示异常")
        self.assertEqual(
            self.page.evaluate("window.calls.filter(c=>c.name==='content_creator_list').length") > 0,
            True, "创作者监控页必须从 content_creator_list() 取数据")


class RunOneCreatorTests(ShellUITestCase):
    """「立即跑 1 条」：一个 Creator 的一条新内容跑到底的用户入口。"""

    def run_button(self):
        self.nav("creators")
        return self.page.locator('[data-act="creator-run-one"]').first

    def test_every_creator_row_has_a_run_one_button(self):
        self.nav("creators")
        buttons = self.page.locator('[data-act="creator-run-one"]')
        self.assertEqual(buttons.count(), 2, "每个 Creator 都要有「立即跑 1 条」")
        self.assertEqual(buttons.first.inner_text().strip(), "立即跑 1 条")

    def test_the_button_can_be_used_again_after_a_successful_run(self):
        """跑完一次不能一直卡在「运行中…」——用户要能接着跑第二条。"""
        self.run_button().click()
        self.page.wait_for_function(
            "() => (document.getElementById('toasts').innerText || '').includes('查看 AI 结果')",
            timeout=15000)
        row = self.page.locator("#content tr", has_text="@techwithtim").first
        button = row.locator('[data-act="creator-run-one"]')
        self.assertEqual(button.inner_text().strip(), "再跑 1 条")
        self.assertFalse(button.is_disabled(), "跑完之后必须还能再点")
        button.click()
        self.page.wait_for_timeout(600)
        self.assertEqual(self.page.evaluate(
            "window.calls.filter(c=>c.name==='content_run_one_creator').length"), 2,
            "第二次点击要真的再跑一次")

    def test_success_shows_the_result_and_offers_a_jump_to_the_ai_result(self):
        button = self.run_button()
        button.click()
        self.page.wait_for_function(
            "() => (document.getElementById('toasts').innerText || '').includes('查看 AI 结果')",
            timeout=15000)
        call = self.page.evaluate("window.calls.find(c=>c.name==='content_run_one_creator')")
        self.assertIsNotNone(call, "按钮必须调用 content_run_one_creator")
        self.assertEqual(call["creatorId"], "c1")
        self.assertTrue(self.page.evaluate(
            "window.calls.some(c=>c.name==='content_run_one_status')"),
            "必须轮询 content_run_one_status，用户要能看到到哪一步了")
        toasts = self.page.locator("#toasts").inner_text()
        self.assertIn("@techwithtim 的 1 条内容已处理完成", toasts)

        # 行内步骤条：检查 / 发现 / 下载 / 字幕 / AI 都要走完
        row = self.page.locator("#content tr", has_text="@techwithtim").first
        self.assertIn("已处理完成", row.inner_text())

        # 「查看 AI 结果」跳到 AI 加工页
        self.page.locator('[data-act="toast-link"]').first.click()
        self.page.wait_for_timeout(250)
        self.assertEqual(self.page.evaluate("location.hash"), "#/ai")
        self.assertIn("AI 加工", self.heading())

    def test_failure_names_the_layer_that_failed(self):
        self.page.evaluate("""window.runOnePlan = {ok:false, processed:0, stage:'download',
          error:'下载失败：网络请求超时'}""")
        self.run_button().click()
        self.page.wait_for_function(
            "() => (document.getElementById('toasts').innerText || '').includes('下载失败')",
            timeout=15000)
        text = self.page.locator("#toasts").inner_text()
        self.assertIn("下载失败：网络请求超时", text)
        self.assertIn("下载", text, "失败提示要说清是哪一层")

    def test_preflight_failure_is_shown_immediately_without_starting_a_run(self):
        """没配下载目录时，点按钮要当场报错，不能起一个必然失败的 run。"""
        self.page.evaluate("""window.pywebview.api.content_factory.content_run_one_creator =
          async()=>({ok:false, started:false, stage:'preflight', needsFolder:true,
                     error:'未配置下载目录：请在「设置 → 存储设置」里填写保存路径'})""")
        self.run_button().click()
        self.page.wait_for_function(
            "() => (document.getElementById('toasts').innerText || '').includes('下载目录')",
            timeout=10000)
        self.assertIn("存储设置", self.page.locator("#toasts").inner_text())
        self.assertEqual(self.page.evaluate(
            "window.calls.filter(c=>c.name==='content_run_one_status').length"), 0,
            "前置检查失败就不该去轮询进度")

    def test_no_new_content_is_reported_as_information_not_as_failure(self):
        self.page.evaluate("""window.runOnePlan = {ok:true, processed:0, stage:'no_content',
          message:'没有发现新的可处理内容（主页读到 5 条，都已在内容库或采集队列里）'}""")
        self.run_button().click()
        self.page.wait_for_function(
            "() => (document.getElementById('toasts').innerText || '').includes('没有发现新的可处理内容')",
            timeout=15000)
        self.assertIn("没有发现新的可处理内容", self.page.locator("#toasts").inner_text())


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
