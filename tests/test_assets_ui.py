"""素材库弹窗的真实浏览器测试。

真 Chrome + 假 `window.pywebview.api` + 全网络阻断，断言零 JS 运行时错误。
重点锁接线：扫描要调到后端、摘要是真数据、没有博主时补齐必须被拦住
（文件名里没有作品 id，不知道链接就没法补），以及后端推送的
`assetBackfill` 事件要能驱动界面文字。
"""

import json
import os
from pathlib import Path
import shutil
import unittest

from playwright.sync_api import sync_playwright


UI_PATH = Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP/ui/index.html"

SCAN_RESULT = {
    "ok": True, "folder": "F:\\TikTok",
    "summary": {"total": 51, "complete": 0, "incomplete": 51, "byGap": {"cover": 50},
                "bytes": 183897779, "videos": 50, "posts": 1},
    "incomplete": [
        {"folder": "F:\\TikTok\\@nasa", "stem": "Clip One_20260905", "type": "video",
         "media": "F:\\TikTok\\@nasa\\Clip One_20260905.mp4",
         "gaps": ["cover", "audio", "info", "meta"], "subtitles": 0},
        {"folder": "F:\\TikTok\\@nasa", "stem": "Clip Two_20260906", "type": "video",
         "media": "F:\\TikTok\\@nasa\\Clip Two_20260906.mp4",
         "gaps": ["meta"], "subtitles": 1},
    ],
    "truncated": 0, "problems": [],
}

BRIDGE = """
window.calls = [];
window.scanResult = %s;
window.backfillResult = {ok:true, updated:7, examined:9, failed:[], folder:'F:\\\\TikTok\\\\@nasa', cancelled:false};
window.pywebview = {api:(()=>{
  const explicit = {
    scan_assets: async f=>{window.calls.push('scan_assets:'+f);return window.scanResult},
    backfill_assets: async (f,u)=>{window.calls.push('backfill_assets:'+f+'|'+u);return window.backfillResult},
    cancel_asset_backfill: async()=>{window.calls.push('cancel_asset_backfill');return {ok:true}},
    choose_folder: async()=>{window.calls.push('choose_folder');return 'F:\\\\TikTok'},
    recent_profiles: async()=>[], refresh_recent_profiles: async()=>({ok:true,profiles:[]}),
    load_task_state: async()=>({ok:true,records:{}}), enrich: async()=>({updated:0}),
    get_update_info: async()=>({ok:true,current:'1.1.0',tokenSet:false}),
    set_cookie_options: async()=>({ok:true,hasSession:false}),
    get_cookie_status: async()=>({ok:true,hasSession:false}),
    get_filename_template: async()=>'', get_learning_options: async()=>({})
  };
  return new Proxy(explicit,{get(target,prop){
    if(typeof prop!=='string') return undefined;
    if(prop in target) return target[prop];
    return async()=>({ok:true});
  }});
})()};
""" % json.dumps(SCAN_RESULT, ensure_ascii=False)


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


class AssetsUITest(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(executable_path=chrome_path(),
                                                       headless=True)
        self.addCleanup(self._stop)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("https://**/*", lambda route: route.abort())
        self.page.add_init_script(BRIDGE)
        self.page.goto(UI_PATH.as_uri())
        self.page.wait_for_selector("#assetsButton", state="attached")
        self.page.wait_for_timeout(200)
        self.page.evaluate("window.calls=[]")

    def _stop(self):
        self.browser.close()
        self.playwright.stop()

    def set_folder(self, folder=r"F:\TikTok"):
        # 用参数传值，避免在 JS 字符串里数反斜杠
        self.page.evaluate("value => { prefs.defaultFolder = value }", folder)

    def open_modal(self):
        self.page.locator("#assetsButton").click()
        self.page.wait_for_selector("#assetsModal:not(.hidden)")

    def test_the_modal_opens_and_scans_the_remembered_folder(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("window.calls.some(c=>c.startsWith('scan_assets'))")
        self.assertIn(r"scan_assets:F:\TikTok", self.page.evaluate("window.calls"))
        self.assertEqual(self.errors, [])

    def test_the_summary_and_the_gap_list_come_from_the_backend(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function(
            "document.getElementById('assetsSummary').textContent.includes('51')")
        summary = self.page.locator("#assetsSummary").inner_text()
        self.assertIn("共 51 条素材", summary)
        self.assertIn("待补 51 条", summary)
        listing = self.page.locator("#assetsList").inner_text()
        self.assertIn("Clip One_20260905", listing)
        self.assertIn("封面", listing)
        self.assertIn("元数据", listing)

    def test_backfill_is_blocked_until_we_know_which_creator(self):
        # 文件名里没有作品 id，不知道链接就没法补 —— 必须拦住而不是空跑
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function(
            "document.getElementById('assetsList').textContent.includes('Clip One')")
        self.assertTrue(self.page.locator("#assetsBackfill").is_disabled(),
                        "不知道博主时按钮就该是灰的")
        # 禁用按钮点不动，所以直接调函数验证兜底那一层
        self.page.evaluate("assetsBackfill()")
        self.page.wait_for_function(
            "document.getElementById('assetsMessage').textContent.length>0")
        self.assertIn("博主", self.page.locator("#assetsMessage").inner_text())
        self.assertEqual(self.page.evaluate(
            "window.calls.filter(c=>c.startsWith('backfill_assets')).length"), 0)

    def test_backfill_runs_with_the_open_profile(self):
        self.set_folder()
        self.page.evaluate("currentUsername='nasa'")
        self.open_modal()
        self.page.wait_for_function(
            "!document.getElementById('assetsBackfill').disabled")
        self.page.locator("#assetsBackfill").click()
        self.page.wait_for_function(
            "window.calls.some(c=>c.startsWith('backfill_assets'))")
        self.assertIn(r"backfill_assets:F:\TikTok|nasa",
                      self.page.evaluate("window.calls"))
        self.page.wait_for_function(
            "document.getElementById('assetsMessage').textContent.includes('更新 7 条')")
        self.assertEqual(self.errors, [])

    def test_the_progress_event_drives_the_message(self):
        self.set_folder()
        self.open_modal()
        self.page.evaluate("assetBackfill({current:3,total:9,id:'1',state:'working'})")
        self.assertIn("3/9", self.page.locator("#assetsMessage").inner_text())
        self.page.evaluate("assetBackfill({done:true,updated:7,failed:1,total:9})")
        self.assertIn("更新 7 条", self.page.locator("#assetsMessage").inner_text())

    def test_closing_the_modal_works(self):
        self.open_modal()
        self.page.locator("#assetsClose").click()
        self.page.wait_for_function(
            "document.getElementById('assetsModal').classList.contains('hidden')")

    def test_a_failed_scan_shows_the_backend_error(self):
        self.page.evaluate(
            "window.scanResult={ok:false,error:'目录不存在：Z:\\\\nope'};")
        self.set_folder("Z:\\nope")
        self.open_modal()
        self.page.wait_for_function(
            "document.getElementById('assetsMessage').textContent.includes('目录不存在')")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
