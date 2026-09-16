# Provider 配置 与 凭据管理（Provider Configuration / Secrets Core）

> 分支 `feature/content-factory-provider-config`，基线 `78f75fe`。
> 这一层只解决一件事：**Content Factory 怎么统一地拿到「可用的外部服务配置」**，
> 以及**密钥怎么存、怎么读、怎么保证不漏**。
> 它不实现任何业务调用 —— 发 prompt、解析 enrichment、转写、上传、发布
> 分别属于 AI Enrichment / ASR / Pipeline / Publish 模块。

---

## 一、架构

```
content_factory/providers/
  models.py           provider 契约与校验（ProviderConfig / ProviderType 目录），不含密钥
  credentials.py      凭据库（DPAPI 加密、掩码、脱敏过滤器），唯一存密钥的地方
  registry.py         provider 实例的注册表 + 本地 JSON 持久化
  connection_test.py  统一连接测试（三档成本：config_only / models / chat_min）
  settings_adapter.py 与「设置 → AI 加工」表单的最小适配（不改 Settings Core）
  paths.py            与 Settings Core 共用的应用数据目录（唯一耦合点）
  __init__.py         公开接口
```

三条主线：

1. **配置与密钥物理分离**。配置进 `content-factory-providers.json`（结构上没有任何密钥字段），
   密钥进 `content-factory-credentials.json`（按当前 Windows 用户加密）。
   配置里只有 `credential_ref` 这个名字。于是「普通 GET 拿到明文」不是靠自觉，是**没有可拿的东西**。
2. **厂商差异是数据，不是代码**。DeepSeek / MiniMax / GLM / OpenAI / OpenAI 兼容
   共用同一条 OpenAI 兼容调用逻辑，差别只有 `default_base_url` / `default_model` /
   `chat_path` / 探针方式，全部写在 `PROVIDER_TYPES` 目录里。新增一家 = 加一条目录项。
3. **兼容优先**。现有 `EnrichmentService` 读的是 `settings.section("ai")["api_key"]`，
   本层**不去改它的代码**，而是把密钥镜像回那个旧字段（默认开启，可关）。
   现有 AI 标注闭环一行不改照常跑，迁移可以慢慢来。

数据流：

```
设置页表单 / 业务模块
        │  （ProviderConfig，永不含明文）
        ▼
ProviderRegistry ──credential_ref──► CredentialStore ──reveal()──► 真实请求
        │                                   │
        │  providers.json                   │  credentials.json（DPAPI）
        ▼                                   ▼
   %LOCALAPPDATA%/TikTokBatchMVP/…
        │
ProviderSettingsAdapter ──►「设置 → AI 加工」表单视图（只有 apiKeySet / apiKeyMask）
```

---

## 二、Secrets 存储策略

| 项目 | 做法 |
|---|---|
| 存放位置 | `%LOCALAPPDATA%/TikTokBatchMVP/content-factory-credentials.json`（与设置、sqlite 同目录，不接云） |
| 加密 | 每个值用 **Windows DPAPI**（`CryptProtectData`，ctypes 调系统 API，零第三方依赖）按当前用户加密 |
| 文件被拷走 | 换机器 / 换 Windows 用户都解不开 → 按「未配置」处理，如实写入诊断信息，不假装成功 |
| 非 Windows / DPAPI 不可用 | 退化为明文，并在文件里如实标注 `"v": "plain"`（不假装加密成功） |
| 文件权限 | `chmod 0600`（尽力而为） |
| 写入 | 原子写（临时文件 + `os.replace`） |
| 掩码 | `mask_secret()` → `****abcd`；长度 ≤ 6 的密钥一位都不露（否则掩码本身就是答案） |
| 留空 | `put()` 忽略空白值 = 「不改」，**绝不删除**已存密钥 |
| 删除 | 显式动作：`drop(ref)` 删整份、`drop(ref, field)` 删单字段；`adapter.clear_api_key()` 对应界面「清除密钥」 |
| 明文出口 | 只有 `CredentialStore.reveal()` / `ProviderSettingsAdapter.reveal_api_key()`，命名显式、便于 grep 审计 |
| 日志 | `SecretRedactionFilter` + `redact()`：已知明文直接替换，另有 `Bearer` / `sk-` / `key=` / query 串兜底 |
| 错误文案 | 连接测试的 `message` 全部过 `redact()`；服务端回显的密钥也会被抹掉 |

八条硬性安全要求对应的落点：

| 要求 | 落点 | 测试 |
|---|---|---|
| 1. UI 普通读取拿不到完整 secret | `public()` / `to_dict()` / `public_view()` 只回 `set` + `mask` | `test_public_views_never_contain_the_secret` |
| 2. logs 不打印 secret | `SecretRedactionFilter`、`redact()` | `test_logs_never_print_the_secret` |
| 3. error message 不含 secret | 结果统一过 `redact()`，异常分类只给类型化文案 | `test_error_messages_do_not_leak_the_secret` |
| 4. test 不写入真实用户 secret | `adapter.test_connection(values)` 只用于本次请求 | `test_staged_connection_test_does_not_persist_the_key` |
| 5. serialization 不暴露 secret | `ProviderConfig` 没有密钥字段，`extra` 里的密钥键被 `validate()` 拒绝 | `test_serialization_of_provider_has_no_secret_field`、`test_extra_rejects_secret_looking_keys` |
| 6. 保存新 secret 支持替换 | `put()` 覆盖同名字段 | `test_secret_update_replaces_previous` |
| 7. 留空不误删 | `put()` 把空白值记入 `skipped` | `test_blank_secret_update_does_not_delete_existing` |
| 8. reset / delete 行为明确 | `drop()` / `clear()` / `adapter.clear_api_key()` / `adapter.reset()` | `test_explicit_delete_and_reset`、`test_clear_whole_store` |

---

## 三、Provider contract

```python
@dataclass
class ProviderConfig:
    provider_id    str    # 实例 id，如 "deepseek-main" / "ai"（AI 设置页那条）
    provider_type  str    # 类型，如 "deepseek" / "minimax" / "glm" / "openai_compatible"
    label          str    # 界面显示名
    base_url       str    # 规整后（补 https、去尾斜杠）
    model          str
    timeout        float  # 1–600 秒，非法直接拒绝
    enabled        bool
    credential_ref str    # "provider:<id>"，指向凭据库，**不是密钥本身**
    extra          dict   # 非密钥附加项（bucket / region / access_key_id / organization…）
    created_at / updated_at
```

派生：`kind`（llm / asr / storage / publish）、`endpoint(path)`、`spec`、`credential_fields`。

方法：

- `validate()` → `[{field, code, message}]`；`validate_or_raise()` → `ProviderError(code, field)`
- `to_dict()` → 落盘 / 跨层传输形式，**结构上无密钥**
- `public(credentials)` → 界面视图（含 `credential.{field}.set/mask`）
- `copy_with(**changes)` / `from_dict(data)`

错误码：`invalid_provider_type` / `unknown_provider` / `duplicate_provider` /
`invalid_provider_id` / `invalid_base_url` / `invalid_model` / `invalid_timeout` /
`unknown_field` / `immutable_field` / `secret_in_extra`。

注册表：`ProviderRegistry`（`content-factory-providers.json`）

```python
registry.create("deepseek", provider_id=None, **fields)   # 未给 id 时生成 "deepseek-main"
registry.update(id, **fields)      # provider_type 不可改；失败的更新不落盘
registry.enable(id) / disable(id) / delete(id, drop_credentials=True)
registry.list(kind=None, enabled_only=False) / get(id) / require(id) / default(kind)
registry.public()                  # 界面视图（无明文）
registry.set_credentials(id, {"api_key": ...}) / clear_credentials(id, field=None)
registry.credentials_view(id) / has_credentials(id) / resolve_credentials(id)  # 后者仅供服务层
```

---

## 四、Credential contract

```python
store.put(ref, {"api_key": "sk-…"})   # 空值忽略；返回 {stored, skipped}
store.reveal(ref, "api_key")          # 明文，仅服务层内部；取到即登记脱敏表
store.public(ref, ("api_key",))       # {"api_key": {"set": True, "mask": "****abcd", "label": "API Key"}}
store.fields(ref) / exists(ref) / is_set(ref, fields) / mask(ref, field)
store.drop(ref, field=None) / clear() / diagnostics()   # diagnostics 不含任何密钥
```

约定：

- **ref 是名字，不是密钥**。默认 `provider:<provider_id>`；以后多实例共用一份凭据只需改 ref。
- 一个 ref 可以存多个字段（LLM 是 `api_key`；对象存储是 `access_key_secret`；
  发布渠道是 `token`），由 provider 类型的 `credential_fields` 声明。
- `is_set()` 要求「字段存在**且解得开**」：解不开按未配置处理，让问题在连接测试时暴露，
  而不是等真实请求在更晚的地方炸。
- `access_key_id` 这类标识符**不算密钥**，放 `extra`（界面可见），只有 Secret 进凭据库。

---

## 五、Connection Test contract

```python
ConnectionTester(registry=…, credentials=…, http=…).test(provider_or_id_or_dict, overrides=…)
```

返回固定结构：

```json
{"ok": true, "provider": "deepseek-main", "provider_type": "deepseek", "kind": "llm",
 "label": "DeepSeek", "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1",
 "probe": "models", "endpoint": "https://api.deepseek.com/v1/models",
 "latency_ms": 328, "error_type": null, "message": "连接成功（models）",
 "checked_at": "2026-09-15 10:00:00"}
```

`error_type` ∈ `invalid_provider / unknown_provider / invalid_config / missing_credential /
auth / not_found / timeout / network / rate_limit / server / http_error / bad_response / unsupported`。

成本三档（默认从便宜到贵，**不会默认产生昂贵调用**）：

| probe | 做法 | 用在 |
|---|---|---|
| `config_only` | 只校验配置与凭据，不联网 | ASR、对象存储、发布渠道（探测会有副作用） |
| `models` | `GET {base_url}/models`（免费元数据），顺带判断 `model` 是否在列表里 | DeepSeek、OpenAI、OpenAI 兼容 |
| `chat_min` | `max_tokens=1, temperature=0` 的一发一收 | MiniMax、GLM；以及 `models` 返回 404 时的兜底 |

其他约定：

- 没配密钥时**不发请求**，直接 `missing_credential`。
- 探测请求超时上限 15 秒（`min(config.timeout, 15)`），界面按钮不会挂死。
- 授权头永远是 `Authorization: Bearer …`，不进 URL（不进日志）。
- 适配层 `adapter.test_connection(values)` 支持用**未保存的表单值**测试，且不落盘。

---

## 六、已支持的 provider 类型

| type_id | kind | 默认 base_url | 默认 model | 探针 | 凭据字段 |
|---|---|---|---|---|---|
| `deepseek` | llm | `https://api.deepseek.com/v1` | `deepseek-chat` | models | api_key |
| `minimax` | llm | `https://api.minimax.chat/v1` | `MiniMax-Text-01` | chat_min（`/text/chatcompletion_v2`） | api_key |
| `glm` | llm | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` | chat_min | api_key |
| `openai` | llm | `https://api.openai.com/v1` | `gpt-4o-mini` | models | api_key |
| `openai_compatible` | llm | 自填 | 自填 | models | api_key |
| `asr` | asr | 自填 | `whisper-1` | config_only | api_key |
| `object_storage` | storage | 可选 | — | config_only | access_key_secret |
| `publish` | publish | 可选 | — | config_only | token |

四种对话厂商共用一条代码路径：MiniMax 只是 `chat_path` 不同，GLM 只是探针不同。
设置页里的中文名（`DeepSeek` / `智谱 GLM` / `海螺`…）由 `match_provider_type()` 映射到类型，
认不出来时落到 `openai_compatible`（自建服务本来就只能填兼容接口）。

---

## 七、测试

```powershell
python -m unittest discover -s tests -t tests -v
```

`tests/test_provider_config.py`（67 项，全部离线、无真实 API Key）覆盖：
建 / 改 / 启停 / 删除、密钥保存 / 更新 / 删除 / 重置、留空不误删、掩码、
普通读取与序列化不泄露、日志与异常不泄露（含服务端回显密钥）、
非法 provider / base_url / timeout、连接测试成功与失败两路（含 401、404 回退、
网络异常、5xx、config_only 不发请求）、注册表增删查、OpenAI 兼容配置、
重启后恢复、DPAPI 落盘非明文、损坏文件降级、以及与既有
`EnrichmentService` 闭环的兼容。

> 环境提示：本机 `F:\Temp` 上 sqlite 的 fsync 极慢（一次写约 1–2 秒），
> 若把 `TEMP` 指到 `F:` 跑全量测试会像「卡死」。把 `TEMP` 指到工作区所在盘即可
> （实测同一套 376 项测试：`TEMP=F:` 超过 10 分钟仍未跑完，`TEMP=E:` 113 秒）。

---

## 八、Integration 时需要改哪些连接点

**本分支没有改任何 UI / 桥接文件**（`ui/app.*`、`web_app.py`、`content_bridge.py` 一行未动）。

### 8.1 `content_bridge.py`（Integration 阶段）

```python
from content_factory.providers import (ProviderRegistry, ProviderSettingsAdapter,
                                       test_connection)

# __init__ 里加两条（沿用现有 _Hidden 白名单约定）
self.bridge_providers = _Hidden(ProviderRegistry())
self.bridge_provider_settings = _Hidden(ProviderSettingsAdapter(
    unwrap(self.bridge_settings), registry=unwrap(self.bridge_providers)))

@property
def _providers(self):
    return unwrap(self.bridge_providers)
```

建议新增的 `content_*` 方法：

| 方法 | 作用 |
|---|---|
| `content_providers(kind="")` | `{"types": registry.types(), "providers": registry.public()}`，界面用它渲染 provider 列表（只有掩码） |
| `content_create_provider(provider_type, values)` | 新建（`ProviderError.code/field` 直接回给表单） |
| `content_save_provider(provider_id, values)` | 更新非密钥字段 |
| `content_delete_provider(provider_id)` | 删除（连带凭据） |
| `content_set_provider_secret(provider_id, values)` | 写凭据；空值 = 不改 |
| `content_clear_provider_secret(provider_id, field="")` | **显式**清除密钥 |
| `content_test_provider(provider_id=None, values=None)` | 统一连接测试（`values` = 未保存的表单值） |
| `content_ai_provider()` | AI 设置页专用视图（`apiKeyMask` / `credentialSource`） |

**必须改的一处既有行为**：今天的 `content_test_ai(values)` 会把界面上的 api_key 先
`settings.update(...)` 落盘再测试（`content_bridge.py:197-203`）。
应改成 `self._provider_settings.test_connection(values)` —— 测试 ≠ 保存。
（本分支不能改这个文件，故留到 Integration。）

`content_bootstrap()` 建议加一行 `self._provider_settings.ensure_provider()`，
让「设置 → AI 加工」里的 provider 在注册表中物化，业务模块就能统一从注册表取配置。

### 8.2 UI（`ui/app.js` / `app.html`）

- API Key 输入框：`value` 永远为空，用 `placeholder = apiKeyMask || "未配置"`；
  旁边加「清除密钥」按钮 → `content_clear_provider_secret("ai")`；
  文案提示「留空表示不修改」。
- 「测试模型」按钮：调 `content_test_provider("ai", 表单值)`，按 `error_type` 给不同提示
  （auth = 换 key；network = 检查网络；not_found = 检查 base_url 是否少了 `/v1`）。
- 新增/编辑 provider 的表单字段直接来自 `content_providers()` 的 `types`，
  不同 `kind` 渲染不同字段（`credential_fields` 决定要几个密钥输入框）。
- 不需要为每家公司写单独的前端分支。

### 8.3 业务模块取配置的方式（AI Enrichment / ASR / Pipeline / Publish）

```python
from content_factory.providers import ProviderRegistry

registry = ProviderRegistry()
config = registry.default("llm")          # 或 registry.get("ai")
if config and config.enabled and registry.has_credentials(config.provider_id):
    api_key  = registry.credentials.reveal(config.credential_ref)   # 只在真正发请求时取
    endpoint = config.endpoint("/chat/completions")
    model    = config.model
    timeout  = config.timeout
```

`EnrichmentService` 现有的 `config()` 语义（provider / model / api_base / api_key /
max_tokens / temperature / prompt_template）完全可以用上面这段替换，
但**是否替换由 AI Enrichment 决定**，本层不越界；在替换之前，
适配层的镜像保证了现状可用。

---

## 九、与 Settings Core 的预期集成方式

**分工**：Settings Core 负责「通用设置的分区、持久化、默认值、校验框架」；
本层负责「provider 的结构化配置 + 密钥」。两边不重叠。

本层对 Settings Core 的接口要求（只有这么点，`settings_adapter.py` 已按最小面写）：

```python
settings.root                              # 可选：应用数据目录
settings.section("ai") -> dict             # 读分区
settings.update("ai", values) -> dict      # 局部更新（密钥留空 = 不动，这是既有保护）
settings.save_section("ai", values)        # 可选：清除密钥时整段写回
settings.public() -> dict                  # 可选：public_view() 会顺带借用
```

`FactorySettings` 这 5 个都有，所以今天集成零成本；Settings Core 换实现时保持这些签名即可
（`test_adapter_survives_settings_without_root_attribute` 用一个最小实现锁住了这个契约，
连 `root` 属性缺失也能工作）。

需要 Settings Core 配合的三件事：

1. **不要在设置里再加一套密钥逻辑**。`api_key` 的写入应转交
   `ProviderSettingsAdapter.save()`，读取用 `api_key_state()` / `reveal_api_key()`。
2. **`content_settings()` / `public()` 返回的 `ai` 分区**建议合并
   `adapter.public_view()` 的 `apiKeyMask` / `credentialSource`（`apiKeySet` 语义不变），
   这样界面能同时显示「已配置」和「是哪个 key」。
3. **迁移方向**：设置 JSON 里的 `ai.api_key` 是历史遗留的明文字段。
   老装机不迁移也能用（`reveal_api_key()` 会回退读它）；
   想彻底清掉时调 `adapter.save(..., mirror=False).clear_api_key()` 一次即可，
   之后 `ProviderSettingsAdapter(settings, mirror_legacy=False)` 关掉镜像。
   建议在设置页加一个「把密钥迁移到安全存储」的一次性按钮。

另：`app_data_root()` 仍是目录定义的唯一权威（`providers/paths.py` 复用它并带兜底），
所以 provider 的 JSON 与设置 JSON、sqlite 一定同目录，「重启后配置仍在」不需要额外约定。
