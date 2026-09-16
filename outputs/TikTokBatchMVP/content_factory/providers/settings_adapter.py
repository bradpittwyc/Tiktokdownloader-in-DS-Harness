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

# 「已迁移」标记写在 provider 记录的 extra 里（非密钥），
# 这样重启之后也不会又冒出一个把明文写回设置的镜像。
LEGACY_MIRROR_FLAG = "legacy_mirror"


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

    # ---- 迁移状态 ------------------------------------------------------
    def mirror_enabled(self):
        """现在还应不应该把密钥镜像回旧设置字段？

        只有「还没迁移过」的装机才需要镜像（那是为了让现有 EnrichmentService 继续可用）。
        迁移一旦完成，标记会落在 provider 记录的 extra 里，重启后依然生效 ——
        否则用户重启一次，明文就又会被写回设置 JSON。
        """
        if not self.mirror_legacy:
            return False
        stored = self.registry.get(self.provider_id)
        if stored is not None and stored.extra.get(LEGACY_MIRROR_FLAG) is False:
            return False
        return True

    def mark_migrated(self, migrated=True):
        """持久化「已迁移 / 未迁移」标记（写在 provider 记录的 extra 里）。"""
        self.ensure_provider()
        config = self.registry.get(self.provider_id)
        extra = dict(config.extra or {})
        extra[LEGACY_MIRROR_FLAG] = not bool(migrated)
        return self.registry.update(self.provider_id, extra=extra)

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
        - 只有「还没迁移过」的装机才把密钥镜像回 `ai.api_key`（迁移后自动关闭）。

        顺序是刻意的：先校验配置 → 再确认凭据后端可写 → 再写凭据 → 最后才写设置。
        任何一步失败都**什么都不落盘**（fail closed）：
        绝不允许「密钥没存进去，却明文写进了设置 JSON」这种静默降级。
        """
        values = dict(values or {})
        provider_values = {key: values.pop(key) for key in PROVIDER_FIELDS if key in values}
        if "extra" in provider_values:
            # extra 是累加的（里面有 legacy_mirror 这类标记），不做整段替换
            merged = dict(self.provider().extra or {})
            merged.update(dict(provider_values["extra"] or {}))
            provider_values["extra"] = merged
        secrets = {key: value for key, value in values.items() if is_secret_name(key)}
        plain = {key: value for key, value in values.items() if key not in secrets}

        candidate = provider_from_legacy({**self.section(), **plain}, provider_id=self.provider_id)
        errors = candidate.validate()
        if errors:
            return {"ok": False, "saved": False, "error": "；".join(entry["message"] for entry in errors),
                    "errors": errors, "code": errors[0]["code"], "field": errors[0]["field"],
                    "apiKey": self.api_key_state()}

        new_secrets = {key: value for key, value in secrets.items() if str(value or "").strip()}
        if new_secrets and not self.credentials.writable():
            reason = self.credentials.unavailable_reason()
            LOGGER.warning("拒绝保存密钥：%s", reason)
            return {"ok": False, "saved": False, "stored": [], "skipped": list(secrets),
                    "code": "credential_backend_unavailable", "field": "api_key",
                    "error": reason, "existingKept": True, "apiKey": self.api_key_state()}

        result = {"ok": True, "stored": [], "skipped": [], "mirrored": False}
        config = self.provider()
        if secrets:
            written = self.credentials.put(config.credential_ref, secrets)
            result["stored"] = written["stored"]
            result["skipped"] = written["skipped"]
            if not written["ok"] and new_secrets:
                # 凭据没写成功 → 后面的设置与镜像一律不做（这就是 fail closed）
                result.update({"ok": False, "saved": False, "code": written["code"],
                               "error": written["error"], "existingKept": True,
                               "apiKey": self.api_key_state()})
                return result
            use_mirror = self.mirror_enabled() if mirror is None else bool(mirror)
            api_key = str(secrets.get("api_key") or "").strip()
            if use_mirror and api_key:
                # 兼容镜像：并入同一次设置写入（一次落盘，不留中间状态）
                plain = {**plain, "api_key": api_key}
                result["mirrored"] = True

        saved = self.settings.update(AI_SECTION, plain) if plain else {}
        self.ensure_provider()
        if provider_values:
            config = self.registry.update(self.provider_id, **provider_values)
        result["provider"] = config.public(self.credentials)
        result["apiKey"] = self.api_key_state()
        result["saved"] = bool(saved)
        return result

    # ---- 迁移：旧设置里的明文密钥 → 凭据库 ------------------------------
    def migrate_legacy_secret(self):
        """把设置 JSON 里的明文 `ai.api_key` 迁进凭据库，成功后清掉旧字段。

        步骤（每一步失败都停在那一步，**绝不在凭据没存好的情况下删旧密钥**）：

            旧 settings.api_key
                  ↓ 1. 写入凭据库（后端不可用 → 直接失败返回，旧值原样保留）
            CredentialStore
                  ↓ 2. 回读校验（读不回来说明没存好 → 回滚本次写入并返回失败）
                  ↓ 3. 清除旧字段 + 持久化「已迁移」标记（此后不再镜像明文）
            完成

        幂等：重复运行只会看到「已经迁移过」，不会重复创建、不会覆盖已有密钥。
        """
        section = self.section()
        legacy = str(section.get("api_key") or "").strip()
        config = self.provider()
        ref, fields = config.credential_ref, list(config.credential_fields)
        stored_ready = self.credentials.is_set(ref, fields)

        if not legacy:
            if stored_ready:
                # 典型情况：已经迁移过（或本来就用凭据库）
                self.mark_migrated(True)
                return {"ok": True, "migrated": False, "reason": "no-legacy-secret",
                        "credentialConfigured": True, "mirrorLegacy": False,
                        "apiKey": self.api_key_state()}
            return {"ok": True, "migrated": False, "reason": "no-secret-to-migrate",
                    "credentialConfigured": False, "mirrorLegacy": self.mirror_enabled(),
                    "apiKey": self.api_key_state()}

        if stored_ready:
            # 凭据库里已经有值：以它为准，不覆盖（幂等 & 不损坏新密钥），
            # 只负责把旧字段清掉并关闭镜像。
            cleared = self._reset_legacy_api_key()
            self.mark_migrated(True)
            return {"ok": True, "migrated": False, "reason": "credential-already-present",
                    "clearedLegacy": cleared, "credentialConfigured": True, "mirrorLegacy": False,
                    "apiKey": self.api_key_state()}

        if not self.credentials.writable():
            reason = self.credentials.unavailable_reason()
            LOGGER.warning("拒绝迁移旧密钥：%s", reason)
            return {"ok": False, "migrated": False, "code": "credential_backend_unavailable",
                    "error": reason, "legacyKept": True, "existingKept": True,
                    "apiKey": self.api_key_state()}

        written = self.credentials.put(ref, {"api_key": legacy})
        if not written["ok"]:
            return {"ok": False, "migrated": False, "code": written["code"],
                    "error": written["error"], "legacyKept": True, "existingKept": True,
                    "apiKey": self.api_key_state()}

        # 回读校验：存进去了、也读得回来，才算迁移成功
        if self.credentials.reveal(ref, "api_key") != legacy:
            rolled_back = self.credentials.drop(ref, "api_key")
            return {"ok": False, "migrated": False, "code": "credential_verify_failed",
                    "error": "凭据写入后回读校验失败，已回滚本次写入；旧设置中的密钥保持原样",
                    "rolledBack": bool(rolled_back), "legacyKept": True, "existingKept": True,
                    "apiKey": self.api_key_state()}

        cleared = self._reset_legacy_api_key()
        self.mark_migrated(True)
        state = self.api_key_state()
        result = {"ok": True, "migrated": True, "reason": "migrated", "clearedLegacy": cleared,
                  "credentialConfigured": state["configured"], "mirrorLegacy": False,
                  "mask": state["mask"], "apiKey": state}
        if not cleared:
            # 凭据已经安全落库，只是旧字段没清掉 —— 如实报告，不假装干净
            result["warning"] = "旧字段 api_key 未能清除，请检查设置实现是否支持 save_section()"
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
        """把旧字段 `ai.api_key` 置空的唯一正确写法。返回是否确实清干净了。

        `settings.update()` 对空字符串是「忽略」（这是防止误删的保护），
        所以清除必须走整段保存：读出现有分区 → 改一个键 → 整段写回。
        """
        if not str(self.section().get("api_key") or ""):
            return True                                     # 本来就是空的
        section = self.section()
        section["api_key"] = ""
        writer = getattr(self.settings, "save_section", None)
        if not callable(writer):                            # pragma: no cover - 兜底
            LOGGER.warning("设置实现缺少 save_section()，无法清除旧字段 api_key")
            return False
        try:
            writer(AI_SECTION, section)
        except Exception as exc:                            # pragma: no cover - 防御
            LOGGER.warning("清除旧字段 api_key 失败：%s", type(exc).__name__)
            return False
        return not str(self.section().get("api_key") or "")

    # ---- 界面视图 ------------------------------------------------------
    def public_view(self):
        """AI 设置页的对外视图：**永远不含明文**，兼容现有字段名。

        保留 `api_key: ""` / `apiKeySet: bool` 两个既有键（老界面不用改），
        新增 `apiKeyMask` / `credentialSource` / `credentialBackend` /
        `legacyMirror` / `providers` 供新界面使用。
        """
        config = self.provider()
        state = self.api_key_state()
        status = self.credentials.status()
        view = config.public(self.credentials)
        view.update({
            "api_key": "",
            "apiKeySet": state["configured"],
            "apiKeyMask": state["mask"],
            "credentialSource": state["source"],
            # 后端不可用时界面要能直接说清「为什么存不了」，而不是只报一句失败
            "credentialBackend": {"backend": status["backend"], "mode": status["mode"],
                                  "secure": status["secure"], "writable": status["writable"],
                                  "reason": status["reason"],
                                  "insecureEntries": status["insecureEntries"]},
            "legacyMirror": self.mirror_enabled(),
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
