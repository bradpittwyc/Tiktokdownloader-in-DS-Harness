"""兼容层：设置实现已拆到 `content_factory/settings/` 包，这里保留原有导入路径。

第一阶段这个文件里有全部默认值和 FactorySettings；现在默认值、schema、校验、
迁移、落盘分别住在 settings/ 包里，**对外名字一个没变**：

    from content_factory.settings_store import DEFAULTS, FactorySettings, app_data_root

新代码请直接用 `content_factory.settings`（同一批对象，多出 apply / reset /
describe / audit / migrate 等能力）。这个文件只做转发，不要往这里加逻辑。
"""

from .settings import (  # noqa: F401
    DEFAULTS,
    SECTIONS,
    SCHEMA_VERSION,
    ApplyResult,
    FactorySettings,
    Issue,
    Migration,
    MigrationReport,
    Section,
    SettingsError,
    SettingsPersistenceError,
    SettingsService,
    SettingsValidationError,
    UnknownSectionError,
    app_data_root,
    section_defaults,
    validate_section,
)
from .settings import default_values as defaults  # noqa: F401

__all__ = [
    "DEFAULTS", "SECTIONS", "SCHEMA_VERSION", "ApplyResult", "FactorySettings",
    "Issue", "Migration", "MigrationReport", "Section", "SettingsError",
    "SettingsPersistenceError", "SettingsService", "SettingsValidationError",
    "UnknownSectionError", "app_data_root", "defaults", "section_defaults", "validate_section",
]
