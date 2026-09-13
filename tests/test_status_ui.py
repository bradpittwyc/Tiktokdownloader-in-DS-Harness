"""状态栏文字：后台补全元数据的进度提示必须在结束时消失。

用户截图里的 "列表可用 · 后台读取数据 98/1272" 就是它残留的样子。
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
window.scrapeCalls = [];
window.recentRows = [{username:'apple', avatar:'', count:2, complete:true, lastSync:1}];
window.cachedResult = {ok:true, cached:true, complete:true, warning:'',
  needsVerification:false, username:'apple', avatar:'', profileStats:{followers:'1.2M'},
  videos:__VIDEOS__,
  message:'已载入本地记录 2 条（最后更新 09:36），没有重新抓取'};
window.scrapedResult = {ok:true, complete:true, warning:'', needsVerification:false,
  username:'apple', avatar:'', profileStats:{}, videos:__VIDEOS__};
window.pywebview = {api:(()=>{
  const explicit = {
    recent_profiles: async()=>window.recentRows,
    refresh_recent_profiles: async()=>({ok:true,profiles:window.recentRows}),
    open_profile: async()=>window.cachedResult,
    recognize: async url=>{window.scrapeCalls.push(url);
      return new Promise(resolve=>{window.finishScrape=resolve})},
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


class StatusTextUITest(unittest.TestCase):
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

    def status(self):
        return self.page.locator("#status").inner_text()

    def run_a_full_scrape(self):
        # 现在是本地记录优先，有缓存就不会真抓 —— 先让缓存落空
        self.page.evaluate("window.cachedResult={ok:false,missing:true}")
        self.page.fill("#url", "https://www.tiktok.com/@apple")
        self.page.locator("#recognize").click()
        self.page.wait_for_function("window.scrapeCalls.length===1")
        self.page.evaluate("window.finishScrape(window.scrapedResult)")
        self.page.wait_for_function(
            "document.getElementById('status').textContent.includes('已读取至作品末页')")

    def test_progress_text_shows_while_metadata_loads(self):
        self.run_a_full_scrape()
        self.page.evaluate("metadataStatus({current:1,total:2})")
        self.assertEqual(self.status(), "列表可用 · 后台读取数据 1/2")

    def test_done_signal_removes_the_progress_text(self):
        self.run_a_full_scrape()
        self.page.evaluate("metadataStatus({current:2,total:2})")
        self.page.evaluate("metadataStatus({done:true})")
        self.assertNotIn("后台读取数据", self.status(),
                         "后台补全结束后不该还挂着进度文字")
        self.assertEqual(self.status(), "已读取至作品末页：2 条")

    def test_done_signal_cannot_clobber_an_in_progress_scrape(self):
        self.run_a_full_scrape()
        self.page.locator("#recognize").click()      # 又发起一次抓取，recognizing=true
        self.page.wait_for_function("window.scrapeCalls.length===2")
        self.page.evaluate("metadataStatus({done:true})")
        self.assertNotIn("已读取至作品末页", self.status(),
                         "上一次的结束信号不能覆盖正在进行的抓取状态")

    def test_opening_a_local_record_says_so_instead_of_the_scrape_summary(self):
        self.page.wait_for_selector(".recent-card")
        self.page.locator(".recent-card").click()
        self.page.wait_for_function(
            "document.getElementById('status').textContent.includes('本地记录')")
        text = self.status()
        self.assertIn("没有重新抓取", text)
        self.assertNotIn("已读取至作品末页", text,
                         "从本地记录打开不该显示成刚抓完")
        self.assertEqual(self.errors, [])

    def test_a_local_record_keeps_its_words_after_metadata_signals(self):
        self.page.wait_for_selector(".recent-card")
        self.page.locator(".recent-card").click()
        self.page.wait_for_function(
            "document.getElementById('status').textContent.includes('本地记录')")
        self.page.evaluate("metadataStatus({done:true})")
        self.assertIn("本地记录", self.status())


if __name__ == "__main__":
    unittest.main()
