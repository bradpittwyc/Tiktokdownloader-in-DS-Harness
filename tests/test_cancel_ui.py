"""UI 层验证：抓取期间按钮变成「暂停抓取」，点它可以中断。

用真实浏览器 + 假 bridge，recognize 挂在一个手动 resolve 的 Promise 上，
这样能稳定地观察"抓取进行中"这个状态。
"""

import os
from pathlib import Path
import shutil
import unittest

from playwright.sync_api import sync_playwright


UI_PATH = Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP/ui/index.html"

BRIDGE = """
window.cancelCalls = 0;
window.scrapeCalls = [];
window.pywebview = {api:(()=>{
  const explicit = {
    recent_profiles: async()=>[],
    refresh_recent_profiles: async()=>({ok:true,profiles:[]}),
    open_profile: async()=>({ok:false,missing:true}),
    recognize: async url=>{window.scrapeCalls.push(url);
      return new Promise(resolve=>{window.finishScrape=resolve})},
    cancel_collect: async()=>{window.cancelCalls++;return {ok:true}},
    load_task_state: async()=>({ok:true,records:{}}),
    enrich: async()=>({updated:0})
  };
  return new Proxy(explicit,{get(target,prop){
    if(typeof prop!=='string') return undefined;
    if(prop in target) return target[prop];
    return async()=>({ok:true});
  }});
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


class CancelButtonUITest(unittest.TestCase):
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

    def start_scrape(self):
        self.page.fill("#url", "https://www.tiktok.com/@apple")
        self.page.locator("#recognize").click()
        self.page.wait_for_function("window.scrapeCalls.length===1")

    def finish(self, payload):
        self.page.evaluate(f"window.finishScrape({payload})")
        self.page.wait_for_function("!document.getElementById('recognize').disabled")

    def test_button_becomes_an_enabled_pause_while_scraping(self):
        self.assertEqual(self.page.locator("#recognize").inner_text(), "识别")
        self.start_scrape()

        button = self.page.locator("#recognize")
        self.assertEqual(button.inner_text(), "暂停抓取")
        self.assertFalse(button.is_disabled(),
                         "抓取期间按钮置灰的话，用户就没有任何中断手段了")

    def test_clicking_pause_asks_the_backend_to_cancel(self):
        self.start_scrape()
        self.page.locator("#recognize").click()
        self.page.wait_for_function("window.cancelCalls===1")
        # 点过之后先显示"正在停止"，避免用户以为没反应而连点
        self.assertEqual(self.page.locator("#recognize").inner_text(), "正在停止…")

    def test_button_returns_to_refresh_after_the_cancel_lands(self):
        self.start_scrape()
        self.page.locator("#recognize").click()
        self.page.wait_for_function("window.cancelCalls===1")
        self.finish("""{ok:true,complete:false,cancelled:true,needsVerification:false,
            warning:'已取消抓取，已保留 0 条。',username:'apple',avatar:'',
            profileStats:{},videos:[]}""")
        self.assertEqual(self.page.locator("#recognize").inner_text(), "重新抓取")
        self.assertIn("已暂停抓取", self.page.locator("#status").inner_text())
        self.assertEqual(self.errors, [])

    def test_a_cancelled_run_is_not_reported_as_a_broken_scrape(self):
        self.start_scrape()
        self.finish("""{ok:true,complete:false,cancelled:true,needsVerification:false,
            warning:'已取消抓取，已保留 12 条。',username:'apple',avatar:'',
            profileStats:{},videos:[]}""")
        status = self.page.locator("#status").inner_text()
        self.assertIn("已暂停抓取", status)
        self.assertNotIn("抓取中断", status)

    def test_a_normal_interruption_still_reads_as_interrupted(self):
        self.start_scrape()
        self.finish("""{ok:true,complete:false,cancelled:false,needsVerification:false,
            warning:'分页连续 45 秒没有前进',username:'apple',avatar:'',
            profileStats:{},videos:[]}""")
        self.assertIn("抓取中断", self.page.locator("#status").inner_text())

    def test_button_says_identify_before_any_profile_is_open(self):
        # 没打开过博主时不该显示"重新抓取"
        self.assertEqual(self.page.locator("#recognize").inner_text(), "识别")

    def test_pause_does_not_fire_a_second_scrape(self):
        self.start_scrape()
        self.page.locator("#recognize").click()
        self.page.wait_for_function("window.cancelCalls===1")
        self.assertEqual(self.page.evaluate("window.scrapeCalls.length"), 1,
                         "抓取中再点按钮不该又发起一次抓取")


if __name__ == "__main__":
    unittest.main()
