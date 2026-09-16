"""Settings Core 的服务层：默认值 → 校验 → 原子落盘 → 读取 → 重置 → 迁移。

对外就一个类：`SettingsService`（旧名 `FactorySettings` 保留为子类）。
其他模块只应该通过它读写设置，不要自己去碰那个 JSON 文件。

## 落盘

`%LOCALAPPDATA%/TikTokBatchMVP/content-factory-settings.json`（与第一阶段同一个文件，
键名、默认值、`apiKeyXXX` 这类界面契约全部没变，旧用户的配置直接可用）。

写入是「临时文件 + fsync + os.replace」：`os.replace` 在 Windows 上是原子的，
所以**要么全旧、要么全新，不会出现半截文件**；写失败时原文件一个字节都没动过。
上一份好文件同时留一份 `.bak`，主文件被外部改坏时可以兜底恢复。

读取是「主文件 → .bak → 默认值」三级回退，任何一级坏掉都不会让程序起不来。

## 缓存与多实例

`load()` 有缓存，但每次都会比对文件的 (mtime, size)：别的实例/别的进程写过，
这边会自动重读，不会拿着旧数据把别人的改动覆盖掉。
同一个文件在进程内共享一把可重入锁，两个实例并发写不会互相踩。

## 两条写入路径

- `apply()` / `validate()` / `reset()`：**新接口**，严格校验，返回 ApplyResult，
  不抛异常（除了编程错误）。非法值一个字都不写。
- `update()` / `save_section()` / `save()`：**旧接口**，保持不变的名字与返回值
  （直接回整份设置），宽松校验：非法字段丢弃、其余照常写；未知分区仍然报错。
  落盘失败按旧行为抛 SettingsPersistenceError。

## 敏感字段

`api_key` / `access_key_secret` / `smtp_password` 由 schema 标为 secret：
`public()`（给界面的视图）只回「是否已设置」，永不回明文；
局部更新时空值表示「保持原值」。
明文只有服务端消费者能拿（`section()` / `get_secret()`）。
Settings Core 只负责保管与屏蔽，**不建第二套 secrets 系统** ——
密钥的归属、轮换与加密由 Provider / Secrets 模块决定（见 docs/settings-core.md）。
"""

import copy
import json
import os
import threading
from dataclasses import dataclass, field as _field
from pathlib import Path

from . import migration as _migration
from .defaults import DEFAULTS, SECTIONS, defaults  # noqa: F401  (DEFAULTS 供旧调用方)
from .schema import SCHEMA_VERSION, VERSION_KEY, alias_map, resolve_section_name
from .validators import (Issue, ValidationResult, audit_section, is_blank,
                         normalize_value, validate_section)


def app_data_root():
    """应用数据目录，与现有下载器保持一致（LOCALAPPDATA/TikTokBatchMVP）。"""
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP"


class SettingsError(Exception):
    """Settings Core 的基类异常。"""


class UnknownSectionError(SettingsError):
    def __init__(self, section):
        self.section = str(section or "")
        super().__init__(f"未知的设置分区：{self.section}")


class SettingsValidationError(SettingsError):
    def __init__(self, section, errors=None):
        self.section = str(section or "")
        self.errors = list(errors or [])
        detail = "；".join(getattr(item, "message", str(item)) for item in self.errors)
        super().__init__(detail or f"{self.section} 的设置值不合法")


class SettingsPersistenceError(SettingsError):
    """读写设置文件失败。抛出来时磁盘上的旧配置保证完好。"""


#: 同一个设置文件在进程内共享一把锁（RLock：同一个线程可以嵌套取）。
_FILE_LOCKS = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _shared_lock(path):
    key = str(Path(path).absolute()).lower()
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
        return lock


@dataclass
class ApplyResult:
    """一次写入（更新 / 替换 / 重置）的结果。"""

    ok: bool
    section: str = ""
    action: str = "update"                     # update | replace | reset | reset_all
    applied: dict = _field(default_factory=dict)
    errors: list = _field(default_factory=list)   # [Issue]：被拒绝的字段与原因
    unknown: list = _field(default_factory=list)  # schema 外的键
    settings: dict = _field(default_factory=dict)
    message: str = ""
    error: str = ""

    @property
    def rejected(self) -> list:
        return [item.field for item in self.errors if getattr(item, "field", "")]

    def as_dict(self) -> dict:
        return {"ok": self.ok, "section": self.section, "action": self.action,
                "applied": dict(self.applied), "rejected": self.rejected,
                "unknown": list(self.unknown), "error": self.error, "message": self.message,
                "errors": [item.as_dict() for item in self.errors]}


class SettingsService:
    """设置读写。一个实例 ≈ 一个设置文件；实例很便宜，随用随建。"""

    filename = "content-factory-settings.json"

    def __init__(self, root=None, path=None):
        self.root = Path(root) if root else app_data_root()
        self.path = Path(path) if path else self.root / self.filename
        self._lock = _shared_lock(self.path)
        self._cache = None
        self._stamp = None
        self._stored_version = None
        self._issues = []
        self.last_result = None                # 最近一次写入结果（诊断用）

    # ---- 路径与版本 ----------------------------------------------------
    @property
    def backup_path(self):
        return self.path.with_name(self.path.name + ".bak")

    @property
    def tmp_path(self):
        return self.path.with_suffix(".tmp")

    @property
    def schema_version(self):
        """本程序支持的设置 schema 版本。"""
        return SCHEMA_VERSION

    @property
    def stored_schema_version(self):
        """磁盘上那份文件的版本（没有版本号的老文件算 1）。"""
        self.load()
        return self._stored_version

    @property
    def migration_pending(self):
        return (self.stored_schema_version or 1) < SCHEMA_VERSION

    def issues(self):
        """加载过程中发现的问题（文件损坏、从备份恢复、迁移未落盘……）。"""
        self.load()
        return [item.as_dict() for item in self._issues]

    def audit(self):
        """体检：已落盘的值是否符合当前 schema（只报问题，不改数据）。"""
        data = self.load()
        problems = []
        for name, spec in SECTIONS.items():
            problems.extend(audit_section(spec, data.get(name)))
        return problems

    # ---- 读 ------------------------------------------------------------
    def load(self, force=False):
        """整份设置（含 schema_version）。返回深拷贝，随便改。"""
        with self._lock:
            if force:
                self._cache = None
            if self._cache is not None and self._disk_stamp() == self._stamp:
                return copy.deepcopy(self._cache)
            return copy.deepcopy(self._build())

    def reload(self):
        """丢掉缓存，重新从磁盘读（外部改过文件时用）。"""
        return self.load(force=True)

    def export(self):
        """整份设置的快照（与 load() 相同，语义更明确，给备份/导出用）。"""
        return self.load()

    def section(self, name):
        """某个分区的全部值（未知分区返回 {}）。"""
        resolved = resolve_section_name(name)
        if resolved is None:
            return {}
        value = self.load().get(resolved)
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def get(self, section, key, default=None):
        return self.section(section).get(key, default)

    def get_secret(self, section, key, default=""):
        """取敏感字段的明文 —— **只给服务端消费者用**（AI 调用 / 上传 / SMTP）。

        界面的任何返回值都必须走 public()。密钥的归属与轮换由
        Provider / Secrets 模块负责，这里只做保管与屏蔽。
        """
        return self.section(section).get(key, default)

    def public(self):
        """给界面用的视图：敏感字段只回「是否已设置」，永不回明文。"""
        data = self.load()
        view = copy.deepcopy(data)
        view.pop(VERSION_KEY, None)             # 文件版本用 schemaVersion 表达即可
        for name, spec in SECTIONS.items():
            bucket = view.get(name)
            if not isinstance(bucket, dict):
                continue
            for item in spec.fields:
                if not item.secret:
                    continue
                bucket[item.flag] = not is_blank(bucket.get(item.key))
                bucket[item.key] = ""
        view["schemaVersion"] = SCHEMA_VERSION
        return view

    def describe(self, section=None):
        """机器可读的 schema 描述（界面生成表单 / 其他 Agent 对齐字段用）。"""
        if section is not None:
            name = resolve_section_name(section)
            if name is None:
                return {}
            return SECTIONS[name].describe()
        return {
            "schemaVersion": SCHEMA_VERSION,
            "versionKey": VERSION_KEY,
            "aliases": alias_map(),
            "storage": {"path": str(self.path), "backup": str(self.backup_path)},
            "sections": [spec.describe() for spec in SECTIONS.values()],
        }

    # ---- 校验（不落盘）-------------------------------------------------
    def validate(self, section, values, *, strict=True, partial=True):
        """只校验不写入。未知分区返回一条 unknown_section 错误（ok=False）。"""
        name = resolve_section_name(section)
        if name is None:
            result = ValidationResult(section=str(section or ""), strict=bool(strict),
                                      partial=bool(partial))
            result.errors.append(Issue(str(section or ""), "", "unknown_section",
                                       f"未知的设置分区：{section}"))
            return result
        return validate_section(SECTIONS[name], values, strict=strict, partial=partial)

    # ---- 写（新接口）---------------------------------------------------
    def apply(self, section, values, *, strict=True, partial=True):
        """校验并写入一个分区，返回 ApplyResult。

        strict=True ：有一个字段不合法就整体拒绝，磁盘一个字节都不动。
        strict=False：丢弃出错的字段，其余照常写入（旧界面的兼容路径）。
        partial=True：局部更新（只动传上来的键）；False = 整段替换。
        """
        name = resolve_section_name(section)
        action = "update" if partial else "replace"
        if name is None:
            message = f"未知的设置分区：{section}"
            return self._finish(ApplyResult(
                ok=False, section=str(section or ""), action=action, error=message,
                errors=[Issue(str(section or ""), "", "unknown_section", message)],
                settings=self.load()))
        if values is None:
            values = {}
        if not isinstance(values, dict):
            message = "设置值必须是「字段 → 值」的字典"
            return self._finish(ApplyResult(
                ok=False, section=name, action=action, error=message,
                errors=[Issue(name, "", "invalid_payload", message, values)],
                settings=self.load()))

        spec = SECTIONS[name]
        validation = validate_section(spec, values, strict=strict, partial=partial)
        if strict and validation.errors:
            return self._finish(ApplyResult(
                ok=False, section=name, action=action, errors=list(validation.errors),
                unknown=sorted(validation.unknown), error=validation.errors[0].message,
                settings=self.load()))

        incoming = dict(validation.values)
        incoming.update(validation.unknown)
        if not incoming:
            # 一个字段都没通过（或本来就没传）：不去动磁盘，也不凭空造设置文件。
            return self._finish(ApplyResult(
                ok=not validation.errors, section=name, action=action,
                errors=list(validation.errors), unknown=sorted(validation.unknown),
                error=validation.errors[0].message if validation.errors else "",
                message="没有可写入的字段，设置文件未被改动", settings=self.load()))
        with self._lock:
            data = self.load()
            bucket = dict(data.get(name) or {}) if partial else {}
            bucket.update(incoming)
            data[name] = bucket
            data[VERSION_KEY] = SCHEMA_VERSION
            try:
                self._commit(data)
            except SettingsPersistenceError as exc:
                errors = list(validation.errors) + [Issue(name, "", "persist_failed", str(exc))]
                return self._finish(ApplyResult(
                    ok=False, section=name, action=action, errors=errors,
                    unknown=sorted(validation.unknown), error=str(exc), settings=self.load()))
        return self._finish(ApplyResult(
            ok=True, section=name, action=action, applied=dict(validation.values),
            errors=list(validation.errors), unknown=sorted(validation.unknown),
            error=validation.errors[0].message if validation.errors else "",
            settings=copy.deepcopy(data)))

    def reset(self, section=None):
        """恢复出厂默认值。section=None / "all" / "*" = 整份重置（连未知键一起清掉）。"""
        if section is None or str(section).strip().lower() in ("all", "*"):
            with self._lock:
                data = defaults()
                data[VERSION_KEY] = SCHEMA_VERSION
                try:
                    self._commit(data)
                except SettingsPersistenceError as exc:
                    return self._finish(ApplyResult(
                        ok=False, action="reset_all", error=str(exc),
                        errors=[Issue("", "", "persist_failed", str(exc))], settings=self.load()))
            return self._finish(ApplyResult(ok=True, action="reset_all",
                                            applied=copy.deepcopy(data),
                                            settings=copy.deepcopy(data)))
        name = resolve_section_name(section)
        if name is None:
            message = f"未知的设置分区：{section}"
            return self._finish(ApplyResult(
                ok=False, section=str(section or ""), action="reset", error=message,
                errors=[Issue(str(section or ""), "", "unknown_section", message)],
                settings=self.load()))
        result = self.apply(name, SECTIONS[name].defaults(), strict=True, partial=False)
        result.action = "reset"
        self.last_result = result
        return result

    # ---- 写（旧接口，签名与返回值保持不变）-----------------------------
    def update(self, section, values):
        """局部更新某个分区（旧接口）。返回整份设置；失败抛 SettingsError。"""
        result = self.apply(section, values, strict=False, partial=True)
        self._raise_if_failed(result, section)
        return copy.deepcopy(result.settings)

    def save_section(self, section, values):
        """整段替换某个分区（旧接口，「恢复默认」在用）。返回整份设置。"""
        result = self.apply(section, values, strict=False, partial=False)
        self._raise_if_failed(result, section)
        return copy.deepcopy(result.settings)

    def save(self, data):
        """整份写入（旧接口）：缺的分区用默认值补齐，非法字段丢弃。"""
        merged = defaults()
        incoming = data if isinstance(data, dict) else {}
        for name, spec in SECTIONS.items():
            payload = self._payload_for(incoming, name)
            if payload is None:
                continue
            result = validate_section(spec, payload, strict=False, partial=True)
            merged[name].update(result.values)
            merged[name].update(result.unknown)
        for key, value in incoming.items():
            if key in SECTIONS or key == VERSION_KEY or resolve_section_name(key):
                continue
            if not is_json_safe(value):
                continue                       # 写不进 JSON 的顶层杂项直接丢掉，不让整份写入失败
            merged[key] = copy.deepcopy(value)
        merged[VERSION_KEY] = SCHEMA_VERSION
        with self._lock:
            self._commit(merged)
        return copy.deepcopy(merged)

    # ---- 迁移 ----------------------------------------------------------
    def migrate(self, force=False, registry=None):
        """把设置文件升到当前 schema 版本。

        正常不需要手动调：load() 发现旧版本就会自动迁移并落盘。
        这个方法留给排查、以及「加了新迁移后主动升级」用；
        它直接读磁盘上的原始内容（不经过已迁移的缓存），所以传自定义 registry 也有意义。
        """
        with self._lock:
            raw, _source, _error = self._read_raw()
            data = self._merge_defaults(raw, [])
            report = _migration.run(data, registry=registry, force=force)
            if report.changed:
                try:
                    self._commit(report.data)
                except SettingsPersistenceError as exc:
                    report.notes.append(str(exc))
                    return report.as_dict()
            self._stored_version = report.to_version
            return report.as_dict()

    # ---- 内部：读写 ----------------------------------------------------
    def _finish(self, result):
        self.last_result = result
        return result

    def _raise_if_failed(self, result, section):
        if result.ok:
            return
        codes = {item.code for item in result.errors}
        if "unknown_section" in codes:
            raise UnknownSectionError(section)
        field_errors = [item for item in result.errors if item.code != "persist_failed"]
        if field_errors:
            raise SettingsValidationError(result.section or section, field_errors)
        raise SettingsPersistenceError(result.error or "设置写入失败")

    def _payload_for(self, incoming, name):
        """从整份数据里取出某个分区的值，别名（asr → transcript）也算。"""
        payload = incoming.get(name)
        if not isinstance(payload, dict):
            payload = None
        for alias, target in alias_map().items():
            if target != name:
                continue
            alias_payload = incoming.get(alias)
            if isinstance(alias_payload, dict):
                payload = dict(payload or {})
                payload.update(alias_payload)
        return payload

    def _disk_stamp(self):
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _read_json(self, path):
        """返回 (ok, data, error)。文件不存在算 (False, None, None)。"""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return False, None, None
        except OSError as exc:
            return False, None, f"读取失败：{exc}"
        try:
            data = json.loads(text)
        except ValueError as exc:
            return False, None, f"不是合法 JSON：{exc}"
        if not isinstance(data, dict):
            return False, None, "顶层不是对象"
        return True, data, None

    def _read_raw(self):
        ok, data, error = self._read_json(self.path)
        if ok:
            return data, "file", None
        if error is None:                      # 文件根本不存在
            return {}, "defaults", None
        ok, backup, backup_error = self._read_json(self.backup_path)
        if ok:
            return backup, "backup", error
        return {}, "defaults", error if backup_error is None else f"{error}；备份也不可用：{backup_error}"

    def _build(self):
        raw, source, error = self._read_raw()
        issues = []
        if source == "backup":
            issues.append(Issue("", "", "recovered_from_backup",
                                f"主设置文件不可用（{error}），已从备份 {self.backup_path.name} 恢复"))
        elif source == "defaults" and error:
            issues.append(Issue("", "", "corrupt_settings_file",
                                f"设置文件无法解析（{error}），本次使用默认值"))

        data = self._merge_defaults(raw, issues)
        report = _migration.run(data)
        data = report.data
        if report.future_version:
            for note in report.notes:
                issues.append(Issue("", VERSION_KEY, "future_schema_version", note))
        else:
            for note in report.notes:
                issues.append(Issue("", VERSION_KEY, "migration_incomplete", note))
        self._normalize(data, issues)

        self._cache = data
        self._issues = issues
        self._stamp = self._disk_stamp()
        self._stored_version = self._persist_migration(report, source)
        return self._cache

    def _persist_migration(self, report, source):
        """把迁移结果写回磁盘，并返回「现在磁盘上是哪个版本」。

        两种情况**不写**：
        - 版本比程序新：读不懂就别动，免得覆盖新版本的数据；
        - 文件本来就不存在 / 读不出来：内存里就是当前版本的默认值，
          没必要因为「读一次」就在用户磁盘上凭空造一个文件。
        """
        if report.future_version:
            return report.from_version
        if not report.changed or source != "file":
            return SCHEMA_VERSION
        try:
            self._write(report.data)
        except SettingsPersistenceError as exc:
            self._issues.append(Issue("", VERSION_KEY, "migration_not_saved", str(exc)))
            return report.from_version
        self._stamp = self._disk_stamp()
        return report.to_version

    def _merge_defaults(self, raw, issues):
        """默认值打底，再把文件里的值盖上去。未知键一律保留（旧文件兼容）。"""
        data = defaults()
        for key, value in (raw or {}).items():
            if key == VERSION_KEY:
                data[key] = value
                continue
            name = key if key in SECTIONS else resolve_section_name(key)
            if name is None:
                data[key] = copy.deepcopy(value)          # 顶层未知键：原样留着
                continue
            if not isinstance(value, dict):
                issues.append(Issue(name, "", "invalid_section_payload",
                                    f"{name} 在设置文件里不是对象，已忽略"))
                continue
            if name != key:
                issues.append(Issue(name, "", "alias_section",
                                    f"设置文件里用了别名分区 {key}，已并入 {name}"))
            data[name].update(copy.deepcopy(value))
        return data

    def _normalize(self, data, issues):
        """把文件里的值按 schema 归一化（"30" → 30）。

        非法值**原样保留**并记一条 issues —— 加载不是写入，
        没道理因为一个字段看不懂就把用户的整份配置重置掉。
        """
        for name, spec in SECTIONS.items():
            bucket = data.get(name)
            if not isinstance(bucket, dict):
                continue
            for key, item in spec.field_map.items():
                if key not in bucket:
                    continue
                value = bucket[key]
                if item.secret and is_blank(value):
                    continue
                ok, coerced, message = normalize_value(item, value)
                if ok:
                    bucket[key] = coerced
                else:
                    issues.append(Issue(name, key, "invalid_persisted_value", message, value))

    def _commit(self, data):
        """写盘 + 更新缓存。写失败时缓存不动、磁盘不动。"""
        if (self._stored_version or 1) > SCHEMA_VERSION:
            raise SettingsPersistenceError(
                f"设置文件版本（{self._stored_version}）高于本程序支持的 {SCHEMA_VERSION}，"
                "已拒绝写入以免覆盖新版本的数据")
        self._write(data)
        self._cache = copy.deepcopy(data)
        self._stamp = self._disk_stamp()
        self._stored_version = SCHEMA_VERSION

    def _dump(self, data):
        try:
            return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SettingsPersistenceError(f"设置内容无法序列化：{exc}") from exc

    def _write(self, data):
        text = self._dump(data)
        tmp = self.tmp_path
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())       # 先落盘再替换，掉电也不会留半截文件
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise SettingsPersistenceError(f"设置写入失败：{exc}") from exc
        self._write_backup(text)

    def _write_backup(self, text):
        """留一份上一版好文件。备份失败不影响主流程。"""
        tmp = self.backup_path.with_name(self.backup_path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(tmp, self.backup_path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def __repr__(self):
        return f"<{type(self).__name__} path={self.path} schema={SCHEMA_VERSION}>"


class FactorySettings(SettingsService):
    """第一阶段的类名（老代码、老测试都在用）。行为与 SettingsService 完全一致。"""


__all__ = ["ApplyResult", "FactorySettings", "SettingsError", "SettingsPersistenceError",
           "SettingsService", "SettingsValidationError", "UnknownSectionError",
           "app_data_root"]
