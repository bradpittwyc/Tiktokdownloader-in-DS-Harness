"""设置文件的版本与迁移。

为什么需要它：设置是**长期活在用户磁盘上**的数据。今天写的字段，
半年后可能改名、拆分区、换语义（比如 storage.file_types 从
"mp4,mov" 字符串变成数组）。没有版本号，程序只能靠猜；猜错就是
「用户的配置被悄悄重置」。

设计：
- 版本号存在文件顶层 `schema_version`（见 schema.VERSION_KEY）。
- 一个 Migration 负责把版本 N 的文件升到 N+1。
- `run()` 从文件里的版本一路加到当前版本，只做加法，绝不回头。
- 版本比程序还新（用户装了更新的版本又退回旧版）→ 不迁移、不覆写，
  只标记 `future_version`，让调用方知道"这份文件我读不懂，别乱动"。
"""

import copy
from dataclasses import dataclass, field as _field
from typing import Callable, Dict, List, Optional

from .schema import SCHEMA_VERSION, VERSION_KEY


@dataclass(frozen=True)
class Migration:
    """把 version 版的文件升到 version + 1。apply(data) 必须返回新的 dict。"""

    version: int
    name: str
    description: str
    apply: Callable[[dict], dict]


@dataclass
class MigrationReport:
    from_version: int
    to_version: int
    applied: List[str] = _field(default_factory=list)
    changed: bool = False
    future_version: bool = False
    data: dict = _field(default_factory=dict)
    notes: List[str] = _field(default_factory=list)

    def as_dict(self) -> dict:
        return {"fromVersion": self.from_version, "toVersion": self.to_version,
                "applied": list(self.applied), "changed": self.changed,
                "futureVersion": self.future_version, "notes": list(self.notes)}


def detect_version(data) -> int:
    """读出文件里的 schema 版本。没有版本号的老文件 = 版本 1。"""
    if not isinstance(data, dict):
        return 1
    raw = data.get(VERSION_KEY)
    if isinstance(raw, bool) or raw is None:
        return 1
    try:
        version = int(raw)
    except (TypeError, ValueError):
        return 1
    return version if version >= 1 else 1


def _v1_to_v2(data: dict) -> dict:
    """1 → 2：补上版本号；storage.file_types 从 "mp4,mov" 字符串统一成数组。

    第一阶段就是「没有版本号的扁平 JSON」，所以这条迁移是基线迁移：
    它同时是后来者的模板 —— 新迁移照这个写，注册进 MIGRATIONS 即可。
    """
    data = copy.deepcopy(data)
    storage = data.get("storage")
    if isinstance(storage, dict):
        file_types = storage.get("file_types")
        if isinstance(file_types, str):
            storage["file_types"] = [part.strip() for part in file_types.split(",") if part.strip()]
    data[VERSION_KEY] = 2
    return data


#: 版本 N → 升到 N+1 的迁移。新增迁移只管往这里加一条，别改历史条目。
MIGRATIONS: Dict[int, Migration] = {
    1: Migration(version=1, name="v1-to-v2",
                 description="补上 schema_version；storage.file_types 统一成数组",
                 apply=_v1_to_v2),
}


def register_migration(migration: Migration, registry: Optional[Dict[int, Migration]] = None) -> None:
    """登记一条迁移（主要给将来的模块与测试用）。"""
    target = MIGRATIONS if registry is None else registry
    if migration.version < 1:
        raise ValueError("迁移版本必须 >= 1")
    target[migration.version] = migration


def run(data, registry: Optional[Dict[int, Migration]] = None,
        force: bool = False) -> MigrationReport:
    """把 data 迁移到当前 schema 版本。

    force=True 时即使版本号已经是当前版本也会照常返回（目前无副作用，留给排查用）。
    """
    table = MIGRATIONS if registry is None else registry
    current = copy.deepcopy(data) if isinstance(data, dict) else {}
    version = detect_version(current)
    report = MigrationReport(from_version=version, to_version=version, data=current)

    if version > SCHEMA_VERSION:
        report.future_version = True
        report.to_version = version
        report.notes.append(
            f"设置文件的版本（{version}）比本程序支持的版本（{SCHEMA_VERSION}）更新，"
            "已按原样读取、不做迁移，也不会覆写它")
        return report

    guard = 0
    while version < SCHEMA_VERSION:
        migration = table.get(version)
        if migration is None:
            report.notes.append(f"缺少 {version} → {version + 1} 的迁移，停在版本 {version}")
            break
        current = migration.apply(current)
        if not isinstance(current, dict):
            raise TypeError(f"迁移 {migration.name} 必须返回 dict")
        current[VERSION_KEY] = version + 1
        report.applied.append(migration.name)
        version += 1
        report.changed = True
        guard += 1
        if guard > 1000:                                    # 防御：迁移表被写成环
            raise RuntimeError("迁移链过长，疑似循环")
    report.to_version = version
    report.data = current
    return report


__all__ = ["MIGRATIONS", "Migration", "MigrationReport", "detect_version",
           "register_migration", "run"]
