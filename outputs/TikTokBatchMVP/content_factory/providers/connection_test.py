"""统一的连接测试。

目标：任何一个 provider 都能用同一条路径回答「这个配置现在能不能用」，
结果结构固定、错误分类固定、**永不泄露凭据**、**默认不产生昂贵调用**。

成本策略（按便宜到贵依次尝试）
------------------------------
1. `config_only`：只校验配置与凭据是否齐备，不联网。ASR / 对象存储 / 发布渠道用这个 ——
   它们的探测要么要上传文件、要么要真的发一条内容，测试按钮不该有副作用。
2. `models`：GET {base_url}/models。对 OpenAI 兼容服务是免费的元数据请求，
   顺带能确认「配置的 model 是否真的存在」。DeepSeek / OpenAI 走这条。
3. `chat_min`：一发一收的最小请求（max_tokens=1、temperature=0、单字 prompt）。
   `/models` 不存在时（MiniMax、GLM）才退到这条，成本约等于 0。

结果契约
--------
{ok, provider, provider_type, kind, model, base_url, probe, endpoint,
 latency_ms, error_type, message, checked_at}
error_type ∈ {None, invalid_provider, unknown_provider, invalid_config, missing_credential,
              auth, not_found, timeout, network, rate_limit, server, http_error,
              bad_response, unsupported}
message 已过 redact()，且从不包含 Authorization 头 / 密钥。

与 AI Enrichment 的边界
-----------------------
这里只回答「配置能不能用」。发 prompt、解析 enrichment、重试策略属于 AI / Pipeline 模块；
`EnrichmentService` 自己的 test_connection() 保持不变，本模块不接管它的调用逻辑。
"""

import json
import logging
import time
from datetime import datetime

from .credentials import redact, remember_secret
from .models import (DEFAULT_TIMEOUT, PROBE_CHAT, PROBE_CONFIG_ONLY, PROBE_MODELS,
                     ProviderConfig, ProviderError, coerce_timeout, match_provider_type,
                     normalize_base_url, provider_type)

LOGGER = logging.getLogger(__name__)

# 探测请求自己的超时上限：界面上的「测试连接」不该挂几分钟
PROBE_TIMEOUT_CAP = 15.0

ERROR_TYPES = (None, "invalid_provider", "unknown_provider", "invalid_config", "missing_credential",
               "auth", "not_found", "timeout", "network", "rate_limit", "server", "http_error",
               "bad_response", "unsupported")

MINIMAL_PROMPT = "ping"

# 错误响应里「哪一段是人话」的通用候选键（不针对任何厂商写分支）
ERROR_TEXT_KEYS = ("message", "msg", "error_msg", "errmsg", "status_msg", "detail",
                   "reason", "error_description", "error")


def _classify_status(status):
    """HTTP 状态码 -> (error_type, message)。"""
    if status in (401, 403):
        return "auth", "凭据被拒绝（401/403）：请检查 API Key 是否正确、是否有该模型的权限"
    if status == 404:
        return "not_found", "接口地址不存在（404）：请检查 base_url 是否写全（很多服务需要 /v1）"
    if status in (408, 504):
        return "timeout", f"服务端超时（{status}）"
    if status == 429:
        return "rate_limit", "触发限流（429）：配置本身没问题，稍后再试或降低并发"
    if 500 <= int(status) <= 599:
        return "server", f"服务端错误（{status}）：对方服务异常，不是本地配置问题"
    return "http_error", f"请求失败（HTTP {status}）"


def _classify_exception(exc):
    """网络异常 -> (error_type, message)。requests 是可选依赖，用类名判断而不是 import。"""
    name = type(exc).__name__
    text = redact(str(exc))
    if "Timeout" in name:
        return "timeout", f"连接超时：{text}"
    if "SSL" in name:
        return "network", f"TLS 证书校验失败：{text}"
    if name in ("ConnectionError", "ConnectTimeout", "ProxyError", "SSLError"):
        return "network", f"无法连接到服务：{text}"
    if name in ("InvalidURL", "MissingSchema", "URLRequired"):
        return "invalid_config", f"接口地址不合法：{text}"
    return "network", f"{name}：{text}"


def _short(text, limit=200):
    """截断 + 脱敏：对方返回的报错里偶尔会回显请求内容。"""
    return redact(str(text or "")).strip()[:limit]


class ConnectionTester:
    """按 provider 类型选择最便宜的探测方式，返回统一结果。"""

    def __init__(self, registry=None, credentials=None, http=None, clock=time.monotonic):
        self.registry = registry
        self.credentials = credentials if credentials is not None else (
            getattr(registry, "credentials", None))
        self._http = http                    # 测试可注入假 requests
        self._clock = clock

    # ---- 入口 ----------------------------------------------------------
    def test(self, provider, overrides=None, api_key=None, probe=None, allow_chat=True,
             request_timeout=None):
        """测试一个 provider。

        provider 可以是：
          - ProviderConfig 实例
          - provider_id 字符串（从注册表取）
          - 一段 dict（界面上还没保存的表单值，临时构造，不落盘）
        overrides 里的 base_url / model / timeout / extra 覆盖配置；
        api_key（或在 overrides 里给 api_key）只用于**本次**测试，绝不写入任何文件。
        """
        started = self._clock()
        overrides = dict(overrides or {})
        staged_key = _pick_secret(overrides) or str(api_key or "").strip()

        try:
            config = self._resolve(provider, overrides)
        except ProviderError as exc:
            return self._result(None, started, ok=False, error_type=exc.code or "invalid_provider",
                                message=str(exc), probe=probe or PROBE_CONFIG_ONLY)
        except Exception as exc:                              # pragma: no cover - 防御
            return self._result(None, started, ok=False, error_type="invalid_provider",
                                message=_short(exc), probe=probe or PROBE_CONFIG_ONLY)

        errors = config.validate()
        if errors:
            return self._result(config, started, ok=False, error_type="invalid_config",
                                message="；".join(entry["message"] for entry in errors),
                                probe=probe or PROBE_CONFIG_ONLY)

        spec = config.spec
        fields = config.credential_fields
        secret = staged_key or self._stored_secret(config, fields)
        if fields and not secret:
            missing = "、".join(fields)
            return self._result(config, started, ok=False, error_type="missing_credential",
                                message=f"尚未配置{spec.credential_label if spec else '凭据'}（{missing}）",
                                probe=probe or PROBE_CONFIG_ONLY)
        if secret:
            # 登记进脱敏表：_stored_secret 已经登记过，但界面直接传进来的临时密钥还没有。
            # 少了这一步，「服务端把 key 回显在错误里」就只能靠形态匹配兜底。
            remember_secret(secret)

        strategy = probe or config.probe_strategy
        if strategy == PROBE_CONFIG_ONLY or not config.base_url:
            return self._result(config, started, ok=True, error_type=None,
                                message="配置与凭据齐备（该类型不做联网探测，避免产生真实调用）",
                                probe=PROBE_CONFIG_ONLY)

        base_url = normalize_base_url(config.base_url)
        if strategy == PROBE_MODELS:
            result = self._probe_models(config, base_url, secret, request_timeout)
            if result["ok"] or result["error_type"] != "not_found" or not allow_chat:
                return self._finish(config, started, result)
            # /models 不存在：退到最小对话请求（MiniMax / GLM 这类）
            return self._finish(config, started,
                                self._probe_chat(config, base_url, secret, request_timeout))
        if strategy == PROBE_CHAT:
            return self._finish(config, started,
                                self._probe_chat(config, base_url, secret, request_timeout))
        return self._result(config, started, ok=False, error_type="unsupported",
                            message=f"不支持的探测方式：{strategy}", probe=str(strategy))

    # ---- 具体探测 ------------------------------------------------------
    def _probe_models(self, config, base_url, secret, request_timeout):
        endpoint = base_url + config.probe_path(PROBE_MODELS)
        status, payload, error = self._request("GET", endpoint, secret, None,
                                               self._timeout(config, request_timeout))
        if error:
            return dict(ok=False, error_type=error[0], message=error[1],
                        endpoint=endpoint, probe=PROBE_MODELS)
        if status >= 400:
            kind, message = _classify_status(status)
            detail = _extract_error(payload)
            if detail:
                message = f"{message}（{detail}）"
            return dict(ok=False, error_type=kind, message=message,
                        endpoint=endpoint, probe=PROBE_MODELS, status=status)
        found = None
        try:
            data = payload if isinstance(payload, dict) else json.loads(payload or "{}")
            names = [str(entry.get("id") or entry.get("name") or "")
                     for entry in (data.get("data") or data.get("models") or [])
                     if isinstance(entry, dict)]
            if names and config.model:
                found = config.model in names
        except Exception:
            found = None
        message = f"连接成功（{PROBE_MODELS}）"
        if found is False:
            message += f"；注意：模型列表里没有 {config.model}，请确认模型名"
        return dict(ok=True, error_type=None, message=message, endpoint=endpoint,
                    probe=PROBE_MODELS, model_found=found)

    def _probe_chat(self, config, base_url, secret, request_timeout):
        endpoint = base_url + config.probe_path(PROBE_CHAT)
        body = {"model": config.model, "messages": [{"role": "user", "content": MINIMAL_PROMPT}],
                "max_tokens": 1, "temperature": 0, "stream": False}
        status, payload, error = self._request("POST", endpoint, secret, body,
                                               self._timeout(config, request_timeout))
        if error:
            return dict(ok=False, error_type=error[0], message=error[1],
                        endpoint=endpoint, probe=PROBE_CHAT)
        if status >= 400:
            kind, message = _classify_status(status)
            detail = _extract_error(payload)
            if detail:
                message = f"{message}（{detail}）"
            return dict(ok=False, error_type=kind, message=message,
                        endpoint=endpoint, probe=PROBE_CHAT, status=status)
        reply = None
        try:
            data = payload if isinstance(payload, dict) else json.loads(payload or "{}")
            choices = data.get("choices") or []
            if choices:
                reply = _short(choices[0].get("message", {}).get("content")
                               or choices[0].get("text") or "", 40)
        except Exception:
            reply = None
        return dict(ok=True, error_type=None,
                    message=f"连接成功（最小请求{('，返回：' + reply) if reply else ''}）",
                    endpoint=endpoint, probe=PROBE_CHAT)

    # ---- HTTP ----------------------------------------------------------
    def _http_client(self):
        if self._http is not None:
            return self._http
        import requests                                      # 延迟导入：没有网络依赖也能跑配置测试
        return requests

    def _request(self, method, endpoint, secret, body, timeout):
        """返回 (status, payload, error)。error 为 (error_type, message)。

        两道硬约束：
        1. 密钥只走 Authorization 头，**永远不进 URL**（URL 会进日志、进历史、进截图）；
        2. 万一配置把密钥写进了 base_url，直接拒绝发送，而不是把它带出去。
        """
        if secret and secret in str(endpoint):
            return 0, None, ("invalid_config",
                             "接口地址里出现了凭据，已拒绝发送：密钥只能通过请求头传递")
        headers = {"Accept": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        client = self._http_client()
        try:
            if method == "GET":
                response = client.get(endpoint, headers=headers, timeout=timeout)
            else:
                response = client.post(endpoint, headers=headers, json=body, timeout=timeout)
        except Exception as exc:
            kind, message = _classify_exception(exc)
            # INFO 而不是 WARNING：连接测试失败是「按钮的正常结果」，
            # 已经结构化返回给界面了，不需要再进异常页。
            LOGGER.info("连接测试失败（%s %s）：%s", method, redact(endpoint), message)
            return 0, None, (kind, message)
        status = int(getattr(response, "status_code", 0) or 0)
        payload = None
        try:
            payload = response.json()
        except Exception:
            payload = getattr(response, "text", None)
        return status, payload, None

    def _timeout(self, config, request_timeout=None):
        if request_timeout:
            return coerce_timeout(request_timeout, PROBE_TIMEOUT_CAP)
        return min(float(config.timeout or DEFAULT_TIMEOUT), PROBE_TIMEOUT_CAP)

    # ---- 组装 ----------------------------------------------------------
    def _resolve(self, provider, overrides):
        """provider 参数 -> ProviderConfig（overrides 只作用于本次调用，不落盘）。"""
        if isinstance(provider, ProviderConfig):
            config = provider
        elif isinstance(provider, str):
            if not self.registry:
                raise ProviderError("没有注册表，无法按 id 查找 provider",
                                    code="unknown_provider", field="provider_id")
            config = self.registry.require(provider)
        elif isinstance(provider, dict):
            payload = dict(provider)
            payload.update({key: value for key, value in overrides.items()
                            if key not in ("api_key",)})
            config = _config_from_payload(payload)
            overrides = {}
        else:
            raise ProviderError(f"无法识别的 provider 参数：{type(provider).__name__}",
                                code="invalid_provider", field="provider")

        changes = {key: value for key, value in overrides.items()
                   if key in ("base_url", "model", "timeout", "extra", "label", "enabled")
                   and value not in (None, "")}
        return config.copy_with(**changes) if changes else config

    def _stored_secret(self, config, fields):
        """从凭据库取本次测试要用的密钥（内存里用，不写回任何文件）。"""
        store = self.credentials
        if store is None or not fields:
            return ""
        for name in fields:
            value = store.reveal(config.credential_ref, name)
            if value:
                return value
        return ""

    def _finish(self, config, started, probe_result):
        return self._result(config, started, probe=probe_result.get("probe"),
                            **{key: value for key, value in probe_result.items() if key != "probe"})

    def _result(self, config, started, ok, error_type=None, message="", probe=None, **extra):
        latency = max(0, int(round((self._clock() - started) * 1000)))
        result = {
            "ok": bool(ok),
            "provider": config.provider_id if config else "",
            "provider_type": config.provider_type if config else "",
            "kind": config.kind if config else "",
            "label": config.label if config else "",
            "model": config.model if config else "",
            "base_url": redact(config.base_url if config else ""),
            "probe": probe or PROBE_CONFIG_ONLY,
            "endpoint": redact(extra.pop("endpoint", "")),
            "latency_ms": latency,
            "error_type": error_type,
            "message": _short(message, 400),
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        result.update({key: value for key, value in extra.items() if value is not None})
        return result


def _pick_secret(values):
    for key in ("api_key", "token", "access_key_secret", "password", "secret"):
        value = values.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _config_from_payload(payload):
    """把一段（可能来自界面的）字典变成 ProviderConfig。"""
    data = dict(payload or {})
    spec = None
    type_id = str(data.get("provider_type") or data.get("providerType") or "").strip()
    if type_id:
        try:
            spec = provider_type(type_id)
        except ProviderError:
            spec = None
    if spec is None:
        spec = match_provider_type(data.get("provider") or data.get("provider_type"),
                                   data.get("base_url") or data.get("api_base"))
    provider_id = str(data.get("provider_id") or data.get("providerId") or "staged").strip() or "staged"
    model = str(data.get("model") or "").strip() or spec.default_model
    return ProviderConfig(
        provider_id=provider_id,
        provider_type=spec.type_id,
        label=str(data.get("label") or spec.label),
        base_url=normalize_base_url(data.get("base_url") or data.get("api_base")
                                    or spec.default_base_url),
        model=model,
        timeout=coerce_timeout(data.get("timeout"), DEFAULT_TIMEOUT),
        enabled=True,
        credential_ref=str(data.get("credential_ref") or f"provider:{provider_id}"),
        extra=dict(data.get("extra") or {}),
    )


def _extract_error(payload, depth=2):
    """从错误响应里抽一句人话（已脱敏）。

    刻意**不针对任何厂商写分支**：各家响应格式不同（OpenAI 的 `error.message`、
    国内厂商的 `base_resp.status_msg`、网关的 `msg`…），
    与其一家一条规则，不如按「常见字段名 + 深度受限的递归」通用地找第一段人话。
    """
    if isinstance(payload, str):
        return _short(payload, 160)
    if depth < 0:
        return ""
    if isinstance(payload, dict):
        for key in ERROR_TEXT_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _short(value, 160)
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = _extract_error(value, depth - 1)
                if found:
                    return found
    if isinstance(payload, list):
        for entry in payload:
            found = _extract_error(entry, depth - 1)
            if found:
                return found
    return ""


def test_connection(provider, registry=None, http=None, credentials=None, **kwargs):
    """便捷函数：`test_connection("deepseek-main")` 或 `test_connection({...表单值...})`。

    `http` 可注入一个 requests 形状的假客户端（测试用，保证不碰网络）。
    """
    return ConnectionTester(registry=registry, credentials=credentials, http=http).test(provider, **kwargs)
