"""重试策略：最大尝试次数 + 指数退避 + 可重试判定。

为什么单独一个模块而不是塞进 queue.py：
「重试几次、隔多久、什么错不该重试」是业务决策，队列只管执行。
分开之后：
- 每个阶段（download / transcript / enrich）可以有不同的策略；
- 策略是纯函数 + 不可变数据，单测不用起线程、不用等真实退避时间；
- 以后从设置页读参数，只需要改 `RetryPolicy.from_settings`。

术语（重要，避免歧义）：
    attempts     = 已经**开始**过的次数（认领任务时就 +1，中途崩溃也算一次）
    max_attempts = 总尝试次数上限。max_attempts=3 表示「第一次 + 两次重试」。
                   所以 max_attempts=1 等于「不重试」。

退避公式：delay = min(max_delay, base_delay * factor ** (attempts - 1))，再按 jitter 抖动。
    attempts=1 失败 -> base_delay
    attempts=2 失败 -> base_delay * factor
    ...
"""

import random
from dataclasses import dataclass, replace

# 默认值：可以被 RetryPolicy(...) 覆盖，也可以从设置文件的 jobs 分区覆盖
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5          # 秒
DEFAULT_FACTOR = 2.0
DEFAULT_MAX_DELAY = 60.0          # 秒，退避上限（防止指数爆炸）
DEFAULT_JITTER = 0.0              # 0 = 不抖动；0.2 = ±20%


class RetryError(RuntimeError):
    """重试策略相关错误的基类。"""


class NonRetryableError(RetryError):
    """永久失败：重试没有意义（未配置 Key、内容不存在、缺依赖……）。

    阶段处理器抛这个异常，或者返回 {"ok": False, "permanent": True}，
    队列就立刻把任务判成 failed，不再占用重试次数。
    """


class RetryableError(RetryError):
    """明确要求重试（网络抖动、上游 5xx……），与普通异常等价但意图更清楚。"""


@dataclass(frozen=True)
class RetryDecision:
    """一次失败之后的判定结果。"""

    retry: bool
    delay: float
    reason: str
    attempts: int
    max_attempts: int

    def to_dict(self):
        return {
            "retry": self.retry,
            "delay": round(float(self.delay), 4),
            "reason": self.reason,
            "attempts": int(self.attempts),
            "maxAttempts": int(self.max_attempts),
        }


@dataclass(frozen=True)
class RetryPolicy:
    """不可变重试策略。"""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay: float = DEFAULT_BASE_DELAY
    factor: float = DEFAULT_FACTOR
    max_delay: float = DEFAULT_MAX_DELAY
    jitter: float = DEFAULT_JITTER
    retry_on: tuple = ()            # 非空时：只有这些异常类型才重试
    no_retry_on: tuple = ()         # 这些异常类型永不重试（优先级最高）

    def __post_init__(self):
        # 归一化：把明显写错的参数夹到合理范围，而不是抛异常
        # （策略来自设置文件，用户可能填 0 / 负数 / 字符串）
        object.__setattr__(self, "max_attempts", max(1, int(self.max_attempts or 1)))
        object.__setattr__(self, "base_delay", max(0.0, float(self.base_delay or 0.0)))
        object.__setattr__(self, "factor", max(1.0, float(self.factor or 1.0)))
        object.__setattr__(self, "max_delay", max(0.0, float(self.max_delay or 0.0)))
        object.__setattr__(self, "jitter", min(1.0, max(0.0, float(self.jitter or 0.0))))

    # ---- 判定 ----------------------------------------------------------
    def allows(self, attempts):
        """还有尝试额度吗？attempts 是「已经用掉的次数」。"""
        return int(attempts) < self.max_attempts

    def delay_for(self, attempts, rand=None):
        """第 attempts 次尝试失败后，应该等多久再试。attempts 从 1 开始。"""
        step = max(0, int(attempts) - 1)
        try:
            delay = float(self.base_delay) * (float(self.factor) ** step)
        except OverflowError:                     # 极端参数下别炸
            delay = float(self.max_delay)
        delay = min(delay, float(self.max_delay))
        if self.jitter:
            spread = delay * self.jitter
            source = rand or random.random
            delay = delay - spread + (2 * spread * float(source()))
        return max(0.0, round(delay, 4))

    def should_retry(self, attempts, error=None):
        """这次失败还要不要再试。只看策略，不看状态。"""
        if not self.allows(attempts):
            return False
        if error is not None:
            if isinstance(error, NonRetryableError):
                return False
            if self.no_retry_on and isinstance(error, self.no_retry_on):
                return False
            if self.retry_on and not isinstance(error, self.retry_on):
                return False
        return True

    def decide(self, attempts, error=None, rand=None):
        """完整判定：要不要重试 + 等多久 + 为什么。"""
        attempts = max(0, int(attempts))
        if not self.allows(attempts):
            return RetryDecision(False, 0.0, "已达到最大尝试次数", attempts, self.max_attempts)
        if not self.should_retry(attempts, error):
            name = type(error).__name__ if error is not None else "该错误"
            return RetryDecision(False, 0.0, f"{name} 不满足重试条件", attempts, self.max_attempts)
        return RetryDecision(True, self.delay_for(attempts, rand=rand),
                             f"可恢复失败，等待 {self.delay_for(attempts, rand=rand):.2f}s 后第 "
                             f"{attempts + 1}/{self.max_attempts} 次尝试",
                             attempts, self.max_attempts)

    # ---- 序列化 / 配置 -------------------------------------------------
    def to_dict(self):
        return {
            "max_attempts": self.max_attempts,
            "base_delay": self.base_delay,
            "factor": self.factor,
            "max_delay": self.max_delay,
            "jitter": self.jitter,
            "retry_on": [cls.__name__ for cls in self.retry_on],
            "no_retry_on": [cls.__name__ for cls in self.no_retry_on],
        }

    def with_(self, **changes):
        """派生一份改了某几项的副本（frozen dataclass 的 replace 包装）。"""
        return replace(self, **changes)

    @classmethod
    def from_dict(cls, data):
        data = data if isinstance(data, dict) else {}
        return cls(
            max_attempts=data.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
            base_delay=data.get("base_delay", DEFAULT_BASE_DELAY),
            factor=data.get("factor", DEFAULT_FACTOR),
            max_delay=data.get("max_delay", DEFAULT_MAX_DELAY),
            jitter=data.get("jitter", DEFAULT_JITTER),
        )

    @classmethod
    def from_settings(cls, settings, kind=None, default=None):
        """从设置文件的可选 `jobs` 分区读策略。

        为什么读一个可能不存在的分区：设置页的契约不在本模块职责内，
        这里只做「有就用、没有就用默认」，不要求 settings_store 增加字段。
        支持两种写法：
            {"jobs": {"max_attempts": 4, "base_delay": 1}}
            {"jobs": {"enrich": {"max_attempts": 5}}}
        """
        base = default or cls()
        section = {}
        if settings is not None:
            try:
                section = settings.section("jobs") or {}
            except Exception:
                section = {}
        if not isinstance(section, dict):
            return base
        shared = {key: value for key, value in section.items()
                  if key in {"max_attempts", "base_delay", "factor", "max_delay", "jitter"}}
        specific = {}
        if kind and isinstance(section.get(kind), dict):
            specific = {key: value for key, value in section[kind].items()
                        if key in {"max_attempts", "base_delay", "factor", "max_delay", "jitter"}}
        if not shared and not specific:
            return base
        merged = {**base.to_dict(), **shared, **specific}
        return cls.from_dict(merged)


# 每个阶段的默认策略（和 pipeline 的三个阶段一一对应）
DOWNLOAD_RETRY = RetryPolicy(max_attempts=3, base_delay=2.0, factor=2.0, max_delay=120.0)
TRANSCRIPT_RETRY = RetryPolicy(max_attempts=2, base_delay=1.0, factor=2.0, max_delay=60.0)
ENRICH_RETRY = RetryPolicy(max_attempts=3, base_delay=1.0, factor=2.0, max_delay=60.0)

DEFAULT_POLICIES = {
    "download": DOWNLOAD_RETRY,
    "transcript": TRANSCRIPT_RETRY,
    "enrich": ENRICH_RETRY,
}


def policies_from_settings(settings, base=None):
    """按设置文件为每个阶段生成策略；settings 为 None 时返回默认表。"""
    base = dict(base or DEFAULT_POLICIES)
    if settings is None:
        return base
    return {kind: RetryPolicy.from_settings(settings, kind=kind, default=policy)
            for kind, policy in base.items()}
