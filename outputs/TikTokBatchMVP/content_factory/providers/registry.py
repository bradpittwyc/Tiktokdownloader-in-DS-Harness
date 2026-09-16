"""Provider 注册表：Content Factory 里「有哪些外部服务可用」的唯一出处。

职责边界（刻意做窄）
--------------------
- **是**：provider 实例的增删改查、启用 / 停用、合法性校验、
  默认 provider 选择、凭据的读写入口（真正的密钥在 credentials.py）。
- **不是**：不做通用设置系统、不做默认值框架、不落 sqlite、
  不发网络请求（连接测试在 connection_test.py）。

和 Settings Core 的关系
-----------------------
设置 JSON 是**面向界面的表单状态**（几十个开关与文本），
provider 是**面向调用方的结构化配置**（一个服务一条，带类型 / 凭据引用 / 超时）。
两者是不同形状的数据，硬塞进同一个 JSON 会让「AI 设置」这类表单页
既要做表单又兼做 registry，所以这里单独一个文件
`content-factory-providers.json`，由 `settings_adapter.py` 负责与设置页双向对齐。

落盘格式（结构上不含密钥）
--------------------------
{"version": 1, "providers": [ProviderConfig.to_dict(), ...]}
密钥只在 `credential_ref` 里以名字出现；明文在 content-factory-credentials.json。
"""

import json
import logging
import threading
from pathlib import Path

from .credentials import CredentialStore
from .models import (DEFAULT_TIMEOUT, PROVIDER_TYPES, ProviderConfig, ProviderError,
                     coerce_timeout, credential_ref_for, normalize_base_url, now_text,
                     provider_type)
from .paths import default_data_root

LOGGER = logging.getLogger(__name__)

PROVIDERS_FILE_NAME = "content-factory-providers.json"
FILE_VERSION = 1


class ProviderRegistry:
    """provider 实例的注册表。线程安全，落盘原子。"""

    def __init__(self, root=None, path=None, credentials=None):
        self.root = Path(root) if root else default_data_root()
        self.path = Path(path) if path else self.root / PROVIDERS_FILE_NAME
        self.credentials = credentials if credentials is not None else CredentialStore(self.root)
        self._lock = threading.RLock()
        self._providers = None                      # provider_id -> ProviderConfig
        self._errors = []

    # ---- 磁盘 ----------------------------------------------------------
    def _read(self):
        if self._providers is not None:
            return self._providers
        loaded = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("providers") if isinstance(raw, dict) else raw
            for entry in (entries or []):
                if not isinstance(entry, dict):
                    continue
                try:
                    config = ProviderConfig.from_dict(entry)
                except Exception as exc:
                    self._errors.append(f"跳过一条无法解析的 provider 记录（{type(exc).__name__}）")
                    continue
                config.validate_or_raise()
                loaded[config.provider_id] = config
        except FileNotFoundError:
            pass
        except Exception as exc:
            # 文件损坏不能让整个应用起不来：按空表处理，并在诊断里如实说明
            self._errors.append(f"provider 文件无法解析（{type(exc).__name__}），已按空表处理")
            LOGGER.warning("provider 文件无法解析（%s）：%s", type(exc).__name__, self.path)
        self._providers = loaded
        return self._providers

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": FILE_VERSION,
                   "providers": [config.to_dict() for config in self._read().values()]}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def reload(self):
        with self._lock:
            self._providers = None
        return self

    # ---- 查询 ----------------------------------------------------------
    def types(self, kind=None):
        """内置 provider 类型目录（界面用它渲染「新增 provider」下拉框）。"""
        return [spec.public() for spec in PROVIDER_TYPES if not kind or spec.kind == kind]

    def list(self, kind=None, enabled_only=False):
        with self._lock:
            items = list(self._read().values())
        if kind:
            items = [config for config in items if config.kind == kind]
        if enabled_only:
            items = [config for config in items if config.enabled]
        return sorted(items, key=lambda config: (config.kind, config.provider_id))

    def ids(self):
        return [config.provider_id for config in self.list()]

    def get(self, provider_id):
        """按 id 取配置；不存在返回 None。"""
        with self._lock:
            return self._read().get(str(provider_id or "").strip())

    def require(self, provider_id):
        """按 id 取配置；不存在抛 ProviderError。"""
        config = self.get(provider_id)
        if config is None:
            raise ProviderError(f"provider 不存在：{provider_id!r}", code="unknown_provider",
                                field="provider_id")
        return config

    def default(self, kind="llm"):
        """挑一个默认 provider：先看已启用的，再退回任意一个。"""
        candidates = self.list(kind=kind)
        if not candidates:
            return None
        enabled = [config for config in candidates if config.enabled]
        return (enabled or candidates)[0]

    def public(self, kind=None, enabled_only=False):
        """给界面 / 桥接层的视图：含凭据掩码，**绝不含明文**。"""
        return [config.public(self.credentials)
                for config in self.list(kind=kind, enabled_only=enabled_only)]

    def has_credentials(self, provider_id):
        config = self.require(provider_id)
        return self.credentials.is_set(config.credential_ref, config.credential_fields)

    # ---- 写入 ----------------------------------------------------------
    def create(self, provider_type_id, provider_id=None, **fields):
        """新建一个 provider。类型未知 / 字段非法时抛 ProviderError。"""
        spec = provider_type(provider_type_id)          # 未知类型在这里就报错
        with self._lock:
            provider_id = str(provider_id or "").strip() or self._next_id(spec.type_id)
            if self.get(provider_id) is not None:
                raise ProviderError(f"provider 已存在：{provider_id}",
                                    code="duplicate_provider", field="provider_id")
            payload = {
                "provider_id": provider_id,
                "provider_type": spec.type_id,
                "label": fields.get("label") or spec.label,
                "base_url": normalize_base_url(fields.get("base_url") or spec.default_base_url),
                "model": str(fields.get("model") or spec.default_model or "").strip(),
                "timeout": coerce_timeout(fields.get("timeout"), DEFAULT_TIMEOUT),
                "enabled": bool(fields.get("enabled", True)),
                "credential_ref": fields.get("credential_ref") or credential_ref_for(provider_id),
                "extra": dict(fields.get("extra") or {}),
            }
            config = ProviderConfig(**payload)
            config.validate_or_raise()
            self._read()[config.provider_id] = config
            self._write()
            return config

    def update(self, provider_id, **fields):
        """局部更新。未给出的字段保持原值；provider_type 不可改（改了凭据语义就变了）。"""
        with self._lock:
            config = self.require(provider_id)
            if "provider_type" in fields and fields["provider_type"] not in (None, config.provider_type):
                raise ProviderError("provider_type 不能修改，请新建一个 provider",
                                    code="immutable_field", field="provider_type")
            data = config.to_dict()
            for key, value in fields.items():
                if value is None:
                    continue
                if key in ("provider_id", "created_at", "updated_at"):
                    raise ProviderError(f"{key} 不能直接修改", code="immutable_field", field=key)
                if key == "base_url":
                    data[key] = normalize_base_url(value)
                elif key == "timeout":
                    data[key] = coerce_timeout(value, config.timeout)
                elif key == "enabled":
                    data[key] = bool(value)
                elif key == "extra":
                    data[key] = dict(value or {})
                elif key == "model":
                    data[key] = str(value or "").strip()
                elif key == "label":
                    data[key] = str(value or "").strip()
                elif key == "credential_ref":
                    data[key] = str(value or "").strip()
                else:
                    raise ProviderError(f"未知字段：{key}", code="unknown_field", field=key)
            updated = ProviderConfig(**data)
            updated.updated_at = now_text()
            updated.validate_or_raise()
            self._read()[config.provider_id] = updated
            self._write()
            return updated

    def set_enabled(self, provider_id, enabled):
        return self.update(provider_id, enabled=bool(enabled))

    def enable(self, provider_id):
        return self.set_enabled(provider_id, True)

    def disable(self, provider_id):
        return self.set_enabled(provider_id, False)

    def delete(self, provider_id, drop_credentials=True):
        """删除 provider；默认连同它的凭据一起删掉（否则会留下无主的密钥）。"""
        with self._lock:
            config = self.require(provider_id)
            if drop_credentials and config.credential_ref:
                self.credentials.drop(config.credential_ref)
            self._read().pop(config.provider_id, None)
            self._write()
        return True

    def upsert(self, config):
        """按 id 写入一条完整配置（迁移 / 测试用）。"""
        config.validate_or_raise()
        with self._lock:
            self._read()[config.provider_id] = config
            self._write()
        return config

    # ---- 凭据（唯一的密钥出入口）---------------------------------------
    def set_credentials(self, provider_id, values):
        """写入凭据。空值会被忽略（= 不改动已存密钥）。"""
        config = self.require(provider_id)
        return self.credentials.put(config.credential_ref, values)

    def clear_credentials(self, provider_id, field=None):
        """显式删除凭据：field 为空删整份，否则只删一个字段。"""
        config = self.require(provider_id)
        return self.credentials.drop(config.credential_ref, field)

    def credentials_view(self, provider_id):
        config = self.require(provider_id)
        return self.credentials.public(config.credential_ref, config.credential_fields)

    def resolve_credentials(self, provider_id):
        """服务层内部取明文（连接测试 / 真实调用）。禁止在桥接层与 UI 使用。"""
        config = self.require(provider_id)
        return self.credentials.bundle(config.credential_ref, config.credential_fields)

    # ---- 诊断 ----------------------------------------------------------
    def diagnostics(self):
        return {"path": str(self.path), "count": len(self.list()),
                "credentials": self.credentials.diagnostics(), "errors": list(self._errors)}

    # ---- 内部 ----------------------------------------------------------
    def _next_id(self, type_id):
        existing = set(self._read())
        candidate = f"{type_id}-main"
        if candidate not in existing:
            return candidate
        index = 2
        while f"{type_id}-{index}" in existing:
            index += 1
        return f"{type_id}-{index}"
