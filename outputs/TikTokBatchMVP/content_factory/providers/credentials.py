"""凭据库：Content Factory 里唯一存放 API Key / Token / Secret 的地方。

为什么要有独立的一层，而不是继续往设置 JSON 里塞
--------------------------------------------------
设置 JSON（`content-factory-settings.json`）是**界面可读可写**的载荷：
`content_settings()` / `public()` 会整份回给 UI，devtools 里一眼可见，
以后还要走备份、导出、报错截图。密钥放在那里，安全性只能靠「每次记得删字段」。
所以密钥单独放一个文件，配置里只留一个引用名（`credential_ref`），
读取路径上根本没有明文可漏。

本地存储策略（这一版技术栈下最合理的方案）
------------------------------------------
1. 独立文件 `%LOCALAPPDATA%/TikTokBatchMVP/content-factory-credentials.json`，权限 0600（尽力而为）。
2. 每个密钥值用 **Windows DPAPI**（`CryptProtectData`，ctypes 调系统 API，零第三方依赖）
   按「当前用户」加密后再落盘：文件被拷到别的机器 / 别的用户下都解不开。
   非 Windows 或 DPAPI 不可用时退化为明文存储，并在文件里如实标注 `plain` ——
   不假装加密成功（这一点比「看起来安全」重要）。
3. 原子写入（临时文件 + os.replace），避免断电写出半个文件。

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


# --------------------------------------------------------------------------
# Windows DPAPI（可选，尽力而为）
# --------------------------------------------------------------------------

class SecretBoxError(RuntimeError):
    """凭据条目无法读取。"""


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


class SecretBox:
    """把一段文本变成「本机本用户才解得开」的存储条目。

    protect() -> {"v": "dpapi"|"plain", "data": ...}
    unprotect() 反过来；解不开（换机器 / 换用户 / 数据损坏）时抛 SecretBoxError。
    """

    @staticmethod
    def available():
        if not hasattr(ctypes, "windll"):
            return False
        try:
            return bool(ctypes.windll.crypt32)          # 探测系统库是否存在
        except Exception:
            return False

    @staticmethod
    def protect(text):
        raw = "" if text is None else str(text)
        if not SecretBox.available():
            return {"v": "plain", "data": raw}
        try:
            protected = _dpapi_protect(raw.encode("utf-8"))
        except Exception as exc:                        # pragma: no cover - 取决于本机环境
            LOGGER.warning("DPAPI 加密不可用，本次凭据按本地明文存储：%s", redact(exc))
            return {"v": "plain", "data": raw}
        return {"v": "dpapi", "data": base64.b64encode(protected).decode("ascii")}

    @staticmethod
    def unprotect(entry):
        entry = dict(entry or {})
        version = entry.get("v") or "plain"
        data = entry.get("data")
        if data is None:
            raise SecretBoxError("凭据条目缺少 data 字段")
        if version == "plain":
            return str(data)
        if version == "dpapi":
            try:
                return _dpapi_unprotect(base64.b64decode(str(data))).decode("utf-8")
            except Exception as exc:
                raise SecretBoxError(
                    "凭据无法解密（通常是文件来自其他机器 / 其他 Windows 用户），请在设置里重新填写") from exc
        raise SecretBoxError(f"未知的凭据存储格式：{version!r}")


# --------------------------------------------------------------------------
# 凭据库
# --------------------------------------------------------------------------

class CredentialStore:
    """按 ref 存取凭据。线程安全，落盘原子。"""

    def __init__(self, root=None, path=None):
        self.root = Path(root) if root else default_data_root()
        self.path = Path(path) if path else self.root / CREDENTIALS_FILE_NAME
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
            os.chmod(self.path, 0o600)                  # 尽力而为；Windows 上主要靠 DPAPI
        except Exception:
            pass
        self._cache = payload

    def reload(self):
        with self._lock:
            self._cache = None
        return self

    def _item(self, ref):
        return dict((self._read().get("items") or {}).get(str(ref or "").strip()) or {})

    # ---- 写入 ----------------------------------------------------------
    def put(self, ref, values):
        """写入若干字段。空值 / 空白值被忽略（= 保持原值），不会删掉已存密钥。

        返回 {"ok": bool, "ref": ..., "stored": [...], "skipped": [...]}。
        """
        ref = str(ref or "").strip()
        if not ref:
            raise ValueError("凭据 ref 不能为空")
        stored, skipped = [], []
        with self._lock:
            payload = self._read()
            items = payload.setdefault("items", {})
            item = dict(items.get(ref) or {})
            for field_name, value in dict(values or {}).items():
                field_name = str(field_name or "").strip()
                if not field_name:
                    continue
                text = "" if value is None else str(value)
                if not text.strip():
                    skipped.append(field_name)          # 留空 = 不改；绝不能当删除
                    continue
                item[field_name] = SecretBox.protect(text)
                remember_secret(text.strip())
                stored.append(field_name)
            if stored:
                item["updated_at"] = now_text()
                items[ref] = item
                self._write(payload)
        return {"ok": bool(stored), "ref": ref, "stored": stored, "skipped": skipped}

    def set(self, ref, value, field="api_key"):
        return self.put(ref, {field: value})

    # ---- 读取 ----------------------------------------------------------
    def reveal(self, ref, field="api_key"):
        """取出明文。

        **仅供服务层内部调用**（连接测试、真实请求）。禁止在桥接层 / UI / 日志里使用；
        取到即登记进脱敏表，之后任何日志里的同一串都会被抹掉。
        """
        entry = self._item(ref).get(str(field or "api_key").strip())
        if not isinstance(entry, dict):
            return ""
        try:
            text = SecretBox.unprotect(entry)
        except SecretBoxError as exc:
            self._errors.append(f"{ref}.{field}: {exc}")
            LOGGER.warning("凭据 %s.%s 无法解密：%s", ref, field, exc)
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

    # ---- 删除 ----------------------------------------------------------
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

    # ---- 诊断 ----------------------------------------------------------
    def diagnostics(self):
        """不含任何密钥的自我描述，给诊断页用。"""
        return {
            "path": str(self.path),
            "protection": "dpapi" if SecretBox.available() else "plain",
            "refs": len(self._read().get("items") or {}),
            "knownSecrets": known_secret_count(),
            "errors": list(self._errors),
        }


def _forget_entry(entry):
    if not isinstance(entry, dict):
        return
    try:
        forget_secret(SecretBox.unprotect(entry))
    except SecretBoxError:
        pass


def field_label(name):
    return {
        "api_key": "API Key",
        "token": "Token",
        "access_key_secret": "AccessKey Secret",
        "access_key_id": "AccessKey ID",
        "password": "密码",
    }.get(str(name), str(name))
