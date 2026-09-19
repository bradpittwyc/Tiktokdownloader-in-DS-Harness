"""左侧边栏：下载器卡片 + 收起/展开。

真 Chrome + 假 `window.pywebview.api` + 全网络阻断，断言零 JS 运行时错误。

侧边栏是一个**下载器列表**：每张卡片一个下载器，只放图标不放文字，
点击切换，收起后变成图标条。加新下载器走 `registerDownloader()` ——
这一组测试同时锁住那条扩展接口还能用。
"""

import json
import os
from pathlib import Path
import shutil
import unittest

from playwright.sync_api import sync_playwright


UI_PATH = Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP/ui/index.html"

BRIDGE = """
window.pywebview={api:(()=>{
  const e={
    get_update_info:async()=>({ok:true,current:'1.6.0',tokenSet:false}),
    get_cookie_status:async()=>({ok:true,hasSession:false}),
    get_filename_template:async()=>'', get_learning_options:async()=>({apiKeySet:false}),
    recent_profiles:async()=>[], refresh_recent_profiles:async()=>({ok:true,profiles:[]}),
    load_task_state:async()=>({ok:true,records:{}}), enrich:async()=>({updated:0}),
    set_cookie_options:async()=>({ok:true,hasSession:false}),
    check_update:async()=>({ok:true,current:'1.6.0',latest:'1.6.0',hasUpdate:false})};
  return new Proxy(e,{get(t,p){if(typeof p!=='string')return undefined;
    if(p in t)return t[p];return async()=>({ok:true})}});
})()};
"""


def chrome_path():
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("chrome") or shutil.which("chromium")


class SidebarUITest(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(executable_path=chrome_path(),
                                                       headless=True)
        self.addCleanup(self._stop)
        self.page = self.browser.new_page(viewport={"width": 1320, "height": 880})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("https://**/*", lambda route: route.abort())
        self.page.add_init_script(BRIDGE)
        self.page.goto(UI_PATH.as_uri())
        self.page.wait_for_selector("#appCards", state="attached")
        self.page.wait_for_timeout(300)

    def _stop(self):
        self.browser.close()
        self.playwright.stop()

    def cards(self):
        return self.page.locator(".app-card")

    def card_ids(self):
        return self.page.eval_on_selector_all(
            ".app-card", "els => els.map(e => e.dataset.app)")

    def click_card(self, app):
        self.page.locator(f'.app-card[data-app="{app}"]').click()
        self.page.wait_for_timeout(200)

    # --- 布局 -----------------------------------------------------------

    def test_the_sidebar_is_to_the_left_of_the_header(self):
        sidebar = self.page.locator("#sidebar").bounding_box()
        header = self.page.locator("header.top").bounding_box()
        self.assertEqual(sidebar["x"], 0)
        self.assertLess(sidebar["x"], header["x"])
        self.assertGreaterEqual(sidebar["width"], 200)

    def test_the_workspace_starts_where_the_sidebar_ends(self):
        sidebar = self.page.locator("#sidebar").bounding_box()
        workspace = self.page.locator(".workspace").bounding_box()
        self.assertAlmostEqual(workspace["x"], sidebar["width"], delta=1)

    # --- 卡片 -----------------------------------------------------------

    def test_one_card_per_registered_downloader_in_order(self):
        self.assertEqual(self.card_ids(), ["tiktok", "youtube"])

    def test_cards_carry_only_an_icon_and_no_text(self):
        # 需求就是"卡片上不用写文字，只放对应 app 的图标"
        self.assertEqual(self.page.locator(".app-card b, .app-card small, .app-card-tag").count(), 0)
        for index in range(self.cards().count()):
            text = self.cards().nth(index).inner_text().strip()
            self.assertLessEqual(len(text), 3, f"卡片上有文字：{text!r}")

    def test_each_card_still_says_what_it_is(self):
        # 没文字就得靠 title / aria-label 说明，否则只剩两个看不懂的图标
        labels = self.page.eval_on_selector_all(
            ".app-card", "els => els.map(e => [e.title, e.getAttribute('aria-label')])")
        self.assertIn("TikTok 下载器", labels[0][0])
        self.assertIn("YouTube 下载器", labels[1][0])
        self.assertIn("尚未接入", labels[1][0])
        for title, aria in labels:
            self.assertTrue(title and aria)

    def test_the_active_card_is_marked(self):
        self.assertIn("active", self.cards().nth(0).get_attribute("class"))
        self.assertNotIn("active", self.cards().nth(1).get_attribute("class"))

    def test_a_card_without_a_pane_is_marked_as_planned(self):
        self.assertIn("planned", self.cards().nth(1).get_attribute("class"))
        self.assertNotIn("planned", self.cards().nth(0).get_attribute("class"))

    # --- 切换 -----------------------------------------------------------

    def test_switching_to_a_planned_downloader_shows_the_placeholder(self):
        self.click_card("youtube")
        self.assertFalse(self.page.locator("#paneTiktok").is_visible())
        self.assertTrue(self.page.locator("#panePlanned").is_visible())
        title = self.page.locator("#plannedTitle").inner_text()
        self.assertIn("YouTube 下载器", title)
        self.assertIn("尚未接入", title)
        self.assertEqual(self.page.evaluate("activeDownloader"), "youtube")
        self.assertEqual(self.errors, [])

    def test_the_placeholder_explains_how_to_wire_it_up(self):
        # 占位不是一句 TODO，而是把接入方式写出来
        self.click_card("youtube")
        self.assertIn("registerDownloader", self.page.locator("#panePlanned").inner_text())

    def test_tools_belong_to_the_active_downloader(self):
        self.assertEqual(self.page.locator(".app-tool").count(), 3)
        self.click_card("youtube")
        self.assertEqual(self.page.locator(".app-tool").count(), 0,
                         "YouTube 还没有工具，不该继续显示 TikTok 的")
        self.click_card("tiktok")
        self.assertEqual(self.page.locator(".app-tool").count(), 3)

    def test_the_tools_still_open_their_panels_after_a_round_trip(self):
        # 工具按钮是 innerHTML 渲染出来的，重新渲染会把监听器丢掉 —— 这条盯住它
        self.click_card("youtube")
        self.click_card("tiktok")
        self.page.locator("#settings").click()
        self.page.wait_for_selector("#settingsModal:not(.hidden)")
        self.page.locator("#settingsCancel").click()
        self.page.wait_for_function(
            "document.getElementById('settingsModal').classList.contains('hidden')")
        self.page.locator("#assetsButton").click()
        self.page.wait_for_selector("#assetsModal:not(.hidden)")
        self.page.locator("#assetsClose").click()
        self.page.wait_for_function(
            "document.getElementById('assetsModal').classList.contains('hidden')")
        self.assertEqual(self.errors, [])

    # --- 收起 / 展开 -----------------------------------------------------

    def toggle(self):
        self.page.locator("#sidebarToggle").click()
        self.page.wait_for_timeout(300)

    def test_collapsing_narrows_the_sidebar_and_moves_the_workspace(self):
        wide = self.page.locator("#sidebar").bounding_box()["width"]
        self.toggle()
        narrow = self.page.locator("#sidebar").bounding_box()["width"]
        self.assertLess(narrow, wide)
        self.assertAlmostEqual(self.page.locator(".workspace").bounding_box()["x"], narrow, delta=1)
        self.assertIn("sidebar-collapsed", self.page.locator(".app").get_attribute("class"))

    def test_the_cards_survive_collapsing(self):
        # 卡片本来就只有图标，收起后应该原样还在
        self.toggle()
        self.assertEqual(self.card_ids(), ["tiktok", "youtube"])
        self.assertTrue(self.cards().first.is_visible())
        self.assertFalse(self.page.locator(".app-tool .sidebar-label").first.is_visible())

    def test_the_toggle_flips_its_own_affordance(self):
        toggle = self.page.locator("#sidebarToggle")
        self.assertIn("收起", toggle.get_attribute("title"))
        self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
        self.toggle()
        self.assertIn("展开", toggle.get_attribute("title"))
        self.assertEqual(toggle.get_attribute("aria-expanded"), "false")

    def test_the_collapsed_state_is_remembered(self):
        # 这是**界面偏好**（用户主动调过），跟设置页那个"激活哪个分类"不是一回事
        self.toggle()
        self.page.reload()
        self.page.wait_for_selector("#appCards", state="attached")
        self.page.wait_for_timeout(300)
        self.assertIn("sidebar-collapsed", self.page.locator(".app").get_attribute("class"))
        saved = self.page.evaluate("JSON.parse(localStorage.getItem('tiktok-prefs')||'{}')")
        self.assertTrue(saved.get("sidebarCollapsed"))

    # --- 扩展接口 -------------------------------------------------------

    def test_registering_a_downloader_adds_a_card(self):
        self.page.evaluate(
            "registerDownloader({id:'bilibili',label:'B 站下载器',icon:'X',"
            "desc:'批量下载 UP 主视频',status:'planned'})")
        self.page.wait_for_timeout(150)
        self.assertEqual(self.card_ids(), ["tiktok", "youtube", "bilibili"])
        self.click_card("bilibili")
        self.assertIn("B 站下载器", self.page.locator("#plannedTitle").inner_text())

    def test_the_card_grid_wraps_when_more_downloaders_arrive(self):
        # 加第 4 个不该把侧边栏撑破，也不该把已有卡片挤没
        for index in range(2, 5):
            self.page.evaluate(
                f"registerDownloader({{id:'app{index}',label:'应用{index}',icon:'X',status:'planned'}})")
        self.page.wait_for_timeout(200)
        self.assertEqual(len(self.card_ids()), 5)
        rows = self.page.eval_on_selector_all(
            ".app-card", "els => [...new Set(els.map(e => Math.round(e.getBoundingClientRect().top)))]")
        self.assertGreater(len(rows), 1, "卡片应该换行，而不是全挤在一行")

    def test_a_duplicate_id_is_rejected_loudly(self):
        message = self.page.evaluate(
            "() => { try { registerDownloader({id:'tiktok'}); return 'no error'; }"
            " catch (e) { return e.message; } }")
        self.assertIn("tiktok", message)

    def test_a_downloader_without_an_id_is_rejected(self):
        message = self.page.evaluate(
            "() => { try { registerDownloader({label:'没有 id'}); return 'no error'; }"
            " catch (e) { return e.message; } }")
        self.assertNotEqual(message, "no error")

    # --- 版本号 ---------------------------------------------------------

    def test_the_sidebar_shows_the_version(self):
        self.page.wait_for_function(
            "document.getElementById('sidebarVersion').textContent.startsWith('v')")
        self.assertEqual(self.page.locator("#sidebarVersion").inner_text(), "v1.6.0")


if __name__ == "__main__":
    unittest.main()
