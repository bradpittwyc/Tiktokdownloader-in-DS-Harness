"""RC1 Bug：「创作者监控页无法添加创作者」。

根因是 UI ↔ Bridge ↔ Collector 的最后一段接线缺失：
Collector Core 早就实现了 content_creator_save / list（CollectorApi），
但 ContentFactoryApi 从来没把它挂进去，界面上那个「＋ 添加创作者」按钮
其实只是 data-act="nav" data-page="library" —— 点了只是打开视频库。

这一份回归网盯住三件事，缺一条用户就用不了：
1. 桥接真的把 CollectorApi 挂上了（同一个 store / settings，不是第二套）
2. pywebview 能发现这些方法（__dir__ 只列类体名字，继承不算）
3. 添加 → 落库 → 重启仍在，重复添加不会产生第二条记录

真实的浏览器交互（modal 打开 / 提交 / 错误提示）在 test_content_factory_ui.py
的 CreatorMonitorTests 里，这里守的是它背后那层 Python 接口。
"""

import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

import webview.util as pywebview_util  # noqa: E402  （界面就是靠它暴露 js_api 的）

from content_bridge import ContentFactoryApi, _Hidden  # noqa: E402
from content_factory.collector.bridge_api import CollectorApi  # noqa: E402
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402


def _nested_code(name):
    """取 inject_pywebview 里那个内层闭包的 code object。"""
    return next(const for const in pywebview_util.inject_pywebview.__code__.co_consts
                if getattr(const, "co_name", "") == name)


def _borrowed_cell(variable):
    """造一个「装着 None」的空格子。

    必须每次调用新建一个：Python 里同一个局部作用域只会有一个 Cell 变量，
    在同一个函数里循环造出来的 cell 其实是同一个（踩过，症状是三个自由变量
    指向同一格、函数最后拿到了另一个函数当 exposed_objects）。
    """
    def holder():
        return Cell, variable          # noqa: F821 - Cell 由调用方注入作用域，只为产出一个 cell
    return holder.__closure__[0]


def pywebview_expose(js_api):
    """用 pywebview 自己的遍历规则跑一遍，而不是凭印象另写一份。

    pywebview 6.2.1 的遍历在 `webview.util.inject_pywebview` 内层的 `get_functions`，
    模块里没有这个名字（所以原先的测试只能复刻一份规则）。这里直接把内层闭包的
    code object 抠出来重建成函数，跑的是真的那一份 —— 重建不了就抛错，而不是悄悄
    跳过：那说明 pywebview 的暴露机制变了，正是要人看一眼的信号。

    闭包有三个自由变量 (exposed_objects, get_args, get_functions)：给每个变量造一个
    格子，函数造好后再回填（get_functions 自引用那格必须等函数本身存在才能填，
    递归是调用时才解析的，所以来得及）。
    """
    class Cell:                       # 借 cell 用的容器
        exposed_objects = None
        get_args = None
        get_functions = None

    cells = {variable: _borrowed_cell(variable)
             for variable in _nested_code("get_functions").co_freevars}

    def build(name):
        code = _nested_code(name)
        return types.FunctionType(code, pywebview_util.inject_pywebview.__globals__, name, None,
                                  tuple(cells[key] for key in code.co_freevars))

    inner = build("get_functions")
    cells["exposed_objects"].cell_contents = []
    cells["get_args"].cell_contents = build("get_args")
    cells["get_functions"].cell_contents = inner
    # 重建出来的函数没有默认值（默认值是挂在原函数对象上的，code object 里没有），
    # 所以三个参数都要显式给。
    return inner(js_api, "", {})


# 界面上「添加创作者」要用的那一组方法（一个都不能少）
CREATOR_METHODS = (
    "content_creator_list", "content_creator_save", "content_creator_delete",
    "content_creator_toggle", "content_creator_set_interval",
    "content_creator_set_priority", "content_creator_check_now",
)


class FakeDownloader:
    """最小下载器替身：Creator 的增删改查不该碰下载，这里只用于确认接线。"""

    def download(self, videos, folder, quality, retry_count=3, concurrency=1):
        return {"ok": len(videos), "failed": [], "folder": folder}


class BridgeWiringTests(unittest.TestCase):
    """桥接层：ContentFactoryApi 必须真的转发到 CollectorApi。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.store = FactoryStore(self.root / "content-factory.db")
        self.settings = FactorySettings(self.root)
        self.downloader = FakeDownloader()
        self.api = ContentFactoryApi(downloader=self.downloader, store=self.store,
                                     settings=self.settings)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_content_factory_api_really_exposes_creator_save(self):
        for name in CREATOR_METHODS:
            self.assertTrue(callable(getattr(self.api, name, None)), f"缺少桥接方法 {name}")

    def test_creator_methods_are_declared_in_the_class_body(self):
        """必须一行一行显式转发：继承来的方法不会出现在 vars(type(self)) 里。"""
        declared = set(vars(ContentFactoryApi))
        for name in CREATOR_METHODS:
            self.assertIn(name, declared, f"{name} 必须在 ContentFactoryApi 类体里显式声明")

    def test_pywebview_can_discover_the_creator_api(self):
        """用 pywebview 自己的遍历规则跑一遍（复刻不如直接用真的）。"""
        names = {name.split(".")[-1] for name in pywebview_expose(_Hidden(self.api))}
        for name in CREATOR_METHODS:
            self.assertIn(name, names, f"pywebview 发现不了 {name}")

    def test_exposed_signature_matches_what_the_ui_calls(self):
        """界面是按位置传参调的（content_creator_save(values)），参数名错了就当场炸。"""
        exposed = pywebview_expose(_Hidden(self.api))
        self.assertEqual(exposed["content_creator_save"], ["values"])
        self.assertEqual(exposed["content_creator_toggle"], ["creator_id", "enabled"])
        self.assertEqual(exposed["content_creator_list"],
                         ["enabled", "search", "due_only", "limit"])

    def test_collector_reuses_the_same_store_and_settings(self):
        """复用现有 store / settings：另建一套等于打开「两个世界」的内容库。"""
        collector = self.api._collector
        self.assertIsInstance(collector, CollectorApi)
        self.assertIs(collector.store, self.store)
        self.assertIs(collector.settings, self.settings)
        self.assertIs(collector.downloader, self.downloader)

    def test_creator_saved_through_the_bridge_lands_in_the_same_database(self):
        result = self.api.content_creator_save({"handle": "@nasa"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.api._store.creators(limit=10)[0]["handle"], "nasa")


class CreatorAddFlowTests(unittest.TestCase):
    """添加创作者的最小闭环 —— 对应试玩清单里的 Case A ~ E。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.db = self.root / "content-factory.db"
        self.api = ContentFactoryApi(downloader=FakeDownloader(),
                                     store=FactoryStore(self.db), settings=FactorySettings(self.root))

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def creators(self):
        """界面真正读的那一份（走 content_creator_list，不是 bootstrap）。"""
        return self.api.content_creator_list()

    # ---- Case A：添加 @nasa 立刻出现在列表里 ----------------------------
    def test_case_a_add_nasa_shows_up_immediately(self):
        result = self.api.content_creator_save({"handle": "@nasa", "display_name": "NASA",
                                                "category": "科技", "priority": "高",
                                                "poll_interval": "30 分钟"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["creator"]["handle"], "nasa")
        listing = self.creators()
        self.assertEqual(listing["stats"]["total"], 1)
        creator = listing["creators"][0]
        self.assertEqual(creator["handle"], "nasa")
        self.assertEqual(creator["display_name"], "NASA")
        self.assertEqual(creator["category"], "科技")
        self.assertEqual(creator["priority"], "高")
        self.assertEqual(creator["poll_interval"], "30 分钟")
        self.assertEqual(creator["poll_interval_seconds"], 1800)
        # 监控状态必须齐全，否则页面上只能写死「正常监控」
        for key in ("enabled", "priority", "poll_interval", "next_check_at",
                    "last_check_at", "checking", "is_due", "due_in_seconds"):
            self.assertIn(key, creator, f"Creator Monitor 数据缺少 {key}")

    def test_added_creator_is_due_for_a_first_check(self):
        """新加的创作者要立刻排一次检查，否则「添加了但一直不抓」。"""
        self.api.content_creator_save({"handle": "nasa"})
        self.assertTrue(self.creators()["creators"][0]["is_due"])

    def test_monitor_can_be_disabled_at_creation_time(self):
        self.api.content_creator_save({"handle": "nasa", "enabled": False})
        self.assertFalse(self.creators()["creators"][0]["enabled"])
        self.assertEqual(self.creators()["stats"]["disabled"], 1)

    # ---- Case B：重启后仍在（新进程 = 新桥接实例读同一个库）-------------
    def test_case_b_survives_a_restart(self):
        self.api.content_creator_save({"handle": "@nasa", "display_name": "NASA"})
        restarted = ContentFactoryApi(downloader=FakeDownloader(),
                                      store=FactoryStore(self.db),
                                      settings=FactorySettings(self.root))
        listing = restarted.content_creator_list()
        self.assertEqual(listing["stats"]["total"], 1)
        self.assertEqual(listing["creators"][0]["handle"], "nasa")
        self.assertEqual(listing["creators"][0]["display_name"], "NASA")

    # ---- Case C：重复添加要说清楚，且不产生第二条 ------------------------
    def test_case_c_duplicate_is_reported_and_not_duplicated(self):
        self.assertTrue(self.api.content_creator_save({"handle": "@nasa"})["ok"])
        again = self.api.content_creator_save({"handle": "@nasa"})
        self.assertFalse(again["ok"])
        self.assertTrue(again["duplicate"])
        self.assertIn("已存在", again["error"])
        self.assertEqual(self.creators()["stats"]["total"], 1)

    def test_duplicate_check_ignores_case_and_the_at_sign(self):
        """@NASA / nasa / @nasa 是同一个人，不能被存成三条。"""
        self.api.content_creator_save({"handle": "@nasa"})
        for handle in ("nasa", "@NASA", "Nasa"):
            result = self.api.content_creator_save({"handle": handle})
            self.assertFalse(result["ok"], f"{handle} 应该被判为已存在")
            self.assertTrue(result["duplicate"])
        self.assertEqual(self.creators()["stats"]["total"], 1)

    # ---- Case D：不带 @ 也能存 ------------------------------------------
    def test_case_d_bare_handle_is_saved_as_is(self):
        result = self.api.content_creator_save({"handle": "nasa"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["creator"]["handle"], "nasa")
        self.assertEqual(self.creators()["creators"][0]["handle"], "nasa")

    # ---- Case E：非法 handle 明确报错且不写库 ----------------------------
    def test_case_e_invalid_handle_is_rejected_without_writing(self):
        for bad in ("bad handle!", "hello@world", "a" * 65, "中文名字", "@"):
            result = self.api.content_creator_save({"handle": bad})
            self.assertFalse(result["ok"], f"{bad!r} 不该被接受")
            self.assertTrue(result["error"])
            self.assertEqual(self.creators()["stats"]["total"], 0, f"{bad!r} 不该写进数据库")
        self.assertEqual(self.api._store.creators(limit=10), [], "creators 表必须还是空的")

    def test_empty_handle_is_rejected_with_a_clear_message(self):
        result = self.api.content_creator_save({"handle": "   "})
        self.assertFalse(result["ok"])
        self.assertIn("不能为空", result["error"])
        self.assertEqual(self.creators()["stats"]["total"], 0)

    # ---- 其它界面动作（同一页的按钮）------------------------------------
    def test_toggle_and_delete_round_trip(self):
        creator_id = self.api.content_creator_save({"handle": "nasa"})["creatorId"]
        self.assertFalse(self.api.content_creator_toggle(creator_id, False)["enabled"])
        self.assertEqual(self.api.content_creator_list(enabled=False)["stats"]["disabled"], 1)
        self.assertTrue(self.api.content_creator_toggle(creator_id, "true")["enabled"])
        self.assertTrue(self.api.content_creator_delete(creator_id)["ok"])
        self.assertEqual(self.creators()["stats"]["total"], 0)

    def test_interval_and_priority_can_be_changed_from_the_page(self):
        creator_id = self.api.content_creator_save({"handle": "nasa"})["creatorId"]
        self.api.content_creator_set_interval(creator_id, "2 小时")
        self.api.content_creator_set_priority(creator_id, "低")
        creator = self.creators()["creators"][0]
        self.assertEqual(creator["poll_interval_seconds"], 7200)
        self.assertEqual(creator["priority"], "低")

    def test_adding_a_creator_without_any_download_works(self):
        """添加 Creator 不能要求用户先下载过视频（这是本次 bug 的原始痛點）。"""
        self.assertEqual(self.api.content_stats()["counts"]["total"], 0, "库里本来没有内容")
        result = self.api.content_creator_save({"handle": "nasa"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.creators()["stats"]["total"], 1)

    def test_added_creator_is_what_the_monitor_will_check(self):
        """添加完就该轮到采集调度看见它（Creator Monitor 与 Collector 是同一份数据）。"""
        self.api.content_creator_save({"handle": "nasa"})
        self.assertEqual(self.api._collector.monitor.stats()["enabled"], 1)
        due = self.api._collector.monitor.due_creators(limit=10)
        self.assertEqual([row["handle"] for row in due], ["nasa"])


if __name__ == "__main__":
    unittest.main()
