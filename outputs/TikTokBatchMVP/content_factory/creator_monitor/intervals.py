"""采集节奏的纯函数：poll_interval 解析 / 优先级权重 / 时间戳换算。

为什么单独一个模块：这三件事是「定时检查」的全部数学部分，也是最容易被
各处重复实现（并各自写错）的部分 —— 界面填的是 "30 分钟" / "1 小时"，
库里存的是文本，调度要的是秒。放在这里，任何地方只有一种解释。

只用本地时区的 "%Y-%m-%d %H:%M:%S" 文本，与 factory_store.now_text() 完全一致：
内容库里所有时间字段都是这个格式，混用两套格式以后对不上。
"""

import datetime
import re

STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# 界面上可选的节奏档位（与设置页「采集间隔」一致）
INTERVAL_CHOICES = (
    ("15 分钟", 900),
    ("30 分钟", 1800),
    ("1 小时", 3600),
    ("2 小时", 7200),
    ("6 小时", 21600),
    ("12 小时", 43200),
    ("24 小时", 86400),
)

# 太短会把 TikTok 惹毛，太长等于没有监控；两头都夹住而不是报错。
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 7 * 86400
DEFAULT_INTERVAL_SECONDS = 3600

PRIORITY_ORDER = ("高", "中", "低")
DEFAULT_PRIORITY = "中"

_UNIT_SECONDS = {
    "秒": 1, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "分钟": 60, "分": 60, "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "小时": 3600, "时": 3600, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "天": 86400, "d": 86400, "day": 86400, "days": 86400,
}

_NUMBER_UNIT = re.compile(r"^(\d+(?:\.\d+)?)\s*([A-Za-z\u4e00-\u9fa5]*)$")


def clamp_seconds(value):
    return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, int(value)))


def parse_interval(value, default=DEFAULT_INTERVAL_SECONDS):
    """把用户/界面里的各种写法统一成秒。

    认得的写法：3600 / "3600" / "60 秒" / "30分钟" / "30m" / "1 小时" / "1h" /
    "1.5 小时" / "1 天"。认不出来一律回默认值 —— 采集节奏宁可用默认值，
    也不能因为一个空字符串就让 Creator 永远不被检查。
    """
    if value is None or value == "":
        return clamp_seconds(default)
    if isinstance(value, bool):
        return clamp_seconds(default)
    if isinstance(value, (int, float)):
        return clamp_seconds(value)
    text = str(value).strip().lower().replace("个", "")
    if not text:
        return clamp_seconds(default)
    match = _NUMBER_UNIT.match(text)
    if not match:
        return clamp_seconds(default)
    amount = float(match.group(1))
    unit = match.group(2).strip()
    factor = _UNIT_SECONDS.get(unit) if unit else None
    if factor is None:
        # 只有数字没有单位：大于 300 当秒，否则当分钟（"30" 显然是 30 分钟）
        factor = 1 if amount > 300 else 60
    return clamp_seconds(amount * factor)


def format_interval(seconds):
    """秒 -> 界面上显示的中文文本（"30 分钟" / "1 小时" / "2 天"）。"""
    seconds = clamp_seconds(seconds)
    for text, preset in INTERVAL_CHOICES:
        if preset == seconds:
            return text
    if seconds % 86400 == 0:
        return f"{seconds // 86400} 天"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def normalize_priority(value, default=DEFAULT_PRIORITY):
    """优先级归一化：认得「高/中/低」以及 high/medium/low、1/2/3。"""
    text = str(value or "").strip().lower()
    if text in ("高", "high", "h", "1", "urgent", "p0"):
        return "高"
    if text in ("低", "low", "l", "3", "p2"):
        return "低"
    if text in ("中", "medium", "normal", "mid", "2", "p1"):
        return "中"
    return default


def priority_weight(value):
    """排序权重：数字越小越先检查（高=0 / 中=1 / 低=2）。"""
    try:
        return PRIORITY_ORDER.index(normalize_priority(value))
    except ValueError:                                  # pragma: no cover - 归一化已兜底
        return 1


def now_text(moment=None):
    return (moment or datetime.datetime.now()).strftime(STAMP_FORMAT)


def stamp(value=None):
    """把「现在」归一成时间戳文本。

    调度逻辑要能被测试用假时钟驱动，所以时间必须从外面传进来；
    但调用方可能给 datetime，也可能给字符串（甚至什么都不给）。
    """
    if isinstance(value, datetime.datetime):
        return now_text(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return now_text()


def parse_stamp(value):
    """文本 -> datetime；解析不出来返回 None（而不是抛异常）。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (STAMP_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def shift(stamp, seconds):
    """某个时间戳 + N 秒后的新时间戳（文本进、文本出）。"""
    base = parse_stamp(stamp) or datetime.datetime.now()
    return now_text(base + datetime.timedelta(seconds=int(seconds)))


def is_due(next_check_at, now=None):
    """next_check_at 允许为空 —— 空的含义是「从没检查过，立刻可查」。"""
    moment = parse_stamp(next_check_at)
    if moment is None:
        return True
    return moment <= (now or datetime.datetime.now())


def seconds_until(stamp, now=None):
    moment = parse_stamp(stamp)
    if moment is None:
        return 0
    return int((moment - (now or datetime.datetime.now())).total_seconds())
