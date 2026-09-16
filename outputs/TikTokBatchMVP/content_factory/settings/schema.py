"""Settings Core 的结构定义：一个设置项长什么样、什么值是合法的。

这一层只有「结构」，没有磁盘、没有缓存、没有业务：

- `Field`   ：单个设置项（类型 / 默认值 / 范围 / 枚举 / 是否敏感 / 是否可选）
- `Section` ：一组设置项，对应界面上的一个设置子页，或一个引擎模块的配置域
- `SCHEMA_VERSION`：设置文件的版本号，迁移用（见 migration.py）

为什么结构要单独写出来：默认值、校验、文档、reset、迁移、以后给界面生成表单，
都必须从同一份定义出发。散落各处的 if 判断迟早会漂移 ——
「校验说不能填 0，默认值却是 0」这类矛盾靠人工 review 是拦不住的。

注意：这里**只**描述配置，不实现任何业务。
`collector.poll_interval_minutes` 怎么用是采集模块的事，
Settings Core 只保证它被定义、被校验、被正确存取。
"""

import copy
from dataclasses import dataclass
from typing import Any, Optional, Tuple

# ---- 类型 ---------------------------------------------------------------
# 用普通字符串而不是 Enum：描述信息要直接进 JSON（describe() 给界面/其他 Agent 用），
# 字符串也让 schema 定义读起来更直白。
BOOL = "bool"
INT = "int"
FLOAT = "float"
NUMBER = "number"          # int 或 float，按输入形态保留
STRING = "string"
ENUM = "enum"              # 取值必须在 choices 里
LIST = "list"              # 元素类型由 item_type 决定
DICT = "dict"
ANY = "any"                # 不做类型约束（只要求能 JSON 序列化）

TYPES = (BOOL, INT, FLOAT, NUMBER, STRING, ENUM, LIST, DICT, ANY)

# 冒号左边是「写法」，右边是「内部规范名」：schema 里写 boolean / integer 也能跑，
# 需求文档里用的就是这组叫法。
TYPE_ALIASES = {
    "boolean": BOOL,
    "bool": BOOL,
    "integer": INT,
    "int": INT,
    "float": FLOAT,
    "double": FLOAT,
    "number": NUMBER,
    "string": STRING,
    "str": STRING,
    "text": STRING,
    "enum": ENUM,
    "choice": ENUM,
    "list": LIST,
    "array": LIST,
    "dict": DICT,
    "object": DICT,
    "any": ANY,
    "json": ANY,
}

# 设置文件当前的 schema 版本。改动字段语义 / 新增必填项时 +1，
# 并在 migration.py 里补一条从旧版本升级的迁移。
SCHEMA_VERSION = 2

# 版本号在文件里的键名。它不是「分区」，是文件级元数据。
VERSION_KEY = "schema_version"

# 未知字段策略
UNKNOWN_KEEP = "keep"      # 保留（兼容旧文件 / 界面多传的字段）
UNKNOWN_REJECT = "reject"  # 拒绝（严格的新分区用）


def normalize_type(value: str) -> str:
    """把 schema 里写的类型名归一化成内部规范名。"""
    name = TYPE_ALIASES.get(str(value or "").strip().lower())
    if name is None:
        raise ValueError(f"未知的设置类型：{value!r}，可选：{sorted(set(TYPE_ALIASES))}")
    return name


@dataclass(frozen=True)
class Field:
    """一个设置项的定义。

    key          字段名（settings.<section>.<key>）
    type         见上面的类型常量
    default      出厂默认值（敏感字段也放真默认值，public() 会屏蔽）
    minimum/maximum  数值范围（含端点）；只有 int/float/number 用
    choices      enum 的候选值（元组，顺序即界面顺序）
    item_type    list 的元素类型
    secret      敏感字段：public() 只回「是否已设置」，永不回明文；
                局部更新时空值表示「保持原值」
    public_flag secret 字段在 public() 里的「已设置」标记名（界面契约，不能乱改）
    optional     允许空值（None / 空字符串）
    description  给界面和文档用的一句话说明
    """

    key: str
    type: str = ANY
    default: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Optional[Tuple[Any, ...]] = None
    item_type: str = STRING
    secret: bool = False
    public_flag: str = ""
    optional: bool = False
    description: str = ""
    # list 字段是否接受 "a,b" / 换行分隔的字符串（兼容界面与旧文件）
    split_string: bool = True

    def __post_init__(self):
        object.__setattr__(self, "type", normalize_type(self.type))
        object.__setattr__(self, "item_type", normalize_type(self.item_type))
        if self.type == ENUM and not self.choices:
            raise ValueError(f"{self.key}: enum 必须给 choices")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"{self.key}: minimum 不能大于 maximum")

    @property
    def flag(self) -> str:
        """public() 里这个敏感字段的「已设置」标记名。"""
        if self.public_flag:
            return self.public_flag
        head, *tail = self.key.split("_")
        return head + "".join(part[:1].upper() + part[1:] for part in tail) + "Set"

    def describe(self) -> dict:
        """给界面 / 其他 Agent 看的机器可读描述（永远不回敏感字段的默认值）。"""
        return {
            "key": self.key,
            "type": self.type,
            "default": None if self.secret else copy.deepcopy(self.default),
            "optional": bool(self.optional),
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices) if self.choices else None,
            "itemType": self.item_type if self.type == LIST else None,
            "secret": bool(self.secret),
            "description": self.description,
        }


@dataclass(frozen=True)
class Section:
    """一组设置项。name 就是落盘时的 JSON 键，也是桥接层的 section 名。"""

    name: str
    title: str
    fields: Tuple[Field, ...]
    description: str = ""
    aliases: Tuple[str, ...] = ()
    unknown: str = UNKNOWN_KEEP
    # 这个分区是不是「界面设置页」：界面只认既有分区名，新引擎分区先给代码用
    ui_section: bool = True

    @property
    def field_map(self) -> dict:
        return {item.key: item for item in self.fields}

    def field(self, key: str) -> Optional[Field]:
        return self.field_map.get(str(key))

    def defaults(self) -> dict:
        """一份全新的默认值（深拷贝，调用方随便改）。"""
        return {item.key: copy.deepcopy(item.default) for item in self.fields}

    def describe(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "aliases": list(self.aliases),
            "unknownFields": self.unknown,
            "uiSection": self.ui_section,
            "fields": [item.describe() for item in self.fields],
        }


# 规范分区名与别名表：由 defaults.py 在定义好分区后调用 register_sections() 回填。
# 放在这里而不是写死在 schema 里，是为了让 schema.py 只依赖自己（不 import 具体分区）。
SECTION_NAMES = {}
_ALIASES = {}
_LOWER_NAMES = {}


def register_sections(sections) -> None:
    """登记全部分区（defaults.py 调用一次）。重复登记直接覆盖，方便测试。"""
    for section in sections:
        SECTION_NAMES[section.name] = section
        _LOWER_NAMES[section.name.lower()] = section.name
        for alias in section.aliases:
            _ALIASES[str(alias).strip().lower()] = section.name


def alias_map() -> dict:
    return dict(_ALIASES)


def resolve_section_name(name: Any) -> Optional[str]:
    """把 section 名（含别名，如 asr → transcript）解析成规范名；未知返回 None。

    大小写不敏感：`"AI"` / `"Collector"` 这类写法也认（少一类没意义的坑）。
    """
    text = str(name or "").strip()
    if not text:
        return None
    if text in SECTION_NAMES:
        return text
    lowered = text.lower()
    if lowered in _LOWER_NAMES:
        return _LOWER_NAMES[lowered]
    return _ALIASES.get(lowered)
