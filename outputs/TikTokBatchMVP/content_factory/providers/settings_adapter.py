"""Provider 层 ↔ 现有设置页（Settings Core）的最小适配层。

为什么需要它
------------
设置页的 `ai` 分区是**面向界面的表单**（provider / model / api_base / api_key …），
Provider 层是**面向调用方的结构**（ProviderConfig + CredentialStore + 连接测试）。
两者必须能互相翻译，而且不能各存一份真相。

这个文件就是那层翻译，只做三件事：
1. 读：把设置页 `ai` 分区翻成 ProviderConfig（密钥从凭据库取）；
2. 写：非密钥字段进设置、密钥进凭据库，并（默认）镜像回旧字段；
3. 视图：给界面一个「永远不含明文」的 ai 设置视图（含掩码）。

对 Settings Core 的接口要求（就这么少）
---------------------------------------
    settings.root                      # 可选：应用数据目录（缺省用 settings_store.app_data_root()）
    settings.section("ai") -> dict     # 读一个分区
    settings.update("ai", values) -> dict
    settings.save_section("ai", values) -> dict   # 可选：仅用于「清除密钥」时重置整段
这 4 个方法现有 `FactorySettings` 全都有，所以今天的集成零成本；
Settings Core 换实现时只要保持这几个签名即可（见 docs/provider-config.md）。

关于「镜像回旧字段」（重要）
----------------------------
现有 `EnrichmentService` 读的是 `settings.section("ai")["api_key"]`。
AI Enrichment 模块正在并行开发，本模块**不去改它的代码**，
所以默认把密钥同时镜像回 `ai.api_key`（这正是当前应用原本的存储位置，
不是新增的泄露面），保证现有闭环一行不改也能继续跑。
集成阶段把调用方换成 `adapter.reveal_api_key()` 之后，
用 `ProviderSettingsAdapter(settings, mirror_legacy=False)` 即可关掉镜像，
密钥就只剩凭据库一份。
"""

import logging

from .connection_test import ConnectionTester
from .credentials import CredentialStore, is_secret_name, redact
from .models import (DEFAULT_TIMEOUT, ProviderError, coerce_timeout, provider_from_legacy)
from .paths import default_data_root
from .registry import ProviderRegistry

LOGGER = logging.getLogger(__name__)

AI_SECTION = "ai"
AI_PROVIDER_ID = "ai"

# 这些字段属于 provider 级配置（注册表），不属于设置页表单
PROVIDER_FIELDS = ("timeout", "enabled", "label", "extra", "credential_ref")


class ProviderSettingsAdapter:
    """设置页 ↔ Provider 层。所有方法都不返回明文密钥（`reveal_api_key` 除外）。"""

    def __init__(self, settings, registry=None, credentials=None, mirror_legacy=True,
                 provider_id=AI_PROVIDER_ID):
        self.settings = settings
        self.provider_id = str(provider_id or AI_PROVIDER_ID)
        self.mirror_legacy = bool(mirror_legacy)
        root = getattr(settings, "root", None) or default_data_root()
        self.credentials = credentials if credentials is not None else CredentialStore(root)
        self.registry = registry if registry is not None else ProviderRegistry(
            root=root, credentials=self.credentials)
        # 兼容写法：注册表先建、凭据库后给的情况
        if getattr(self.registry, "credentials", None) is None:
            self.registry.credentials = self.credentials

    # ---- 读 ------------------------------------------------------------
    def section(self):
        """设置页 ai 分区的**原始**内容（含明文 api_key，仅供本层内部使用）。"""
        reader = getattr(self.settings, "section", None)
        if not callable(reader):
            return {}
        section = reader(AI_SECTION)
        return dict(section or {}) if isinstance(section, dict) else {}

    def provider(self):
        """当前的 AI provider（读操作，不写盘）。

        provider_id 固定为 "ai"，与「设置 → AI 加工设置」一一对应；
        其余 provider 由注册表独立管理。

        字段归属（避免两份真相互相覆盖）：
        - 设置页负责 provider / api_base / model（用户在表单里编辑的就是这些）；
        - 注册表负责 timeout / enabled / label / extra / credential_ref（provider 级配置）。
        """
        section = self.section()
        config = provider_from_legacy(section, provider_id=self.provider_id)
        stored = self.registry.get(self.provider_id)
        if stored is not None:
            config = config.copy_with(timeout=stored.timeout, enabled=stored.enabled,
                                      label=stored.label, extra=stored.extra,
                                      credential_ref=stored.credential_ref or config.credential_ref,
                                      created_at=stored.created_at)
        return config

    def ensure_provider(self):
        """把当前设置物化成注册表里的一条 provider 记录（幂等，写操作）。"""
        return self.registry.upsert(self.provider())

    def api_key_state(self):
        """密钥状态：是否已配置 / 掩码 / 来源。**不含明文**。"""
        config = self.provider()
        fields = config.credential_fields
        stored_ready = self.credentials.is_set(config.credential_ref, fields)
        legacy = str(self.section().get("api_key") or "").strip()
        if stored_ready:
            masks = [self.credentials.mask(config.credential_ref, name) for name in fields]
            return {"configured": True, "source": "credential",
                    "mask": " ".join(mask for mask in masks if mask)}
        if legacy:
            from .credentials import mask_secret
            return {"configured": True, "source": "settings", "mask": mask_secret(legacy)}
        return {"configured": False, "source": "", "mask": ""}

    def reveal_api_key(self):
        """取明文密钥：凭据库优先，其次旧设置字段（老装机无需迁移即可用）。

        **仅供服务层内部调用**（真实请求、连接测试）。禁止进 UI / 桥接返回值。
        """
        config = self.provider()
        for name in config.credential_fields:
            value = self.credentials.reveal(config.credential_ref, name)
            if value:
                return value
        return str(self.section().get("api_key") or "").strip()

    def credentials_view(self):
        config = self.provider()
        return self.credentials.public(config.credential_ref, config.credential_fields)

    # ---- 写 ------------------------------------------------------------
    def save(self, values, mirror=None):
        """保存 AI 设置。

        分流规则（这是本层最要紧的行为）：
        - 非密钥字段 → 设置 JSON，行为与今天完全一致；
        - 密钥字段 → 凭据库；空值 / 空白值 = **忽略**，绝不删除已存密钥；
        - provider 级字段（timeout / enabled / label / extra）→ 注册表；
        - 默认把密钥镜像回 `ai.api_key`，让现有 EnrichmentService 继续可用。

        先校验后落盘：配置不合法时**什么都不写**，不会留下半截状态。
        """
        values = dict(values or {})
        provider_values = {key: values.pop(key) for key in PROVIDER_FIELDS if key in values}
        secrets = {key: value for key, value in values.items() if is_secret_name(key)}
        plain = {key: value for key, value in values.items() if key not in secrets}

        candidate = provider_from_legacy({**self.section(), **plain}, provider_id=self.provider_id)
        errors = candidate.validate()
        if errors:
            return {"ok": False, "saved": False, "error": "；".join(entry["message"] for entry in errors),
                    "errors": errors, "code": errors[0]["code"], "field": errors[0]["field"],
                    "apiKey": self.api_key_state()}

        saved = self.settings.update(AI_SECTION, plain) if plain else {}
        result = {"ok": True, "stored": [], "skipped": [], "mirrored": False}
        config = self.provider()
        if secrets:
            written = self.credentials.put(config.credential_ref, secrets)
            result["stored"] = written["stored"]
            result["skipped"] = written["skipped"]
            use_mirror = self.mirror_legacy if mirror is None else bool(mirror)
            api_key = secrets.get("api_key")
            if use_mirror and api_key is not None and str(api_key).strip():
                self.settings.update(AI_SECTION, {"api_key": str(api_key).strip()})
                result["mirrored"] = True

        self.ensure_provider()
        if provider_values:
            config = self.registry.update(self.provider_id, **provider_values)
        result["provider"] = config.public(self.credentials)
        result["apiKey"] = self.api_key_state()
        result["saved"] = bool(saved)
        return result

    def clear_api_key(self, field="api_key"):
        """显式清除密钥（界面的「清除密钥」按钮走这里）。

        注意与「留空」的区别：留空 = 不改；这个方法是明确的删除动作。
        """
        config = self.provider()
        removed = self.credentials.drop(config.credential_ref, field)
        if field == "api_key":
            self._reset_legacy_api_key()
        return {"ok": True, "removed": removed, "apiKey": self.api_key_state()}

    def reset(self):
        """重置整条 AI provider：凭据清空 + 注册表记录删除 + 旧字段置空。"""
        config = self.provider()
        removed = self.credentials.drop(config.credential_ref)
        self._reset_legacy_api_key()
        if self.registry.get(self.provider_id) is not None:
            self.registry.delete(self.provider_id, drop_credentials=False)
        return {"ok": True, "removed": removed}

    def _reset_legacy_api_key(self):
        """把旧字段 `ai.api_key` 置空的唯一正确写法。

        `settings.update()` 对空字符串是「忽略」（这是防止误删的保护），
        所以清除必须走整段保存：读出现有分区 → 改一个键 → 整段写回。
        """
        section = self.section()
        if not str(section.get("api_key") or ""):
            return
        section["api_key"] = ""
        writer = getattr(self.settings, "save_section", None)
        if callable(writer):
            writer(AI_SECTION, section)
        else:                                            # pragma: no cover - 兜底
            LOGGER.warning("设置实现缺少 save_section()，无法清除旧字段 api_key")

    # ---- 界面视图 ------------------------------------------------------
    def public_view(self):
        """AI 设置页的对外视图：**永远不含明文**，兼容现有字段名。

        保留 `api_key: ""` / `apiKeySet: bool` 两个既有键（老界面不用改），
        新增 `apiKeyMask` / `credentialSource` / `providers` 供新界面使用。
        """
        config = self.provider()
        state = self.api_key_state()
        view = config.public(self.credentials)
        view.update({
            "api_key": "",
            "apiKeySet": state["configured"],
            "apiKeyMask": state["mask"],
            "credentialSource": state["source"],
            "section": self._public_section(),
        })
        return view

    def _public_section(self):
        """设置页原始视图（如果 Settings Core 提供了 public()，直接借它用）。"""
        reader = getattr(self.settings, "public", None)
        if not callable(reader):
            return {}
        try:
            view = reader() or {}
        except Exception:                                # pragma: no cover - 防御
            return {}
        section = dict(view.get(AI_SECTION) or {})
        section.pop("api_key", None)                     # 双保险：绝不回明文
        return section

    def providers_view(self):
        """全部 provider（含 AI 这条）的对外视图。"""
        self.ensure_provider()
        return self.registry.public()

    # ---- 连接测试 ------------------------------------------------------
    def tester(self, http=None):
        return ConnectionTester(registry=self.registry, credentials=self.credentials, http=http)

    def test_connection(self, values=None, http=None, **kwargs):
        """测试 AI provider。

        `values` 是界面上**还没保存**的表单值：只作用于本次测试，
        不写设置、不写凭据库（用户点「测试」不等于同意保存）。
        测的就是「表单现在显示的东西」—— 包括刚切换的服务商与 base_url。
        """
        staged = dict(values or {})
        api_key = str(staged.pop("api_key", "") or "").strip()
        if staged.get("timeout"):
            try:
                coerce_timeout(staged["timeout"], DEFAULT_TIMEOUT)
            except ProviderError as exc:
                return {"ok": False, "provider": self.provider_id, "provider_type": "",
                        "model": str(staged.get("model") or ""), "latency_ms": 0,
                        "error_type": exc.code, "message": redact(exc), "probe": "config_only"}
        section = dict(self.section())
        section.update({key: value for key, value in staged.items()
                        if key in ("provider", "api_base", "base_url", "model", "timeout")})
        if (staged.get("provider") or staged.get("provider_type")) and not (
                staged.get("api_base") or staged.get("base_url")):
            # 换了服务商却没填地址 → 用新类型的默认地址，
            # 否则会拿着 DeepSeek 的 base_url 去测 GLM（那是必然失败的假结论）
            section.pop("api_base", None)
        config = provider_from_legacy(section, provider_id=self.provider_id)
        explicit_type = str(staged.get("provider_type") or "").strip()
        if explicit_type:
            config = config.copy_with(provider_type=explicit_type)
        return self.tester(http=http).test(config, api_key=api_key, **kwargs)
