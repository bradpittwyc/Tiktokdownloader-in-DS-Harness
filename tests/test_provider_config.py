"""Provider 配置 / 凭据管理 / 连接测试 的回归网。

全部离线：HTTP 注入假的客户端，不碰网络、不用真实 API Key。
锁住的是这一层的验收项：

- provider 的建 / 改 / 启停 / 删除，重启后仍在；
- 密钥只进凭据库，普通读取（public / to_dict / JSON 序列化 / 设置视图）拿不到明文；
- 掩码 `****abcd`；留空 = 不改（绝不能删掉已存的密钥）；删除是显式动作；
- 日志、异常文案、连接测试返回里都不出现密钥；
- 非法 provider / base_url / timeout 被明确拒绝；
- 连接测试成功与失败两路都给出固定结构（ok / provider / latency / error_type / message）；
- 已有的 AI 标注闭环（EnrichmentService）在接入本层后仍然可用。
"""

import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_factory.ai_enrichment import EnrichmentService  # noqa: E402
from content_factory.providers import (  # noqa: E402
    BACKEND_DPAPI, BACKEND_ENV, BACKEND_PLAIN, BACKEND_TEST, CredentialStore, PlaintextBackend,
    ProviderConfig, ProviderError, ProviderRegistry, ProviderSettingsAdapter, PROVIDER_TYPES,
    SecretRedactionFilter, TestCredentialBackend, UnavailableBackend, WindowsDPAPIBackend,
    catalog_problems, coerce_timeout, credential_ref_for, install_log_filter, is_secret_field,
    mask_secret, match_provider_type, provider_type, redact, redact_mapping,
    register_credential_backend, registered_credential_backends, test_connection)
from content_factory.settings_store import FactorySettings  # noqa: E402

FAKE_KEY = "sk-unit-test-9f8e7d6c5b4a"
DPAPI_READY = WindowsDPAPIBackend().available()

# 本模块会**故意**踩出大量 fail-closed 告警（拒绝保存、解不开、迁移失败…）。
# 默认情况下 Python 的 lastResort handler 会把它们打到 stderr，把测试输出刷到没法看。
# 这里把 providers 包的日志出口收进 NullHandler；需要断言日志的用例
# 显式用 assertLogs("content_factory.providers…") 去抓。
_PROVIDERS_LOGGER = logging.getLogger("content_factory.providers")
_PROVIDERS_LOGGER.addHandler(logging.NullHandler())
_PROVIDERS_LOGGER.propagate = False


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHttp:
    """按 (method, url 子串) 返回预设响应；记录每次调用以便断言「有没有真发请求」。"""

    def __init__(self, get=None, post=None):
        self.get_reply = get if get is not None else FakeResponse(200, {"data": []})
        self.post_reply = post if post is not None else FakeResponse(200, {"choices": []})
        self.calls = []

    def _reply(self, method, url, headers, timeout):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "timeout": timeout})
        reply = self.get_reply if method == "GET" else self.post_reply
        if isinstance(reply, Exception):
            raise reply
        return reply

    def get(self, url, headers=None, timeout=None):
        return self._reply("GET", url, headers, timeout)

    def post(self, url, headers=None, json=None, timeout=None):
        return self._reply("POST", url, headers, timeout)


class TempProviderCase(unittest.TestCase):
    """默认用**测试后端**，让单测与真实 DPAPI 完全解耦。

    默认后端的 fail-closed 行为由 `FailClosedBackendTests` 单独覆盖。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.backend = TestCredentialBackend(namespace=self.temp.name)
        self.credentials = CredentialStore(self.root, backend=self.backend)
        self.registry = ProviderRegistry(root=self.root, credentials=self.credentials)
        self.settings = FactorySettings(self.root)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def adapter(self, **kwargs):
        return ProviderSettingsAdapter(self.settings, registry=self.registry,
                                       credentials=self.credentials, **kwargs)


# ---------------------------------------------------------------------------
# 1–3. provider 的建 / 改 / 启停
# ---------------------------------------------------------------------------

class ProviderCrudTests(TempProviderCase):
    def test_create_provider_fills_type_defaults(self):
        config = self.registry.create("deepseek")
        self.assertEqual(config.provider_id, "deepseek-main")
        self.assertEqual(config.kind, "llm")
        self.assertEqual(config.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(config.model, "deepseek-chat")
        self.assertEqual(config.timeout, 60.0)
        self.assertTrue(config.enabled)
        self.assertEqual(config.credential_ref, "provider:deepseek-main")
        self.assertEqual(config.validate(), [])

    def test_create_with_explicit_values(self):
        config = self.registry.create("openai_compatible", provider_id="self-hosted",
                                      label="自建网关", base_url="http://10.0.0.8:8000/v1/",
                                      model="qwen2.5-7b", timeout=12.5)
        self.assertEqual(config.provider_id, "self-hosted")
        self.assertEqual(config.base_url, "http://10.0.0.8:8000/v1", "尾部斜杠应被规整掉")
        self.assertEqual(config.endpoint("/models"), "http://10.0.0.8:8000/v1/models")
        self.assertEqual(config.timeout, 12.5)

    def test_update_persists_to_disk(self):
        self.registry.create("deepseek")
        updated = self.registry.update("deepseek-main", model="deepseek-reasoner", timeout=30)
        self.assertEqual(updated.model, "deepseek-reasoner")
        self.assertEqual(updated.timeout, 30)
        self.assertEqual(ProviderRegistry(self.root).get("deepseek-main").model, "deepseek-reasoner")

    def test_partial_update_keeps_other_fields(self):
        self.registry.create("deepseek", provider_id="d1", label="主模型", model="deepseek-chat")
        self.registry.update("d1", base_url="https://proxy.example.com/v1")
        config = self.registry.get("d1")
        self.assertEqual(config.label, "主模型")
        self.assertEqual(config.model, "deepseek-chat")
        self.assertEqual(config.base_url, "https://proxy.example.com/v1")

    def test_enable_and_disable(self):
        self.registry.create("deepseek")
        self.assertFalse(self.registry.disable("deepseek-main").enabled)
        self.assertTrue(self.registry.get("deepseek-main").enabled is False)
        self.assertEqual(self.registry.list(enabled_only=True), [])
        self.assertTrue(self.registry.enable("deepseek-main").enabled)
        self.assertEqual(len(self.registry.list(enabled_only=True)), 1)

    def test_default_prefers_enabled(self):
        self.registry.create("deepseek", provider_id="a")
        self.registry.create("glm", provider_id="b")
        self.registry.disable("a")
        self.assertEqual(self.registry.default("llm").provider_id, "b")
        self.assertIsNone(self.registry.default("asr"))

    def test_delete_removes_provider_and_its_credential(self):
        config = self.registry.create("deepseek")
        self.registry.set_credentials(config.provider_id, {"api_key": FAKE_KEY})
        self.assertTrue(self.registry.delete(config.provider_id))
        self.assertIsNone(self.registry.get(config.provider_id))
        self.assertFalse(self.credentials.is_set(config.credential_ref))

    def test_types_catalog_is_exposed(self):
        types = {entry["type_id"]: entry for entry in self.registry.types()}
        for expected in ("deepseek", "minimax", "glm", "openai", "openai_compatible",
                         "asr", "object_storage", "publish"):
            self.assertIn(expected, types)
        self.assertEqual(types["deepseek"]["kind"], "llm")
        self.assertEqual(types["object_storage"]["credential_fields"], ["access_key_secret"])
        self.assertEqual(types["asr"]["probe"], "config_only")


# ---------------------------------------------------------------------------
# 4–8. 密钥的保存 / 更新 / 删除 / 掩码
# ---------------------------------------------------------------------------

class CredentialTests(TempProviderCase):
    def setUp(self):
        super().setUp()
        self.config = self.registry.create("deepseek")

    def test_secret_is_saved_and_recognised(self):
        self.registry.set_credentials(self.config.provider_id, {"api_key": FAKE_KEY})
        self.assertTrue(self.registry.has_credentials(self.config.provider_id))
        self.assertEqual(self.credentials.reveal(self.config.credential_ref), FAKE_KEY)

    def test_secret_update_replaces_previous(self):
        self.registry.set_credentials(self.config.provider_id, {"api_key": FAKE_KEY})
        self.registry.set_credentials(self.config.provider_id, {"api_key": "sk-second-key-0001"})
        self.assertEqual(self.credentials.reveal(self.config.credential_ref), "sk-second-key-0001")
        self.assertEqual(self.credentials.fields(self.config.credential_ref), ["api_key"])

    def test_blank_secret_update_does_not_delete_existing(self):
        self.registry.set_credentials(self.config.provider_id, {"api_key": FAKE_KEY})
        result = self.registry.set_credentials(self.config.provider_id, {"api_key": "   "})
        self.assertEqual(result["stored"], [])
        self.assertEqual(result["skipped"], ["api_key"])
        self.assertEqual(self.credentials.reveal(self.config.credential_ref), FAKE_KEY)
        self.assertTrue(self.registry.has_credentials(self.config.provider_id))

    def test_explicit_delete_and_reset(self):
        self.registry.set_credentials(self.config.provider_id, {"api_key": FAKE_KEY})
        self.assertEqual(self.registry.clear_credentials(self.config.provider_id), 1)
        self.assertEqual(self.credentials.reveal(self.config.credential_ref), "")
        self.assertFalse(self.registry.has_credentials(self.config.provider_id))
        # 再删一次是幂等的，不报错
        self.assertEqual(self.registry.clear_credentials(self.config.provider_id), 0)

    def test_mask_only_reveals_last_four(self):
        self.registry.set_credentials(self.config.provider_id, {"api_key": FAKE_KEY})
        mask = self.registry.credentials_view(self.config.provider_id)["api_key"]["mask"]
        self.assertEqual(mask, "****" + FAKE_KEY[-4:])
        self.assertNotIn(FAKE_KEY[:-4], mask)
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret("abc"), "****", "太短的密钥一位都不露")
        self.assertEqual(mask_secret("123456"), "****")
        self.assertEqual(mask_secret("1234567"), "****4567")

    def test_multiple_credential_fields(self):
        storage = self.registry.create("object_storage", provider_id="oss", label="阿里云 OSS")
        self.registry.set_credentials("oss", {"access_key_secret": "LTAI-secret-value-9"})
        view = self.registry.credentials_view("oss")
        self.assertTrue(view["access_key_secret"]["set"])
        self.assertEqual(view["access_key_secret"]["mask"], "****ue-9")
        self.assertTrue(self.registry.has_credentials("oss"))
        self.assertEqual(self.registry.clear_credentials("oss", "access_key_secret"), 1)
        self.assertFalse(self.registry.has_credentials(storage.provider_id))

    def test_clear_whole_store(self):
        self.registry.set_credentials(self.config.provider_id, {"api_key": FAKE_KEY})
        other = self.registry.create("glm", provider_id="glm-1")
        self.registry.set_credentials(other.provider_id, {"api_key": "glm-key-abcdef"})
        self.assertEqual(self.credentials.clear(), 2)
        self.assertEqual(self.credentials.diagnostics()["refs"], 0)


# ---------------------------------------------------------------------------
# 7 / 9. 普通读取不返回密钥 + 掩码 + 脱敏
# ---------------------------------------------------------------------------

class NoLeakTests(TempProviderCase):
    def setUp(self):
        super().setUp()
        self.config = self.registry.create("deepseek")
        self.registry.set_credentials(self.config.provider_id, {"api_key": FAKE_KEY})
        self.adapter = self.adapter()

    def test_public_views_never_contain_the_secret(self):
        blobs = [
            json.dumps(self.registry.public(), ensure_ascii=False),
            json.dumps(self.config.public(self.credentials), ensure_ascii=False),
            json.dumps(self.registry.get("deepseek-main").to_dict(), ensure_ascii=False),
            json.dumps(self.registry.credentials_view("deepseek-main"), ensure_ascii=False),
            json.dumps(self.registry.diagnostics(), ensure_ascii=False),
            json.dumps(self.credentials.diagnostics(), ensure_ascii=False),
            json.dumps(self.adapter.public_view(), ensure_ascii=False),
        ]
        for blob in blobs:
            self.assertNotIn(FAKE_KEY, blob)
        self.assertIn("****" + FAKE_KEY[-4:], blobs[0], "掩码要出现，表示「已配置」")

    def test_serialization_of_provider_has_no_secret_field(self):
        stored = json.loads((self.root / "content-factory-providers.json").read_text(encoding="utf-8"))
        self.assertNotIn(FAKE_KEY, json.dumps(stored, ensure_ascii=False))
        self.assertNotIn("api_key", json.dumps(stored, ensure_ascii=False))

    def test_settings_section_keeps_existing_ui_contract(self):
        self.adapter.save({"api_key": FAKE_KEY, "model": "deepseek-chat"})
        view = self.adapter.public_view()
        self.assertEqual(view["api_key"], "", "既有字段仍然是空串")
        self.assertTrue(view["apiKeySet"])
        self.assertEqual(view["apiKeyMask"], "****" + FAKE_KEY[-4:])
        self.assertEqual(view["credentialSource"], "credential")
        self.assertNotIn(FAKE_KEY, json.dumps(view, ensure_ascii=False))

    def test_settings_public_view_has_no_plaintext(self):
        self.adapter.save({"api_key": FAKE_KEY})
        public = self.settings.public()
        self.assertEqual(public["ai"]["api_key"], "", "设置视图永远不回明文")
        self.assertNotIn(FAKE_KEY, json.dumps(public, ensure_ascii=False))

    def test_redact_helpers(self):
        self.assertNotIn(FAKE_KEY, redact(f"Authorization: Bearer {FAKE_KEY}"))
        self.assertNotIn(FAKE_KEY, redact(f"api_key={FAKE_KEY}"))
        self.assertNotIn(FAKE_KEY, redact(f"https://x/y?api_key={FAKE_KEY}&z=1"))
        self.assertNotIn("sk-live-abcdef123456", redact("key=sk-live-abcdef123456"))
        cleaned = redact_mapping({"api_key": FAKE_KEY, "apiKeySet": True, "note": f"用 {FAKE_KEY} 调"})
        self.assertEqual(cleaned["api_key"], "****")
        self.assertTrue(cleaned["apiKeySet"], "状态位布尔值不能被抹掉")
        self.assertNotIn(FAKE_KEY, cleaned["note"])

    def test_logs_never_print_the_secret(self):
        stream = io.StringIO()
        logger = logging.getLogger("content_factory.providers.test")
        handler = logging.StreamHandler(stream)
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        install_log_filter(logger)
        self.assertIsInstance(handler.filters[0], SecretRedactionFilter)
        logger.info("调用模型失败：Authorization=Bearer %s", FAKE_KEY)
        logger.warning("请求参数 %s", {"api_key": FAKE_KEY})
        output = stream.getvalue()
        self.assertNotIn(FAKE_KEY, output)
        self.assertIn("****", output)

    def test_error_messages_do_not_leak_the_secret(self):
        boom = FakeHttp(get=RuntimeError(f"connection refused while key={FAKE_KEY}"))
        result = test_connection("deepseek-main", registry=self.registry, http=boom)
        self.assertFalse(result["ok"])
        self.assertNotIn(FAKE_KEY, json.dumps(result, ensure_ascii=False))

    def test_server_echoed_secret_is_redacted(self):
        echo = FakeHttp(get=FakeResponse(401, {"error": {"message": f"invalid key {FAKE_KEY}"}}))
        result = test_connection("deepseek-main", registry=self.registry, http=echo)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "auth")
        self.assertNotIn(FAKE_KEY, json.dumps(result, ensure_ascii=False))

    def test_secret_field_detection_is_not_over_eager(self):
        for name in ("api_key", "apiKey", "access_key_secret", "smtp_password", "token",
                     "client_secret", "openai_api_key"):
            self.assertTrue(is_secret_field(name), name)
        for name in ("max_tokens", "token_limit", "temperature", "api_base", "access_key_id",
                     "apiKeySet", "apiKeyMask", "batch_size", "webhook_url"):
            self.assertFalse(is_secret_field(name), name)

    def test_extra_rejects_secret_looking_keys(self):
        with self.assertRaises(ProviderError) as caught:
            self.registry.create("deepseek", provider_id="bad-extra",
                                 extra={"api_key": "should-not-be-here"})
        self.assertEqual(caught.exception.code, "secret_in_extra")


# ---------------------------------------------------------------------------
# 11–13. 非法输入
# ---------------------------------------------------------------------------

class ValidationTests(TempProviderCase):
    def test_unknown_provider_type_is_rejected(self):
        with self.assertRaises(ProviderError) as caught:
            self.registry.create("not-a-vendor")
        self.assertEqual(caught.exception.code, "invalid_provider_type")

    def test_unknown_provider_id_is_rejected(self):
        with self.assertRaises(ProviderError) as caught:
            self.registry.update("ghost", model="x")
        self.assertEqual(caught.exception.code, "unknown_provider")
        with self.assertRaises(ProviderError):
            self.registry.require("ghost")

    def test_duplicate_provider_id_is_rejected(self):
        self.registry.create("deepseek", provider_id="dup")
        with self.assertRaises(ProviderError) as caught:
            self.registry.create("glm", provider_id="dup")
        self.assertEqual(caught.exception.code, "duplicate_provider")

    def test_provider_type_cannot_be_changed(self):
        self.registry.create("deepseek", provider_id="fixed")
        with self.assertRaises(ProviderError) as caught:
            self.registry.update("fixed", provider_type="glm")
        self.assertEqual(caught.exception.code, "immutable_field")

    def test_invalid_base_url_is_rejected(self):
        cases = ["ftp://api.example.com/v1", "http://", "https://user:pass@api.example.com/v1",
                 "not a url", "javascript:alert(1)"]
        for index, value in enumerate(cases):
            with self.assertRaises(ProviderError, msg=value) as caught:
                self.registry.create("openai_compatible", provider_id=f"bad-{index}",
                                     base_url=value, model="m")
            self.assertEqual(caught.exception.code, "invalid_base_url", value)

    def test_base_url_requires_scheme_but_accepts_bare_host(self):
        config = self.registry.create("openai_compatible", provider_id="bare",
                                      base_url="api.example.com/v1", model="m")
        self.assertEqual(config.base_url, "https://api.example.com/v1")

    def test_empty_base_url_is_rejected_when_required(self):
        with self.assertRaises(ProviderError) as caught:
            self.registry.create("openai_compatible", base_url="", model="m")
        self.assertEqual(caught.exception.code, "invalid_base_url")
        # 对象存储不要求 base_url
        self.registry.create("object_storage", provider_id="oss")

    def test_invalid_timeout_is_rejected(self):
        for value in (0, -1, 601, "abc", None if False else float("nan")):
            with self.assertRaises(ProviderError, msg=repr(value)) as caught:
                coerce_timeout(value)
            self.assertEqual(caught.exception.code, "invalid_timeout")
        with self.assertRaises(ProviderError):
            self.registry.create("deepseek", provider_id="bad-timeout", timeout=9999)

    def test_timeout_is_coerced_from_string(self):
        self.assertEqual(coerce_timeout("30"), 30.0)
        self.assertEqual(coerce_timeout(None, 45), 45.0)

    def test_missing_model_falls_back_to_type_default(self):
        config = self.registry.create("deepseek", provider_id="no-model", model="")
        self.assertEqual(config.model, "deepseek-chat", "空 model 用类型默认值补齐")
        self.registry.create("openai_compatible", provider_id="custom",
                             base_url="https://llm.internal/v1", model="qwen-max")
        with self.assertRaises(ProviderError) as caught:
            self.registry.update("custom", model="")
        self.assertEqual(caught.exception.code, "invalid_model")
        self.assertEqual(self.registry.get("custom").model, "qwen-max", "失败的更新不能落盘")

    def test_provider_config_validate_reports_all_errors(self):
        config = ProviderConfig(provider_id="x!", provider_type="deepseek", base_url="ftp://h",
                                model="m", timeout=0)
        codes = {entry["code"] for entry in config.validate()}
        self.assertIn("invalid_provider_id", codes)
        self.assertIn("invalid_base_url", codes)
        self.assertIn("invalid_timeout", codes)


# ---------------------------------------------------------------------------
# 14–15. 连接测试
# ---------------------------------------------------------------------------

class ConnectionTestTests(TempProviderCase):
    def setUp(self):
        super().setUp()
        self.registry.create("deepseek", provider_id="ds")

    def test_success_via_models_probe(self):
        self.registry.set_credentials("ds", {"api_key": FAKE_KEY})
        http = FakeHttp(get=FakeResponse(200, {"data": [{"id": "deepseek-chat"}]}))
        result = test_connection("ds", registry=self.registry, http=http)
        self.assertTrue(result["ok"])
        self.assertEqual(result["probe"], "models")
        self.assertEqual(result["endpoint"], "https://api.deepseek.com/v1/models")
        self.assertEqual(result["model"], "deepseek-chat")
        self.assertEqual(result["error_type"], None)
        self.assertIsInstance(result["latency_ms"], int)
        self.assertTrue(result["model_found"])
        self.assertEqual(http.calls[0]["method"], "GET")

    def test_missing_credential_is_reported_without_network(self):
        http = FakeHttp()
        result = test_connection("ds", registry=self.registry, http=http)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "missing_credential")
        self.assertEqual(http.calls, [], "没配密钥就不该发请求")

    def test_auth_failure_returns_fixed_shape(self):
        self.registry.set_credentials("ds", {"api_key": FAKE_KEY})
        http = FakeHttp(get=FakeResponse(401, {"error": {"message": "Authentication Fails"}}))
        result = test_connection("ds", registry=self.registry, http=http)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "auth")
        self.assertEqual(result["provider"], "ds")
        self.assertIn("API Key", result["message"])
        for key in ("ok", "provider", "model", "latency_ms", "error_type", "message", "probe"):
            self.assertIn(key, result)

    def test_models_probe_falls_back_to_minimal_chat(self):
        self.registry.set_credentials("ds", {"api_key": FAKE_KEY})
        http = FakeHttp(get=FakeResponse(404, {"error": "not found"}),
                        post=FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]}))
        result = test_connection("ds", registry=self.registry, http=http)
        self.assertTrue(result["ok"], "只有极小请求能验证时必须退到它，而不是判定不通")
        self.assertEqual(result["probe"], "chat_min")
        self.assertEqual([call["method"] for call in http.calls], ["GET", "POST"])

    def test_chat_probe_is_minimal_and_uses_bearer_header(self):
        self.registry.create("minimax", provider_id="mm")
        self.registry.set_credentials("mm", {"api_key": FAKE_KEY})
        payloads = []

        class RecordingHttp(FakeHttp):
            def post(self, url, headers=None, timeout=None, json=None):
                payloads.append(json)
                return super().post(url, headers=headers, timeout=timeout, json=json)

        http = RecordingHttp(post=FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]}))
        result = test_connection("mm", registry=self.registry, http=http)
        self.assertTrue(result["ok"])
        self.assertEqual(result["endpoint"], "https://api.minimax.chat/v1/text/chatcompletion_v2")
        self.assertEqual(http.calls[0]["headers"]["Authorization"], f"Bearer {FAKE_KEY}")
        self.assertEqual(payloads[0]["max_tokens"], 1, "探测必须是最小请求，不能默认烧钱")
        self.assertEqual(payloads[0]["temperature"], 0)
        self.assertEqual(len(payloads[0]["messages"]), 1)

    def test_network_error_is_classified(self):
        self.registry.set_credentials("ds", {"api_key": FAKE_KEY})
        http = FakeHttp(get=ConnectionError("dns lookup failed"))
        result = test_connection("ds", registry=self.registry, http=http)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "network")

    def test_server_error_is_classified(self):
        self.registry.set_credentials("ds", {"api_key": FAKE_KEY})
        http = FakeHttp(get=FakeResponse(503, {"error": "upstream busy"}))
        result = test_connection("ds", registry=self.registry, http=http)
        self.assertEqual(result["error_type"], "server")

    def test_config_only_types_never_call_the_network(self):
        self.registry.create("asr", provider_id="whisper", base_url="https://api.openai.com/v1")
        self.registry.set_credentials("whisper", {"api_key": FAKE_KEY})
        http = FakeHttp()
        result = test_connection("whisper", registry=self.registry, http=http)
        self.assertTrue(result["ok"])
        self.assertEqual(result["probe"], "config_only")
        self.assertEqual(http.calls, [], "ASR 探测会产生真实调用，必须只校验配置")

    def test_invalid_config_is_reported_before_network(self):
        http = FakeHttp()
        result = test_connection({"provider_type": "deepseek", "base_url": "ftp://x", "model": "m"},
                                 registry=self.registry, http=http)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_config")
        self.assertEqual(http.calls, [])

    def test_unknown_provider_object_is_reported(self):
        result = test_connection(12345, registry=self.registry, http=FakeHttp())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_provider")

    def test_staged_connection_test_does_not_persist_the_key(self):
        adapter = self.adapter()
        http = FakeHttp(get=FakeResponse(200, {"data": [{"id": "deepseek-chat"}]}))
        result = adapter.test_connection({"api_key": FAKE_KEY, "model": "deepseek-chat"}, http=http)
        self.assertTrue(result["ok"])
        self.assertFalse(self.credentials.is_set("provider:ai"), "测试连接不能写凭据库")
        self.assertNotIn(FAKE_KEY, json.dumps(self.settings.public(), ensure_ascii=False))
        self.assertNotIn(FAKE_KEY, self._credentials_file_text())

    def test_staged_connection_test_follows_the_form_provider(self):
        """表单切到 GLM 再点测试，测的就该是 GLM，而不是已保存的 DeepSeek。"""
        adapter = self.adapter()
        http = FakeHttp(post=FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]}))
        result = adapter.test_connection({"provider": "智谱 GLM", "model": "glm-4-flash",
                                          "api_key": FAKE_KEY}, http=http)
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider_type"], "glm")
        self.assertEqual(result["endpoint"],
                         "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.assertEqual(self.settings.section("ai")["provider"], "DeepSeek", "测试不改设置")

    def test_probe_timeout_is_capped(self):
        self.registry.set_credentials("ds", {"api_key": FAKE_KEY})
        self.registry.update("ds", timeout=600)
        http = FakeHttp(get=FakeResponse(200, {"data": []}))
        test_connection("ds", registry=self.registry, http=http)
        self.assertLessEqual(http.calls[0]["timeout"], 15)

    def _credentials_file_text(self):
        path = self.root / "content-factory-credentials.json"
        return path.read_text(encoding="utf-8") if path.is_file() else ""


# ---------------------------------------------------------------------------
# 16–17. 注册表视图 / OpenAI 兼容 / 类型匹配
# ---------------------------------------------------------------------------

class RegistryAndTypeTests(TempProviderCase):
    def test_registry_lists_and_filters(self):
        self.registry.create("deepseek")
        self.registry.create("glm", provider_id="glm-main")
        self.registry.create("object_storage", provider_id="oss")
        self.assertEqual(len(self.registry.list()), 3)
        self.assertEqual([c.provider_id for c in self.registry.list(kind="llm")],
                         ["deepseek-main", "glm-main"])
        self.assertEqual([c.provider_id for c in self.registry.list(kind="storage")], ["oss"])
        self.assertEqual(self.registry.ids(), ["deepseek-main", "glm-main", "oss"])

    def test_openai_compatible_provider_round_trip(self):
        config = self.registry.create("openai_compatible", provider_id="gateway",
                                      label="公司网关", base_url="https://llm.internal/v1",
                                      model="qwen-max", timeout=20,
                                      extra={"organization": "acme", "access_key_id": "AK-123"})
        self.assertEqual(config.kind, "llm")
        self.assertEqual(config.extra["organization"], "acme")
        self.assertEqual(config.extra["access_key_id"], "AK-123", "标识符可以放 extra")
        reopened = ProviderRegistry(self.root).get("gateway")
        self.assertEqual(reopened.extra, config.extra)
        self.assertEqual(reopened.endpoint("chat/completions"), "https://llm.internal/v1/chat/completions")

    def test_every_llm_vendor_shares_one_code_path(self):
        """四家对话厂商都是「OpenAI 兼容 + 目录差异」，不各写一套逻辑。"""
        ids = [spec.type_id for spec in PROVIDER_TYPES if spec.kind == "llm"]
        self.assertEqual(sorted(ids), ["deepseek", "glm", "minimax", "openai", "openai_compatible"])
        for spec in PROVIDER_TYPES:
            if spec.kind == "llm":
                self.assertTrue(spec.chat_path.startswith("/"))

    def test_match_provider_type_from_legacy_names(self):
        self.assertEqual(match_provider_type("DeepSeek").type_id, "deepseek")
        self.assertEqual(match_provider_type("智谱 GLM").type_id, "glm")
        self.assertEqual(match_provider_type("MiniMax").type_id, "minimax")
        self.assertEqual(match_provider_type("", "https://api.deepseek.com/v1").type_id, "deepseek")
        self.assertEqual(match_provider_type("某不存在的服务").type_id, "openai_compatible")

    def test_provider_type_lookup_raises_for_unknown(self):
        with self.assertRaises(ProviderError):
            provider_type("nope")


# ---------------------------------------------------------------------------
# 18. 重启后配置仍在 + 存储安全
# ---------------------------------------------------------------------------

class PersistenceTests(TempProviderCase):
    def test_providers_and_secrets_survive_restart(self):
        config = self.registry.create("glm", provider_id="glm-main", model="glm-4-air", timeout=25)
        self.registry.set_credentials("glm-main", {"api_key": FAKE_KEY})
        self.registry.disable("glm-main")

        # 「重启」：全新的对象，只共享磁盘目录（+ 同一个测试后端的 vault）
        fresh_credentials = CredentialStore(self.root, backend=self.backend)
        fresh_registry = ProviderRegistry(root=self.root, credentials=fresh_credentials)
        restored = fresh_registry.get("glm-main")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.model, "glm-4-air")
        self.assertEqual(restored.timeout, 25)
        self.assertFalse(restored.enabled)
        self.assertEqual(restored.provider_type, "glm")
        self.assertEqual(fresh_credentials.reveal(config.credential_ref), FAKE_KEY)
        self.assertTrue(fresh_registry.has_credentials("glm-main"))

    def test_settings_and_provider_layer_survive_restart(self):
        adapter = self.adapter()
        adapter.save({"api_key": FAKE_KEY, "model": "deepseek-reasoner",
                      "api_base": "https://api.deepseek.com/v1"})
        # 「重启」：全新的设置 / 注册表 / 凭据库，只共享磁盘目录（+ 同一个测试后端）
        fresh = ProviderSettingsAdapter(FactorySettings(self.root),
                                        credentials=CredentialStore(self.root, backend=self.backend))
        self.assertEqual(fresh.provider().model, "deepseek-reasoner")
        self.assertTrue(fresh.api_key_state()["configured"])
        self.assertEqual(fresh.reveal_api_key(), FAKE_KEY)
        self.assertEqual(fresh.public_view()["apiKeyMask"], "****" + FAKE_KEY[-4:])

    def test_test_backend_never_writes_plaintext(self):
        self.registry.create("deepseek")
        self.registry.set_credentials("deepseek-main", {"api_key": FAKE_KEY})
        raw = (self.root / "content-factory-credentials.json").read_text(encoding="utf-8")
        self.assertNotIn(FAKE_KEY, raw, "落盘内容不得是明文")
        stored = json.loads(raw)["items"]["provider:deepseek-main"]["api_key"]
        self.assertEqual(stored["v"], BACKEND_TEST)
        self.assertEqual(self.credentials.status()["mode"], "development")

    @unittest.skipUnless(DPAPI_READY, "DPAPI 不可用（非 Windows）")
    def test_dpapi_backend_encrypts_at_rest_and_round_trips(self):
        store = CredentialStore(self.root, backend=WindowsDPAPIBackend())
        self.assertTrue(store.put("provider:deepseek-main", {"api_key": FAKE_KEY})["ok"])
        raw = (self.root / "content-factory-credentials.json").read_text(encoding="utf-8")
        self.assertNotIn(FAKE_KEY, raw, "落盘内容不得是明文")
        stored = json.loads(raw)["items"]["provider:deepseek-main"]["api_key"]
        self.assertEqual(stored["v"], BACKEND_DPAPI)
        self.assertEqual(store.reveal("provider:deepseek-main"), FAKE_KEY)
        self.assertEqual(store.status()["mode"], "secure")

    def test_undecryptable_secret_is_treated_as_unset(self):
        path = self.root / "content-factory-credentials.json"
        path.write_text(json.dumps({
            "version": 1,
            "items": {"provider:deepseek-main": {
                "api_key": {"v": "dpapi", "data": "bm90LWEtcmVhbC1ibG9i"}, "updated_at": "x"}}},
        ), encoding="utf-8")
        self.registry.create("deepseek")
        with self.assertLogs("content_factory.providers", level="WARNING") as captured:
            self.assertEqual(self.credentials.reveal("provider:deepseek-main"), "")
            self.assertFalse(self.registry.has_credentials("deepseek-main"))
        self.assertTrue(any("无法读取" in line for line in captured.output))
        self.assertTrue(self.credentials.diagnostics()["errors"], "解不开要如实记录，而不是假装没有")

    def test_corrupt_files_fall_back_to_empty(self):
        (self.root / "content-factory-providers.json").write_text("{ not json", encoding="utf-8")
        (self.root / "content-factory-credentials.json").write_text("{ not json", encoding="utf-8")
        with self.assertLogs("content_factory.providers", level="WARNING") as captured:
            fresh = ProviderRegistry(root=self.root)
            self.assertEqual(fresh.list(), [])
            self.assertTrue(fresh.diagnostics()["errors"])
        self.assertTrue(any("无法解析" in line for line in captured.output))
        # 仍然可以正常新建（不会因为旧文件坏了就永久不可用）
        self.assertEqual(fresh.create("deepseek").provider_id, "deepseek-main")


# ---------------------------------------------------------------------------
# 与既有 AI 标注闭环的兼容
# ---------------------------------------------------------------------------

class LegacyIntegrationTests(TempProviderCase):
    def test_adapter_save_keeps_enrichment_service_working(self):
        adapter = self.adapter()
        adapter.save({"api_key": FAKE_KEY, "model": "deepseek-chat",
                      "api_base": "https://api.deepseek.com/v1"})
        service = EnrichmentService(self.settings)
        self.assertTrue(service.configured(), "现有闭环必须仍然认为「已配置」")
        self.assertEqual(service.config()["api_key"], FAKE_KEY)
        self.assertEqual(service.config()["model"], "deepseek-chat")

    def test_adapter_reads_legacy_key_written_before_this_layer_existed(self):
        """老装机：密钥本来就在设置 JSON 里，不迁移也应照常可用。"""
        self.settings.update("ai", {"api_key": FAKE_KEY, "model": "deepseek-chat"})
        adapter = self.adapter()
        state = adapter.api_key_state()
        self.assertTrue(state["configured"])
        self.assertEqual(state["source"], "settings")
        self.assertEqual(state["mask"], "****" + FAKE_KEY[-4:])
        self.assertEqual(adapter.reveal_api_key(), FAKE_KEY)
        self.assertNotIn(FAKE_KEY, json.dumps(adapter.public_view(), ensure_ascii=False))

    def test_mirror_can_be_turned_off(self):
        adapter = self.adapter(mirror_legacy=False)
        adapter.save({"api_key": FAKE_KEY})
        self.assertEqual(self.settings.section("ai")["api_key"], "")
        self.assertEqual(adapter.reveal_api_key(), FAKE_KEY, "关掉镜像后仍从凭据库取到")

    def test_clear_api_key_removes_both_copies(self):
        adapter = self.adapter()
        adapter.save({"api_key": FAKE_KEY})
        self.assertEqual(adapter.clear_api_key()["removed"], 1)
        self.assertFalse(adapter.api_key_state()["configured"])
        self.assertEqual(self.settings.section("ai")["api_key"], "")
        self.assertEqual(adapter.reveal_api_key(), "")

    def test_blank_save_keeps_key_and_other_fields_update(self):
        adapter = self.adapter()
        adapter.save({"api_key": FAKE_KEY, "model": "deepseek-chat"})
        result = adapter.save({"api_key": "", "temperature": 0.5, "model": "deepseek-reasoner"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped"], ["api_key"])
        self.assertEqual(adapter.reveal_api_key(), FAKE_KEY)
        self.assertEqual(self.settings.section("ai")["model"], "deepseek-reasoner")
        self.assertEqual(self.settings.section("ai")["temperature"], 0.5)

    def test_invalid_save_writes_nothing(self):
        adapter = self.adapter()
        adapter.save({"api_key": FAKE_KEY})
        result = adapter.save({"api_base": "ftp://nope", "model": "x"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_base_url")
        self.assertEqual(self.settings.section("ai")["api_base"], "https://api.deepseek.com/v1")
        self.assertEqual(adapter.reveal_api_key(), FAKE_KEY)

    def test_ai_provider_is_materialised_in_the_registry(self):
        adapter = self.adapter()
        adapter.save({"api_key": FAKE_KEY, "timeout": 30})
        provider = self.registry.get("ai")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.provider_type, "deepseek")
        self.assertEqual(provider.timeout, 30)
        self.assertEqual(provider.credential_ref, credential_ref_for("ai"))
        self.assertTrue(provider.public(self.credentials)["credential_ready"])
        self.assertNotIn(FAKE_KEY, json.dumps(adapter.providers_view(), ensure_ascii=False))

    def test_adapter_survives_settings_without_root_attribute(self):
        class MinimalSettings:
            """模拟 Settings Core 换实现后的最小接口。"""

            def __init__(self, root):
                self._data = {"ai": {"provider": "DeepSeek", "model": "deepseek-chat",
                                     "api_base": "https://api.deepseek.com/v1", "api_key": ""}}
                self._root = root

            def section(self, name):
                return dict(self._data.get(name) or {})

            def update(self, name, values):
                bucket = self._data.setdefault(name, {})
                for key, value in values.items():
                    if key == "api_key" and not str(value or "").strip():
                        continue
                    bucket[key] = value
                return dict(bucket)

            def save_section(self, name, values):
                self._data[name] = dict(values)
                return dict(values)

        adapter = ProviderSettingsAdapter(MinimalSettings(self.root), registry=self.registry,
                                          credentials=self.credentials)
        result = adapter.save({"api_key": FAKE_KEY, "model": "deepseek-reasoner"})
        self.assertTrue(result["ok"])
        self.assertEqual(adapter.reveal_api_key(), FAKE_KEY)
        self.assertNotIn(FAKE_KEY, json.dumps(adapter.public_view(), ensure_ascii=False))

    def test_adapter_uses_app_data_root_when_settings_has_none(self):
        """Settings Core 的实例没有 root 属性时，退回共享的应用数据目录。"""
        class Rootless:
            def section(self, name):
                return {}

            def update(self, name, values):
                return dict(values)

        adapter = ProviderSettingsAdapter(Rootless())
        self.assertEqual(adapter.registry.root, Path(self.temp.name) / "TikTokBatchMVP")
        self.assertEqual(adapter.credentials.path.parent, Path(self.temp.name) / "TikTokBatchMVP")


# ---------------------------------------------------------------------------
# Hardening A：凭据后端 fail-closed（DPAPI 不可用时绝不落明文）
# ---------------------------------------------------------------------------

# 不带 sk- 前缀、也不带 key= 上下文的密钥：
# 只有「显式登记过」才可能被脱敏，用来证明登记这一步真的在起作用
STEALTH_KEY = "zz29f8e7d6c5b4a1029384756"


class BrokenBackend(TestCredentialBackend):
    """加密时抛错的后端：模拟 DPAPI 调用失败。"""

    backend_id = "broken"
    label = "会失败的后端"

    def protect(self, text):
        from content_factory.providers import CredentialBackendError
        raise CredentialBackendError("模拟加密失败")


class LyingBackend(TestCredentialBackend):
    """写进去没问题、但读出来是别的内容：模拟「看起来成功其实没存好」。"""

    backend_id = "lying"

    def unprotect(self, entry):
        return "something-else-entirely"


class FailClosedBackendTests(TempProviderCase):
    def credentials_file(self):
        return self.root / "content-factory-credentials.json"

    def file_text(self):
        return self.credentials_file().read_text(encoding="utf-8") if self.credentials_file().is_file() else ""

    # ---- 默认后端 ------------------------------------------------------
    @unittest.skipUnless(DPAPI_READY, "DPAPI 不可用（非 Windows）")
    def test_default_backend_is_dpapi(self):
        store = CredentialStore(self.root)
        self.assertEqual(store.backend.backend_id, BACKEND_DPAPI)
        self.assertTrue(store.writable())
        self.assertTrue(store.status()["secure"])
        self.assertEqual(store.status()["mode"], "secure")

    def test_dpapi_unavailable_in_production_refuses_to_save(self):
        """生产/默认模式下后端不可用 → 结构化错误 + 一个字节都不落盘。"""
        with patch.object(WindowsDPAPIBackend, "available", return_value=False):
            store = CredentialStore(self.root)
            self.assertFalse(store.writable())
            result = store.put("provider:ai", {"api_key": FAKE_KEY})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "credential_backend_unavailable")
        self.assertTrue(result["error"], "必须给出可读的原因")
        self.assertEqual(result["stored"], [])
        self.assertTrue(result.get("existingKept"))
        self.assertFalse(self.credentials_file().exists(), "拒绝保存时不允许创建任何文件")

    def test_dpapi_unavailable_does_not_break_existing_configuration(self):
        """后端突然不可用（升级 / 换机器）时，已有条目必须原样可读、不被改写。"""
        self.registry.create("deepseek")
        self.registry.set_credentials("deepseek-main", {"api_key": FAKE_KEY})
        before = self.file_text()

        with patch.object(WindowsDPAPIBackend, "available", return_value=False):
            store = CredentialStore(self.root, backend=UnavailableBackend("DPAPI 不可用"))
            result = store.put("provider:deepseek-main", {"api_key": "sk-new-key-0002"})
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "credential_backend_unavailable")

        self.assertEqual(self.file_text(), before, "失败的写入不能改动文件")
        self.assertEqual(self.credentials.reveal("provider:deepseek-main"), FAKE_KEY,
                         "老密钥仍然读得出来（读取按条目格式分派，不看当前后端）")

    def test_protect_failure_fails_closed(self):
        store = CredentialStore(self.root, backend=BrokenBackend())
        result = store.put("provider:ai", {"api_key": FAKE_KEY})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "credential_write_failed")
        self.assertNotIn(FAKE_KEY, result["error"])
        self.assertFalse(self.credentials_file().exists())

    def test_no_silent_plaintext_fallback_anywhere(self):
        """把「后端不可用」这条路走一遍，磁盘上不允许出现任何明文。"""
        with patch.object(WindowsDPAPIBackend, "available", return_value=False):
            store = CredentialStore(self.root)
            store.put("provider:ai", {"api_key": FAKE_KEY})
            store.set("provider:ai", FAKE_KEY, field="token")
        self.assertNotIn(FAKE_KEY, self.file_text())
        self.assertFalse(self.credentials_file().exists())

    # ---- 显式非安全后端 -------------------------------------------------
    def test_plaintext_backend_requires_explicit_opt_in(self):
        default = CredentialStore(self.root, backend_name=BACKEND_PLAIN)
        self.assertFalse(default.writable(), "没显式开启时明文后端必须被拒绝")
        self.assertEqual(default.put("provider:ai", {"api_key": FAKE_KEY})["code"],
                         "credential_backend_unavailable")

        allowed = CredentialStore(self.root, backend_name=BACKEND_PLAIN, allow_insecure=True)
        self.assertTrue(allowed.writable())
        self.assertTrue(allowed.put("provider:ai", {"api_key": FAKE_KEY})["ok"])
        self.assertEqual(allowed.status()["mode"], "insecure")
        self.assertIn(FAKE_KEY, self.file_text())               # 显式选择的结果，如实标注
        self.assertEqual(allowed.status()["insecureEntries"], 1)

    def test_allow_insecure_can_come_from_env(self):
        with patch.dict(os.environ, {"CONTENT_FACTORY_ALLOW_INSECURE_CREDENTIALS": "1"}):
            self.assertTrue(CredentialStore(self.root, backend_name=BACKEND_PLAIN).writable())
        with patch.dict(os.environ, {"CONTENT_FACTORY_ALLOW_INSECURE_CREDENTIALS": "0"}):
            self.assertFalse(CredentialStore(self.root, backend_name=BACKEND_PLAIN).writable())

    def test_backend_name_can_come_from_env(self):
        with patch.dict(os.environ, {BACKEND_ENV: BACKEND_TEST}):
            store = CredentialStore(self.root)
        self.assertEqual(store.backend.backend_id, BACKEND_TEST)
        self.assertTrue(store.writable())
        self.assertEqual(store.status()["mode"], "development", "测试后端不落明文，但不是安全后端")

    def test_unknown_backend_name_fails_closed(self):
        store = CredentialStore(self.root, backend_name="magic-cloud-kms")
        self.assertFalse(store.writable())
        self.assertEqual(store.put("provider:ai", {"api_key": FAKE_KEY})["code"],
                         "credential_backend_unavailable")
        self.assertIn("magic-cloud-kms", store.unavailable_reason())

    def test_custom_backend_can_be_registered_without_touching_the_registry(self):
        """未来接 Keychain / Secret Service 的路径：注册一个工厂即可，别处不用改。"""
        class FakeKeychainBackend(TestCredentialBackend):
            backend_id = "keychain"
            label = "Keychain（示例）"
            secure = True

        register_credential_backend("keychain", lambda: FakeKeychainBackend(namespace="kc"))
        self.assertIn("keychain", registered_credential_backends())
        store = CredentialStore(self.root, backend_name="keychain")
        self.assertEqual(store.backend.backend_id, "keychain")
        self.assertTrue(store.writable())
        registry = ProviderRegistry(root=self.root, credentials=store)
        registry.create("deepseek")
        self.assertTrue(registry.set_credentials("deepseek-main", {"api_key": FAKE_KEY})["ok"])
        self.assertTrue(registry.has_credentials("deepseek-main"))
        self.assertEqual(store.reveal(credential_ref_for("deepseek-main")), FAKE_KEY)
        # 未注册的名字仍然是 fail closed
        self.assertFalse(CredentialStore(self.root, backend_name="someones-kms").writable())

    # ---- 后端替换 ------------------------------------------------------
    def test_backend_substitution_keeps_registry_working(self):
        """换后端不影响 ProviderRegistry：老条目照样能读，新条目用新后端写。"""
        path = self.credentials_file()
        path.write_text(json.dumps({"version": 1, "items": {
            "provider:deepseek-main": {"api_key": {"v": "plain", "data": "legacy-key-0001"},
                                       "updated_at": "2026-01-01 00:00:00"}}}), encoding="utf-8")
        store = CredentialStore(self.root, backend=TestCredentialBackend(namespace="sub-1"))
        registry = ProviderRegistry(root=self.root, credentials=store)
        registry.create("deepseek")
        self.assertTrue(registry.has_credentials("deepseek-main"), "历史明文条目仍可读")
        self.assertEqual(store.status()["insecureEntries"], 1)

        # 用新后端覆盖写：条目换成测试后端的格式，旧的明文条目不复存在
        self.assertTrue(registry.set_credentials("deepseek-main", {"api_key": FAKE_KEY})["ok"])
        self.assertEqual(store.status()["insecureEntries"], 0)
        self.assertNotIn("legacy-key-0001", self.file_text())

        # 换成不认识该格式的后端：按「未配置」处理，而不是抛异常或误判为可用
        other = ProviderRegistry(root=self.root,
                                 credentials=CredentialStore(self.root,
                                                             backend=PlaintextBackend()))
        self.assertFalse(other.has_credentials("deepseek-main"))

    def test_unavailable_backend_still_reads_legacy_plaintext_but_refuses_to_reprotect(self):
        self.credentials_file().write_text(json.dumps({"version": 1, "items": {
            "provider:ai": {"api_key": {"v": "plain", "data": FAKE_KEY},
                            "updated_at": "2026-01-01 00:00:00"}}}), encoding="utf-8")
        store = CredentialStore(self.root, backend=UnavailableBackend("DPAPI 不可用"))
        self.assertEqual(store.reveal("provider:ai"), FAKE_KEY, "不能因为升级就让用户的配置失效")
        self.assertEqual(store.status()["insecureEntries"], 1)
        before = self.file_text()
        result = store.reprotect()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "credential_backend_unavailable")
        self.assertTrue(result.get("existingKept"))
        self.assertEqual(self.file_text(), before, "拒绝执行时不能动原数据")

    def test_reprotect_encrypts_legacy_plaintext(self):
        self.credentials_file().write_text(json.dumps({"version": 1, "items": {
            "provider:ai": {"api_key": {"v": "plain", "data": FAKE_KEY},
                            "updated_at": "2026-01-01 00:00:00"}}}), encoding="utf-8")
        result = self.credentials.reprotect()
        self.assertTrue(result["ok"])
        self.assertEqual(result["converted"], 1)
        self.assertNotIn(FAKE_KEY, self.file_text())
        self.assertEqual(self.credentials.reveal("provider:ai"), FAKE_KEY)
        self.assertEqual(self.credentials.status()["insecureEntries"], 0)
        self.assertEqual(self.credentials.reprotect()["converted"], 0, "重复执行是幂等的")

    # ---- 适配层也要 fail closed -----------------------------------------
    def test_adapter_save_is_atomic_when_backend_is_unavailable(self):
        settings_before = json.dumps(self.settings.section("ai"), ensure_ascii=False)
        adapter = ProviderSettingsAdapter(
            self.settings, registry=self.registry,
            credentials=CredentialStore(self.root, backend=UnavailableBackend("DPAPI 不可用")))
        result = adapter.save({"api_key": FAKE_KEY, "model": "deepseek-reasoner"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "credential_backend_unavailable")
        self.assertFalse(result["saved"])
        self.assertEqual(json.dumps(self.settings.section("ai"), ensure_ascii=False), settings_before,
                         "密钥存不进去时，连非密钥字段也不落盘（不能留下半截状态）")
        self.assertNotIn(FAKE_KEY, self.file_text())
        self.assertFalse(adapter.public_view()["credentialBackend"]["writable"])
        self.assertTrue(adapter.public_view()["credentialBackend"]["reason"])

    def test_adapter_never_mirrors_when_backend_is_unavailable(self):
        """最危险的一条路径：镜像本来会把明文写进设置 —— 存不进去时绝不能镜像。"""
        adapter = ProviderSettingsAdapter(
            self.settings, registry=self.registry,
            credentials=CredentialStore(self.root, backend=UnavailableBackend()))
        adapter.save({"api_key": FAKE_KEY})
        self.assertEqual(self.settings.section("ai")["api_key"], "")
        settings_path = self.settings.path
        text = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else ""
        self.assertNotIn(FAKE_KEY, text)


# ---------------------------------------------------------------------------
# Hardening B：旧密钥迁移（幂等 + 失败不删旧值）
# ---------------------------------------------------------------------------

class LegacyMigrationTests(TempProviderCase):
    def setUp(self):
        super().setUp()
        # 老装机：明文 key 就躺在设置里
        self.settings.update("ai", {"api_key": FAKE_KEY, "model": "deepseek-chat",
                                    "api_base": "https://api.deepseek.com/v1"})
        self.adapter = self.adapter()

    def settings_file_text(self):
        return self.settings.path.read_text(encoding="utf-8")

    def test_migration_moves_key_into_credential_store_and_clears_settings(self):
        self.assertEqual(self.adapter.api_key_state()["source"], "settings")
        result = self.adapter.migrate_legacy_secret()
        self.assertTrue(result["ok"])
        self.assertTrue(result["migrated"])
        self.assertTrue(result["clearedLegacy"])
        self.assertFalse(result["mirrorLegacy"])
        self.assertEqual(self.credentials.reveal(credential_ref_for("ai")), FAKE_KEY)
        self.assertEqual(self.settings.section("ai")["api_key"], "")
        self.assertNotIn(FAKE_KEY, self.settings_file_text(), "迁移后设置里不允许再有完整密钥")
        self.assertEqual(self.adapter.api_key_state()["source"], "credential")
        self.assertNotIn(FAKE_KEY, json.dumps(self.adapter.public_view(), ensure_ascii=False))

    def test_migration_is_idempotent(self):
        first = self.adapter.migrate_legacy_secret()
        mask = self.adapter.api_key_state()["mask"]
        snapshot = self.settings_file_text()
        for _ in range(3):
            again = self.adapter.migrate_legacy_secret()
            self.assertTrue(again["ok"])
            self.assertFalse(again["migrated"])
            self.assertEqual(again["reason"], "no-legacy-secret")
        self.assertEqual(self.credentials.fields(credential_ref_for("ai")), ["api_key"],
                         "重复迁移不能多出字段")
        self.assertEqual(self.credentials.reveal(credential_ref_for("ai")), FAKE_KEY)
        self.assertEqual(self.adapter.api_key_state()["mask"], mask)
        self.assertEqual(self.settings_file_text(), snapshot)
        self.assertTrue(first["migrated"])

    def test_migration_keeps_legacy_secret_when_backend_unavailable(self):
        adapter = ProviderSettingsAdapter(
            self.settings, registry=self.registry,
            credentials=CredentialStore(self.root, backend=UnavailableBackend("DPAPI 不可用")))
        result = adapter.migrate_legacy_secret()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "credential_backend_unavailable")
        self.assertTrue(result["legacyKept"])
        self.assertEqual(self.settings.section("ai")["api_key"], FAKE_KEY, "失败时绝不删旧密钥")
        self.assertIn(FAKE_KEY, self.settings_file_text())
        self.assertEqual(adapter.api_key_state()["source"], "settings")
        self.assertFalse((self.root / "content-factory-credentials.json").exists())

    def test_migration_rolls_back_when_verification_fails(self):
        """写入「成功」但回读不一致 → 回滚本次写入，旧设置字段保持原样。"""
        lying = LyingBackend(namespace="lying")
        adapter = ProviderSettingsAdapter(
            self.settings, registry=self.registry,
            credentials=CredentialStore(self.root, backend=lying))
        result = adapter.migrate_legacy_secret()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "credential_verify_failed")
        self.assertTrue(result["legacyKept"])
        self.assertEqual(self.settings.section("ai")["api_key"], FAKE_KEY)
        self.assertFalse(adapter.credentials.exists(credential_ref_for("ai")),
                         "校验失败要把刚写进去的东西回滚掉")

    def test_migration_keeps_existing_credential_untouched(self):
        self.registry.upsert(self.adapter.provider())
        self.registry.set_credentials("ai", {"api_key": "sk-already-in-store-01"})
        result = self.adapter.migrate_legacy_secret()
        self.assertTrue(result["ok"])
        self.assertFalse(result["migrated"])
        self.assertEqual(result["reason"], "credential-already-present")
        self.assertEqual(self.credentials.reveal(credential_ref_for("ai")), "sk-already-in-store-01",
                         "已迁移过的密钥不能被旧值覆盖")
        self.assertEqual(self.settings.section("ai")["api_key"], "")
        self.assertFalse(self.adapter.mirror_enabled())

    def test_migration_without_any_secret_is_a_noop(self):
        self.settings.save_section("ai", {**self.settings.section("ai"), "api_key": ""})
        result = self.adapter.migrate_legacy_secret()
        self.assertTrue(result["ok"])
        self.assertFalse(result["migrated"])
        self.assertEqual(result["reason"], "no-secret-to-migrate")
        self.assertFalse(result["credentialConfigured"])

    def test_after_migration_settings_never_store_a_new_key(self):
        self.adapter.migrate_legacy_secret()
        result = self.adapter.save({"api_key": "sk-brand-new-key-0002"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["mirrored"], "迁移后不允许再镜像明文")
        self.assertEqual(self.settings.section("ai")["api_key"], "")
        self.assertNotIn("sk-brand-new-key-0002", self.settings_file_text())
        self.assertEqual(self.adapter.reveal_api_key(), "sk-brand-new-key-0002")

    def test_migration_flag_survives_restart(self):
        self.adapter.migrate_legacy_secret()
        # 「重启」：新设置对象 / 新适配器（共享磁盘与同一个测试后端）
        fresh = ProviderSettingsAdapter(FactorySettings(self.root),
                                        credentials=CredentialStore(self.root, backend=self.backend))
        self.assertFalse(fresh.mirror_enabled(), "重启后不能又冒出明文镜像")
        result = fresh.save({"api_key": "sk-after-restart-0003"})
        self.assertFalse(result["mirrored"])
        self.assertNotIn("sk-after-restart-0003", self.settings_file_text())

    def test_legacy_install_keeps_working_before_migration(self):
        """迁移之前，现有 AI 闭环必须照常读到密钥（老装机零迁移成本）。"""
        self.assertEqual(self.adapter.reveal_api_key(), FAKE_KEY)
        self.assertTrue(EnrichmentService(self.settings).configured())
        self.assertEqual(self.adapter.api_key_state()["source"], "settings")


# ---------------------------------------------------------------------------
# Hardening C：连接测试的泄露面
# ---------------------------------------------------------------------------

class ConnectionTestHardeningTests(TempProviderCase):
    def setUp(self):
        super().setUp()
        self.registry.create("deepseek", provider_id="ds")

    def test_server_echo_of_staged_key_is_redacted(self):
        """界面临时填的密钥（没进过凭据库）被服务端回显时，也必须被抹掉。"""
        http = FakeHttp(get=FakeResponse(401, {"error": {"message": f"invalid api key {STEALTH_KEY}"}}))
        result = test_connection("ds", registry=self.registry, http=http, api_key=STEALTH_KEY)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "auth")
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(STEALTH_KEY, blob)
        self.assertIn("****", blob)

    def test_stored_key_echo_is_redacted(self):
        self.registry.set_credentials("ds", {"api_key": STEALTH_KEY})
        http = FakeHttp(post=FakeResponse(500, {"msg": f"boom {STEALTH_KEY}"}))
        result = test_connection("ds", registry=self.registry, http=http,
                                 probe="chat_min")
        self.assertNotIn(STEALTH_KEY, json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["error_type"], "server")

    def test_secret_never_travels_in_the_url(self):
        self.registry.set_credentials("ds", {"api_key": STEALTH_KEY})
        http = FakeHttp(get=FakeResponse(200, {"data": []}))
        result = test_connection("ds", registry=self.registry, http=http)
        self.assertNotIn(STEALTH_KEY, result["endpoint"])
        self.assertNotIn("?", result["endpoint"], "探测地址不允许带查询串")
        self.assertEqual(http.calls[0]["headers"]["Authorization"], f"Bearer {STEALTH_KEY}")
        self.assertNotIn("Authorization", json.dumps(result, ensure_ascii=False))

    def test_key_smuggled_into_base_url_is_refused(self):
        self.registry.create("openai_compatible", provider_id="evil",
                             base_url=f"https://api.example.com/v1?api_key={STEALTH_KEY}",
                             model="m")
        self.registry.set_credentials("evil", {"api_key": STEALTH_KEY})
        http = FakeHttp()
        result = test_connection("evil", registry=self.registry, http=http)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_config")
        self.assertEqual(http.calls, [], "地址里带凭据时必须拒绝发送")
        self.assertNotIn(STEALTH_KEY, json.dumps(result, ensure_ascii=False))

    def test_rate_limit_and_timeout_are_classified(self):
        self.registry.set_credentials("ds", {"api_key": FAKE_KEY})
        limited = test_connection("ds", registry=self.registry,
                                  http=FakeHttp(get=FakeResponse(429, {"error": "slow down"})))
        self.assertEqual(limited["error_type"], "rate_limit")

        class TimeoutError_(Exception):
            pass

        TimeoutError_.__name__ = "Timeout"
        slow = test_connection("ds", registry=self.registry, http=FakeHttp(get=TimeoutError_("timed out")))
        self.assertEqual(slow["error_type"], "timeout")

    def test_no_credential_means_no_request(self):
        http = FakeHttp()
        result = test_connection("ds", registry=self.registry, http=http)
        self.assertEqual(result["error_type"], "missing_credential")
        self.assertEqual(http.calls, [])

    def test_failed_probe_never_logs_the_key(self):
        """连接测试自己产生的日志里也不能出现密钥。"""
        target = logging.getLogger("content_factory.providers")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(SecretRedactionFilter())
        previous_level = target.level
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
        try:
            test_connection("ds", registry=self.registry, api_key=STEALTH_KEY,
                            http=FakeHttp(get=ConnectionError(f"refused while key={STEALTH_KEY}")))
            test_connection("ds", registry=self.registry, api_key=STEALTH_KEY,
                            http=FakeHttp(get=FakeResponse(500, {"message": f"boom {STEALTH_KEY}"})))
        finally:
            target.removeHandler(handler)
            target.setLevel(previous_level)
        output = stream.getvalue()
        self.assertTrue(output, "至少应该留下一条日志")
        self.assertNotIn(STEALTH_KEY, output)

    def test_probe_strategy_and_path_can_come_from_config_data(self):
        self.registry.create("openai_compatible", provider_id="gateway",
                             base_url="https://llm.internal/v1", model="m",
                             extra={"probe": "chat_min", "chat_path": "/openai/chat/completions"})
        self.registry.set_credentials("gateway", {"api_key": FAKE_KEY})
        http = FakeHttp(post=FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]}))
        result = test_connection("gateway", registry=self.registry, http=http)
        self.assertTrue(result["ok"])
        self.assertEqual(result["endpoint"], "https://llm.internal/v1/openai/chat/completions")
        self.assertEqual(result["probe"], "chat_min")

    def test_absolute_url_override_is_rejected(self):
        with self.assertRaises(ProviderError) as caught:
            self.registry.create("openai_compatible", provider_id="exfil",
                                 base_url="https://llm.internal/v1", model="m",
                                 extra={"chat_path": "https://evil.example.com/steal"})
        self.assertEqual(caught.exception.code, "invalid_path_override")
        with self.assertRaises(ProviderError) as caught:
            self.registry.create("openai_compatible", provider_id="exfil2",
                                 base_url="https://llm.internal/v1", model="m",
                                 extra={"probe": "chat_max"})
        self.assertEqual(caught.exception.code, "invalid_probe")


# ---------------------------------------------------------------------------
# Hardening D：目录自洽 + 只有一条 OpenAI 兼容传输路径
# ---------------------------------------------------------------------------

class CatalogTests(TempProviderCase):
    def test_catalog_is_self_consistent(self):
        self.assertEqual(catalog_problems(), [], "目录数据本身必须自洽")

    def test_endpoint_joining_is_data_driven(self):
        for spec in PROVIDER_TYPES:
            if not spec.default_base_url:
                continue
            config = self.registry.create(spec.type_id, provider_id=f"probe-{spec.type_id}")
            self.assertEqual(config.base_url, spec.default_base_url)
            self.assertEqual(config.endpoint(config.probe_path("models")),
                             spec.default_base_url + config.probe_path("models"))
            self.assertEqual(config.endpoint(config.probe_path("chat_min")),
                             spec.default_base_url + config.probe_path("chat_min"))

    def test_all_llm_vendors_share_one_transport(self):
        """四家对话厂商必须走同一条请求路径，差异只在目录数据里。"""
        shapes = {}
        for spec in PROVIDER_TYPES:
            if spec.kind != "llm":
                continue
            provider_id = f"llm-{spec.type_id}"
            base_url = spec.default_base_url or "https://gateway.example.com/v1"
            self.registry.create(spec.type_id, provider_id=provider_id, base_url=base_url,
                                 model=spec.default_model or "some-model")
            self.registry.set_credentials(provider_id, {"api_key": FAKE_KEY})
            http = FakeHttp(post=FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]}),
                            get=FakeResponse(404, {"error": "no models"}))
            result = test_connection(provider_id, registry=self.registry, http=http)
            self.assertTrue(result["ok"], provider_id)
            call = http.calls[-1]
            shapes[spec.type_id] = (call["method"], call["headers"]["Accept"],
                                    call["headers"]["Content-Type"])
        self.assertEqual(len(shapes), 5)
        self.assertEqual(len(set(shapes.values())), 1,
                         f"各厂商的传输形状必须完全一致：{shapes}")

    def test_no_vendor_specific_client_modules(self):
        """结构上禁止出现 deepseek_client.py / glm_client.py 这种复制品。"""
        package = Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP/content_factory/providers"
        modules = sorted(path.name for path in package.glob("*.py"))
        self.assertEqual(modules, ["__init__.py", "connection_test.py", "credentials.py",
                                   "models.py", "paths.py", "registry.py", "settings_adapter.py"])
        for name in modules:
            self.assertNotIn("_client", name)

    def test_model_list_is_advisory(self):
        """目录里的模型名只是建议值：填目录外的模型不能报错。"""
        config = self.registry.create("deepseek", provider_id="future-model",
                                      model="deepseek-v99-experimental")
        self.assertEqual(config.validate(), [])
        self.assertEqual(config.model, "deepseek-v99-experimental")


if __name__ == "__main__":
    unittest.main()
