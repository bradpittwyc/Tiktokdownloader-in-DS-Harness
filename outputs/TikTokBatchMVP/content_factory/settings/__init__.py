"""Content Factory 的统一设置基础设施（Settings Core）。

职责边界（谁负责什么，写清楚免得别的模块重复造）：

    定义 / 默认值 / Schema / 校验 / 落盘 / 读取 / 重置 / 版本与迁移   ← 本模块
    配置怎么用（轮询、转写、发布、上传……）                          ← 各业务模块
    密钥的归属、加密、轮换                                          ← Provider / Secrets 模块

快速用法：

    from content_factory.settings import FactorySettings, SECTIONS, DEFAULTS

    settings = FactorySettings()                  # 默认 %LOCALAPPDATA%/TikTokBatchMVP
    settings.section("collector")                 # 一个分区（带默认值，未知分区返回 {}）
    settings.get("collector", "poll_interval_minutes", 30)

    result = settings.apply("collector", {"poll_interval_minutes": 15})   # 严格校验 + 原子落盘
    result.ok, result.errors, result.settings

    settings.validate("collector", {"poll_interval_minutes": 0}).errors   # 只校验不写
    settings.reset("collector")                    # 恢复该分区默认值
    settings.public()                              # 给界面的视图：密钥只回「是否已设置」
    settings.describe()                            # 机器可读的 schema（类型/范围/默认值）

旧接口（`update` / `save_section` / `save` / `public` / `load`）签名与返回值保持不变，
第一阶段的调用方（content_bridge、ai_enrichment、pipeline）不需要任何改动。
"""

from .defaults import (DEFAULTS, ENGINE_SECTIONS, SECTIONS, SECTIONS_LIST, UI_SECTIONS,
                       section_defaults)
# 注意：函数叫 default_values，不叫 defaults —— 后者是子模块名（content_factory.settings.defaults），
# 同名导出会把子模块从包属性上盖掉，让 `import content_factory.settings.defaults as d` 拿到函数。
from .defaults import defaults as default_values
from .migration import (MIGRATIONS, Migration, MigrationReport, detect_version,
                        register_migration)
from .schema import (SCHEMA_VERSION, VERSION_KEY, Field, Section, alias_map,
                     normalize_type, register_sections, resolve_section_name)
from .service import (ApplyResult, FactorySettings, SettingsError, SettingsPersistenceError,
                      SettingsService, SettingsValidationError, UnknownSectionError,
                      app_data_root)
from .validators import Issue, ValidationResult, audit_section, normalize_value, validate_section

__all__ = [
    # 读写
    "SettingsService", "FactorySettings", "ApplyResult", "app_data_root",
    # 结构
    "SECTIONS", "SECTIONS_LIST", "UI_SECTIONS", "ENGINE_SECTIONS", "DEFAULTS",
    "default_values", "section_defaults", "Field", "Section", "SCHEMA_VERSION", "VERSION_KEY",
    "normalize_type", "resolve_section_name", "alias_map", "register_sections",
    # 校验
    "Issue", "ValidationResult", "validate_section", "normalize_value", "audit_section",
    # 迁移
    "MIGRATIONS", "Migration", "MigrationReport", "detect_version", "register_migration",
    # 异常
    "SettingsError", "SettingsPersistenceError", "SettingsValidationError", "UnknownSectionError",
]
