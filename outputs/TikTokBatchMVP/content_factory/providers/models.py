"""Provider 配置契约：一个外部服务「长什么样、怎么校验、怎么对外暴露」。

这一层只回答一个问题：**一个 provider 该由哪些字段描述**。
它不发起任何请求（连接测试在 connection_test.py）、不落盘（持久化在 registry.py）、
也不碰密钥明文（密钥在 credentials.py）。

设计取舍
--------
1. 厂商差异写成**数据**，不写成类。DeepSeek / MiniMax / GLM / OpenAI 兼容
   的区别只是「默认 base_url、默认 model、探针路径」这几项，
   全部收在 `PROVIDER_TYPES` 目录里；新增一家 = 加一条目录项，
   不需要复制一份调用逻辑。
2. `ProviderConfig.to_dict()`（落盘 / 跨层传输用的形式）**在设计上就没有密钥字段**：
   密钥只以 `credential_ref`（一个名字）被引用。
   所以「序列化意外泄露 secret」在结构上不可能发生，而不是靠每次记得删字段。
3. `extra` 是给非密钥的厂商附加项用的（如对象存储的 access_key_id / bucket / region）。
   如果谁把 `api_key` / `token` / `secret` 之类的键塞进 extra，`validate()` 会直接拒绝 —— 
   把「密钥不许混进可序列化配置」变成一条可执行的规则，而不是一句约定。
"""

from dataclasses import dataclass, field
from datetime import datetime
import re
from urllib.parse import urlsplit

# 连接超时的合法区间（秒）：下界防止「等于没测」，上界防止界面卡死
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 600.0
DEFAULT_TIMEOUT = 60.0

# provider_id 的形态：字母数字与 . _ -，够用且能安全地拼进 ref / 文件名
PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

# 一眼就是密钥的字段名。两个用途：拒绝 extra 里混入密钥、以及日志/序列化脱敏。
#
# 规则刻意做成「整词匹配」而不是子串匹配：`max_tokens` / `token_limit` / `temperature`
# 这类普通配置绝不能因为含 "token" 就被当成密钥（那会导致保存时被静默丢弃）。
SECRET_FIELD_NAMES = frozenset({
    "api_key", "apikey", "secret", "client_secret", "app_secret", "access_key_secret",
    "secret_key", "private_key", "access_key", "token", "access_token", "auth_token",
    "refresh_token", "bearer", "authorization", "password", "passwd", "pwd",
    "smtp_password", "credential", "credentials",
})

# `api_key_set` / `apiKeyMask` 是「状态位」，不是密钥本身
STATE_SUFFIXES = ("set", "mask", "configured", "state", "present", "ready")

PROVIDER_KINDS = ("llm", "asr", "storage", "publish")

# 连接测试的三档成本策略（顺序 = 从便宜到贵）
PROBE_STRATEGIES = ("config_only", "models", "chat_min")
PROBE_CONFIG_ONLY = "config_only"
PROBE_MODELS = "models"
PROBE_CHAT = "chat_min"

# 允许在 provider.extra 里按实例覆盖的探针相关键。
# 为什么要留这个口子：厂商 endpoint 会变（尤其是自建网关与国内厂商），
# 与其把新的规则硬编码进代码，不如让「数据」表达；但覆盖值必须是**相对路径**，
# 否则一个被篡改的配置就能把 Bearer 密钥送到别的域名去。
EXTRA_OVERRIDE_KEYS = ("chat_path", "models_path", "probe")


def normalize_field_name(name):
    """字段名归一化：camelCase → snake_case、去空白、转小写。"""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name or "").strip())
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def is_secret_field(name):
    """字段名是不是「密钥」。（`access_key_id` 这类标识符不算，可以放 extra。）"""
    key = normalize_field_name(name)
    if not key:
        return False
    for suffix in STATE_SUFFIXES:
        if key.endswith("_" + suffix):
            stem = key[:-(len(suffix) + 1)]
            if stem in SECRET_FIELD_NAMES:
                return False                            # api_key_set 只是布尔状态
    if key in SECRET_FIELD_NAMES:
        return True
    if re.search(r"(secret|password|passwd|token)$", key):
        return True                                     # *_secret / *_password / *_token
    return bool(re.search(r"api_?key", key))             # openai_api_key / myApiKey


class ProviderError(ValueError):
    """Provider 配置错误。

    带 `code` / `field` 是为了让界面能定位到具体输入框，
    也让「哪一种错」可被测试断言，而不是靠匹配错误文案。
    消息里永远不放密钥（本模块也拿不到密钥）。
    """

    def __init__(self, message, code="invalid_provider", field=""):
        super().__init__(message)
        self.code = code
        self.field = field

    def as_dict(self):
        return {"ok": False, "error": str(self), "code": self.code, "field": self.field}


@dataclass(frozen=True)
class ProviderType:
    """一种 provider 类型（不是实例）。所有厂商差异都收在这里。"""

    type_id: str
    label: str
    kind: str = "llm"
    default_base_url: str = ""
    default_model: str = ""
    models: tuple = ()
    credential_fields: tuple = ("api_key",)
    credential_label: str = "API Key"
    requires_base_url: bool = True
    requires_model: bool = True
    # 连接测试策略：config_only（只校验配置）/ models（拉模型列表，最便宜）/ chat_min（一发一收的最小请求）
    probe: str = "chat_min"
    models_path: str = "/models"
    chat_path: str = "/chat/completions"
    aliases: tuple = ()
    note: str = ""

    def public(self):
        """给界面用的类型描述：有哪些类型、默认填什么、要填几个凭据字段。

        `models` 只是**建议值**（模型名变化很快），不是白名单，不做任何校验。
        """
        return {
            "type_id": self.type_id,
            "label": self.label,
            "kind": self.kind,
            "default_base_url": self.default_base_url,
            "default_model": self.default_model,
            "models": list(self.models),
            "credential_fields": list(self.credential_fields),
            "credential_label": self.credential_label,
            "requires_base_url": self.requires_base_url,
            "requires_model": self.requires_model,
            "probe": self.probe,
            "models_path": self.models_path,
            "chat_path": self.chat_path,
            "note": self.note,
        }


# 内置目录：今天要能覆盖 DeepSeek / MiniMax / GLM / OpenAI 兼容 / ASR / 对象存储 / 发布。
PROVIDER_TYPES = (
    ProviderType(
        type_id="deepseek",
        label="DeepSeek",
        kind="llm",
        default_base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        models=("deepseek-chat", "deepseek-reasoner"),
        probe="models",
        aliases=("deepseek", "深度求索", "深度搜索"),
        note="OpenAI 兼容接口，支持 /models 探测。",
    ),
    ProviderType(
        type_id="minimax",
        label="MiniMax",
        kind="llm",
        default_base_url="https://api.minimax.chat/v1",
        default_model="MiniMax-Text-01",
        models=("MiniMax-Text-01", "abab6.5s-chat"),
        probe="chat_min",
        chat_path="/text/chatcompletion_v2",
        aliases=("minimax", "海螺", "abab"),
        note="对话路径与 OpenAI 不同，故单独声明 chat_path；无公开免费 /models。",
    ),
    ProviderType(
        type_id="glm",
        label="智谱 GLM",
        kind="llm",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4-flash",
        models=("glm-4-flash", "glm-4-air", "glm-4-plus"),
        probe="chat_min",
        aliases=("glm", "zhipu", "bigmodel", "chatglm", "智谱", "智谱清言"),
        note="glm-4-flash 有免费额度，最小请求成本最低。",
    ),
    ProviderType(
        type_id="openai",
        label="OpenAI",
        kind="llm",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        models=("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"),
        probe="models",
        aliases=("openai", "gpt", "chatgpt"),
        note="标准 OpenAI 接口。",
    ),
    ProviderType(
        type_id="openai_compatible",
        label="OpenAI 兼容接口",
        kind="llm",
        default_base_url="",
        default_model="",
        models=(),
        probe="models",
        aliases=("openai compatible", "openai-compatible", "兼容", "自定义", "custom", "other"),
        note="任何遵循 /chat/completions 的自建或第三方服务都填这里。",
    ),
    ProviderType(
        type_id="asr",
        label="语音识别服务",
        kind="asr",
        default_base_url="",
        default_model="whisper-1",
        models=("whisper-1",),
        probe="config_only",
        requires_model=False,
        aliases=("asr", "whisper", "语音识别", "转写"),
        note="转写需要上传音频，不做联网探测；连接测试只校验配置与凭据是否齐备。",
    ),
    ProviderType(
        type_id="object_storage",
        label="对象存储",
        kind="storage",
        default_base_url="",
        default_model="",
        requires_base_url=False,
        requires_model=False,
        credential_fields=("access_key_secret",),
        credential_label="AccessKey Secret",
        probe="config_only",
        aliases=("oss", "s3", "cos", "对象存储", "storage", "阿里云 oss", "腾讯云 cos"),
        note="access_key_id / bucket / region 属于非密钥配置，放 extra；只有 Secret 进凭据库。",
    ),
    ProviderType(
        type_id="publish",
        label="发布渠道",
        kind="publish",
        default_base_url="",
        default_model="",
        requires_base_url=False,
        requires_model=False,
        credential_fields=("token",),
        credential_label="Token",
        probe="config_only",
        aliases=("publish", "发布", "webhook", "渠道"),
        note="发布接口差异大，连接测试只校验配置；真实探测留给发布模块。",
    ),
)

_TYPES_BY_ID = {spec.type_id: spec for spec in PROVIDER_TYPES}


def provider_types(kind=None):
    """列出内置类型（可按 kind 过滤）。"""
    return [spec for spec in PROVIDER_TYPES if not kind or spec.kind == kind]


def provider_type(type_id):
    """按 type_id 取类型定义；未知类型抛 ProviderError。"""
    key = str(type_id or "").strip().lower()
    spec = _TYPES_BY_ID.get(key)
    if spec is None:
        raise ProviderError(
            f"未知的 provider 类型：{type_id!r}（可用：{', '.join(_TYPES_BY_ID)}）",
            code="invalid_provider_type", field="provider_type")
    return spec


def match_provider_type(value, base_url=""):
    """把用户填的「服务商」文字（如设置页的 DeepSeek）映射到内置类型。

    先按 type_id / label / 别名精确匹配，再按 base_url 的主机名兜底，
    最后落到 openai_compatible —— 认不出来时按「兼容接口」处理，
    比直接报错更符合用户预期（自建服务本来就只能填兼容接口）。
    """
    text = str(value or "").strip().lower()
    if text:
        for spec in PROVIDER_TYPES:
            if text in (spec.type_id.lower(), spec.label.lower()):
                return spec
        for spec in PROVIDER_TYPES:
            if text in {alias.lower() for alias in spec.aliases}:
                return spec
        for spec in PROVIDER_TYPES:
            if text and (text in spec.type_id.lower() or text in spec.label.lower()):
                return spec
    host = urlsplit(normalize_base_url(base_url)).hostname or ""
    if host:
        for spec in PROVIDER_TYPES:
            if spec.default_base_url and urlsplit(spec.default_base_url).hostname == host:
                return spec
    return _TYPES_BY_ID["openai_compatible"]


def normalize_base_url(value):
    """规整 base_url：去空白、缺协议时补 https、去尾部斜杠。

    只在**真有主机名**时才去尾斜杠：否则 `http://` 会被削成 `http:`，
    下一轮校验再给它补一个 https:// 前缀，凭空造出一个「合法」地址。
    """
    text = str(value or "").strip().strip('"').strip("'").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    return text.rstrip("/") if urlsplit(text).netloc else text


def validate_base_url(value):
    """返回错误文案；合法则返回空串。"""
    text = normalize_base_url(value)
    if not text:
        return "base_url 不能为空"
    if re.search(r"\s", text):
        return f"base_url 不能包含空格：{text}"
    parsed = urlsplit(text)
    if parsed.scheme not in ("http", "https"):
        return f"base_url 只支持 http / https：{text}"
    if not parsed.hostname:
        return f"base_url 缺少主机名：{text}"
    if parsed.username or parsed.password:
        return "base_url 不能内嵌用户名 / 密码（凭据请放在凭据库里）"
    try:
        port = parsed.port
    except ValueError:
        return f"base_url 端口不合法：{text}"
    if port is not None and not 0 < port < 65536:
        return f"base_url 端口超出范围：{text}"
    if any(char in text for char in ("<", ">", "{", "}")):
        return f"base_url 含有占位符，请填写真实地址：{text}"
    return ""


def coerce_timeout(value, default=DEFAULT_TIMEOUT):
    """把超时规整成秒（float）；非法值抛 ProviderError。"""
    if value in (None, ""):
        return float(default)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ProviderError(f"timeout 必须是数字（秒）：{value!r}",
                            code="invalid_timeout", field="timeout")
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        raise ProviderError(f"timeout 不是有效数字：{value!r}", code="invalid_timeout", field="timeout")
    if seconds < MIN_TIMEOUT or seconds > MAX_TIMEOUT:
        raise ProviderError(
            f"timeout 必须在 {MIN_TIMEOUT:g}–{MAX_TIMEOUT:g} 秒之间：{value!r}",
            code="invalid_timeout", field="timeout")
    return round(seconds, 2)


def is_secret_name(name):
    """`is_secret_field` 的同义词，保留给「不关心实现细节」的调用方。"""
    return is_secret_field(name)


def _validate_path_override(value, field):
    """校验 extra 里的路径覆盖。只允许相对路径（防止把密钥送到别的域名）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text or text.startswith("//"):
        return f"{field} 必须是相对路径（以 / 开头），不能是完整 URL：{text}"
    if not text.startswith("/"):
        return f"{field} 必须以 / 开头：{text}"
    if re.search(r"\s", text):
        return f"{field} 不能包含空格：{text}"
    if "?" in text or "#" in text:
        return f"{field} 不能带查询串或锚点：{text}"
    return ""


def catalog_problems():
    """Provider 目录的自检：返回问题清单（空 = 目录本身是自洽的）。

    目录是**数据**，数据也会写错（重复 type_id、探针名拼错、默认地址非法…）。
    与其等到运行时才发现，不如让测试断言这个函数返回空列表。
    """
    problems = []
    seen = set()
    for spec in PROVIDER_TYPES:
        if spec.type_id in seen:
            problems.append(f"type_id 重复：{spec.type_id}")
        seen.add(spec.type_id)
        if spec.kind not in PROVIDER_KINDS:
            problems.append(f"{spec.type_id}: 未知 kind {spec.kind!r}")
        if spec.probe not in PROBE_STRATEGIES:
            problems.append(f"{spec.type_id}: 未知 probe {spec.probe!r}")
        for name, path in (("models_path", spec.models_path), ("chat_path", spec.chat_path)):
            problem = _validate_path_override(path, name)
            if problem:
                problems.append(f"{spec.type_id}: {problem}")
        if spec.default_base_url:
            problem = validate_base_url(spec.default_base_url)
            if problem:
                problems.append(f"{spec.type_id}: 默认 base_url 非法 —— {problem}")
        if spec.probe == PROBE_MODELS and not spec.models_path:
            problems.append(f"{spec.type_id}: 用 models 探针却没有 models_path")
        if spec.probe == PROBE_CHAT and not spec.chat_path:
            problems.append(f"{spec.type_id}: 用 chat_min 探针却没有 chat_path")
        if spec.kind in ("llm", "asr") and not spec.credential_fields:
            problems.append(f"{spec.type_id}: {spec.kind} 类型没有声明凭据字段")
    return problems


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class ProviderConfig:
    """一个 provider 实例。**不含任何密钥**，只持有凭据引用。"""

    provider_id: str
    provider_type: str
    label: str = ""
    base_url: str = ""
    model: str = ""
    timeout: float = DEFAULT_TIMEOUT
    enabled: bool = True
    credential_ref: str = ""
    extra: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    # ---- 类型信息 ------------------------------------------------------
    @property
    def spec(self):
        return _TYPES_BY_ID.get(str(self.provider_type or "").strip().lower())

    @property
    def kind(self):
        spec = self.spec
        return spec.kind if spec else ""

    @property
    def credential_fields(self):
        spec = self.spec
        return tuple(spec.credential_fields) if spec else ("api_key",)

    @property
    def probe_strategy(self):
        """连接测试策略：实例 extra 覆盖 > 类型目录 > 兜底 chat_min。"""
        override = str(self.extra.get("probe") or "").strip()
        if override in PROBE_STRATEGIES:
            return override
        spec = self.spec
        return spec.probe if spec else PROBE_CHAT

    def probe_path(self, strategy=None):
        """某档探测要用的路径：实例 extra 覆盖 > 类型目录。

        只接受**相对路径**：否则一份被篡改的配置就能把 Authorization 头送到别的域名去。
        """
        strategy = strategy or self.probe_strategy
        key = "models_path" if strategy == PROBE_MODELS else "chat_path"
        override = str(self.extra.get(key) or "").strip()
        if override.startswith("/") and "://" not in override:
            return override
        spec = self.spec
        default = (spec.models_path if strategy == PROBE_MODELS else spec.chat_path) if spec else ""
        return default or ("/models" if strategy == PROBE_MODELS else "/chat/completions")

    def __post_init__(self):
        self.provider_id = str(self.provider_id or "").strip()
        self.provider_type = str(self.provider_type or "").strip().lower()
        self.label = str(self.label or "").strip() or self.provider_id
        self.model = str(self.model or "").strip()
        self.extra = dict(self.extra or {})
        if not self.credential_ref:
            self.credential_ref = credential_ref_for(self.provider_id)
        if not self.created_at:
            self.created_at = now_text()
        if not self.updated_at:
            self.updated_at = self.created_at

    # ---- 派生 ----------------------------------------------------------
    def endpoint(self, path=""):
        """拼接接口地址。path 为空时返回规整后的 base_url。"""
        base = normalize_base_url(self.base_url)
        if not path:
            return base
        tail = "/" + str(path).lstrip("/")
        return (base + tail) if base else ""

    def validate(self):
        """返回错误列表（[{field, message, code}]）；空列表表示配置可用。"""
        errors = []
        if not PROVIDER_ID_PATTERN.match(self.provider_id or ""):
            errors.append({"field": "provider_id", "code": "invalid_provider_id",
                           "message": f"provider_id 只能包含字母数字与 . _ -（1–64 位）：{self.provider_id!r}"})
        spec = self.spec
        if spec is None:
            errors.append({"field": "provider_type", "code": "invalid_provider_type",
                           "message": f"未知的 provider 类型：{self.provider_type!r}"})
        # 停用只是状态位，配置本身仍然必须合法 —— 否则「启用」的那一刻才报错就太晚了
        if self.base_url or (spec and spec.requires_base_url):
            problem = validate_base_url(self.base_url)
            if problem:
                errors.append({"field": "base_url", "code": "invalid_base_url", "message": problem})
        if spec and spec.requires_model and not self.model:
            errors.append({"field": "model", "code": "invalid_model", "message": "model 不能为空"})
        if len(self.model) > 200:
            errors.append({"field": "model", "code": "invalid_model", "message": "model 名称过长"})
        try:
            coerce_timeout(self.timeout)
        except ProviderError as exc:
            errors.append({"field": "timeout", "code": exc.code, "message": str(exc)})
        if len(self.label) > 80:
            errors.append({"field": "label", "code": "invalid_label", "message": "label 过长（≤80 字）"})
        if not isinstance(self.extra, dict):
            errors.append({"field": "extra", "code": "invalid_extra", "message": "extra 必须是对象"})
        else:
            for key in self.extra:
                if is_secret_field(key):
                    errors.append({
                        "field": f"extra.{key}", "code": "secret_in_extra",
                        "message": f"extra 里不允许放密钥字段 {key!r}：请用凭据库（credential_ref）保存"})
            probe = str(self.extra.get("probe") or "").strip()
            if probe and probe not in PROBE_STRATEGIES:
                errors.append({
                    "field": "extra.probe", "code": "invalid_probe",
                    "message": f"probe 只能是 {' / '.join(PROBE_STRATEGIES)}：{probe!r}"})
            for key in ("models_path", "chat_path"):
                problem = _validate_path_override(self.extra.get(key), key)
                if problem:
                    errors.append({"field": f"extra.{key}", "code": "invalid_path_override",
                                   "message": problem})
        return errors

    def validate_or_raise(self):
        errors = self.validate()
        if errors:
            first = errors[0]
            raise ProviderError(first["message"], code=first["code"], field=first["field"])
        return self

    # ---- 序列化 --------------------------------------------------------
    # 注意：这里**没有**任何密钥字段，也没有可以顺手塞进密钥的自由字段
    # （extra 里的密钥键会被 validate() 拒绝）。
    SERIALIZED_FIELDS = ("provider_id", "provider_type", "label", "base_url", "model",
                         "timeout", "enabled", "credential_ref", "extra",
                         "created_at", "updated_at")

    def to_dict(self):
        """落盘 / 跨层传输用的形式：结构上不可能带密钥。"""
        return {
            "provider_id": self.provider_id,
            "provider_type": self.provider_type,
            "label": self.label,
            "base_url": self.base_url,
            "model": self.model,
            "timeout": self.timeout,
            "enabled": bool(self.enabled),
            "credential_ref": self.credential_ref,
            "extra": dict(self.extra),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def public(self, credentials=None):
        """给界面 / 桥接层用的视图：只带凭据「是否已配置 + 掩码」。"""
        view = self.to_dict()
        spec = self.spec
        view["kind"] = self.kind
        view["credential_label"] = spec.credential_label if spec else "凭据"
        view["credential"] = (credentials.public(self.credential_ref, self.credential_fields)
                              if credentials is not None else {})
        if credentials is not None:
            view["credential_ready"] = credentials.is_set(self.credential_ref, self.credential_fields)
        else:
            view["credential_ready"] = False
        return view

    def copy_with(self, **changes):
        data = self.to_dict()
        data.update(changes)
        return ProviderConfig(**data)

    @classmethod
    def from_dict(cls, data):
        payload = {key: value for key, value in dict(data or {}).items()
                   if key in cls.SERIALIZED_FIELDS}
        return cls(**payload)


def credential_ref_for(provider_id):
    """provider → 凭据引用。默认一一对应，也允许以后多条 provider 共用一份凭据。"""
    return f"provider:{str(provider_id or '').strip()}"


def provider_from_legacy(section, provider_id="ai", fallback_type="openai_compatible"):
    """把现有设置页 `ai` 分区的字段翻译成一个 ProviderConfig（**不含密钥**）。

    这是与既有 AI 闭环的兼容点：老设置里 provider / api_base / model 直接映射过来，
    api_key 不在这里处理（它进凭据库）。
    """
    section = dict(section or {})
    spec = match_provider_type(section.get("provider"), section.get("api_base"))
    if str(section.get("provider") or "").strip() == "" and not section.get("api_base"):
        spec = provider_type(fallback_type)
    base_url = normalize_base_url(section.get("api_base")) or spec.default_base_url
    model = str(section.get("model") or "").strip() or spec.default_model
    return ProviderConfig(
        provider_id=provider_id,
        provider_type=spec.type_id,
        label=spec.label,
        base_url=base_url,
        model=model,
        timeout=coerce_timeout(section.get("timeout"), DEFAULT_TIMEOUT),
        enabled=True,
        credential_ref=credential_ref_for(provider_id),
        extra={},
    )
