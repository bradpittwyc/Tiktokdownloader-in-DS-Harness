# Settings Core —— 统一设置基础设施

> 分支：`feature/content-factory-settings-core`
> 代码：`outputs/TikTokBatchMVP/content_factory/settings/`（旧路径 `content_factory/settings_store.py` 保留为转发层）
> 测试：`tests/test_settings_core.py`（74 个用例，全部离线）

这一层只解决一件事：**所有模块用同一套方式描述、校验、保存、读取、重置配置**。
它不实现采集、转写、AI、流水线、发布 —— 那些是各模块自己的事。

---

## 一、职责边界

| 事情 | 归属 |
|---|---|
| 字段定义 / 默认值 / 类型与范围 / 枚举 | **Settings Core**（`schema.py` + `defaults.py`） |
| 校验 / 归一化 / 拒绝非法值 | **Settings Core**（`validators.py`） |
| 落盘 / 读取 / 重置 / 版本与迁移 | **Settings Core**（`service.py` + `migration.py`） |
| `poll_interval_minutes` 到点之后干什么 | Collector 模块 |
| 转写走字幕还是 ASR、怎么调模型 | ASR 模块 |
| 队列怎么排队、失败怎么重试 | Pipeline 模块 |
| 明文密钥存哪、怎么加密、怎么轮换 | Provider / Secrets 模块（Settings Core **不建第二套**） |

---

## 二、分区

### 界面分区（`ui/app.js` 的 7 个设置子页，名字与默认值沿用第一阶段，不许改）

| 分区 | 说明 |
|---|---|
| `general` | 基础设置（语言 / 主题 / 启动行为） |
| `work_mode` | 工作模式（并发、任务间隔） |
| `logging` | 日志与监控 |
| `collect` | 采集设置（界面分区；引用了采集偏好，但引擎参数请看 `collector`） |
| `ai` | AI 加工设置（provider / model / api_base / 参数 / prompt；`api_key` 是敏感字段） |
| `publish` | 发布设置（界面分区；真实发布请看 `publishing`） |
| `storage` | 存储设置（本地目录 / 对象存储 / 上传 / 清理 / 备份） |
| `notify` | 通知设置（桌面 / 声音 / 事件 / 渠道 / 免打扰） |
| `account` | 账号与节点身份 |

⚠️ 界面「基础设置」这一页会把 `work_mode` 与 `logging` 的字段一起提交，
但保存按钮的 `data-section` 是 `general`。界面不能改，所以这些字段实际存在
`general` 里 —— Settings Core 的**宽松模式**（`update()`）会原样保留它们；
严格模式（`apply()`）会按「未知字段」拒绝，programmatic 调用方请直接用对应分区。

### 引擎分区（给并行开发的模块；只有定义、校验、存取）

| 分区 | 别名 | 用途 | 默认值取向 |
|---|---|---|---|
| `collector` | `monitor` | Creator Monitor / Collector | `enabled=False`、`poll_interval_minutes=30`、`concurrency=2`、`retry_count=3` |
| `transcript` | `asr` / `transcribe` | 字幕与语音转写 | `enabled=True`、`provider=auto`、`prefer_native_subtitle=True` |
| `pipeline` | — | 流水线编排 | `enabled=True`、`concurrency=2`、`auto_transcribe/auto_enrich=True` |
| `publishing` | `publisher` | Publisher | `enabled=False`、`dry_run=True`（本阶段不做真实发布） |

`collector.poll_interval_minutes`（分钟）就是采集轮询间隔；Settings Core 只保证
「必须 > 0、会存住、读得回来」，怎么用由 Collector 决定。

---

## 三、对外 API

```python
from content_factory.settings import FactorySettings, SettingsService, SECTIONS, DEFAULTS

settings = FactorySettings()                 # 默认 %LOCALAPPDATA%/TikTokBatchMVP
settings = FactorySettings(root)             # 指定目录（测试用）
settings = FactorySettings(path=some_json)   # 直接指定文件
```

### 读

| 方法 | 说明 |
|---|---|
| `load(force=False)` | 整份设置（含 `schema_version`），返回深拷贝 |
| `reload()` | 丢掉缓存重新读 |
| `section(name)` | 一个分区（未知分区返回 `{}`，支持别名） |
| `get(section, key, default=None)` | 单个值 |
| `get_secret(section, key)` | **服务端专用**：敏感字段明文 |
| `public()` | **给界面**：敏感字段只回 `apiKeySet` / `secretSet` / `passwordSet` |
| `describe(section=None)` | 机器可读 schema（类型 / 范围 / 枚举 / 默认值 / 是否敏感） |
| `audit()` / `issues()` | 体检：落盘值是否符合 schema；加载过程中的问题 |

### 写

| 方法 | 校验 | 失败时 | 用途 |
|---|---|---|---|
| `apply(section, values, strict=True, partial=True)` | 严格 | 返回 `ApplyResult(ok=False)`，磁盘不动 | 新代码首选 |
| `validate(section, values, strict=True, partial=True)` | 只校验 | 不写盘 | 表单预校验 |
| `reset(section=None)` | 严格 | 同上 | 恢复某分区 / 整份默认值 |
| `update(section, values)` | 宽松 | 非法字段丢弃；I/O 失败抛异常 | **旧接口**（桥接层在用） |
| `save_section(section, values)` | 宽松 | 同上 | **旧接口**（整段替换，恢复默认在用） |
| `save(data)` | 宽松 | 同上 | **旧接口**（整份写入） |
| `migrate(force=False)` | — | — | 手动触发迁移（正常由 `load()` 自动完成） |

`strict=True`：只要有一个字段不合法就整体拒绝，磁盘一个字节都不动。
`strict=False`：丢弃出错的字段，其余照常写入 —— 这是界面的兼容路径。
`partial=True`：局部更新；`partial=False`：整段替换。

`ApplyResult` 字段：`ok / section / action / applied / errors / rejected /
unknown / error / message / settings`，`as_dict()` 可直接回给界面。

### 异常

`SettingsError` → `UnknownSectionError`（未知分区）、`SettingsValidationError`（字段非法）、
`SettingsPersistenceError`（读写失败；抛出时磁盘上的旧配置保证完好）。

---

## 四、Persistence（落盘）

- 文件：`%LOCALAPPDATA%/TikTokBatchMVP/content-factory-settings.json`（UTF-8、缩进 2、`ensure_ascii=False`）
- 备份：同目录 `content-factory-settings.json.bak`（上一份好文件）
- 写入：临时文件 → `flush` → `fsync` → `os.replace`（Windows 上原子）→ 写备份
  → **要么全旧、要么全新，不会有半截文件**；写失败时原文件与内存缓存都不动
- 读取：主文件 → `.bak` → 默认值 三级回退；主文件坏了会在 `issues()` 里报出来
- 缓存：`load()` 有缓存，但每次都比对文件 `(mtime, size)`；**别的实例/别的 Agent 写过会自动重读**，
  不会拿着旧缓存把别人的改动覆盖掉
- 并发：同一文件在进程内共享一把 `RLock`（多实例安全）；跨进程是「最后写入者胜」，
  不做文件锁，也不做合并

版本号：文件顶层 `schema_version`（当前 **2**）。没有版本号的老文件按版本 1 处理。

---

## 五、校验规则

- 类型：`bool` / `int` / `float` / `number` / `string` / `enum` / `list` / `dict` / `any`
  （`boolean` / `integer` / `array` 等写法也认）
- 类型宽松还原：`"30"` → `30`、`3.0` → `3`、`"on"/"off"/"1"/"0"/"是"/"关"` → `True/False`、
  `"a,b"` / 换行分隔 → `["a","b"]`、数字 → 字符串
- 范围：`minimum` / `maximum` 含端点，**越界一律拒绝，不静默截断**。
  例：`collector.poll_interval_minutes ≥ 1`、`retry_count ≥ 0`、`concurrency ≥ 1`、
  `ai.temperature ∈ [0, 2]`、`quality_threshold ∈ [0, 1]`、`similarity_threshold ∈ [0, 100]`
- 枚举：取值必须在 `choices` 里（如 `theme`、`level`、`review_mode`、`publish_frequency`）
- 可选：`optional=True` 的字段接受 `None` / 空串（字符串统一成 `""`，其余类型统一成 `None`）
- 未知字段：严格模式报 `unknown_field`；宽松模式原样保留（旧文件与界面串页字段靠这个活着）
- 未知分区：一律拒绝（`unknown_section`），不会在文件里凭空长出新分区
- 已知但非法的**已落盘**值：加载时**原样保留**并记进 `audit()` / `issues()`。
  加载不是写入 —— 一个字段看不懂，没道理把用户整份配置重置掉

---

## 六、Versioning / Migration

```python
@dataclass(frozen=True)
class Migration:
    version: int                     # 把 N 版升到 N+1
    name: str
    description: str
    apply: Callable[[dict], dict]    # 返回新的整份数据
```

- 版本从文件里的 `schema_version` 读出，缺省视为 1
- `load()` 发现旧版本会自动按 `MIGRATIONS` 链逐级升级并**把结果写回磁盘**
- 版本比程序新（用户装过更新的版本又退回旧版）→ 不迁移、不覆写，只记
  `future_schema_version`，并且**拒绝一切写入**以免覆盖新版本数据
- 新增迁移：写一个 `apply(data)->data`，`register_migration(Migration(...))` 或直接加进
  `MIGRATIONS`，同时把 `SCHEMA_VERSION` +1
- 已有迁移：`1 → 2`：补上版本号；`storage.file_types` 从 `"mp4,mov"` 字符串统一成数组

---

## 七、多 Agent 接入清单

1. **只通过 `FactorySettings` / `SettingsService` 读写设置**，不要自己打开那个 JSON。
2. 需要新字段 → 在 `defaults.py` 对应分区加一行 `F(...)`（带类型、默认值、范围），
   同时把 `SCHEMA_VERSION` +1 并在 `migration.py` 补一条迁移（只补版本号也行）。
   **不要**把字段塞进别的分区，也不要在自己模块里存一份平行配置。
3. 新增敏感字段 → `secret=True`（+ `public_flag` 保持界面契约），
   `public()` 会自动只回「是否已设置」。**不要**把明文回给界面。
4. 读取用 `section()/get()`；写入用 `apply()` 并检查 `result.ok`；
   界面表单预校验用 `validate()`。
5. 设置项含义写进 `description`（`describe()` 会带给界面与其他 Agent）。

### 还需要 Integration Agent 补的桥接方法

界面目前只能保存 9 个界面分区（`content_bridge.py` 里写死了一份白名单），
所以引擎分区暂时只能由 Python 代码读写。建议后续补：

```
content_settings_schema()                        → self._settings.describe()
content_settings_validate(section, values)       → self._settings.validate(...).as_dict()
content_settings_apply(section, values)          → self._settings.apply(..., strict=True).as_dict()
content_settings_issues()                        → {"issues": ..., "audit": ...}
```

并改两处已有的：

```
content_save_settings(section, values)   # 分区白名单改成 resolve_section_name(section) is not None
content_reset_settings(section, all=False)  # all=true → self._settings.reset()；否则按分区 reset
```

改完之后，界面（或未来的设置页）就能配置引擎分区，`ApplyResult.rejected` 也能把
「哪个字段为什么没存进去」如实显示出来 —— 现在界面拿不到校验错误，只能显示"已保存"。

---

## 八、Secrets 边界与 Provider / Secrets 模块的整合

现状（**保持不动，向后兼容**）：`ai.api_key`、`storage.access_key_secret`、
`notify.smtp_password` 三个字段仍然存在设置文件里，`ai_enrichment` 直接读
`section("ai")["api_key"]`。第一阶段就是这么跑的，动它等于破坏现有 AI 标注。

Settings Core 只做两件事：**保管**（schema 标 `secret=True`）与**屏蔽**
（`public()` 永不回明文，只回 `apiKeySet` / `secretSet` / `passwordSet`）。
它**没有**加密、没有 vault、没有第二套 secrets 系统。

将来 Provider / Secrets 模块接手时，建议这样切（每一步都不破坏旧数据）：

1. Provider 模块定义 `provider config`（provider / base_url / model / timeout / 密钥引用）
   与自己的存储（DPAPI 加密文件或系统凭据库），并对 Settings Core 暴露
   `resolve(section, key) -> secret | None`。
2. Settings Core 侧只加一个**读取钩子**：`get_secret()` 先问 Provider，再回落到
   设置文件里的旧值；`public()` 逻辑完全不变（继续只回「是否已设置」）。
3. 新增写入走 Provider（`set_secret`），设置文件里对应字段逐步只留空值；
   等所有消费方都改完，再发一条 schema 迁移把明文清掉并把字段标为 `deprecated`。
4. 明文只在服务端内存里出现，任何回给界面的结构（`public()` / `describe()` /
   `ApplyResult`）都不许带密钥；日志里也不许打（本模块没打过）。

在 Provider 就位之前，**不要**把 `api_key` 从设置里删掉或改名 ——
`ai_enrichment.EnrichmentService.config()` 和界面上的「测试连接」都依赖它。

---

## 九、怎么测

```powershell
python -m unittest tests.test_settings_core                      # Settings Core（74 个）
python -m unittest tests.test_content_factory tests.test_content_bridge   # 既有设置调用方
python -m unittest discover -s tests -t tests                    # 全量
```
