"""所有设置分区的定义与默认值 —— Settings Core 的单一事实来源。

分两类：

1. **界面分区**（ui_section=True）：与 `ui/app.js` 的 7 个设置子页一一对应，
   沿用第一阶段的 9 个分区名与全部默认值，一个字都没改（旧文件、旧调用方保持不变）：
   general / work_mode / logging / collect / ai / publish / storage / notify / account

   ⚠️ 已知的界面怪癖：「基础设置」这一页把 work_mode 与 logging 的字段一起收上来，
   但保存按钮的 data-section 是 `general`，所以那两个分区的键实际落在 general 里。
   界面不能改，所以 Settings Core 对未知字段走「保留」策略（见 service 的宽松模式），
   这些键照旧存得住。programmatic 调用方请直接用 work_mode / logging 分区。

2. **引擎分区**（ui_section=False）：为并行开发的模块预留的配置域。
   general / ai / collector / transcript(asr) / pipeline / storage / publishing
   这些领域在 Settings Core 里**只有定义与校验**，怎么用是各模块自己的事：
   - collector   → Creator Monitor / Collector
   - transcript  → ASR（别名 asr）
   - pipeline    → Pipeline
   - publishing  → Publisher（本阶段不做真实发布，默认 dry_run）

   引擎分区的默认值一律取「安全值」：不启用、不并发轰炸、不真发布。
"""

from ..ai_enrichment import DEFAULT_PROMPT_TEMPLATE
from .schema import (ANY, BOOL, ENUM, FLOAT, INT, LIST, STRING, Field, Section,
                     register_sections, resolve_section_name)

# 界面里"字幕抓取优先级"的可选值（与 app.js 的 fieldSelect 完全一致）
_SUBTITLE_PRIORITY = (
    "原生字幕 > 自动生成 > 不抓取", "仅原生字幕", "仅自动生成", "不抓取字幕",
)
_PUBLISH_PLATFORMS = ("抖音", "小红书", "B站", "视频号", "YouTube", "Tony Learning OS")
_MEDIA_FILE_TYPES = ["mp4", "mov", "avi", "jpg", "png", "webp"]


def F(key, type=ANY, default=None, **kwargs):
    """Field 的简写，让下面的分区定义读起来像一张表。"""
    return Field(key=key, type=type, default=default, **kwargs)


# =========================================================================
# 一、界面分区（默认值与第一阶段完全一致，不允许改：旧文件、旧调用方都在这上面）
# =========================================================================

GENERAL = Section(
    name="general", title="基础设置",
    description="应用语言、主题、启动与后台运行偏好",
    aliases=("basic",),
    fields=(
        F("app_language", ENUM, "zh-CN", choices=("zh-CN", "en-US"), description="界面语言"),
        F("theme", ENUM, "system", choices=("system", "light", "dark"), description="主题模式"),
        F("auto_start", BOOL, True, description="开机自启动"),
        F("start_minimized", BOOL, False, description="启动时最小化到托盘"),
        F("run_in_background", BOOL, False, description="最小化后继续后台运行"),
    ),
)

WORK_MODE = Section(
    name="work_mode", title="工作模式",
    description="运行模式与任务并发偏好（第一阶段的调度只是偏好，未实现真实调度）",
    fields=(
        F("mode", ENUM, "auto24", choices=("manual", "timed", "auto24"), description="运行模式"),
        F("concurrent_tasks", INT, 3, minimum=1, maximum=64, description="同时运行任务数"),
        F("task_interval_sec", INT, 60, minimum=0, maximum=86400, description="任务间隔（秒）"),
    ),
)

LOGGING = Section(
    name="logging", title="日志与监控",
    description="日志级别、详细日志、清理与运行报告",
    fields=(
        F("level", ENUM, "INFO", choices=("DEBUG", "INFO", "WARN", "ERROR"), description="日志级别"),
        F("save_detail", BOOL, True, description="保存详细日志"),
        F("auto_clean_days", INT, 30, minimum=1, maximum=3650, description="日志自动清理（天）"),
        F("notify_on_error", BOOL, True, description="错误时发送通知"),
        F("daily_report", BOOL, False, description="每日运行报告"),
    ),
)

COLLECT = Section(
    name="collect", title="采集设置",
    description="创作者监控、内容过滤、采集来源与限额（界面分区；新引擎请用 collector）",
    aliases=("collect_settings",),
    fields=(
        F("group", STRING, "默认分组", description="监控分组"),
        F("auto_collect", BOOL, True, description="自动采集"),
        F("interval_minutes", INT, 30, minimum=1, maximum=10080, description="检查频率（分钟）"),
        F("priority_english", BOOL, True, description="优先抓取英语内容"),
        F("new_video_auto_join", BOOL, True, description="新视频自动入队"),
        F("duration_min_sec", INT, 15, minimum=0, maximum=86400, description="最短时长（秒）"),
        F("duration_max_sec", INT, 600, minimum=0, maximum=86400, description="最长时长（秒）"),
        F("low_quality_filter", BOOL, True, description="低价值内容过滤"),
        F("filter_rules", LIST, ["广告营销", "纯图文", "低播放量", "重复内容", "无实质内容"],
          item_type=STRING, description="过滤规则"),
        F("dedupe_method", ENUM, "视频指纹（推荐）",
          choices=("视频指纹（推荐）", "标题相似", "作者 + 发布时间"), description="去重方式"),
        F("similarity_threshold", INT, 90, minimum=0, maximum=100, description="相似度阈值（%）"),
        F("check_fingerprint", BOOL, True, description="去重时检查视频指纹"),
        F("check_title", BOOL, True, description="去重时检查标题"),
        F("check_author_time", BOOL, True, description="去重时检查作者与发布时间"),
        F("check_audio", BOOL, False, description="去重时检查音频指纹"),
        # 界面「同时检查」多选框：界面上没读回设置值，但保存时会传上来，所以纳入 schema。
        F("dedupe_checks", LIST, [], item_type=STRING, description="去重检查项（界面字段）"),
        F("history_enabled", BOOL, True, description="启用历史内容回填"),
        F("history_days", INT, 90, minimum=1, maximum=3650, description="回填时间范围（天）"),
        F("history_per_creator", INT, 200, minimum=1, maximum=100000, description="每创作者回填上限"),
        F("history_speed_per_hour", INT, 100, minimum=1, maximum=100000, description="回填速度（条/小时）"),
        F("download_quality", ENUM, "1080p", choices=("1080p", "720p", "540p", "最佳画质"),
          description="下载清晰度"),
        F("subtitle_priority", ENUM, _SUBTITLE_PRIORITY[0], choices=_SUBTITLE_PRIORITY,
          description="字幕抓取优先级"),
        F("extract_audio", BOOL, True, description="自动提取音频"),
        F("subtitle_format", ENUM, "SRT", choices=("SRT", "VTT", "TXT"), description="字幕保存格式"),
        F("keywords_allow", LIST, ["tutorial", "how to", "tips", "tricks"], item_type=STRING,
          description="关键词白名单"),
        F("keywords_block", LIST, ["ad", "sponsored", "promo", "giveaway"], item_type=STRING,
          description="关键词黑名单"),
        F("source_scope", ENUM, "仅创作者主页", choices=("仅创作者主页", "主页 + 话题页", "全部"),
          description="采集来源范围"),
        F("include_types", LIST, ["公开视频", "图文帖子"], item_type=STRING, description="包含内容类型"),
        F("exclude_keywords", STRING, "", description="排除指定内容（每行一个）"),
        F("fail_retry", INT, 3, minimum=0, maximum=100, description="失败重试次数"),
        F("window_start", STRING, "00:00", description="采集时间窗口开始"),
        F("window_end", STRING, "23:59", description="采集时间窗口结束"),
        F("daily_limit", INT, 1000, minimum=0, maximum=1000000, description="单日采集上限"),
        F("per_creator_limit", INT, 50, minimum=1, maximum=1000000, description="每创作者上限"),
    ),
)

AI = Section(
    name="ai", title="AI 加工设置",
    description="模型服务商、生成参数与 Prompt 模板（api_key 由 Secrets/Provider 模块统一管理）",
    fields=(
        F("provider", STRING, "DeepSeek", description="AI 服务商"),
        F("model", STRING, "deepseek-chat", description="主模型"),
        F("fallback_model", STRING, "deepseek-coder", description="备用模型"),
        F("embedding_model", STRING, "text-embedding-v3", description="Embedding 模型"),
        F("asr_provider", STRING, "OpenAI Whisper（本地）", description="ASR / 转写方式"),
        F("output_language", STRING, "双语（中英）", description="输出语言"),
        F("api_base", STRING, "https://api.deepseek.com/v1", description="API 地址（OpenAI 兼容）"),
        F("api_key", STRING, "", secret=True, public_flag="apiKeySet",
          description="API Key（敏感：只回是否已设置）"),
        F("max_tokens", INT, 4000, minimum=1, maximum=1000000, description="最大 Token 数"),
        F("temperature", FLOAT, 0.7, minimum=0.0, maximum=2.0, description="温度（创造性）"),
        F("batch_size", INT, 5, minimum=1, maximum=200, description="批处理大小"),
        F("extract_key_points", BOOL, True, description="自动提取重点表达"),
        F("detect_grammar", BOOL, True, description="自动识别语法点"),
        F("generate_examples", BOOL, True, description="自动生成例句"),
        F("generate_exercises", BOOL, True, description="自动生成练习"),
        F("generate_cards", BOOL, True, description="自动生成学习卡片"),
        F("quality_threshold", FLOAT, 0.6, minimum=0.0, maximum=1.0, description="内容质量阈值"),
        F("learning_value_min", FLOAT, 0.7, minimum=0.0, maximum=1.0, description="学习价值最低分"),
        F("prompt_template", STRING, DEFAULT_PROMPT_TEMPLATE, description="Prompt 模板"),
    ),
)

PUBLISH = Section(
    name="publish", title="发布设置",
    description="发布平台、频率、标签与队列（界面分区；真实发布请用 publishing）",
    aliases=("publish_settings",),
    fields=(
        F("targets", LIST, list(_PUBLISH_PLATFORMS), item_type=STRING, description="发布目标平台"),
        F("sync_to_learning_os", BOOL, True, description="同步到 Tony Learning OS"),
        F("publish_frequency", ENUM, "每天固定数量",
          choices=("每天固定数量", "每天固定时段", "间隔发布"), description="发布频率"),
        F("daily_count", INT, 5, minimum=0, maximum=10000, description="每日发布数量"),
        F("slots", LIST, ["09:00 – 12:00", "14:00 – 18:00", "19:00 – 22:00"], item_type=STRING,
          description="发布时段"),
        F("category_rule", ENUM, "按内容主题自动匹配",
          choices=("按内容主题自动匹配", "按创作者分类", "手动指定"), description="分类映射规则"),
        F("default_category", STRING, "生活技巧", description="默认分类"),
        F("topic_tags", LIST, ["AI 推荐", "热门话题"], item_type=STRING, description="话题标签"),
        F("custom_tags", LIST, [], item_type=STRING, description="自定义标签"),
        F("title_rule", STRING, "AI 生成（结合关键词）", description="标题生成规则"),
        F("title_template", STRING, "{title} | {theme} | Tony Content", description="标题模板"),
        F("seo_keywords", ENUM, "自动提取 + 自定义关键词",
          choices=("自动提取 + 自定义关键词", "仅自动提取", "仅自定义"), description="SEO 关键词策略"),
        F("video_template", STRING, "使用视频第 1 帧", description="封面模板"),
        F("review_mode", ENUM, "auto", choices=("auto", "manual", "mixed"), description="审核模式"),
        F("max_retry", INT, 3, minimum=0, maximum=100, description="最大重试次数"),
        F("retry_interval_min", INT, 10, minimum=1, maximum=10080, description="重试间隔（分钟）"),
        F("notify_on_fail", BOOL, True, description="失败时通知"),
        F("queue_priority", ENUM, "高（优先发布）", choices=("高（优先发布）", "中", "低"),
          description="队列优先级"),
        F("api_rate_limit", BOOL, True, description="启用接口限流"),
        F("queue_concurrency", INT, 3, minimum=1, maximum=64, description="队列并发数"),
    ),
)

STORAGE = Section(
    name="storage", title="存储设置",
    description="本地目录、对象存储、上传策略、保留与清理、备份",
    fields=(
        F("video_path", STRING, "", description="视频保存路径"),
        F("cover_path", STRING, "", description="封面保存路径"),
        F("library_path", STRING, "", description="素材保存路径"),
        F("temp_path", STRING, "", description="临时文件路径"),
        F("auto_create_dir", BOOL, True, description="自动创建目录"),
        F("disk_warn_gb", INT, 10, minimum=1, maximum=100000, description="磁盘空间预警（GB）"),
        F("object_storage", ENUM, "阿里云 OSS",
          choices=("阿里云 OSS", "腾讯云 COS", "AWS S3", "七牛云"), description="对象存储类型"),
        F("bucket", STRING, "tce-media", description="Bucket 名称"),
        F("region", STRING, "华东1（杭州）", description="地域"),
        F("access_key_id", STRING, "", description="Access Key ID（标识，非密钥）"),
        F("access_key_secret", STRING, "", secret=True, public_flag="secretSet",
          description="Access Key Secret（敏感：只回是否已设置）"),
        F("custom_domain", STRING, "https://img.example.com", description="自定义域名（CDN）"),
        F("storage_prefix", STRING, "tce/", description="存储路径前缀"),
        F("save_location", ENUM, "同时保存到本地和对象存储",
          choices=("同时保存到本地和对象存储", "仅本地", "仅对象存储"), description="默认存储位置"),
        F("upload_timing", ENUM, "处理完成后立即上传",
          choices=("处理完成后立即上传", "手动上传", "定时上传"), description="上传时机"),
        F("upload_concurrency", INT, 3, minimum=1, maximum=64, description="并发上传数"),
        F("upload_retry", INT, 3, minimum=0, maximum=100, description="上传失败重试次数"),
        F("upload_size_limit_mb", INT, 500, minimum=1, maximum=1000000, description="单文件大小限制（MB）"),
        # 旧默认值是逗号分隔的字符串，但界面保存时写成数组 —— 这里统一成数组（schema 是唯一事实来源）
        F("file_types", LIST, list(_MEDIA_FILE_TYPES), item_type=STRING, description="允许的文件类型"),
        F("resume_upload", BOOL, True, description="断点续传"),
        F("verify_md5", BOOL, True, description="校验文件完整性"),
        F("keep_video_days", INT, 180, minimum=1, maximum=36500, description="视频保留（天）"),
        F("keep_image_days", INT, 365, minimum=1, maximum=36500, description="图片保留（天）"),
        F("keep_temp_days", INT, 7, minimum=1, maximum=36500, description="临时文件保留（天）"),
        F("keep_policy", ENUM, "自动删除", choices=("自动删除", "仅提醒", "不处理"), description="保留策略"),
        F("clean_schedule", BOOL, True, description="启用自动清理"),
        F("clean_cycle", ENUM, "每天", choices=("每天", "每周", "每月"), description="清理周期"),
        F("clean_time", STRING, "02:00", description="清理执行时间"),
        F("clean_expired_temp", BOOL, True, description="清理过期临时文件"),
        F("clean_recycle", BOOL, True, description="清理回收站"),
        F("clean_unused", BOOL, True, description="清理未使用缓存"),
        F("clean_failed", BOOL, True, description="清理失败下载"),
        F("backup_enabled", BOOL, True, description="启用自动备份"),
        F("backup_items", LIST, ["配置文件", "发布记录", "重要素材"], item_type=STRING, description="备份内容"),
        F("backup_cycle", ENUM, "每周", choices=("每天", "每周", "每月"), description="备份周期"),
        F("backup_keep", INT, 4, minimum=1, maximum=1000, description="保留备份数量"),
    ),
)

NOTIFY = Section(
    name="notify", title="通知设置",
    description="桌面通知、声音、事件订阅、渠道与免打扰（smtp_password 敏感）",
    fields=(
        F("desktop_task_start", BOOL, True, description="任务开始桌面通知"),
        F("desktop_task_done", BOOL, True, description="任务完成桌面通知"),
        F("desktop_task_fail", BOOL, True, description="任务失败桌面通知"),
        F("desktop_ai_done", BOOL, True, description="AI 完成桌面通知"),
        F("desktop_publish_done", BOOL, True, description="发布完成桌面通知"),
        F("desktop_storage_low", BOOL, True, description="存储不足桌面通知"),
        F("desktop_system_error", BOOL, True, description="系统异常桌面通知"),
        F("sound_enabled", BOOL, True, description="启用声音提醒"),
        F("sound_task_start", BOOL, True, description="任务开始提示音"),
        F("sound_task_done", BOOL, True, description="任务完成提示音"),
        F("sound_ai_done", BOOL, True, description="AI 完成提示音"),
        F("sound_task_fail", BOOL, True, description="任务失败提示音"),
        F("sound_repeat", INT, 1, minimum=1, maximum=100, description="提示音重复次数"),
        F("sound_gap_sec", INT, 3, minimum=1, maximum=3600, description="提示音间隔（秒）"),
        F("channel_email", BOOL, True, description="邮件通知渠道"),
        F("channel_wecom", BOOL, False, description="企业微信渠道"),
        F("channel_feishu", BOOL, False, description="飞书渠道"),
        F("channel_dingtalk", BOOL, False, description="钉钉渠道"),
        F("channel_slack", BOOL, False, description="Slack 渠道"),
        F("channel_webhook", BOOL, False, description="自定义 Webhook 渠道"),
        F("smtp_server", STRING, "smtp.example.com", description="SMTP 服务器"),
        F("smtp_port", INT, 587, minimum=1, maximum=65535, description="SMTP 端口"),
        F("smtp_user", STRING, "", description="SMTP 用户名"),
        F("smtp_password", STRING, "", secret=True, public_flag="passwordSet",
          description="SMTP 授权码（敏感：只回是否已设置）"),
        F("smtp_to", STRING, "", description="收件人邮箱"),
        F("smtp_encryption", ENUM, "STARTTLS", choices=("STARTTLS", "SSL/TLS", "不加密"),
          description="SMTP 加密方式"),
        F("webhook_url", STRING, "", description="Webhook 地址"),
        F("webhook_method", ENUM, "POST", choices=("POST", "PUT", "GET"), description="Webhook 请求方式"),
        F("webhook_format", ENUM, "JSON（推荐）", choices=("JSON（推荐）", "表单", "纯文本"),
          description="Webhook 消息格式"),
        F("webhook_template", STRING,
          '{\n  "text": "【{{app_name}}】{{title}}：{{message}}",\n  "time": "{{time}}"\n}',
          description="Webhook 消息模板"),
        F("event_task_start", BOOL, False, description="订阅：任务开始"),
        F("event_task_done", BOOL, True, description="订阅：任务完成"),
        F("event_task_fail", BOOL, True, description="订阅：任务失败"),
        F("event_ai_done", BOOL, True, description="订阅：AI 完成"),
        F("event_publish_fail", BOOL, True, description="订阅：发布失败"),
        F("event_storage_low", BOOL, True, description="订阅：存储不足"),
        F("event_system_error", BOOL, True, description="订阅：系统异常"),
        # 注意：旧默认值「仅屏蔽紧急通知」不在界面的下拉候选里（界面是"非紧急/全部"）。
        # 界面不能改，所以这里不设 enum —— 设了会让默认值自己校验不过。
        F("min_level", ENUM, "INFO", choices=("INFO", "WARN", "ERROR"), description="最低通知级别"),
        F("repeat_interval_min", INT, 30, minimum=1, maximum=10080, description="重复通知间隔（分钟）"),
        F("max_retry", INT, 3, minimum=0, maximum=100, description="通知失败重试次数"),
        F("retry_interval_min", INT, 5, minimum=1, maximum=10080, description="通知重试间隔（分钟）"),
        F("quiet_enabled", BOOL, True, description="启用免打扰"),
        F("quiet_start", STRING, "23:00", description="免打扰开始时间"),
        F("quiet_end", STRING, "08:00", description="免打扰结束时间"),
        F("quiet_mode", STRING, "仅屏蔽紧急通知", description="免打扰范围"),
        F("preview_type", ENUM, "任务完成通知",
          choices=("任务完成通知", "任务失败通知", "AI 加工完成", "发布完成"), description="通知预览类型"),
        F("preview_message", STRING, "这是一条测试消息，用于验证通知渠道是否正常工作。",
          description="通知预览内容"),
    ),
)

ACCOUNT = Section(
    name="account", title="账号管理",
    description="工作区账号与 Worker 节点身份（本阶段无真实账号服务）",
    fields=(
        F("display_name", STRING, "Tony", description="显示名称"),
        F("email", STRING, "tony@example.com", description="邮箱"),
        F("plan", STRING, "专业版", description="套餐"),
        F("node_id", STRING, "worker_001", description="Worker 节点 ID"),
        F("node_name", STRING, "本地开发节点", description="Worker 节点名称"),
        F("two_factor", BOOL, True, description="双重验证（界面传 on/off 字符串）"),
        F("verify_method", ENUM, "Google Authenticator",
          choices=("Google Authenticator", "短信验证"), description="验证方式"),
    ),
)

UI_SECTIONS = (GENERAL, WORK_MODE, LOGGING, COLLECT, AI, PUBLISH, STORAGE, NOTIFY, ACCOUNT)


# =========================================================================
# 二、引擎分区（给并行开发的模块用；只有定义、校验、存取）
# =========================================================================

COLLECTOR = Section(
    name="collector", title="采集引擎",
    description="Creator Monitor / Collector 的运行参数（本模块只负责定义与校验）",
    ui_section=False,
    aliases=("monitor",),
    fields=(
        F("enabled", BOOL, False, description="是否启用自动监控（默认关：不擅自起后台任务）"),
        F("poll_interval_minutes", INT, 30, minimum=1, maximum=10080,
          description="轮询间隔（分钟，必须 > 0）"),
        F("concurrency", INT, 2, minimum=1, maximum=64, description="并发采集数（>= 1）"),
        F("retry_count", INT, 3, minimum=0, maximum=100, description="失败重试次数（>= 0）"),
        F("request_timeout_sec", INT, 30, minimum=1, maximum=3600, description="单次请求超时（秒）"),
        F("fetch_history_days", INT, 0, minimum=0, maximum=36500, description="回填历史天数（0 = 不限）"),
    ),
)

TRANSCRIPT = Section(
    name="transcript", title="转写 / ASR",
    description="字幕与语音转写参数（别名 asr；转写实现由 ASR 模块负责）",
    ui_section=False,
    aliases=("asr", "transcribe"),
    fields=(
        F("enabled", BOOL, True, description="是否启用转写"),
        F("provider", ENUM, "auto", choices=("auto", "subtitle", "whisper", "external", "disabled"),
          description="转写来源（auto = 有字幕用字幕，否则 ASR）"),
        F("language", STRING, "", optional=True, description="语言（空 = 自动识别）"),
        F("model", STRING, "", optional=True, description="模型名（空 = 用服务商默认）"),
        F("prefer_native_subtitle", BOOL, True, description="优先使用原生字幕"),
        F("max_transcript_chars", INT, 0, minimum=0, maximum=10000000,
          description="转写文本上限（0 = 不限）"),
        F("timeout_sec", INT, 120, minimum=1, maximum=86400, description="单条转写超时（秒）"),
        F("retry_count", INT, 2, minimum=0, maximum=100, description="失败重试次数（>= 0）"),
    ),
)

PIPELINE = Section(
    name="pipeline", title="流水线引擎",
    description="Pipeline 的行级编排参数（本模块只负责定义与校验）",
    ui_section=False,
    fields=(
        F("enabled", BOOL, True, description="是否启用流水线编排"),
        F("concurrency", INT, 2, minimum=1, maximum=64, description="同时处理的内容条数（>= 1）"),
        F("retry_count", INT, 2, minimum=0, maximum=100, description="单条失败重试次数（>= 0）"),
        F("stage_timeout_sec", INT, 300, minimum=1, maximum=86400, description="单阶段超时（秒）"),
        F("auto_transcribe", BOOL, True, description="入库后自动转写"),
        F("auto_enrich", BOOL, True, description="转写后自动 AI 标注"),
        F("batch_size", INT, 5, minimum=1, maximum=1000, description="批量处理条数"),
        F("stop_on_error", BOOL, False, description="出错是否停止整条流水线"),
    ),
)

PUBLISHING = Section(
    name="publishing", title="发布引擎",
    description="Publisher 的运行参数（真实发布尚未实现，默认 dry_run 保护）",
    ui_section=False,
    aliases=("publisher",),
    fields=(
        F("enabled", BOOL, False, description="是否启用真实发布（默认关）"),
        F("dry_run", BOOL, True, description="演练模式：只记录不真发（默认开）"),
        F("targets", LIST, list(_PUBLISH_PLATFORMS), item_type=STRING, description="目标平台"),
        F("concurrency", INT, 1, minimum=1, maximum=32, description="并发发布数（>= 1）"),
        F("retry_count", INT, 3, minimum=0, maximum=100, description="失败重试次数（>= 0）"),
        F("daily_limit", INT, 5, minimum=0, maximum=100000, description="每日发布上限"),
        F("interval_minutes", INT, 30, minimum=1, maximum=10080, description="发布间隔（分钟）"),
    ),
)

ENGINE_SECTIONS = (COLLECTOR, TRANSCRIPT, PIPELINE, PUBLISHING)

# 分区顺序 = 落盘 JSON 的键顺序 = describe() 的顺序。界面分区在前，保持旧文件观感。
SECTIONS_LIST = UI_SECTIONS + ENGINE_SECTIONS

register_sections(SECTIONS_LIST)

#: 规范分区名 → Section
SECTIONS = {section.name: section for section in SECTIONS_LIST}


def defaults(section=None) -> dict:
    """全部默认值（或某个分区的默认值）的深拷贝。

    永远不要直接改 DEFAULTS 里的值：每次都要拿一份新的。
    """
    if section is not None:
        return SECTIONS[section].defaults()
    return {name: spec.defaults() for name, spec in SECTIONS.items()}


#: 全部默认值：`{分区: {字段: 默认值}}`。兼容第一阶段的同名常量（桥接层在用）。
DEFAULTS = defaults()


def section_defaults(section) -> dict:
    """某个分区的默认值；未知分区返回 {}。支持别名。"""
    name = resolve_section_name(section)
    return SECTIONS[name].defaults() if name in SECTIONS else {}


__all__ = [
    "SECTIONS", "SECTIONS_LIST", "DEFAULTS", "UI_SECTIONS", "ENGINE_SECTIONS",
    "SCHEMA_VERSION", "defaults", "section_defaults",
]
