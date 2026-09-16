# Tony Content Engine —— 第一阶段 MVP（内容工厂）

> 起点：本分支 `feature/content-factory-ai-enrichment-mvp` 的第一个 commit `ef244e6`。
> 目标：在**不改动原下载器能力**的前提下，把 TikTok 下载器升级成内容工厂的第一阶段 ——
> 8 个主页面 + 7 个设置子页全部可切换，并跑通「本地 AI 标注闭环」。

---

## 一、这一版做了什么

**界面**（`ui/app.html` + `app.css` + `app.js`）

| 页面 | 数据来源 |
|---|---|
| 首页 | 真实统计（内容总数 / 已标注 / 已转写 / 已下载 / 创作者 / 失败） |
| 创作者监控 | 真实创作者 + 真实内容分类分布；监控规则面板为示意开关 |
| 自动采集 | 真实采集阶段计数 + 内容列表；采集规则面板为示意开关 |
| 内容流水线 | 真实内容 + 7 阶段进度点；可筛选、可批量分析 |
| AI 加工 | 真实标注结果（主题 / CEFR / 口音 / 语速 / 学习价值 / 关键词 / 表达 / 语法点 / 重点句 / 摘要 / 推荐任务） |
| 云端发布 | **只有界面**（按约定本阶段不接真实发布），数据取自本地内容库 |
| 异常处理 | 真实：本地 `scrape.log` + 内容库失败任务 |
| 设置（7 子页） | 真实本地持久化（表单可编辑，重启后仍在） |
| 视频库 | 内嵌原 `index.html`，下载 / 抓取 / 字幕能力完全保留 |

**闭环**：下载完成 → 自动入库 → 字幕文件转 transcript → 调大模型 → 固定 JSON 落 sqlite
→ AI 加工页展示 → 可「重新分析」→ 失败可重试 → 重启后数据仍在。

---

## 二、目录与职责

```
outputs/TikTokBatchMVP/
  ui/app.html app.css app.js      内容工厂外壳（v2 主界面）
  ui/index.html                   原下载器（一行未改，作为「视频库」内嵌）
  content_bridge.py               唯一同时接触下载器与服务的桥接层
  content_factory/
    factory_store.py   sqlite：creators / content_items / ai_enrichments
    settings_store.py  7 个设置分区的本地 JSON（deep-merge 默认值）
    ai_enrichment.py   固定 schema、JSON 容错解析、失败重试、prompt 模板
    transcript.py      字幕文件 → 纯文本；ASR 接口（无后端时明确报不可用）
    pipeline.py        编排：入库 → 转写 → 标注，进度事件回传
    mock_data.py       演示数据（source_type=demo，可一键清除）
    errors_feed.py     异常页数据源（本地日志 + 失败任务）
  web_app.py        Api（下载器）+ content_factory 属性 + 下载完成回写
scripts/capture_ui.py             开发期截图工具（对照参考图用）
docs/ui-references/               14 张 UI 参考图
docs/ui-screenshots/              真实窗口的实现截图
```

**分层原则**：`content_factory/` 不 import webview，也不知道界面的存在；
界面只认 `window.pywebview.api.content_factory.content_*`；
两边唯一的接缝是 `content_bridge.py`。

---

## 三、数据模型

```sql
creators(id, handle UNIQUE, display_name, avatar, category, status,
         followers, videos, priority, poll_interval, created_at, updated_at)

content_items(id, source_type, source_video_id, source_url, creator_id, creator_handle,
              title, description, local_video_path, local_audio_path, local_subtitle_path,
              thumbnail_path, transcript_text, duration,
              download_status, transcript_status, ai_status, last_error,
              created_at, updated_at)
              唯一性：同 (source_type, source_video_id) 只入库一次

ai_enrichments(id, content_item_id UNIQUE, topic, subtopic, cefr_level, accent, speech_speed,
               learning_value, keywords, expressions, grammar_points, key_sentences,
               summary_zh, recommended_task, raw_json, raw_response, attempts, model, analyzed_at)
```

**状态机**：`pending → running → done | failed`；AI 多一个 `queued`（批量入队后）。

**存储位置**：`%LOCALAPPDATA%/TikTokBatchMVP/content-factory.db`（sqlite，WAL）、
`content-factory-settings.json`。不接任何云端数据库。

---

## 四、桥接 API 契约（26 个方法）

```
概览  content_bootstrap() -> {ok, stats, items, creators, settings, errors, demoCount, stages[7]}
      content_stats() / content_topic_distribution() / content_worker_status()
内容  content_items(status, search, limit) / content_item(id)
      content_creators() / content_errors(limit)
设置  content_settings()                    # 密钥只回 apiKeySet，不回明文
      content_save_settings(section, values)  # 空字符串的密钥字段 = 保持原值
      content_reset_settings(section) / content_test_ai(values)
闭环  content_enrich(id) / content_reanalyze(id) / content_enrich_pending(ids, limit)
      content_set_transcript(id, text) / content_transcript(id)
入库  content_register_download({id,title,url,folder,media,subtitles[],state})
      content_ingest_videos(videos, handle) / content_import_folder(folder, handle)
      content_ingest_downloader() / content_seed_demo() / content_clear_demo()
其他  content_events(since) / content_choose_folder(kind) / content_open_folder(path)
```

**进度事件**（界面据此刷新）：`enrichProgress { id, state(pending|running|done|failed|idle),
message, stage(download|transcript|enrich), index, total, enrichment?, elapsed? }`

---

## 五、踩过的坑（都已写进对应文件注释，别踩回去）

### 1. pywebview 会**递归**展开 js_api 上的非下划线属性

`webview/util.py:180-211` 用 `dir()` 递归 + `getattr` 链（`:268`）发现方法。后果：

- 把 `self.store` / `self.settings` / `self.pipeline` 裸挂在桥接对象上，
  会多出上百个 `store.upsert_item` 这类伪接口 —— 内部实现被迫变成对外契约。
- 用 `_serializable = False` 藏：pywebview 是**整个属性跳过**，
  连 `content_factory` 本身都不暴露（实测：exposed 里一个 `content_*` 都没有）。
- 用双下划线槽位藏：包装对象对外一个公开属性都没有，pywebview 同样发现不了方法。

最终方案：`_Hidden` 白名单包装 —— 只放行 `content_*` 开头的名字，
其余一律 `AttributeError`；内部取真实对象走 `unwrap()`（`object.__getattribute__`）。
`test_content_bridge.py` 用与 pywebview 完全相同的遍历规则把这条契约锁住了。

### 2. `waitForApi` 探错了层级 → 页面永远停在「正在启动内容工厂…」

内容工厂方法挂在 `content_factory` 命名空间下，但外壳最初探的是顶层
`api.content_stats`，于是永远等不到，6 秒后跳「没有检测到应用桥接」。

**测试为什么没拦住**：当时的假桥接也是平铺的 `api.content_stats`，
和错误实现"自洽"，所以 23 个 UI 测试一路绿。
修法有两步：外壳改成探 `api.content_factory.content_stats`，
**并且把假桥接改成与真实结构完全一致**（见 `test_content_factory_ui.py` 顶部注释）。

### 3. `ImageGrab.grab()` 抓的是屏幕，不是窗口

窗口没被激活时截到的是压在下面的别的窗口（第一次截出来是 ChatGPT 页面）。
而 `SetForegroundWindow` 对非前台进程会被 Windows 静默忽略。
改用 `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)`：让窗口自己画到内存 DC，
不需要可见或被激活。另外别用固定 sleep 猜"渲染好了"，要轮询渲染完成标志。

### 4. AI 标注失败必须**写明原因**，不能静默

没配 API Key 时，`enrich_one` 直接把内容标成 `failed` 并把原因写进 `last_error`
（"未配置 API Key：请在「设置 → AI 加工设置」里填写后重试"），界面据此显示失败态 + 重试按钮。
如果这里静默跳过，用户会看到一条永远"待分析"的内容，无从下手。

### 5. 交接失败不能连累下载

`Api._notify_content_factory` 整体包在 try/except 里：
内容工厂出问题只写日志，下载主流程必须照常成功（`test_content_download_handoff.py::test_handoff_failure_does_not_break_the_download` 守这条）。

---

## 六、AI 标注的失败语义（给其他模块的契约）

`enrich_one(item_id)` **不抛异常**，任何失败都返回 `{"ok": False, "error": <人能看懂的原因>}`，
同时把内容标成 `ai_status="failed"` 并把同一句话写进 `content_items.last_error`。
界面只读 `last_error` 这一个字段，所以「原因要能照着做」是这个接口的硬要求。

已实现的失败分类（全部真机实测过）：

| 场景 | `last_error` 大致内容 |
|---|---|
| 没配 Key | 未配置 API Key：请在「设置 → AI 加工设置」里填写后重试 |
| Key 无效（HTTP 401/403） | API Key 无效或没有权限：…检查 API Key（附服务端原话） |
| 模型名/参数错（400） | 请求被拒绝：通常是模型名或参数不对…（附服务端原话） |
| 地址错（404） | 接口地址不对：应形如 `https://api.deepseek.com/v1`，不要带 `/chat/completions` |
| 额度不足/限流（402/429） | 额度不足或请求过于频繁… |
| 服务端故障（5xx） | 服务端错误（503）：通常是服务商临时故障，稍后重试即可 |
| 字幕太短 | 字幕文本过短（N 字符，至少要 40 字符）…请补全字幕或手工粘贴 |
| 无字幕且无 ASR | 该内容没有字幕轨…（ASR 相关文案由转写层给） |
| 模型返回非 JSON | 模型返回里找不到合法 JSON 对象 |

两条重要行为：

1. **HTTP 失败不重试**。只有"模型答了但不是合法 JSON"才重试一次（并明确要求只输出 JSON）。
   否则「Key 无效」这种必然失败的场景会让用户白等两轮请求。
2. **字幕短于 `MIN_TRANSCRIPT_CHARS`（40）直接失败且不发请求**。
   实测 17 字符的占位字幕会被"标注"出一份看着像模像样、实际毫无价值的结果
   （连占位文字都被当成地道表达），那种结果比明确失败更糟。

`ai_status` 取值：`pending → queued → running → done | failed`。

---

## 七、怎么跑 / 怎么测

```powershell
# 启动（v2 主界面 = 内容工厂外壳）
python outputs/TikTokBatchMVP/web_app.py

# 全量测试（301 个，全部离线；UI 测试需要本机有 Chrome 或 Edge）
python -m unittest discover -s tests -t tests

# 只看内容工厂相关
python -m unittest tests.test_content_factory tests.test_content_bridge `
                   tests.test_content_download_handoff tests.test_content_factory_ui

# 想先看效果又没有真实数据：首页点「生成演示数据」（标记为 demo，可一键清除）

# 对照参考图截屏（开发期工具，不是应用的一部分）
python scripts/capture_ui.py docs/ui-screenshots --demo
```

**跑通真实 AI 标注**：`设置 → AI 加工设置` 填 API Key（服务商 / 模型 / max_tokens /
temperature / 输出语言 / prompt 模板都在这一页；若下载器里已配过，直接点「导入已有配置」）
→ `测试连接` → 回 `AI 加工` 点 `重新分析`。没有 Key 时链路照样走完，只是停在"失败 + 原因"。

两个真机验证脚本（会真的联网，只用于人工排查，不进测试套件）：

```powershell
# 成功路径：真的调模型，打印 12 个字段、耗时、尝试次数，并从库里读回来校验落盘
python scripts/verify_ai_closure.py --limit 3 --reuse-learning

# 失败路径：故意制造 7 种失败（错误 Key / 错误模型 / 错误地址 / 空回复 /
# 非 JSON / 坏 JSON / 服务端错误），打印用户实际会看到的那句原因；收尾自动清理
python scripts/verify_ai_failures.py --reuse-learning
```

---

## 八、明确没做（按约定留到下一阶段）

云端数据库、对象存储上传、真实多平台发布、发布队列、定时 / 24-7 真实调度、
异常自动恢复、推荐系统、Today 学习页、多用户与登录体系、Tony Learning OS 前端。
这些在界面上都有位置（发布页、采集规则面板、账号管理），但只有界面与本地保存，没有真实逻辑。
