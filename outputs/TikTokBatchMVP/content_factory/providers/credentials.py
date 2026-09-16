"""凭据库：Content Factory 里唯一存放 API Key / Token / Secret 的地方。

为什么要有独立的一层，而不是继续往设置 JSON 里塞
--------------------------------------------------
设置 JSON（`content-factory-settings.json`）是**界面可读可写**的载荷：
`content_settings()` / `public()` 会整份回给 UI，devtools 里一眼可见，
以后还要走备份、导出、报错截图。密钥放在那里，安全性只能靠「每次记得删字段」。
所以密钥单独放一个文件，配置里只留一个引用名（`credential_ref`），
读取路径上根本没有明文可漏。

fail-closed 策略（这一版最重要的行为）
--------------------------------------
**生产/默认模式下，写不进去就不写，绝不悄悄降级成明文。**

- 默认后端是 `WindowsDPAPIBackend`（`CryptProtectData`，按当前 Windows 用户加密，
  ctypes 调系统 API，零第三方依赖）。
- DPAPI 不可用（非 Windows、系统加密组件缺失）时，`put()` 返回结构化错误
  `credential_backend_unavailable`，**一个字节都不落盘**，已有配置原样保留；
  同时日志里给一句明确的原因与出路，而不是把 API Key 明文写进文件。
- 明文后端（`PlaintextBackend`）与进程内测试后端（`TestCredentialBackend`）
  只在**显式选择**时才生效：构造参数 `backend=/backend_name=/allow_insecure=`，
  或环境变量 `CONTENT_FACTORY_CREDENTIAL_BACKEND=test|plain`
  （`plain` 还要 `CONTENT_FACTORY_ALLOW_INSECURE_CREDENTIALS=1`）。
  默认值永远是「安全的那个」，没有任何隐式降级路径。

后端抽象（未来接 macOS Keychain / Linux Secret Service 不用改上层）
-------------------------------------------------------------------
`CredentialBackend` 只要求四件事：`available()` / `protect()` / `unprotect()` / `public()`。
**写入用当前后端，读取按条目里的 `v` 字段分派** —— 所以换后端不会读不出老条目，
ProviderRegistry、Settings 适配层、连接测试都不需要知道后端是什么。

历史遗留的 `"v": "plain"` 条目仍然可读（不能因为升级就让用户的配置失效），
但 `status()` 会把它们数出来（`insecureEntries`），
`reprotect()` 可以把它们重新加密 —— 没有安全后端时这个方法同样拒绝执行。

对外行为（都有测试锁住）
------------------------
- `public()` 只回 `{set: bool, mask: '****abcd'}`，永远不回明文；
- 空值 / 空白值写入 = **忽略**（界面把密钥框留空表示「不改」，绝不能顺手删掉已存的密钥）；
- 删除是显式动作：`drop(ref)` 删整份、`drop(ref, field)` 删单个字段；
- 明文只在服务层内部通过 `reveal()` 取，取到即登记进脱敏表，
  之后即使被写进日志 / 异常文案也会变成 `****`。
"""

import base64
import ctypes
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path

from .models import is_secret_field, now_text
from .paths import default_data_root

LOGGER = logging.getLogger(__name__)

CREDENTIALS_FILE_NAME = "content-factory-credentials.json"
FILE_VERSION = 1

# 掩码规则：只露最后 4 位。太短的密钥一位都不露，避免「掩码本身就是答案」
MASK_PREFIX = "****"
MASK_KEEP = 4

# 后端标识（也是条目里的 v 字段）
BACKEND_DPAPI = "dpapi"
BACKEND_TEST = "test"
BACKEND_PLAIN = "plain"
BACKEND_NONE = "none"

BACKEND_ENV = "CONTENT_FACTORY_CREDENTIAL_BACKEND"
ALLOW_INSECURE_ENV = "CONTENT_FACTORY_ALLOW_INSECURE_CREDENTIALS"

# 进程内已知的密钥明文，供 redact() 使用（写入过 / 读取过就登记）
_KNOWN_SECRETS = set()
_KNOWN_LOCK = threading.Lock()

_PATTERNS = (
    # Authorization: Bearer xxx
    (re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]{6,})"), r"\1****"),
    # 常见前缀形态的 key
    (re.compile(r"\b(sk|rk|pk|api|key)-[A-Za-z0-9._\-]{6,}", re.I), r"\1-****"),
    # key=value / "api_key": "value" / access_key_secret: value
    (re.compile(r"(?i)([\"']?(?:api[_-]?key|access[_-]?key[_-]?(?:id|secret)|secret|token|password|passwd|"
                r"authorization|credential)[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}\]]{4,})"), r"\1****"),
    # URL 查询串里的密钥（有些厂商把 key 放 query，最容易漏进日志）
    (re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|key)=)([^&\s]{4,})"), r"\1****"),
)

REDACTED = "****"


def is_secret_name(name):
    """字段名是否按密钥处理（规则与 models.is_secret_field 共用一份）。"""
    return is_secret_field(name)


def mask_secret(value):
    """`sk-1234567890abcd` -> `****abcd`。空值返回空串。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= MASK_KEEP + 2:
        return MASK_PREFIX
    return MASK_PREFIX + text[-MASK_KEEP:]


def remember_secret(value):
    """把明文登记进脱敏表（供 redact 使用）。太短的值不登记，避免误伤正常文本。"""
    text = str(value or "")
    if len(text) < 6:
        return
    with _KNOWN_LOCK:
        _KNOWN_SECRETS.add(text)


def forget_secret(value):
    with _KNOWN_LOCK:
        _KNOWN_SECRETS.discard(str(value or ""))


def known_secret_count():
    with _KNOWN_LOCK:
        return len(_KNOWN_SECRETS)


def redact(text):
    """把文本里可能出现的密钥抹成 `****`。

    用于日志、异常文案、连接测试返回的 detail。
    先替换「已知明文」（最可靠），再按形态兜底。
    """
    if text is None:
        return ""
    result = str(text)
    with _KNOWN_LOCK:
        for secret in sorted(_KNOWN_SECRETS, key=len, reverse=True):
            if secret and secret in result:
                result = result.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_mapping(data):
    """递归脱敏：密钥字段的字符串值整段抹掉，其余文本按 redact 处理。

    只动字符串值 —— `apiKeySet: true` 这类状态位必须原样保留，否则界面读到一串星号。
    """
    if isinstance(data, dict):
        return {key: (REDACTED if is_secret_name(key) and isinstance(value, str) and value
                      else redact_mapping(value))
                for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [redact_mapping(entry) for entry in data]
    if isinstance(data, str):
        return redact(data)
    return data


class SecretRedactionFilter(logging.Filter):
    """日志过滤器：任何一路日志里的密钥都会被抹掉。

    宁可能弄脏一点普通文本，也不让 key 落进日志文件 ——
    `scrape.log` 会被异常处理页读出来展示给用户，漏进去就等于漏到界面上。
    """

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        if getattr(record, "exc_text", None):
            record.exc_text = redact(record.exc_text)
        return True


def install_log_filter(logger=None):
    """给 logger 及其现有 handler 挂上脱敏过滤器（可重复调用，不会重复挂）。

    注意：只在**已经配置好 logging 之后**调用才能覆盖全部 handler；
    给 logger 自身挂的那一份对新 handler 也仍然生效于「记录产生时」，
    所以即便之后又加了 handler，记录里的明文也已经被替换掉了。
    """
    target = logger if logger is not None else logging.getLogger()
    filter_ = next((entry for entry in target.filters
                    if isinstance(entry, SecretRedactionFilter)), None) or SecretRedactionFilter()
    if filter_ not in target.filters:
        target.addFilter(filter_)
    for handler in list(target.handlers):
        if not any(isinstance(entry, SecretRedactionFilter) for entry in handler.filters):
            handler.addFilter(filter_)
    return filter_


# ---------------------------------------------------------------------------
# 凭据后端
# ---------------------------------------------------------------------------

class CredentialBackendError(RuntimeError):
    """凭据后端不可用 / 读写失败。

    这是一个**必须向上报告**的错误：调用方要能明确区分
    「没有配置密钥」和「有密钥但存不进去」，后者绝不允许被当成前者静默处理。
    """


class CredentialBackend:
    """凭据后端契约：把明文变成一个可落盘的条目，再还原回来。

    自定义后端只要实现这 4 个方法即可被 CredentialStore 使用
    （未来的 macOS Keychain / Linux Secret Service 后端就是这么加的），
    ProviderRegistry 与 Settings 适配层完全不需要改动。
    """

    backend_id = BACKEND_NONE
    label = "未配置"
    secure = False                 # 是否提供真实的加密保护
    persists_plaintext = False     # 是否会把明文写进文件

    def available(self):
        return False

    def protect(self, text):
        raise CredentialBackendError(f"凭据后端 {self.backend_id} 不可用，已拒绝写入")

    def unprotect(self, entry):
        raise CredentialBackendError(f"凭据后端 {self.backend_id} 无法读取该条目")

    def public(self):
        return {"backend": self.backend_id, "label": self.label,
                "secure": bool(self.secure), "available": bool(self.available()),
                "persists_plaintext": bool(self.persists_plaintext)}


class UnavailableBackend(CredentialBackend):
    """没有可用的安全后端时的占位后端：写入一律失败，绝不降级。"""

    backend_id = BACKEND_NONE
    label = "不可用"

    def __init__(self, reason="", requested=""):
        self.reason = reason or "没有可用的安全凭据后端"
        self.requested = str(requested or "")

    def available(self):
        return False

    def public(self):
        data = super().public()
        data.update({"reason": self.reason, "requested": self.requested})
        return data

    def protect(self, text):
        raise CredentialBackendError(self.reason)

    def unprotect(self, entry):
        raise CredentialBackendError(self.reason)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(data):                               # pragma: no cover - 依赖 Windows API
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"CryptProtectData 失败（{ctypes.GetLastError()}）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data):                             # pragma: no cover - 依赖 Windows API
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"CryptUnprotectData 失败（{ctypes.GetLastError()}）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


class WindowsDPAPIBackend(CredentialBackend):
    """默认后端：Windows DPAPI，按当前用户加密（文件被拷走也解不开）。"""

    backend_id = BACKEND_DPAPI
    label = "Windows DPAPI（当前用户）"
    secure = True

    def available(self):
        if not hasattr(ctypes, "windll"):
            return False
        try:
            return bool(ctypes.windll.crypt32)              # 探测系统库是否存在
        except Exception:
            return False

    def protect(self, text):
        raw = "" if text is None else str(text)
        protected = _dpapi_protect(raw.encode("utf-8"))     # 失败就抛，绝不退化成明文
        return {"v": self.backend_id, "data": base64.b64encode(protected).decode("ascii")}

    def unprotect(self, entry):
        data = (entry or {}).get("data")
        if data is None:
            raise CredentialBackendError("凭据条目缺少 data 字段")
        try:
            return _dpapi_unprotect(base64.b64decode(str(data))).decode("utf-8")
        except Exception as exc:
            raise CredentialBackendError(
                "凭据无法解密（通常是文件来自其他机器 / 其他 Windows 用户），请在设置里重新填写") from exc


class PlaintextBackend(CredentialBackend):
    """明文后端：**只在显式允许的开发 / 测试模式下**才会被选到。"""

    backend_id = BACKEND_PLAIN
    label = "明文（仅开发/测试，需显式开启）"
    secure = False
    persists_plaintext = True

    def available(self):
        return True

    def protect(self, text):
        return {"v": self.backend_id, "data": "" if text is None else str(text)}

    def unprotect(self, entry):
        return str((entry or {}).get("data") or "")


_TEST_VAULTS = {}


class TestCredentialBackend(CredentialBackend):
    """测试后端：确定性、进程内。

    明文只留在内存的 vault 里，落盘的是一个 sha256 token —— 所以
    「测试写入的密钥不会变成磁盘上的明文」这件事本身也能被测。
    同一个 `namespace` 的实例共享 vault，便于模拟「重启后仍能读到」。
    """

    backend_id = BACKEND_TEST
    label = "测试后端（进程内，不落盘明文）"
    secure = False
    persists_plaintext = False

    def __init__(self, namespace="default", vault=None):
        self.namespace = str(namespace or "default")
        self._vault = vault if vault is not None else _TEST_VAULTS.setdefault(self.namespace, {})

    def available(self):
        return True

    def protect(self, text):
        raw = "" if text is None else str(text)
        token = hashlib.sha256(f"{self.namespace}\0{raw}".encode("utf-8")).hexdigest()[:32]
        self._vault[token] = raw
        return {"v": self.backend_id, "data": token}

    def unprotect(self, entry):
        token = str((entry or {}).get("data") or "")
        if token not in self._vault:
            raise CredentialBackendError("测试后端的进程内数据已丢失（重启后需要重新录入密钥）")
        return self._vault[token]

    @classmethod
    def clear_vaults(cls):
        _TEST_VAULTS.clear()


def _env_flag(name):
    return str(os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


# 自定义后端工厂：{名字: 无参工厂}。
# 未来接 macOS Keychain / Linux Secret Service 时，只要实现 CredentialBackend 的
# 4 个方法并 register_credential_backend("keychain", KeychainBackend)，
# 不改 ProviderRegistry、不改 Settings 适配层、不改连接测试。
_BACKEND_FACTORIES = {}


def register_credential_backend(name, factory):
    """按名字注册一个凭据后端工厂（可随后用 `backend_name=`/环境变量选中）。"""
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("后端名字不能为空")
    if not callable(factory):
        raise TypeError("后端工厂必须可调用")
    _BACKEND_FACTORIES[key] = factory
    return factory


def registered_credential_backends():
    return sorted(_BACKEND_FACTORIES)


def resolve_credential_backend(backend=None, backend_name=None, allow_insecure=None):
    """决定用哪个凭据后端。默认永远是安全的那一个（Windows DPAPI）。

    优先级：显式 backend 对象 > 显式 backend_name > 环境变量 > dpapi 默认。
    内置名字优先；未命中时查已注册的自定义后端；都命中不了 → fail closed。
    任何指向「明文 / 测试」的选择都必须是**显式**的。
    """
    if backend is not None:
        return backend
    allow = _env_flag(ALLOW_INSECURE_ENV) if allow_insecure is None else bool(allow_insecure)
    requested = str(backend_name or os.environ.get(BACKEND_ENV) or BACKEND_DPAPI).strip().lower()
    if requested in (BACKEND_TEST, "memory", "in-memory", "inmemory"):
        return TestCredentialBackend()
    if requested in (BACKEND_PLAIN, "plaintext"):
        if allow:
            return PlaintextBackend()
        return UnavailableBackend(
            f"明文凭据后端需要显式开启（allow_insecure=True 或 {ALLOW_INSECURE_ENV}=1）；"
            "默认拒绝把密钥明文落盘", requested)
    if requested == BACKEND_DPAPI:
        dpapi = WindowsDPAPIBackend()
        if dpapi.available():
            return dpapi
        return UnavailableBackend(
            "Windows DPAPI 不可用（非 Windows 或系统加密组件缺失）：已拒绝把新密钥明文落盘；"
            f"现有配置未被修改。开发环境可显式设置 {BACKEND_ENV}=test 使用进程内测试后端", requested)
    factory = _BACKEND_FACTORIES.get(requested)
    if factory is not None:
        return factory()
    return UnavailableBackend(f"未知的凭据后端：{requested!r}", requested)


# ---------------------------------------------------------------------------
# 凭据库
# ---------------------------------------------------------------------------

class CredentialStore:
    """按 ref 存取凭据。线程安全，落盘原子，写不进去就 fail closed。"""

    def __init__(self, root=None, path=None, backend=None, backend_name=None, allow_insecure=None):
        self.root = Path(root) if root else default_data_root()
        self.path = Path(path) if path else self.root / CREDENTIALS_FILE_NAME
        self.backend = resolve_credential_backend(backend=backend, backend_name=backend_name,
                                                  allow_insecure=allow_insecure)
        self._lock = threading.RLock()
        self._cache = None
        self._errors = []

    # ---- 磁盘 ----------------------------------------------------------
    def _read(self):
        if self._cache is not None:
            return self._cache
        payload = {"version": FILE_VERSION, "items": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                items = raw.get("items")
                payload = {"version": raw.get("version") or FILE_VERSION,
                           "items": items if isinstance(items, dict) else {}}
        except FileNotFoundError:
            pass
        except Exception as exc:
            # 不把文件内容带进错误信息（里面是密文 / 明文混合）
            self._errors.append(f"凭据文件无法解析（{type(exc).__name__}），已按空库处理")
            LOGGER.warning("凭据文件无法解析（%s），已按空库处理：%s", type(exc).__name__, self.path)
        self._cache = payload
        return self._cache

    def _write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, 0o600)                      # 尽力而为；Windows 上主要靠 DPAPI
        except Exception:
            pass
        self._cache = payload

    def reload(self):
        with self._lock:
            self._cache = None
        return self

    def _item(self, ref):
        return dict((self._read().get("items") or {}).get(str(ref or "").strip()) or {})

    # ---- 后端状态 ------------------------------------------------------
    def writable(self):
        return bool(self.backend.available())

    def unavailable_reason(self):
        return "" if self.writable() else str(getattr(self.backend, "reason", "")
                                              or f"凭据后端 {self.backend.backend_id} 不可用")

    def status(self):
        """后端与存储状态（不含任何密钥），给界面 / 诊断用。"""
        info = self.backend.public()
        writable = bool(info.get("available"))
        if not writable:
            mode = "unavailable"
        elif info.get("secure"):
            mode = "secure"
        elif info.get("persists_plaintext"):
            mode = "insecure"
        else:
            mode = "development"
        info.update({"writable": writable, "mode": mode,
                     "reason": "" if writable else self.unavailable_reason(),
                     "path": str(self.path), "refs": len(self._read().get("items") or {}),
                     "insecureEntries": self.count_insecure_entries()})
        return info

    def count_insecure_entries(self):
        total = 0
        for item in (self._read().get("items") or {}).values():
            for name, entry in dict(item or {}).items():
                if name != "updated_at" and isinstance(entry, dict) \
                        and str(entry.get("v") or BACKEND_PLAIN) == BACKEND_PLAIN:
                    total += 1
        return total

    # ---- 写入 ----------------------------------------------------------
    def put(self, ref, values):
        """写入若干字段。

        行为（fail-closed）：
        - 空白值 = 忽略（= 保持原值），不会删掉已存密钥；
        - 后端不可用 / 加密失败 → 返回结构化错误，**不写任何东西**；
        - 成功才落盘，且是一次原子写。

        返回 {"ok", "ref", "stored", "skipped", "code", "error", "backend"}。
        """
        ref = str(ref or "").strip()
        if not ref:
            raise ValueError("凭据 ref 不能为空")

        pending, skipped = {}, []
        for field_name, value in dict(values or {}).items():
            field_name = str(field_name or "").strip()
            if not field_name:
                continue
            text = "" if value is None else str(value)
            if not text.strip():
                skipped.append(field_name)                  # 留空 = 不改；绝不能当删除
                continue
            pending[field_name] = text

        if not pending:
            return {"ok": False, "ref": ref, "stored": [], "skipped": skipped,
                    "code": "nothing_to_store", "backend": self.backend.backend_id,
                    "error": "没有需要保存的密钥（留空表示不修改已存密钥）"}

        if not self.writable():
            reason = self.unavailable_reason()
            LOGGER.warning("拒绝保存凭据 %s：%s", ref, reason)
            return {"ok": False, "ref": ref, "stored": [], "skipped": skipped,
                    "code": "credential_backend_unavailable", "backend": self.backend.backend_id,
                    "error": reason, "existingKept": True}

        try:
            entries = {field: self.backend.protect(text) for field, text in pending.items()}
        except CredentialBackendError as exc:
            LOGGER.warning("凭据加密失败，已放弃保存 %s：%s", ref, redact(exc))
            return {"ok": False, "ref": ref, "stored": [], "skipped": skipped,
                    "code": "credential_write_failed", "backend": self.backend.backend_id,
                    "error": redact(exc), "existingKept": True}
        except Exception as exc:                            # pragma: no cover - 防御
            LOGGER.warning("凭据加密异常，已放弃保存 %s：%s", ref, type(exc).__name__)
            return {"ok": False, "ref": ref, "stored": [], "skipped": skipped,
                    "code": "credential_write_failed", "backend": self.backend.backend_id,
                    "error": redact(f"{type(exc).__name__}: {exc}"), "existingKept": True}

        with self._lock:
            payload = self._read()
            items = payload.setdefault("items", {})
            item = dict(items.get(ref) or {})
            item.update(entries)
            item["updated_at"] = now_text()
            items[ref] = item
            self._write(payload)

        for text in pending.values():
            remember_secret(text)
        return {"ok": True, "ref": ref, "stored": list(pending), "skipped": skipped,
                "code": "", "error": "", "backend": self.backend.backend_id}

    def set(self, ref, value, field="api_key"):
        return self.put(ref, {field: value})

    # ---- 读取 ----------------------------------------------------------
    def _decrypt(self, entry):
        """按条目自己的格式解密。

        读取不依赖「当前后端是谁」—— 换后端 / 换机器都不能让老配置直接读不出来。
        """
        entry = dict(entry or {})
        version = str(entry.get("v") or BACKEND_PLAIN)
        if version == BACKEND_PLAIN:
            return str(entry.get("data") or "")             # 历史遗留明文：仍可读，status() 会报警
        if version == BACKEND_DPAPI:
            return WindowsDPAPIBackend().unprotect(entry)
        if version == self.backend.backend_id:
            return self.backend.unprotect(entry)
        raise CredentialBackendError(f"未知的凭据存储格式：{version!r}")

    def reveal(self, ref, field="api_key"):
        """取出明文。

        **仅供服务层内部调用**（连接测试、真实请求）。禁止在桥接层 / UI / 日志里使用；
        取到即登记进脱敏表，之后任何日志里的同一串都会被抹掉。
        """
        entry = self._item(ref).get(str(field or "api_key").strip())
        if not isinstance(entry, dict):
            return ""
        try:
            text = self._decrypt(entry)
        except CredentialBackendError as exc:
            self._errors.append(f"{ref}.{field}: {exc}")
            LOGGER.warning("凭据 %s.%s 无法读取：%s", ref, field, exc)
            return ""
        remember_secret(text)
        return text

    def bundle(self, ref, fields=None):
        """取多个字段的明文（服务层内部用）。"""
        names = list(fields) if fields else self.fields(ref)
        return {name: self.reveal(ref, name) for name in names}

    def fields(self, ref):
        item = self._item(ref)
        return sorted(key for key, value in item.items()
                      if key != "updated_at" and isinstance(value, dict))

    def exists(self, ref):
        return bool(self.fields(ref))

    def is_set(self, ref, fields=None):
        """是否已配置（= 字段存在**且**解得开）。

        解不开（换了机器 / 换用户 / 数据损坏）按「未配置」处理：
        调用方拿不到可用的密钥，把它当作已配置只会让真实请求在更晚的地方炸。
        """
        names = [str(name) for name in fields] if fields else self.fields(ref)
        if not names:
            return False
        return all(bool(self.reveal(ref, name)) for name in names)

    def mask(self, ref, field="api_key"):
        return mask_secret(self.reveal(ref, field))

    def public(self, ref, fields=None):
        """对外视图：只有 set / mask / label，没有明文。"""
        names = list(fields) if fields else self.fields(ref)
        available = set(self.fields(ref))
        return {
            name: {"set": name in available,
                   "mask": self.mask(ref, name) if name in available else "",
                   "label": field_label(name)}
            for name in names
        }

    def updated_at(self, ref):
        return str(self._item(ref).get("updated_at") or "")

    # ---- 删除 / 重新加密 ------------------------------------------------
    def drop(self, ref, field=None):
        """显式删除。field 为空 = 删除整份凭据。返回删掉的字段数。"""
        ref = str(ref or "").strip()
        removed = 0
        with self._lock:
            payload = self._read()
            items = payload.setdefault("items", {})
            item = dict(items.get(ref) or {})
            if not item:
                return 0
            names = [name for name, entry in item.items()
                     if name != "updated_at" and isinstance(entry, dict)]
            if field:
                target = str(field)
                if target in names:
                    _forget_entry(item.pop(target))
                    names.remove(target)
                    removed = 1
                if names:
                    items[ref] = item
                else:
                    items.pop(ref, None)
            else:
                for name in names:
                    _forget_entry(item.get(name))
                removed = len(names)
                items.pop(ref, None)
            self._write(payload)
        return removed

    def clear(self):
        """清空整个凭据库（「重置全部凭据」）。返回清掉的 ref 数。"""
        with self._lock:
            payload = self._read()
            items = payload.get("items") or {}
            count = len(items)
            if count:
                for item in items.values():
                    for name, entry in dict(item or {}).items():
                        if name != "updated_at":
                            _forget_entry(entry)
                self._write({"version": FILE_VERSION, "items": {}})
        return count

    def reprotect(self, dry_run=False):
        """把历史遗留的明文条目重新加密成当前后端。

        后端不可用时**拒绝执行**（fail closed）：宁可不转换，也不动原数据。
        返回 {"ok", "converted", "pending", "backend", "code", "error"}。
        """
        candidates = []
        with self._lock:
            items = self._read().get("items") or {}
            for ref, item in items.items():
                for name, entry in dict(item or {}).items():
                    if name == "updated_at" or not isinstance(entry, dict):
                        continue
                    if str(entry.get("v") or BACKEND_PLAIN) != BACKEND_PLAIN:
                        continue
                    text = str(entry.get("data") or "")
                    if text:
                        candidates.append((ref, name, text))
        if not candidates:
            return {"ok": True, "converted": 0, "pending": 0, "backend": self.backend.backend_id}
        if dry_run:
            return {"ok": True, "converted": 0, "pending": len(candidates),
                    "backend": self.backend.backend_id, "dryRun": True}
        if not self.writable():
            return {"ok": False, "converted": 0, "pending": len(candidates),
                    "code": "credential_backend_unavailable",
                    "backend": self.backend.backend_id,
                    "error": self.unavailable_reason(), "existingKept": True}
        try:
            converted = {(ref, name): self.backend.protect(text)
                         for ref, name, text in candidates}
        except CredentialBackendError as exc:
            return {"ok": False, "converted": 0, "pending": len(candidates),
                    "code": "credential_write_failed", "backend": self.backend.backend_id,
                    "error": redact(exc), "existingKept": True}
        with self._lock:
            payload = self._read()
            items = payload.setdefault("items", {})
            for (ref, name), entry in converted.items():
                items.setdefault(ref, {})[name] = entry
            self._write(payload)
        return {"ok": True, "converted": len(converted), "pending": 0,
                "backend": self.backend.backend_id}

    # ---- 诊断 ----------------------------------------------------------
    def diagnostics(self):
        """不含任何密钥的自我描述，给诊断页用。"""
        status = self.status()
        return {
            "path": str(self.path),
            "backend": status["backend"],
            "mode": status["mode"],
            "secure": status["secure"],
            "writable": status["writable"],
            "reason": status["reason"],
            "refs": status["refs"],
            "insecureEntries": status["insecureEntries"],
            "knownSecrets": known_secret_count(),
            "errors": list(self._errors),
        }


def _forget_entry(entry):
    """删除条目后把它从脱敏表里摘掉。

    只有明文条目能直接摘（密文条目本来就只登记过解密后的明文；
    留在表里也只是让脱敏更激进一点，不影响正确性）。
    """
    if not isinstance(entry, dict):
        return
    if str(entry.get("v") or BACKEND_PLAIN) == BACKEND_PLAIN:
        forget_secret(str(entry.get("data") or ""))


def field_label(name):
    return {
        "api_key": "API Key",
        "token": "Token",
        "access_key_secret": "AccessKey Secret",
        "access_key_id": "AccessKey ID",
        "password": "密码",
    }.get(str(name), str(name))
