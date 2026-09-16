"""设置的校验与归一化：把「界面 / 其他模块递过来的值」变成「可以落盘的值」。

规则只有三条，但每条都有明确的取舍：

1. **类型要严，入口要宽。** 界面上的数字框在用户输错时会回传字符串，
   `two_factor` 这种开关回传的是 "on"/"off"。这些都不是脏数据，
   能明确还原的就还原（"30" → 30），还原不了的才判非法
   （"abc" 不是整数 → 非法）。
2. **范围是硬约束。** `poll_interval_minutes ≥ 1`、`retry_count ≥ 0`、
   `concurrency ≥ 1` —— 越界一律拒绝，不做静默截断。静默截断会让
   用户以为存进去了，实际是另一个值。
3. **非法值不得污染已有配置。** 严格模式下只要有错就整体拒绝、一个字都不写；
   宽松模式下丢弃出错的字段，其余照常写入（旧调用方的兼容路径）。

校验结果分三份：`values`（通过的值）、`errors`（拒绝的值 + 原因）、
`unknown`（schema 里没定义的键）。为什么把 unknown 单列：界面的「基础设置」页
会把 work_mode / logging 的字段一起传上来，它们不在 general 的 schema 里，
但必须能存住（界面不能改，旧文件里也有它们）。
"""

import json
import math
from dataclasses import dataclass, field as _field
from typing import Any

from .schema import (ANY, BOOL, DICT, ENUM, FLOAT, INT, LIST, NUMBER, STRING,
                     UNKNOWN_REJECT, Field, Section, normalize_type)

# Python 类型名 → schema 类型名（enum 候选值的类型推断用；认不出来就跳过该候选）
_PY_TYPES = {"bool": BOOL, "int": INT, "float": FLOAT, "str": STRING}

# 「空」的定义：None 与纯空白字符串。字符串字段本身允许空串（很多默认值就是 ""），
# 只有 optional / secret 字段才把空值当成「没填」。
_TRUE_WORDS = {"true", "1", "yes", "y", "on", "t", "是", "开", "启用", "已开启"}
_FALSE_WORDS = {"false", "0", "no", "n", "off", "f", "否", "关", "停用", "未开启"}


@dataclass(frozen=True)
class Issue:
    """一条校验问题。code 是机器判据，message 是给人看的（界面可直接显示）。"""

    section: str
    field: str
    code: str
    message: str
    value: Any = None

    def as_dict(self) -> dict:
        return {"section": self.section, "field": self.field, "code": self.code,
                "message": self.message, "value": self.value}


@dataclass
class ValidationResult:
    """一次分区校验的结果。`ok` 只看 errors —— 宽松模式下服务层会丢弃出错字段、
    继续写其余字段，所以「ok=False」不等于「什么都没做」，看 ApplyResult。"""

    section: str
    values: dict = _field(default_factory=dict)     # 通过校验并归一化后的值
    errors: list = _field(default_factory=list)     # [Issue]
    unknown: dict = _field(default_factory=dict)    # schema 外的键（原样保留）
    strict: bool = True
    partial: bool = True

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"ok": self.ok, "section": self.section, "strict": self.strict,
                "partial": self.partial, "values": dict(self.values),
                "unknown": sorted(self.unknown), "errors": [i.as_dict() for i in self.errors]}


def is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def is_json_safe(value) -> bool:
    """能不能安全地写进 JSON（拒绝 set / bytes / NaN / Infinity 这类）。"""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


# ---- 单值归一化 ---------------------------------------------------------

def _to_bool(value):
    if isinstance(value, bool):
        return True, value, ""
    if isinstance(value, int) and value in (0, 1):
        return True, bool(value), ""
    if isinstance(value, float) and value in (0.0, 1.0):
        return True, bool(int(value)), ""
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True, True, ""
        if word in _FALSE_WORDS:
            return True, False, ""
    return False, None, f"必须是布尔值（true/false），收到 {value!r}"


def _to_int(value):
    if isinstance(value, bool):
        return False, None, f"必须是整数，收到布尔值 {value!r}"
    if isinstance(value, int):
        return True, value, ""
    if isinstance(value, float):
        if value.is_integer():
            return True, int(value), ""
        return False, None, f"必须是整数，收到小数 {value!r}"
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            return False, None, f"必须是整数，收到 {value!r}"
        if not math.isfinite(number):
            return False, None, f"必须是整数，收到 {value!r}"
        if number.is_integer():
            return True, int(number), ""
        return False, None, f"必须是整数，收到小数 {value!r}"
    return False, None, f"必须是整数，收到 {type(value).__name__}"


def _to_float(value):
    if isinstance(value, bool):
        return False, None, f"必须是数字，收到布尔值 {value!r}"
    if isinstance(value, (int, float)):
        return True, float(value), ""
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return False, None, f"必须是数字，收到 {value!r}"
        if not math.isfinite(number):
            return False, None, f"必须是数字，收到 {value!r}"
        return True, number, ""
    return False, None, f"必须是数字，收到 {type(value).__name__}"


def _to_number(value):
    """int 与 float 都收；整数值保持 int，避免把 3 写成 3.0。"""
    if isinstance(value, bool):
        return False, None, f"必须是数字，收到布尔值 {value!r}"
    if isinstance(value, int):
        return True, value, ""
    return _to_float(value)


def _to_string(value):
    if isinstance(value, str):
        return True, value, ""
    if isinstance(value, bool):
        return False, None, f"必须是字符串，收到布尔值 {value!r}"
    if isinstance(value, (int, float)):
        return True, str(value), ""
    return False, None, f"必须是字符串，收到 {type(value).__name__}"


def _to_list(field: Field, value):
    if isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return True, [], ""
        if not field.split_string:
            return False, None, f"必须是数组，收到字符串 {value!r}"
        parts = [part.strip() for part in text.replace("\r", "\n").replace(",", "\n").split("\n")]
        items = [part for part in parts if part]
    else:
        return False, None, f"必须是数组，收到 {type(value).__name__}"

    item_type = normalize_type(field.item_type)
    out = []
    for index, item in enumerate(items):
        ok, coerced, message = _normalize_scalar(item_type, item)
        if not ok:
            return False, None, f"第 {index + 1} 个元素不合法：{message}"
        out.append(coerced)
    return True, out, ""


def _normalize_scalar(ftype, value):
    if ftype == BOOL:
        return _to_bool(value)
    if ftype == INT:
        return _to_int(value)
    if ftype == FLOAT:
        return _to_float(value)
    if ftype == NUMBER:
        return _to_number(value)
    if ftype == STRING:
        return _to_string(value)
    if ftype == DICT:
        if isinstance(value, dict):
            return True, value, ""
        return False, None, f"必须是对象，收到 {type(value).__name__}"
    if ftype == ANY:
        if is_json_safe(value):
            return True, value, ""
        return False, None, f"值无法写入 JSON：{value!r}"
    return False, None, f"未知类型 {ftype!r}"


def _match_choice(field: Field, value):
    """enum 候选匹配：先按同类型全等，再按「能还原成候选类型」比较。

    这样 history_days 写 90（数字）和 "90"（界面上某些 select 回传字符串）都算命中，
    而 True / "true" 不会误配到 1。
    """
    choices = tuple(field.choices or ())
    for choice in choices:
        if type(choice) is type(value) and choice == value:
            return True, choice
    for choice in choices:
        ftype = _PY_TYPES.get(type(choice).__name__)
        if ftype is None:
            continue
        ok, coerced, _ = _normalize_scalar(ftype, value)
        if ok and coerced == choice and type(coerced) is type(choice):
            return True, choice
    return False, None


def normalize_value(field: Field, value):
    """按 field 的定义归一化一个值。返回 (ok, 归一化后的值, 失败原因)。"""
    if field.optional and is_blank(value):
        # 可选字段：空就是空。字符串统一成 ""（与默认值同型），其余类型统一成 None。
        return True, ("" if field.type == STRING else None), ""

    ftype = field.type
    if ftype == ENUM:
        ok, matched = _match_choice(field, value)
        if not ok:
            return False, None, f"取值必须是 {' / '.join(str(c) for c in field.choices)} 之一，收到 {value!r}"
        return True, matched, ""

    if ftype == LIST:
        ok, coerced, message = _to_list(field, value)
    else:
        ok, coerced, message = _normalize_scalar(ftype, value)
    if not ok:
        return False, None, message

    if field.minimum is not None or field.maximum is not None:
        if not isinstance(coerced, (int, float)) or isinstance(coerced, bool):
            return False, None, f"{field.key} 不支持数值范围校验（类型是 {ftype}）"
        if field.minimum is not None and coerced < field.minimum:
            return False, None, f"{field.key} 不能小于 {_num(field.minimum)}（收到 {_num(coerced)}）"
        if field.maximum is not None and coerced > field.maximum:
            return False, None, f"{field.key} 不能大于 {_num(field.maximum)}（收到 {_num(coerced)}）"
    return True, coerced, ""


def _num(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# ---- 分区校验 -----------------------------------------------------------

def validate_section(section: Section, values, *, strict: bool = True,
                     partial: bool = True) -> ValidationResult:
    """校验一个分区的局部/整体更新。

    strict=True  ：schema 外的键也算错误（新的引擎分区用这个）。
    strict=False ：schema 外的键原样保留（兼容旧界面与旧文件）。
    partial=True ：局部更新 —— 敏感字段传空值表示「保持原值」，不报错。
    """
    result = ValidationResult(section=section.name, strict=bool(strict), partial=bool(partial))
    if values is None:
        return result
    if not isinstance(values, dict):
        result.errors.append(Issue(section.name, "", "invalid_payload",
                                   f"{section.name} 的设置值必须是「字段 → 值」的字典"))
        return result

    field_map = section.field_map
    for key, value in values.items():
        name = str(key)
        spec = field_map.get(name)
        if spec is None:
            if not is_json_safe(value):
                result.errors.append(Issue(section.name, name, "not_serializable",
                                           f"{name} 的值无法写入 JSON：{value!r}", value))
                continue
            result.unknown[name] = value
            if strict or section.unknown == UNKNOWN_REJECT:
                result.errors.append(Issue(section.name, name, "unknown_field",
                                           f"未知的设置项：{section.name}.{name}", value))
            continue

        if partial and spec.secret and is_blank(value):
            continue                                  # 空密钥 = 保持原值
        if not is_json_safe(value):
            result.errors.append(Issue(section.name, name, "not_serializable",
                                       f"{name} 的值无法写入 JSON：{value!r}", value))
            continue

        ok, coerced, message = normalize_value(spec, value)
        if not ok:
            result.errors.append(Issue(section.name, name, "invalid_value", message, value))
            continue
        result.values[name] = coerced
    return result


def audit_section(section: Section, stored) -> list:
    """体检：已落盘的值是否符合当前 schema（只报问题，不改数据）。

    加载时的策略是「不破坏用户数据」——坏值原样留着、这里报出来；
    写入时的策略是「非法值一个都不写」。
    """
    issues = []
    if not isinstance(stored, dict):
        return [Issue(section.name, "", "invalid_section_payload",
                      f"{section.name} 在设置文件里不是对象，已按默认值处理")]
    field_map = section.field_map
    for key, value in stored.items():
        spec = field_map.get(key)
        if spec is None:
            continue                                  # 未知键允许存在（旧文件 / 界面怪癖）
        ok, _coerced, message = normalize_value(spec, value)
        if not ok:
            issues.append(Issue(section.name, key, "invalid_persisted_value", message, value))
    return issues


def coercion_ok(field: Field, value) -> bool:
    """这个值能不能按 field 的定义归一化（只问道理，不取值）。"""
    return normalize_value(field, value)[0]


__all__ = ["Issue", "ValidationResult", "audit_section", "coercion_ok", "is_blank",
           "is_json_safe", "normalize_value", "validate_section"]
