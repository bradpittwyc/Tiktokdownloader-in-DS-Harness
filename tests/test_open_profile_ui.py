"""UI 层验证：点博主头像 / 输入链接时，有本地记录就直接进页面，不重新抓取。

用真实浏览器 + 假 bridge，不发任何网络请求。
"""

import json
import os
from pathlib import Path
import shutil
import unittest

from playwright.sync_api import sync_playwright


UI_PATH = Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP/ui/index.html"

VIDEOS = [
    {"id": "1", "url": "https://www.tiktok.com/@apple/video/1", "title": "作品一", "type": "video"},
    {"id": "2", "url": "https://www.tiktok.com/@apple/video/2", "title": "作品二", "type": "video"},
]

BRIDGE = """
window.openCalls = [];
window.scrapeCalls = [];
window.otherApiCalls = [];
window.recentRows = [{username:'apple', avatar:'', count:2, complete:true, lastSync:1}];
window.cachedResult = {ok:true, cached:true, complete:true, warning:'',
  needsVerification:false, username:'apple', avatar:'', profileStats:{followers:'1.2M'},
  videos:__VIDEOS__,
  message:'已载入本地记录 2 条（最后更新 09:36），没有重新抓取'};
window.pywebview = {api:(()=>{
  // 前端一共调用 29 个 bridge 方法；这里只显式实现本次要断言的那几个，
  // 其余的走 Proxy 兜底，免得测试因为无关方法缺失而失败。
  const explicit = {
    recent_profiles: async()=>window.recentRows,
    refresh_recent_profiles: async()=>({ok:true,profiles:window.recentRows}),
    open_profile: async url=>{window.openCalls.push(url);return window.cachedResult},
    recognize: async url=>{window.scrapeCalls.push(url);return {ok:true,complete:true,warning:'',
        needsVerification:false,username:'apple',avatar:'',profileStats:{},videos:__VIDEOS__}},
    load_task_state: async()=>({ok:true,records:{}}),
    enrich: async()=>({updated:0})
  };
  return new Proxy(explicit,{get(target,prop){
    if(typeof prop!=='string') return undefined;
    if(prop in target) return target[prop];
    return async()=>{window.otherApiCalls.push(prop);return {ok:true}};
  }});
})()};
""".replace("__VIDEOS__", json.dumps(VIDEOS, ensure_ascii=False))


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


class OpenLocalRecordUITest(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(executable_path=chrome_path(),
                                                       headless=True)
        self.addCleanup(self._stop)
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("https://**/*", lambda route: route.abort())
        self.page.add_init_script(BRIDGE)
        self.page.goto(UI_PATH.as_uri())

    def _stop(self):
        self.browser.close()
        self.playwright.stop()

    def test_clicking_a_recent_avatar_uses_the_local_record(self):
        self.page.wait_for_selector(".recent-card")
        self.page.locator(".recent-card").click()
        self.page.wait_for_function("window.openCalls.length===1")

        self.assertEqual(self.page.evaluate("window.scrapeCalls.length"), 0,
                         "点头像不该重新抓取")
        self.assertEqual(self.page.evaluate("window.openCalls"),
                         ["https://www.tiktok.com/@apple"])
        # 列表直接用本地记录渲染出来了
        self.assertEqual(self.page.locator("#allCount").inner_text(), "2")
        self.assertTrue(self.page.locator("#profile").is_visible())
        self.assertEqual(self.page.locator("#list .row").count(), 2)
        self.assertEqual(self.errors, [])

    def test_the_card_shows_how_much_is_stored_locally(self):
        self.page.wait_for_selector(".recent-card")
        title = self.page.locator(".recent-card").get_attribute("title")
        self.assertIn("2 条", title)
        self.assertIn("已抓完", title)

    def test_pasting_a_new_link_checks_the_local_record_first(self):
        self.page.fill("#url", "https://www.tiktok.com/@banana")
        self.page.locator("#recognize").click()
        self.page.wait_for_function("window.openCalls.length===1")
        self.assertEqual(self.page.evaluate("window.openCalls"),
                         ["https://www.tiktok.com/@banana"])
        self.assertEqual(self.page.evaluate("window.scrapeCalls.length"), 0,
                         "换了博主也应该先看本地记录")
        self.assertEqual(self.errors, [])

    def test_refreshing_the_same_profile_really_scrapes(self):
        self.page.wait_for_selector(".recent-card")
        self.page.locator(".recent-card").click()
        self.page.wait_for_function("window.openCalls.length===1")
        self.assertEqual(self.page.locator("#recognize").inner_text(), "重新抓取")

        self.page.evaluate("window.openCalls=[];window.scrapeCalls=[]")
        self.page.locator("#recognize").click()
        self.page.wait_for_function("window.scrapeCalls.length===1")
        self.assertEqual(self.page.evaluate("window.openCalls.length"), 0,
                         "「重新抓取」必须真的抓，不能再命中本地记录")
        self.assertEqual(self.page.evaluate("window.scrapeCalls"),
                         ["https://www.tiktok.com/@apple"])
        self.assertEqual(self.errors, [])

    def test_without_a_local_record_it_falls_through_to_scraping(self):
        self.page.evaluate("window.cachedResult={ok:false,missing:true,username:'stranger'}")
        self.page.fill("#url", "https://www.tiktok.com/@stranger")
        self.page.locator("#recognize").click()
        self.page.wait_for_function("window.scrapeCalls.length===1")
        self.assertEqual(self.page.evaluate("window.scrapeCalls"),
                         ["https://www.tiktok.com/@stranger"])
        self.assertEqual(self.page.evaluate("window.openCalls"),
                         ["https://www.tiktok.com/@stranger"])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
