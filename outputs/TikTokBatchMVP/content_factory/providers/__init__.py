"""统一 Provider 配置与凭据管理基础设施。

给调用方的一句话：**别再从设置 JSON 里读 api_key，改成问 Provider 层要配置。**

```python
from content_factory.providers import ProviderRegistry, ProviderSettingsAdapter

registry = ProviderRegistry()                       # 读 %LOCALAPPDATA%/TikTokBatchMVP/*.json

# 1) 取「可用的 provider 配置」（不含明文）
config = registry.get("deepseek-main")
if config and config.enabled and registry.has_credentials(config.provider_id):
    endpoint = config.endpoint("/chat/completions")
    model = config.model

# 2) 只在真正发请求时取明文（服务层内部；桥接层/UI 永远不要调）
api_key = registry.credentials.reveal(config.credential_ref)

# 3) 连接测试：统一契约、默认最便宜的探测方式、错误已分类且不含凭据
from content_factory.providers import test_connection
test_connection("deepseek-main", registry=registry)
# -> {ok, provider, model, latency_ms, error_type, message, probe, endpoint, ...}

# 4) 老装机一次性迁移：把设置里的明文 api_key 搬进凭据库并关掉明文镜像
from content_factory.providers import ProviderSettingsAdapter
ProviderSettingsAdapter(settings).migrate_legacy_secret()
```

五个文件各管一件事：

| 文件 | 职责 |
|---|---|
| `models.py` | provider 契约与校验（ProviderConfig / ProviderType 目录），**不含密钥** |
| `credentials.py` | 凭据库 + 凭据后端（DPAPI / 测试 / 明文）+ 掩码脱敏，唯一存密钥的地方 |
| `registry.py` | provider 实例的注册表与持久化 |
| `connection_test.py` | 统一连接测试（config_only / models / chat_min 三档成本） |
| `settings_adapter.py` | 与现有「设置 → AI 加工」表单的最小适配 + 旧密钥迁移 |

密钥默认 fail closed：安全后端不可用时**拒绝保存**新密钥（返回结构化错误、
现有配置不动），只有显式选择 `backend_name="test"` / `"plain"`（或设
`CONTENT_FACTORY_CREDENTIAL_BACKEND`）才会走非加密后端。

本包**不**实现任何业务调用：发 prompt、解析 enrichment、重试、发布、上传
分别属于 AI Enrichment / Pipeline / Publish 模块。
"""

from .connection_test import ConnectionTester, ERROR_TYPES, test_connection  # noqa: F401
from .credentials import (BACKEND_DPAPI, BACKEND_ENV, BACKEND_NONE,  # noqa: F401
                          BACKEND_PLAIN, BACKEND_TEST, CredentialBackend,
                          CredentialBackendError, CredentialStore, PlaintextBackend,
                          SecretRedactionFilter, TestCredentialBackend, UnavailableBackend,
                          WindowsDPAPIBackend, install_log_filter, is_secret_name,
                          mask_secret, redact, redact_mapping, register_credential_backend,
                          registered_credential_backends, resolve_credential_backend)
from .models import (DEFAULT_TIMEOUT, MAX_TIMEOUT, MIN_TIMEOUT, PROBE_STRATEGIES,  # noqa: F401
                     PROVIDER_TYPES, ProviderConfig, ProviderError, ProviderType,
                     catalog_problems, coerce_timeout, credential_ref_for, is_secret_field,
                     match_provider_type, normalize_base_url, provider_from_legacy,
                     provider_type, provider_types, validate_base_url)
from .paths import default_data_root  # noqa: F401
from .registry import ProviderRegistry  # noqa: F401
from .settings_adapter import AI_PROVIDER_ID, ProviderSettingsAdapter  # noqa: F401

__all__ = [
    # 契约
    "ProviderConfig", "ProviderType", "ProviderError", "PROVIDER_TYPES",
    "provider_type", "provider_types", "match_provider_type", "provider_from_legacy",
    "normalize_base_url", "validate_base_url", "coerce_timeout", "credential_ref_for",
    "catalog_problems", "DEFAULT_TIMEOUT", "MIN_TIMEOUT", "MAX_TIMEOUT", "PROBE_STRATEGIES",
    # 凭据
    "CredentialStore", "CredentialBackend", "CredentialBackendError",
    "WindowsDPAPIBackend", "TestCredentialBackend", "PlaintextBackend", "UnavailableBackend",
    "resolve_credential_backend", "register_credential_backend", "registered_credential_backends",
    "BACKEND_DPAPI", "BACKEND_TEST", "BACKEND_PLAIN",
    "BACKEND_NONE", "BACKEND_ENV",
    "mask_secret", "redact", "redact_mapping", "is_secret_field", "is_secret_name",
    "SecretRedactionFilter", "install_log_filter",
    # 注册表
    "ProviderRegistry",
    # 连接测试
    "ConnectionTester", "test_connection", "ERROR_TYPES",
    # 设置适配
    "ProviderSettingsAdapter", "AI_PROVIDER_ID",
    # 路径
    "default_data_root",
]
