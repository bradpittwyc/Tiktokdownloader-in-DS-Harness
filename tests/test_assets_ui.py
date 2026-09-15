"""素材库弹窗的真实浏览器测试。

真 Chrome + 假 `window.pywebview.api` + 全网络阻断，断言零 JS 运行时错误。

重点锁三件事：
1. 扫描/补齐的接线（目录、用户名要传对）
2. **检索、排序、筛选全在本地做** —— 敲键不发请求（发一次就多一次延迟，
   而且断网就用不了）
3. 没有博主时补齐必须被拦住（文件名里没有作品 id，不知道链接就没法补）
"""

import json
import os
from pathlib import Path
import shutil
import unittest

from playwright.sync_api import sync_playwright


UI_PATH = Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP/ui/index.html"

ROWS = [
    {"folder": "F:\\TikTok\\@nasa", "stem": "We are going back_20260905", "type": "video",
     "media": "F:\\TikTok\\@nasa\\We are going back_20260905.mp4",
     "gaps": ["cover", "audio", "info", "meta"], "subtitles": 0,
     "id": "111", "title": "We are going back to the Moon", "author": "nasa",
     "nickname": "NASA", "date": "20260905", "duration": 66, "views": 341100,
     "likes": 31900, "comments": 12, "shares": 30, "hashtags": ["Moon", "Artemis"],
     "resolution": "720x1280", "desc": "Summer winds down #Moon", "hasMeta": False},
    {"folder": "F:\\TikTok\\@nasa", "stem": "Midnight pasta_20260906", "type": "video",
     "media": "F:\\TikTok\\@nasa\\Midnight pasta_20260906.mp4",
     "gaps": [], "subtitles": 1,
     "id": "222", "title": "Midnight pasta experiment", "author": "chef",
     "nickname": "Chef Ann", "date": "20260906", "duration": 30, "views": 1200,
     "likes": 90, "comments": 1, "shares": 2, "hashtags": ["food"],
     "resolution": "1080x1920", "desc": "a midnight snack #food", "hasMeta": True},
    {"folder": "F:\\TikTok\\@nasa", "stem": "Photo set_20260907_[333]", "type": "image",
     "media": "F:\\TikTok\\@nasa\\Photo set_20260907_[333]",
     "gaps": ["meta"], "subtitles": 0,
     "id": "333", "title": "Photo set from Tokyo", "author": "taro", "nickname": "Taro",
     "date": "20260907", "duration": None, "views": 88000, "likes": 7000,
     "comments": 3, "shares": 8, "hashtags": [], "resolution": "",
     "desc": "Tokyo streets", "hasMeta": False},
]

SCAN_RESULT = {
    "ok": True, "folder": "F:\\TikTok",
    "summary": {"total": 3, "complete": 1, "incomplete": 2,
                "byGap": {"cover": 1, "audio": 1, "info": 1, "meta": 2},
                "bytes": 183897779, "videos": 2, "posts": 1},
    "rows": ROWS,
    "problems": [],
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
    get_update_info: async()=>({ok:true,current:'1.2.0',tokenSet:false}),
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

    def listing(self):
        return self.page.locator("#assetsList").inner_text()

    def search(self, text):
        self.page.fill("#assetsQuery", text)
        self.page.wait_for_timeout(250)      # 防抖 120ms

    # --- 接线 ---------------------------------------------------------

    def test_the_modal_opens_and_scans_the_remembered_folder(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("window.calls.some(c=>c.startsWith('scan_assets'))")
        self.assertIn(r"scan_assets:F:\TikTok", self.page.evaluate("window.calls"))
        self.assertEqual(self.errors, [])

    def test_it_lists_every_asset_not_only_the_incomplete_ones(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function(
            "document.getElementById('assetsSummary').textContent.includes('共 3 条素材')")
        self.assertIn("完整 1 / 待补 2", self.page.locator("#assetsSummary").inner_text())
        listing = self.listing()
        for title in ("We are going back to the Moon", "Midnight pasta experiment",
                      "Photo set from Tokyo"):
            self.assertIn(title, listing)

    def test_each_row_shows_the_creator_date_duration_and_gap(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
        listing = self.listing()
        self.assertIn("nasa", listing)
        self.assertIn("20260905", listing)
        self.assertIn("66s", listing)
        self.assertIn("34.1万播放", listing)
        self.assertIn("缺 封面、音频、原始信息、元数据", listing)

    # --- 本地检索 -----------------------------------------------------

    def test_search_matches_the_title(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
        self.search("pasta")
        listing = self.listing()
        self.assertIn("Midnight pasta experiment", listing)
        self.assertNotIn("We are going back to the Moon", listing)
        self.assertIn("当前显示 1 条", self.page.locator("#assetsSummary").inner_text())

    def test_search_matches_the_description_and_hashtags_and_author(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
        self.search("artemis")                       # 话题
        self.assertIn("We are going back to the Moon", self.listing())
        self.search("midnight snack")                # 文案（两个词都只在这条里）
        self.assertIn("Midnight pasta experiment", self.listing())
        self.search("taro")                          # 作者
        self.assertIn("Photo set from Tokyo", self.listing())
        self.assertNotIn("Midnight pasta", self.listing())

    def test_multiple_terms_all_have_to_match(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
        self.search("moon pasta")                    # 没有任何一条同时含这两个词
        self.assertIn("没有匹配的素材", self.listing())

    def test_search_is_case_insensitive(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
        self.search("MOON")
        self.assertIn("We are going back to the Moon", self.listing())

    def test_searching_never_calls_the_backend_again(self):
        # 检索必须在本地做完，否则每敲一个字都要等一次往返
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("window.calls.some(c=>c.startsWith('scan_assets'))")
        self.page.evaluate("window.calls=[]")
        self.search("pasta")
        self.search("moon")
        self.assertEqual(self.page.evaluate(
            "window.calls.filter(c=>c.startsWith('scan_assets')).length"), 0)

    def test_no_query_restores_the_full_list(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
        self.search("pasta")
        self.assertNotIn("Photo set", self.listing())
        self.search("")
        self.assertIn("Photo set from Tokyo", self.listing())

    # --- 排序与筛选 ---------------------------------------------------

    def test_sorting_by_views_puts_the_biggest_first(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
        self.page.select_option("#assetsSort", "views")
        self.page.wait_for_timeout(100)
        listing = self.listing()
        self.assertLess(listing.index("We are going back to the Moon"),
                        listing.index("Photo set from Tokyo"))
        self.assertLess(listing.index("Photo set from Tokyo"),
                        listing.index("Midnight pasta"))

    def test_sorting_by_duration_is_descending(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
        self.page.select_option("#assetsSort", "duration")
        self.page.wait_for_timeout(100)
        listing = self.listing()
        self.assertLess(listing.index("We are going back to the Moon"),
                        listing.index("Midnight pasta"))

    def test_the_type_filter_narrows_to_photo_posts(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
        self.page.select_option("#assetsType", "image")
        self.page.wait_for_timeout(100)
        listing = self.listing()
        self.assertIn("Photo set from Tokyo", listing)
        self.assertNotIn("Midnight pasta", listing)

    def test_the_gaps_filter_shows_only_what_needs_backfilling(self):
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
        self.page.check("#assetsOnlyGaps")
        self.page.wait_for_timeout(100)
        listing = self.listing()
        self.assertIn("We are going back to the Moon", listing)
        self.assertNotIn("Midnight pasta", listing)      # 这条是完整的

    def test_the_backfill_button_follows_the_current_filter(self):
        # 只看待补时，按钮就该是在补"这批"；筛到一条都不缺时它该灰掉
        self.set_folder()
        self.page.evaluate("currentUsername='nasa'")
        self.open_modal()
        self.page.wait_for_function("!document.getElementById('assetsBackfill').disabled")
        self.page.check("#assetsOnlyGaps")
        self.page.wait_for_timeout(100)
        self.assertFalse(self.page.locator("#assetsBackfill").is_disabled())
        self.search("pasta")                              # 只剩那条完整的
        self.assertTrue(self.page.locator("#assetsBackfill").is_disabled(),
                        "筛出来的都不缺文件时不该还能点补齐")

    # --- 补齐与错误处理 -----------------------------------------------

    def test_backfill_is_blocked_until_we_know_which_creator(self):
        # 文件名里没有作品 id，不知道链接就没法补 —— 必须拦住而不是空跑
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
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
        self.page.wait_for_function("!document.getElementById('assetsBackfill').disabled")
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
