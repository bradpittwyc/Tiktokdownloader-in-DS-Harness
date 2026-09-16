# ASR / Transcript Core（`content_factory/asr/`）

> 分支：`feature/content-factory-asr-core`　基线：`78f75fe`
> 责任范围：把**本地视频 / 音频**变成 transcript，并写回内容库；不改 UI、不改桥接层、
> 不改 AI 标注实现、不改公共数据模型。

---

## 一、这条链路做什么

```
本地视频 / 音频
  → 检查已有字幕（content_items.local_subtitle_path，以及媒体旁边同名字幕）
  → 有字幕就直接读（零成本、最准）
  → 没有字幕才进 ASR（本机 faster-whisper / openai-whisper，其次外部 provider）
  → 得到 transcript
  → 写回已有 ContentItem / Store
  → 更新 transcript_status
  → 失败给出结构化错误（错误码 + 人话 + 是否可重试），并且可以重试
```

状态机沿用内容库已有的 `content_items.transcript_status`，**没有新增状态**：

```
pending → running → done | failed
```

**没有新增表、没有加列。** 文本与状态只写 `content_items` 里已有的列：
`transcript_text` / `transcript_status` / `last_error` / `local_subtitle_path`。

---

## 二、四种来源（结果里明确区分）

| `result.source` | 含义 | 实现 |
|---|---|---|
| `subtitle` | 已有字幕文件直接读取 | `SubtitleProvider`（复用 `transcript.py`） |
| `local_asr` | 本机 faster-whisper / openai-whisper | `LocalAsrProvider`（复用 `transcribe_media`） |
| `external_asr` | 外部 ASR 服务（OpenAI 兼容 `/audio/transcriptions`） | `ExternalAsrProvider`（**接口 + 真实 HTTP 调用**；今天没有可用 API，默认不可用） |
| `cached` / `manual` | 库里已有文本 / 人工粘贴 | `TranscriptService` / `pipeline.set_transcript` |

外部 provider **不会伪造任何转写结果**：没配置时 `available()` 为 False，
`fetch()` 抛 `provider_unconfigured`。测试里用注入的替身验证「外部来源接进链里能跑通」，
但那明确是测试替身，不是真实 API 结果。

---

## 三、对外接口

```python
from content_factory.asr import TranscriptService, TranscriptError, TranscriptErrorCode

service = TranscriptService(store, settings, emit=回调可省略)

result = service.transcribe(item_id)            # 主入口：字幕优先 → 本机 ASR → 外部
result = service.transcribe(item_id, allow_asr=False)   # 只读字幕，不跑 ASR
result = service.transcribe(item_id, force=True)        # 忽略已有文本，重跑
result = service.retry(item_id)                 # 失败后重试（done 的默认不重跑）
summary = service.retry_failed(limit=20)        # 批量重试失败项（同步，不是队列）
state  = service.status(item_id)                # 状态 + 文本 + 来源 + 结构化错误
rows   = service.providers_status()             # 每个来源当前能不能用
```

`TranscriptResult`（成功与失败同一个结构）：

```python
{ok, itemId, text, chars, source, status, provider, asset,
 attempts, cached, retried, elapsed, error, notes}
```

失败时 `error` 是：

```python
{code, message, provider, retryable, hint, detail, kind}
```

`message` 是给人看的那句话，直接进 `content_items.last_error`（界面当纯文本渲染）；
`code` 给程序判断该给用户哪条出路；`retryable` 决定要不要显示「重试」。

### pipeline 里的入口（保持旧签名不变）

```python
pipeline.ensure_transcript(item_id)       # -> (ok, text, reason)   旧接口，未变
pipeline.transcribe_item(item_id)         # -> TranscriptResult     结构化结果
pipeline.retry_transcript(item_id)        # -> TranscriptResult
pipeline.transcribe_status(item_id)       # -> dict（状态 + 错误码）
pipeline.stats()["asrProviders"]          # 每个来源是否可用
```

---

## 四、错误码

| code | 什么时候出现 | 可重试 |
|---|---|---|
| `cancelled` | 被取消 | ✅ |
| `asr_disabled` | 调用方明确关掉了 ASR（`allow_asr=False`）且没有字幕 | ❌ |
| `subtitle_unreadable` | 有字幕文件但读不出文本（空文件 / 格式不支持） | ✅ |
| `backend_unavailable` | 本机没装识别后端，且没有字幕 | ✅（装完后端） |
| `media_missing` | 没有可用的本地媒体文件（没下载 / 文件被移走） | ✅（下载完成） |
| `provider_unconfigured` | 外部 ASR 没配置（本阶段没有可用 API） | ✅（配好 Key） |
| `no_input` | 没有任何转写来源 | ❌ |
| `asr_failed` | 识别过程本身失败 | ✅ |
| `empty_result` | 识别回来是空的 | ✅ |
| `item_missing` | 内容不存在 | ❌ |
| `unknown` | 兜底（含 provider 自身异常） | ✅ |

多条错误同时出现时按 `ERROR_PRIORITY` 挑最能说明问题的一条报出来
（例如「没装后端」+「没有媒体文件」报前者，与旧版本文案一致）。

---

## 五、运行记录（来源信息往哪放）

主链路只写 `content_items`。来源（字幕 / ASR / 外部）与错误码写在附属的小 JSON 里：

```
%LOCALAPPDATA%/TikTokBatchMVP/transcripts/<item_id>.json
```

好处是**不动公共表结构**（三条并行开发线共用 `content_items`，加列会直接撞车），
同时重启之后仍然知道一条文本是「字幕读出来的」还是「ASR 转出来的」、上次失败的错误码是什么。
这些文件是纯附加信息：丢了 / 坏了只丢来源信息，`transcript_text` 与 `transcript_status` 照样读得回来。

---

## 六、配置

### 设置页「AI 加工 → ASR / 转写」（已有的键，本分支把它接上了）

`ai.asr_provider` 的下拉本来就只有三个选项（`ui/app.js:1119`），但一直没有代码读它。
ASR Core 现在按它裁剪 provider 链：

| 设置值 | 实际链路 | 没有字幕时 |
|---|---|---|
| `OpenAI Whisper（本地）`（默认） | 字幕 → 本机 ASR → 外部服务 | 本机后端不可用时报 `backend_unavailable` |
| `外部服务` | 字幕 → 外部服务（跳过本机后端） | 报 `provider_unconfigured` |
| `不启用` | 只有字幕 | 报 `asr_disabled`（不可重试，并说明在哪儿改） |

字幕永远允许：关掉 ASR 不等于放弃一条本来就有字幕的内容。
`pipeline.stats()` 里的 `asrMode` / `asrProviders[].enabled` 就是当前生效的模式。

### 可选配置（**没有**改动 settings_store 的公共默认值）

`ai` 分区里存在下面这些键时会被读取（不存在就用默认值，不报错）：

| 键 | 作用 |
|---|---|
| `asr_model` | 本机识别模型大小（faster-whisper 的 `model_size`） |
| `asr_language` | 识别语言 |
| `asr_api_base` / `asr_api_key` / `asr_external_model` | 外部 ASR 服务地址 / Key / 模型名 |

设置页要暴露这几个键属于公共 contract 的变更，留给集成阶段统一处理（见「建议」）。

---

## 七、怎么测

```powershell
# 只看 ASR
python -m unittest tests.test_content_asr

# 相关回归（转写被 pipeline 调用）
python -m unittest tests.test_content_factory tests.test_content_bridge `
                   tests.test_content_download_handoff

# 全量
python -m unittest discover -s tests -t tests
```

`tests/test_content_asr.py` 全部离线：ASR 后端与外部 HTTP 都是测试替身，
本机没装 faster-whisper 也能跑。覆盖已有字幕 / 无字幕 / ASR 成功 / ASR 失败 /
retry / 重启后持久化 / 外部 provider 未配置 / 公共契约形状。

---

## 八、这一版明确没做

- **没有可用的外部 ASR API**：接口与真实 HTTP 调用都在，但没有 Key 就没有真实结果；
- 没有语音活动检测调参、没有时间轴 / 分段（SRT）输出 —— 当前只需要纯文本；
- 没有后台转写队列（本模块不负责通用任务调度）：`retry_failed` 是同步执行；
- 没有在界面上暴露「转写重试」按钮：需要在 `content_bridge.py` 加一个
  `content_transcript_retry`（该文件本分支不允许改）；
- 没有把转写阶段的状态单独接进 `errors_feed`（异常页目前按 `last_error` 关键词分类，
  结构化错误码已经写进运行记录，接的时候直接用 `code` 即可）。
