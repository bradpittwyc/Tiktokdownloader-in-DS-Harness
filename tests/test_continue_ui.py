"""UI 层验证：「继续抓取」在后台跑，当前页面完全不受影响。

用真实浏览器 + 假 bridge，零网络。
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
window.continueCalls = [];
window.scrapeCalls = [];
window.recentRows = [{username:'apple', avatar:'', count:2, complete:false, lastSync:1}];
window.cachedResult = {ok:true, cached:true, complete:false,
  warning:'本地记录上次没抓完，点「继续抓取」可以在后台接着补齐，不影响当前页面。',
  needsVerification:false, username:'apple', avatar:'', profileStats:{followers:'1.2M'},
  videos:__VIDEOS__, message:'已载入本地记录 2 条，没有重新抓取'};
window.pywebview = {api:(()=>{
  const explicit = {
    recent_profiles: async()=>window.recentRows,
    refresh_recent_profiles: async()=>({ok:true,profiles:window.recentRows}),
    open_profile: async()=>window.cachedResult,
    recognize: async url=>{window.scrapeCalls.push(url);return {ok:true,complete:true,
        warning:'',needsVerification:false,username:'apple',avatar:'',
        profileStats:{},videos:__VIDEOS__}},
    continue_collect: async url=>{window.continueCalls.push(url);return {ok:true,started:true}},
    load_task_state: async()=>({ok:true,records:{}}),
    enrich: async()=>({updated:0})
  };
  return new Proxy(explicit,{get(target,prop){
    if(typeof prop!=='string') return undefined;
    if(prop in target) return target[prop];
    return async()=>({ok:true});
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


class ContinueCollectUITest(unittest.TestCase):
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
        self.page.wait_for_selector("#recognize")

    def _stop(self):
        self.browser.close()
        self.playwright.stop()

    def open_incomplete_record(self):
        self.page.wait_for_selector(".recent-card")
        self.page.locator(".recent-card").click()
        self.page.wait_for_function("!!document.getElementById('continueCollect')")

    def test_an_incomplete_record_offers_to_continue(self):
        self.open_incomplete_record()
        self.assertTrue(self.page.locator("#scrapeNotice").is_visible())
        self.assertIn("继续抓取",
                      self.page.locator("#scrapeNotice").inner_text())
        self.assertEqual(self.page.locator("#continueCollect").inner_text(), "继续抓取")

    def test_a_complete_record_offers_nothing(self):
        self.page.evaluate("window.cachedResult.complete=true")
        self.page.evaluate("window.cachedResult.warning=''")
        self.page.wait_for_selector(".recent-card")
        self.page.locator(".recent-card").click()
        self.page.wait_for_function(
            "document.getElementById('status').textContent.includes('本地记录')")
        self.assertEqual(self.page.locator("#continueCollect").count(), 0)
        self.assertTrue(self.page.locator("#scrapeNotice").is_hidden())

    def test_clicking_it_starts_a_background_run(self):
        self.open_incomplete_record()
        self.page.locator("#continueCollect").click()
        self.page.wait_for_function("window.continueCalls.length===1")
        self.assertEqual(self.page.evaluate("window.continueCalls"),
                         ["https://www.tiktok.com/@apple"])
        self.assertEqual(self.page.evaluate("window.scrapeCalls.length"), 0,
                         "后台继续不该走 recognize 那条阻塞路径")

    def test_the_page_stays_fully_usable_while_it_runs(self):
        self.open_incomplete_record()
        rows = self.page.locator("#list .row").count()
        self.page.locator("#continueCollect").click()
        self.page.wait_for_function("window.continueCalls.length===1")

        # 没有任何"加载中"的副作用
        self.assertFalse(self.page.evaluate("recognizing"),
                         "后台抓取不该把界面推进加载态")
        self.assertTrue(self.page.locator("#scrapeLoading").is_hidden())
        self.assertFalse(self.page.locator("#url").is_disabled())
        self.assertFalse(self.page.locator("#recognize").is_disabled())
        self.assertEqual(self.page.locator("#list .row").count(), rows,
                         "列表不该被清空或重建")
        self.assertEqual(self.page.locator("#continueCollect").inner_text(), "后台抓取中…")

        # 勾选、翻页这些操作照常可用
        self.page.locator("#selectAll").click()
        self.assertIn("已选 2", self.page.locator("#selection").inner_text())

    def test_finishing_for_real_hides_the_notice(self):
        self.open_incomplete_record()
        self.page.locator("#continueCollect").click()
        self.page.wait_for_function("window.continueCalls.length===1")
        self.page.evaluate("""backgroundCollect({username:'apple',running:false,complete:true,
            count:900,needsVerification:false,warning:''})""")
        self.assertTrue(self.page.locator("#scrapeNotice").is_hidden())
        self.assertIn("已读取至作品末页", self.page.locator("#status").inner_text())
        self.assertEqual(self.page.locator("#continueCollect").count(), 0)

    def test_finishing_short_leaves_the_button_usable(self):
        self.open_incomplete_record()
        self.page.locator("#continueCollect").click()
        self.page.wait_for_function("window.continueCalls.length===1")
        self.page.evaluate("""backgroundCollect({username:'apple',running:false,complete:false,
            count:900,needsVerification:false,warning:'分页连续 45 秒没有前进'})""")
        button = self.page.locator("#continueCollect")
        self.assertEqual(button.inner_text(), "继续抓取")
        self.assertFalse(button.is_disabled())
        self.assertTrue(self.page.locator("#scrapeNotice").is_visible())

    def test_a_failed_background_run_reports_and_recovers(self):
        self.open_incomplete_record()
        self.page.locator("#continueCollect").click()
        self.page.wait_for_function("window.continueCalls.length===1")
        self.page.evaluate("""backgroundCollect({username:'apple',running:false,
            error:'TikTok 没有返回可读取的作品'})""")
        self.assertIn("后台抓取失败", self.page.locator("#status").inner_text())
        self.assertEqual(self.page.locator("#continueCollect").inner_text(), "继续抓取")
        self.assertFalse(self.page.locator("#continueCollect").is_disabled())

    def test_switching_creator_leaves_the_open_page_alone(self):
        self.open_incomplete_record()
        self.page.locator("#continueCollect").click()
        self.page.wait_for_function("window.continueCalls.length===1")
        self.page.fill("#url", "https://www.tiktok.com/@banana")
        notice_before = self.page.locator("#scrapeNotice").inner_text()

        self.page.evaluate("""backgroundCollect({username:'apple',running:false,complete:true,
            count:900,needsVerification:false,warning:''})""")

        self.assertEqual(self.page.locator("#scrapeNotice").inner_text(), notice_before,
                         "用户已经切走了，不该重建当前页面的提示条")
        self.assertIn("已后台补齐", self.page.locator("#status").inner_text())

    def test_the_bar_reports_progress_without_touching_pagination(self):
        self.open_incomplete_record()
        self.page.evaluate("window.continueCalls=[]")
        # 翻到第 2 页需要更多数据，这里只验证 archiveUpdate 不会把页码打回 1
        self.page.evaluate("""archiveUpdate({username:'apple',complete:false,
            videos:window.cachedResult.videos})""")
        self.assertEqual(self.page.evaluate("currentPage"), 1)
        self.assertEqual(self.page.locator("#list .row").count(), 2)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
