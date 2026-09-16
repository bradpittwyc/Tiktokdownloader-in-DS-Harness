"""Settings Core 的回归网：默认值 / 校验 / 存取 / 重置 / 版本与迁移。

这里锁住的是「所有业务模块都要依赖的东西」，所以断言写得比一般单元测试更细：

- 默认值自己必须过得了自己的 schema（否则 reset 会把分区写坏）
- 界面的真实载荷（类型混杂、字段串页、on/off 字符串）必须存得进去
- 非法值一个都不许落盘，且**已有配置一个字都不许被改动**
- 程序重启（新实例、无缓存）后配置必须还在
- 旧接口（update / save_section / save / public / DEFAULTS）行为不变

全部离线：只碰临时目录，不发网络请求。
"""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_bridge import ContentFactoryApi  # noqa: E402
from content_factory.ai_enrichment import EnrichmentService, DEFAULT_PROMPT_TEMPLATE  # noqa: E402
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.settings import (  # noqa: E402
    DEFAULTS, ENGINE_SECTIONS, MIGRATIONS, SCHEMA_VERSION, SECTIONS, VERSION_KEY, FactorySettings,
    Migration, SettingsPersistenceError, SettingsService, UnknownSectionError, default_values,
    register_migration, resolve_section_name, validate_section,
)
from content_factory.settings.migration import detect_version, run as run_migrations  # noqa: E402
from content_factory.settings_store import (  # noqa: E402
    DEFAULTS as LEGACY_DEFAULTS, FactorySettings as LegacyFactorySettings,
    app_data_root as legacy_app_data_root, defaults as legacy_defaults,
)


#: 第一阶段 `settings_store.DEFAULTS` 里 9 个界面分区的指纹。
#: 由 `git show HEAD:.../settings_store.py` 的旧默认值算出（sha256，sort_keys 的规范 JSON）。
#: 改这里之前先想清楚：旧用户的配置文件正是按这份默认值长的。
LEGACY_DEFAULTS_FINGERPRINT = "5de4c549af2458454a6fc19e4b834fffff9975d50f48ec4807c4b630fdf243cb"

LEGACY_SECTION_ORDER = ("general", "work_mode", "logging", "collect", "ai", "publish",
                        "storage", "notify", "account")


def legacy_shape_defaults():
    """把当前 DEFAULTS 还原成第一阶段的形态，用于比对指纹。

    有意为之的两处差异（都在本文件里有专门用例）：
    - collect.dedupe_checks 是这次新增的界面字段，旧默认值里没有；
    - storage.file_types 旧的是 "mp4,mov,..." 字符串，现在统一成数组。
    """
    snapshot = {}
    for name in LEGACY_SECTION_ORDER:
        bucket = {key: value for key, value in DEFAULTS[name].items()
                  if not (name == "collect" and key == "dedupe_checks")}
        if name == "storage":
            bucket["file_types"] = ",".join(bucket["file_types"])
        snapshot[name] = bucket
    return snapshot


class SettingsCase(unittest.TestCase):
    """每个用例一个临时 LOCALAPPDATA，互不干扰。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.settings = FactorySettings(self.root)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    # ---- 小工具 --------------------------------------------------------
    def reopen(self):
        """模拟「程序重启」：全新实例、没有内存缓存。"""
        return FactorySettings(self.root)

    def raw(self, path=None):
        return json.loads((path or self.settings.path).read_text(encoding="utf-8"))

    def snapshot(self):
        return self.settings.path.read_text(encoding="utf-8")


class DefaultsTests(SettingsCase):
    def test_every_section_has_fields_and_defaults(self):
        self.assertEqual(set(DEFAULTS), set(SECTIONS))
        for name, spec in SECTIONS.items():
            self.assertTrue(spec.fields, f"{name} 必须至少有一个字段")
            self.assertEqual(set(DEFAULTS[name]), {item.key for item in spec.fields})

    def test_every_default_value_passes_its_own_schema(self):
        """默认值必须过自己的校验 —— 否则 reset 会把分区写成半截。"""
        for name, spec in SECTIONS.items():
            result = validate_section(spec, spec.defaults(), strict=True, partial=False)
            self.assertTrue(result.ok, f"{name} 的默认值不合法：{result.as_dict()['errors']}")

    def test_no_default_is_a_secret_placeholder(self):
        for name, spec in SECTIONS.items():
            for item in spec.fields:
                if item.secret:
                    self.assertIn(DEFAULTS[name][item.key], ("", None),
                                  f"{item.key} 是敏感字段，默认值必须是空")

    def test_first_phase_defaults_are_unchanged(self):
        """第一阶段被界面/旧文件依赖的默认值，一个都不许变。"""
        expected = {
            ("general", "theme"): "system",
            ("work_mode", "concurrent_tasks"): 3,
            ("logging", "level"): "INFO",
            ("collect", "interval_minutes"): 30,
            ("collect", "download_quality"): "1080p",
            ("ai", "model"): "deepseek-chat",
            ("ai", "temperature"): 0.7,
            ("ai", "max_tokens"): 4000,
            ("publish", "daily_count"): 5,
            ("storage", "disk_warn_gb"): 10,
            ("notify", "smtp_port"): 587,
            ("account", "node_id"): "worker_001",
        }
        for (section, key), value in expected.items():
            self.assertEqual(DEFAULTS[section][key], value, f"{section}.{key} 默认值被改了")
        self.assertEqual(DEFAULTS["ai"]["prompt_template"], DEFAULT_PROMPT_TEMPLATE)

    def test_all_first_phase_defaults_match_the_old_fingerprint(self):
        """175 个旧默认值的整体指纹：任何一处被改动都会在这里炸出来。"""
        blob = json.dumps(legacy_shape_defaults(), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(blob.encode("utf-8")).hexdigest(),
                         LEGACY_DEFAULTS_FINGERPRINT,
                         "第一阶段默认值被改动了；确实要改就必须同时加一条 schema 迁移")

    def test_defaults_are_copies_not_shared_state(self):
        first = default_values("collect")
        first["filter_rules"].append("偷偷加一条")
        self.assertNotIn("偷偷加一条", DEFAULTS["collect"]["filter_rules"])
        self.assertNotEqual(first["filter_rules"], default_values("collect")["filter_rules"])

    def test_engine_sections_exist_for_the_parallel_modules(self):
        for name in ("collector", "transcript", "pipeline", "publishing"):
            self.assertIn(name, SECTIONS)
            self.assertFalse(SECTIONS[name].ui_section)
        self.assertEqual(resolve_section_name("asr"), "transcript")
        self.assertEqual(resolve_section_name("monitor"), "collector")
        self.assertEqual(resolve_section_name("publisher"), "publishing")
        self.assertEqual(resolve_section_name("  ASR "), "transcript", "别名大小写/空格不敏感")
        self.assertEqual(resolve_section_name("Collector"), "collector", "规范名大小写不敏感")
        self.assertIsNone(resolve_section_name("nope"))
        self.assertEqual(len(SECTIONS), len({s.name for s in ENGINE_SECTIONS}) + 9)

    def test_engine_sections_default_to_safe_values(self):
        """新引擎分区默认不许自作主张跑起来（不启用、不真发布、不并发轰炸）。"""
        self.assertFalse(DEFAULTS["collector"]["enabled"])
        self.assertFalse(DEFAULTS["publishing"]["enabled"])
        self.assertTrue(DEFAULTS["publishing"]["dry_run"])
        self.assertGreater(DEFAULTS["collector"]["poll_interval_minutes"], 0)
        self.assertGreater(DEFAULTS["publishing"]["interval_minutes"], 0)
        for name in ("collector", "pipeline", "publishing"):
            self.assertGreaterEqual(DEFAULTS[name]["concurrency"], 1, f"{name} 并发默认值必须 >= 1")


class LoadTests(SettingsCase):
    def test_first_run_gives_defaults_without_creating_a_file(self):
        data = self.settings.load()
        self.assertEqual(data["general"]["theme"], "system")
        self.assertFalse(self.settings.path.exists(), "只读不该往磁盘写文件")

    def test_load_returns_a_detached_copy(self):
        first = self.settings.load()
        first["ai"]["model"] = "被改坏了"
        self.assertEqual(self.settings.load()["ai"]["model"], "deepseek-chat")

    def test_section_and_get_read_through_the_schema(self):
        self.assertEqual(self.settings.section("general")["app_language"], "zh-CN")
        self.assertEqual(self.settings.get("collector", "poll_interval_minutes"), 30)
        self.assertEqual(self.settings.section("不存在的分区"), {})
        self.assertIsNone(self.settings.get("general", "不存在的字段"))

    def test_unknown_keys_survive_a_reload(self):
        self.settings.update("general", {"我的自定义键": 123})
        self.settings.update("collect", {"dedupe_checks": ["视频指纹"]})
        self.settings.path.write_text(json.dumps({
            **self.raw(), "top_level_extra": {"a": 1},
        }, ensure_ascii=False), encoding="utf-8")
        fresh = self.reopen()
        self.assertEqual(fresh.get("general", "我的自定义键"), 123)
        self.assertEqual(fresh.get("collect", "dedupe_checks"), ["视频指纹"])
        self.assertEqual(fresh.load()["top_level_extra"], {"a": 1})

    def test_non_dict_section_falls_back_to_defaults(self):
        self.settings.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.path.write_text(json.dumps({"ai": 5, "storage": "坏了"}), encoding="utf-8")
        fresh = self.reopen()
        self.assertEqual(fresh.section("ai")["model"], "deepseek-chat")
        self.assertEqual(fresh.section("storage")["bucket"], "tce-media")
        self.assertTrue(any(item["code"] == "invalid_section_payload" for item in fresh.issues()))

    def test_corrupt_file_falls_back_to_defaults(self):
        self.settings.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.path.write_text("{ 这不是 JSON", encoding="utf-8")
        fresh = self.reopen()
        self.assertEqual(fresh.section("general")["theme"], "system")
        self.assertTrue(any(item["code"] == "corrupt_settings_file" for item in fresh.issues()))

    def test_corrupt_file_recovers_from_backup(self):
        self.settings.update("storage", {"video_path": "D:/videos"})
        self.assertTrue(self.settings.backup_path.exists(), "写完应留一份备份")
        self.settings.path.write_text("{ 坏了", encoding="utf-8")
        fresh = self.reopen()
        self.assertEqual(fresh.section("storage")["video_path"], "D:/videos")
        self.assertTrue(any(item["code"] == "recovered_from_backup" for item in fresh.issues()))

    def test_persisted_numbers_written_as_strings_are_coerced(self):
        self.settings.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.path.write_text(json.dumps({
            "collect": {"interval_minutes": "15", "similarity_threshold": "80"},
            "ai": {"max_tokens": "2048", "temperature": "0.2"},
        }), encoding="utf-8")
        fresh = self.reopen()
        self.assertIsInstance(fresh.get("collect", "interval_minutes"), int)
        self.assertEqual(fresh.get("collect", "interval_minutes"), 15)
        self.assertEqual(fresh.get("ai", "max_tokens"), 2048)
        self.assertEqual(fresh.get("ai", "temperature"), 0.2)

    def test_invalid_persisted_value_is_kept_and_reported_not_destroyed(self):
        """加载不是写入：一个字段看不懂，不许把用户整份配置重置。"""
        self.settings.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.path.write_text(json.dumps({
            "collect": {"similarity_threshold": 5000, "group": "保留我"},
        }, ensure_ascii=False), encoding="utf-8")
        fresh = self.reopen()
        self.assertEqual(fresh.get("collect", "similarity_threshold"), 5000, "原值必须还在")
        self.assertEqual(fresh.get("collect", "group"), "保留我")
        problems = fresh.audit()
        self.assertTrue(any(item.field == "similarity_threshold" for item in problems))

    def test_alias_section_in_the_file_is_merged(self):
        self.settings.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.path.write_text(json.dumps({
            "asr": {"provider": "whisper", "retry_count": 5},
        }), encoding="utf-8")
        fresh = self.reopen()
        self.assertEqual(fresh.get("transcript", "provider"), "whisper")
        self.assertEqual(fresh.get("transcript", "retry_count"), 5)
        self.assertTrue(any(item["code"] == "alias_section" for item in fresh.issues()))


class SaveTests(SettingsCase):
    def test_update_merges_only_the_given_keys(self):
        self.settings.update("collect", {"interval_minutes": 15})
        section = self.reopen().section("collect")
        self.assertEqual(section["interval_minutes"], 15)
        self.assertEqual(section["daily_limit"], 1000, "没传的键必须保持原样")

    def test_update_never_touches_other_sections(self):
        self.settings.update("storage", {"bucket": "my-bucket"})
        self.settings.update("ai", {"model": "custom-model"})
        data = self.reopen().load()
        self.assertEqual(data["storage"]["bucket"], "my-bucket")
        self.assertEqual(data["ai"]["model"], "custom-model")

    def test_save_section_replaces_the_whole_section(self):
        self.settings.update("general", {"我的自定义键": 1})
        self.settings.save_section("general", {"theme": "dark"})
        data = self.reopen().load()
        self.assertEqual(data["general"]["theme"], "dark")
        self.assertNotIn("我的自定义键", data["general"], "整段替换应把未知键一起换掉")
        self.assertEqual(data["general"]["app_language"], "zh-CN", "缺的键读回来是默认值")

    def test_save_writes_a_complete_document(self):
        written = self.settings.save({"ai": {"model": "m2"}})
        self.assertEqual(written["ai"]["model"], "m2")
        raw = self.raw()
        self.assertEqual(raw[VERSION_KEY], SCHEMA_VERSION)
        self.assertEqual(set(raw) - {VERSION_KEY}, set(SECTIONS))

    def test_blank_secret_keeps_the_previous_value(self):
        self.settings.update("ai", {"api_key": "sk-keep"})
        self.settings.update("ai", {"api_key": "", "model": "m2"})
        data = self.settings.load()
        self.assertEqual(data["ai"]["api_key"], "sk-keep")
        self.assertEqual(data["ai"]["model"], "m2")

    def test_secret_is_masked_in_public_but_readable_server_side(self):
        self.settings.update("ai", {"api_key": "sk-secret"})
        self.settings.update("storage", {"access_key_secret": "oss-secret"})
        self.settings.update("notify", {"smtp_password": "smtp-secret"})
        public = self.settings.public()
        for section, key, flag in (("ai", "api_key", "apiKeySet"),
                                   ("storage", "access_key_secret", "secretSet"),
                                   ("notify", "smtp_password", "passwordSet")):
            self.assertEqual(public[section][key], "", f"{section}.{key} 不能回明文")
            self.assertTrue(public[section][flag], f"{section}.{flag} 必须为 True")
        self.assertEqual(self.settings.get_secret("ai", "api_key"), "sk-secret")
        self.assertNotIn("sk-secret", json.dumps(public, ensure_ascii=False))

    def test_public_never_contains_any_secret_value(self):
        self.settings.update("ai", {"api_key": "sk-canary"})
        self.settings.update("notify", {"smtp_password": "smtp-canary"})
        blob = json.dumps(self.settings.public(), ensure_ascii=False)
        self.assertNotIn("canary", blob)
        self.assertIn("schemaVersion", self.settings.public())

    def test_apply_strict_rejects_the_whole_payload(self):
        self.settings.apply("collector", {"poll_interval_minutes": 15})
        before = self.snapshot()
        result = self.settings.apply("collector",
                                     {"poll_interval_minutes": 10, "concurrency": 0})
        self.assertFalse(result.ok)
        self.assertEqual(result.applied, {})
        self.assertIn("concurrency", result.rejected)
        self.assertEqual(self.snapshot(), before, "整体拒绝时磁盘一个字节都不该变")
        self.assertEqual(self.reopen().get("collector", "poll_interval_minutes"), 15)

    def test_apply_lenient_drops_bad_fields_and_keeps_the_rest(self):
        self.settings.apply("collector", {"poll_interval_minutes": 15})
        result = self.settings.apply("collector",
                                     {"poll_interval_minutes": 0, "concurrency": 4},
                                     strict=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.rejected, ["poll_interval_minutes"])
        stored = self.reopen().section("collector")
        self.assertEqual(stored["poll_interval_minutes"], 15, "非法值不得覆盖旧值")
        self.assertEqual(stored["concurrency"], 4, "合法字段照常写入")

    def test_unknown_section_is_rejected_everywhere(self):
        result = self.settings.apply("不存在的分区", {"a": 1})
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].code, "unknown_section")
        with self.assertRaises(UnknownSectionError):
            self.settings.update("不存在的分区", {"a": 1})
        with self.assertRaises(UnknownSectionError):
            self.settings.save_section("不存在的分区", {"a": 1})
        self.assertFalse(self.settings.reset("不存在的分区").ok)
        self.assertFalse(self.settings.validate("不存在的分区", {}).ok)
        self.assertFalse(self.settings.path.exists(), "被拒绝的写入不该创建文件")

    def test_unknown_field_is_an_error_in_strict_mode_only(self):
        strict = self.settings.apply("pipeline", {"没有这个字段": 1}, strict=True)
        self.assertFalse(strict.ok)
        self.assertEqual(strict.errors[0].code, "unknown_field")
        lenient = self.settings.apply("pipeline", {"没有这个字段": 1}, strict=False)
        self.assertTrue(lenient.ok)
        self.assertEqual(self.reopen().get("pipeline", "没有这个字段"), 1)

    def test_apply_rejects_non_dict_payloads_and_unserializable_values(self):
        self.assertFalse(self.settings.apply("ai", ["不是字典"]).ok)
        result = self.settings.apply("ai", {"model": {"不是": "字符串"}})
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].code, "invalid_value")
        weird = self.settings.apply("pipeline", {"自定义": {1, 2, 3}}, strict=False)
        self.assertEqual(weird.errors[0].code, "not_serializable")
        self.assertFalse(self.settings.path.exists())

    def test_partial_update_only_changes_what_was_sent(self):
        self.settings.apply("transcript", {"provider": "whisper", "retry_count": 4})
        self.settings.apply("transcript", {"language": "en"}, partial=True)
        stored = self.reopen().section("transcript")
        self.assertEqual(stored["provider"], "whisper")
        self.assertEqual(stored["retry_count"], 4)
        self.assertEqual(stored["language"], "en")

    def test_validate_does_not_write(self):
        self.settings.validate("collector", {"poll_interval_minutes": 5})
        self.assertFalse(self.settings.path.exists())

    def test_ui_basic_page_payload_is_accepted_by_the_legacy_path(self):
        """界面的「基础设置」页会把 work_mode / logging 的字段一起传上来（界面不能改）。"""
        payload = {
            "app_language": "zh-CN", "theme": "dark", "auto_start": True,
            "start_minimized": False, "run_in_background": True,
            "mode": "timed", "concurrent_tasks": 5, "task_interval_sec": 120,
            "level": "DEBUG", "save_detail": False, "auto_clean_days": 7,
            "notify_on_error": False, "daily_report": True,
        }
        self.settings.update("general", payload)
        stored = self.reopen().section("general")
        self.assertEqual(stored["theme"], "dark")
        self.assertEqual(stored["mode"], "timed", "串页过来的 work_mode 字段必须存得住")
        self.assertEqual(stored["auto_clean_days"], 7, "串页过来的 logging 字段必须存得住")

    def test_ui_account_page_off_on_strings_are_coerced(self):
        result = self.settings.apply("account", {"two_factor": "on", "verify_method": "短信验证"})
        self.assertTrue(result.ok, result.error)
        self.assertIs(self.settings.load()["account"]["two_factor"], True)
        self.settings.apply("account", {"two_factor": "off"})
        self.assertIs(self.reopen().load()["account"]["two_factor"], False)

    def test_ui_storage_file_types_string_is_normalized_to_a_list(self):
        result = self.settings.apply("storage", {"file_types": "mp4,mov,jpg"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.reopen().get("storage", "file_types"), ["mp4", "mov", "jpg"])
        self.settings.apply("storage", {"file_types": ["mp4", "webp"]})
        self.assertEqual(self.reopen().get("storage", "file_types"), ["mp4", "webp"])


class ValidationTests(SettingsCase):
    def errors_for(self, section, values, strict=True):
        return self.settings.validate(section, values, strict=strict).errors

    def codes(self, section, values, strict=True):
        return [item.code for item in self.errors_for(section, values, strict=strict)]

    def test_boolean_type(self):
        self.assertEqual(self.codes("general", {"auto_start": "可能吧"}), ["invalid_value"])
        for word, expected in (("on", True), ("off", False), (1, True), (0, False),
                               (True, True), (False, False), ("是", True), ("关", False)):
            result = self.settings.validate("general", {"auto_start": word})
            self.assertTrue(result.ok, f"{word!r} 应该被接受")
            self.assertIs(result.values["auto_start"], expected)

    def test_integer_type(self):
        for good, expected in ((3, 3), ("3", 3), (3.0, 3)):
            result = self.settings.validate("collector", {"retry_count": good})
            self.assertTrue(result.ok, f"{good!r} 应该被接受")
            self.assertEqual(result.values["retry_count"], expected)
        for bad in ("abc", 1.5, None, [1], {"a": 1}, True):
            self.assertEqual(self.codes("collector", {"retry_count": bad}), ["invalid_value"],
                             f"{bad!r} 不该被当成整数")

    def test_float_type(self):
        result = self.settings.validate("ai", {"temperature": "0.3"})
        self.assertTrue(result.ok)
        self.assertEqual(result.values["temperature"], 0.3)
        self.assertEqual(self.codes("ai", {"temperature": "hot"}), ["invalid_value"])
        self.assertEqual(self.codes("ai", {"temperature": True}), ["invalid_value"])

    def test_string_type(self):
        self.assertTrue(self.settings.validate("ai", {"model": "gpt-4o-mini"}).ok)
        self.assertEqual(self.codes("ai", {"model": 123}), [])       # 数字能还原成字符串
        self.assertEqual(self.codes("ai", {"model": {"a": 1}}), ["invalid_value"])
        self.assertEqual(self.codes("ai", {"model": [1, 2]}), ["invalid_value"])

    def test_enum_type(self):
        self.assertTrue(self.settings.validate("general", {"theme": "dark"}).ok)
        self.assertEqual(self.codes("general", {"theme": "neon"}), ["invalid_value"])
        self.assertTrue(self.settings.validate("logging", {"level": "DEBUG"}).ok)
        self.assertEqual(self.codes("logging", {"level": "TRACE"}), ["invalid_value"])
        self.assertEqual(self.codes("publish", {"review_mode": "什么模式"}), ["invalid_value"])

    def test_list_type(self):
        result = self.settings.validate("collect", {"keywords_allow": ["a", "b"]})
        self.assertTrue(result.ok)
        self.assertEqual(result.values["keywords_allow"], ["a", "b"])
        self.assertEqual(self.settings.validate("collect", {"keywords_allow": "a\nb"}).values
                         ["keywords_allow"], ["a", "b"])
        self.assertEqual(self.settings.validate("collect", {"keywords_allow": []}).values
                         ["keywords_allow"], [])
        self.assertEqual(self.codes("collect", {"keywords_allow": {"a": 1}}), ["invalid_value"])

    def test_optional_field_accepts_blank(self):
        self.assertTrue(self.settings.validate("transcript", {"language": ""}).ok)
        result = self.settings.validate("transcript", {"language": None})
        self.assertTrue(result.ok, result.as_dict()["errors"])
        self.assertEqual(result.values["language"], "", "可选字符串的空值统一成 ''")
        self.settings.apply("transcript", {"language": None})
        self.assertEqual(self.reopen().get("transcript", "language"), "")

    def test_numeric_ranges_are_enforced(self):
        cases = [
            ("collector", "poll_interval_minutes", 0),
            ("collector", "poll_interval_minutes", -5),
            ("collector", "retry_count", -1),
            ("collector", "concurrency", 0),
            ("pipeline", "concurrency", 0),
            ("pipeline", "retry_count", -2),
            ("publishing", "concurrency", 0),
            ("transcript", "retry_count", -3),
            ("ai", "temperature", 5),
            ("ai", "temperature", -1),
            ("ai", "quality_threshold", 1.5),
            ("collect", "similarity_threshold", 200),
            ("notify", "smtp_port", 70000),
            ("storage", "disk_warn_gb", 0),
        ]
        for section, key, value in cases:
            errors = self.errors_for(section, {key: value})
            self.assertTrue(errors, f"{section}.{key}={value} 必须被拒绝")
            self.assertEqual(errors[0].code, "invalid_value")
            self.assertIn("不能", errors[0].message)

    def test_numeric_boundaries_are_inclusive(self):
        self.assertTrue(self.settings.validate("collector", {"poll_interval_minutes": 1}).ok)
        self.assertTrue(self.settings.validate("collector", {"retry_count": 0}).ok)
        self.assertTrue(self.settings.validate("ai", {"temperature": 0}).ok)
        self.assertTrue(self.settings.validate("ai", {"temperature": 2}).ok)
        self.assertTrue(self.settings.validate("collect", {"similarity_threshold": 100}).ok)

    def test_unknown_field_and_section_codes(self):
        strict = self.settings.validate("ai", {"不存在": 1}, strict=True)
        self.assertEqual(strict.errors[0].code, "unknown_field")
        lenient = self.settings.validate("ai", {"不存在": 1}, strict=False)
        self.assertTrue(lenient.ok)
        self.assertEqual(lenient.unknown, {"不存在": 1})
        self.assertEqual(self.settings.validate("nope", {}).errors[0].code, "unknown_section")

    def test_validation_result_is_serializable(self):
        payload = self.settings.validate("ai", {"temperature": 9}).as_dict()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"][0]["field"], "temperature")
        json.dumps(payload, ensure_ascii=False)

    def test_describe_exposes_types_ranges_and_hides_secret_defaults(self):
        described = self.settings.describe()
        self.assertEqual(described["schemaVersion"], SCHEMA_VERSION)
        names = [item["name"] for item in described["sections"]]
        self.assertEqual(names, list(SECTIONS))
        collector = self.settings.describe("collector")
        bounds = {item["key"]: (item["minimum"], item["maximum"]) for item in collector["fields"]}
        self.assertEqual(bounds["poll_interval_minutes"], (1, 10080))
        ai = {item["key"]: item for item in self.settings.describe("ai")["fields"]}
        self.assertTrue(ai["api_key"]["secret"])
        self.assertIsNone(ai["api_key"]["default"], "describe 不许回敏感字段的默认值")
        self.assertEqual(self.settings.describe("asr")["name"], "transcript")
        self.assertEqual(self.settings.describe("没有这个分区"), {})


class ResetTests(SettingsCase):
    def test_reset_one_section_restores_defaults_only_there(self):
        self.settings.update("ai", {"model": "custom-model", "temperature": 0.1})
        self.settings.update("storage", {"bucket": "my-bucket"})
        result = self.settings.reset("ai")
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "reset")
        data = self.reopen().load()
        self.assertEqual(data["ai"]["model"], "deepseek-chat")
        self.assertEqual(data["ai"]["temperature"], 0.7)
        self.assertEqual(data["storage"]["bucket"], "my-bucket", "重置不能误伤别的分区")

    def test_reset_accepts_an_alias(self):
        self.settings.update("transcript", {"retry_count": 9})
        self.settings.reset("asr")
        self.assertEqual(self.reopen().get("transcript", "retry_count"), 2)

    def test_reset_all_restores_everything(self):
        self.settings.update("ai", {"model": "custom-model"})
        self.settings.update("general", {"我的自定义键": 5})
        result = self.settings.reset()
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "reset_all")
        data = self.reopen().load()
        self.assertEqual(data["ai"]["model"], "deepseek-chat")
        self.assertNotIn("我的自定义键", data["general"])
        self.assertEqual(data[VERSION_KEY], SCHEMA_VERSION)

    def test_reset_writes_through_the_bridge_helper(self):
        self.settings.update("collector", {"poll_interval_minutes": 5})
        self.settings.save_section("collector", DEFAULTS["collector"])
        self.assertEqual(self.reopen().get("collector", "poll_interval_minutes"), 30)


class PersistenceTests(SettingsCase):
    def test_values_survive_a_restart(self):
        self.settings.update("storage", {"video_path": "D:/TikTok/videos"})
        self.settings.apply("collector", {"poll_interval_minutes": 15})
        fresh = self.reopen()
        self.assertEqual(fresh.get("storage", "video_path"), "D:/TikTok/videos")
        self.assertEqual(fresh.get("collector", "poll_interval_minutes"), 15)

    def test_file_is_human_readable_json_with_a_version(self):
        self.settings.update("ai", {"model": "m2"})
        raw = self.raw()
        self.assertEqual(raw[VERSION_KEY], SCHEMA_VERSION)
        text = self.snapshot()
        self.assertIn("deepseek", text)
        self.assertIn("\n", text, "落盘应是缩进过的 JSON，便于人工排查")

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        self.settings.update("general", {"theme": "dark"})
        self.assertFalse(self.settings.tmp_path.exists())
        self.assertTrue(self.settings.backup_path.exists())
        json.loads(self.settings.backup_path.read_text(encoding="utf-8"))

    def test_failed_write_keeps_the_old_config_intact(self):
        self.settings.update("storage", {"bucket": "good-bucket"})
        before = self.snapshot()
        with patch("os.replace", side_effect=OSError("磁盘满了")):
            result = self.settings.apply("storage", {"bucket": "new-bucket"})
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[-1].code, "persist_failed")
        self.assertEqual(self.snapshot(), before, "写失败不能损坏已有配置")
        self.assertFalse(self.settings.tmp_path.exists(), "半成品临时文件必须清掉")
        fresh = self.reopen()
        self.assertEqual(fresh.get("storage", "bucket"), "good-bucket")
        self.assertNotEqual(fresh.get("storage", "bucket"), "new-bucket")

    def test_legacy_update_raises_on_write_failure(self):
        self.settings.update("storage", {"bucket": "good-bucket"})
        before = self.snapshot()
        with patch("os.replace", side_effect=OSError("磁盘满了")):
            with self.assertRaises(SettingsPersistenceError):
                self.settings.update("storage", {"bucket": "new-bucket"})
        self.assertEqual(self.snapshot(), before)

    def test_memory_cache_is_not_poisoned_by_a_failed_write(self):
        self.settings.update("storage", {"bucket": "good-bucket"})
        with patch("os.replace", side_effect=OSError("磁盘满了")):
            self.settings.apply("storage", {"bucket": "new-bucket"})
        self.assertEqual(self.settings.get("storage", "bucket"), "good-bucket")

    def test_external_change_from_another_instance_is_picked_up(self):
        """多个 Agent 各持一个实例：别人写过之后，这边不能拿着旧缓存把改动盖掉。"""
        mine = FactorySettings(self.root)
        theirs = FactorySettings(self.root)
        mine.load()                                     # 先建立缓存
        theirs.apply("collector", {"poll_interval_minutes": 45})
        self.assertEqual(mine.get("collector", "poll_interval_minutes"), 45)
        mine.apply("pipeline", {"batch_size": 9})       # 我这边再写，不能丢别人的字段
        fresh = self.reopen().load()
        self.assertEqual(fresh["collector"]["poll_interval_minutes"], 45)
        self.assertEqual(fresh["pipeline"]["batch_size"], 9)

    def test_concurrent_updates_do_not_lose_data(self):
        errors = []

        def worker(index):
            try:
                FactorySettings(self.root).update("pipeline", {"batch_size": index + 1})
            except Exception as exc:                     # pragma: no cover - 失败时给出原因
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        stored = self.reopen().get("pipeline", "batch_size")
        self.assertTrue(1 <= stored <= 8)
        raw = self.raw()
        self.assertEqual(raw[VERSION_KEY], SCHEMA_VERSION, "并发写之后文件仍然合法")

    def test_file_from_a_newer_program_version_is_not_overwritten(self):
        self.settings.path.parent.mkdir(parents=True, exist_ok=True)
        future = {"schema_version": SCHEMA_VERSION + 5, "future_section": {"x": 1}}
        self.settings.path.write_text(json.dumps(future), encoding="utf-8")
        fresh = self.reopen()
        self.assertEqual(fresh.stored_schema_version, SCHEMA_VERSION + 5)
        self.assertTrue(any(item["code"] == "future_schema_version" for item in fresh.issues()))
        result = fresh.apply("ai", {"model": "不许写"})
        self.assertFalse(result.ok)
        self.assertEqual(self.raw()["future_section"], {"x": 1})
        self.assertEqual(self.raw()["schema_version"], SCHEMA_VERSION + 5)


class MigrationTests(SettingsCase):
    def test_version_detection(self):
        self.assertEqual(detect_version({}), 1)
        self.assertEqual(detect_version({"schema_version": 2}), 2)
        self.assertEqual(detect_version({"schema_version": "3"}), 3)
        self.assertEqual(detect_version({"schema_version": "坏了"}), 1)
        self.assertEqual(detect_version(None), 1)

    def test_v1_file_is_migrated_and_stamped_on_disk(self):
        self.settings.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.path.write_text(json.dumps({
            "storage": {"file_types": "mp4,mov,jpg", "bucket": "老桶"},
            "ai": {"model": "老模型"},
        }, ensure_ascii=False), encoding="utf-8")
        fresh = self.reopen()
        self.assertEqual(fresh.stored_schema_version, SCHEMA_VERSION)
        self.assertEqual(fresh.get("storage", "file_types"), ["mp4", "mov", "jpg"])
        self.assertEqual(fresh.get("storage", "bucket"), "老桶", "迁移不能丢用户的旧值")
        on_disk = self.raw()
        self.assertEqual(on_disk[VERSION_KEY], SCHEMA_VERSION, "迁移结果必须落盘")
        self.assertEqual(on_disk["storage"]["file_types"], ["mp4", "mov", "jpg"])

    def test_migration_registry_covers_every_version_up_to_current(self):
        version = 1
        while version < SCHEMA_VERSION:
            self.assertIn(version, MIGRATIONS, f"缺少 {version} → {version + 1} 的迁移")
            self.assertEqual(MIGRATIONS[version].version, version)
            version += 1

    def test_migrations_run_in_order(self):
        registry = {}
        register_migration(Migration(1, "one", "第一步", lambda data: {**data, "trace": ["one"]}),
                           registry)
        register_migration(Migration(2, "two", "第二步",
                                     lambda data: {**data, "trace": data["trace"] + ["two"]}),
                           registry)
        with patch("content_factory.settings.migration.SCHEMA_VERSION", 3):
            report = run_migrations({"schema_version": 1}, registry=registry)
        self.assertEqual(report.applied, ["one", "two"])
        self.assertEqual(report.data["trace"], ["one", "two"])
        self.assertEqual(report.data[VERSION_KEY], 3)
        self.assertTrue(report.changed)

    def test_missing_migration_is_reported_not_guessed(self):
        with patch("content_factory.settings.migration.SCHEMA_VERSION", 9):
            report = run_migrations({"schema_version": 1}, registry={})
        self.assertFalse(report.changed)
        self.assertEqual(report.to_version, 1)
        self.assertTrue(report.notes)

    def test_future_version_is_left_alone(self):
        report = run_migrations({"schema_version": SCHEMA_VERSION + 2, "x": 1})
        self.assertTrue(report.future_version)
        self.assertFalse(report.changed)
        self.assertEqual(report.data["x"], 1)

    def test_migrate_is_idempotent(self):
        self.settings.update("ai", {"model": "m2"})
        first = self.settings.migrate()
        before = self.snapshot()
        second = self.settings.migrate()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(first["changed"])
        self.assertFalse(second["changed"])
        self.assertFalse(self.settings.migration_pending)
        self.assertEqual(self.settings.schema_version, SCHEMA_VERSION)

    def test_old_file_without_version_gets_the_new_engine_sections(self):
        self.settings.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.path.write_text(json.dumps({"general": {"theme": "dark"}}), encoding="utf-8")
        fresh = self.reopen()
        self.assertEqual(fresh.get("general", "theme"), "dark")
        self.assertFalse(fresh.get("collector", "enabled"), "新分区应带默认值出现")
        self.assertEqual(fresh.get("collector", "poll_interval_minutes"), 30)


class LegacyCompatibilityTests(SettingsCase):
    def test_settings_store_module_still_exports_the_same_objects(self):
        self.assertIs(LEGACY_DEFAULTS, DEFAULTS)
        self.assertIs(LegacyFactorySettings, FactorySettings)
        self.assertTrue(issubclass(LegacyFactorySettings, SettingsService))
        self.assertTrue(issubclass(FactorySettings, SettingsService))
        self.assertEqual(legacy_app_data_root().name, "TikTokBatchMVP")
        self.assertIs(legacy_defaults, default_values)
        self.assertEqual(legacy_defaults("ai")["model"], "deepseek-chat")

    def test_legacy_defaults_are_plain_mutable_dicts(self):
        self.assertIsInstance(DEFAULTS, dict)
        self.assertIsInstance(DEFAULTS["ai"], dict)
        self.assertIsInstance(DEFAULTS["collect"]["filter_rules"], list)

    def test_legacy_instance_surface(self):
        settings = self.settings
        self.assertEqual(settings.root, self.root)
        self.assertEqual(settings.path, self.root / "content-factory-settings.json")
        self.assertIsInstance(settings.load(), dict)
        self.assertIsInstance(settings.section("ai"), dict)
        self.assertIsInstance(settings.update("ai", {"model": "m2"}), dict)
        self.assertIsInstance(settings.save_section("ai", DEFAULTS["ai"]), dict)
        self.assertIsInstance(settings.save(settings.load()), dict)
        self.assertIsInstance(settings.public(), dict)

    def test_ai_enrichment_still_reads_its_config(self):
        self.settings.update("ai", {"model": "deepseek-reasoner", "temperature": 0.2,
                                    "max_tokens": 2048, "api_key": "sk-x"})
        config = EnrichmentService(self.settings).config()
        self.assertEqual(config["model"], "deepseek-reasoner")
        self.assertEqual(config["temperature"], 0.2)
        self.assertEqual(config["max_tokens"], 2048)
        self.assertEqual(config["api_key"], "sk-x")
        self.assertTrue(config["prompt_template"])

    def test_bridge_settings_endpoints_still_work(self):
        api = ContentFactoryApi(store=FactoryStore(self.root / "cf.db"), settings=self.settings)
        saved = api.content_save_settings("storage", {"video_path": "D:/TikTok/videos"})
        self.assertTrue(saved["ok"])
        self.assertEqual(api.content_settings()["storage"]["video_path"], "D:/TikTok/videos")

        api.content_save_settings("ai", {"api_key": "sk-secret"})
        self.assertEqual(api.content_settings()["ai"]["api_key"], "")
        self.assertTrue(api.content_settings()["ai"]["apiKeySet"])

        api.content_save_settings("storage", {"bucket": "my-bucket"})
        api.content_reset_settings("ai")
        settings = api.content_settings()
        self.assertNotEqual(settings["ai"]["model"], "sk-secret")
        self.assertEqual(settings["storage"]["bucket"], "my-bucket", "重置一个分区不能误伤别的")
        self.assertEqual(settings["ai"]["model"], "deepseek-chat")

        rejected = api.content_save_settings("nope", {"a": 1})
        self.assertFalse(rejected["ok"])
        self.assertIn("未知的设置分区", rejected["error"])

    def test_bridge_can_save_and_reset_engine_sections_after_whitelisting(self):
        """桥接层目前写死了 9 个界面分区；新引擎分区经 Settings Core 直接可用。"""
        api = ContentFactoryApi(store=FactoryStore(self.root / "cf.db"), settings=self.settings)
        self.assertFalse(api.content_save_settings("collector", {"poll_interval_minutes": 5})["ok"],
                         "桥接层还没放行引擎分区 —— 连接方式见 docs/settings-core.md")
        self.assertTrue(self.settings.apply("collector", {"poll_interval_minutes": 5}).ok)
        self.assertEqual(api.content_bootstrap()["settings"]["collector"]["poll_interval_minutes"], 5)


if __name__ == "__main__":
    unittest.main()
