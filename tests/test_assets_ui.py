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
     "tags": [], "summary": "",
     "resolution": "720x1280", "desc": "Summer winds down #Moon", "hasMeta": False},
    {"folder": "F:\\TikTok\\@nasa", "stem": "Midnight pasta_20260906", "type": "video",
     "media": "F:\\TikTok\\@nasa\\Midnight pasta_20260906.mp4",
     "gaps": [], "subtitles": 1,
     "id": "222", "title": "Midnight pasta experiment", "author": "chef",
     "nickname": "Chef Ann", "date": "20260906", "duration": 30, "views": 1200,
     "likes": 90, "comments": 1, "shares": 2, "hashtags": ["food"],
     "tags": [], "summary": "",
     "resolution": "1080x1920", "desc": "a midnight snack #food", "hasMeta": True},
    {"folder": "F:\\TikTok\\@nasa", "stem": "Photo set_20260907_[333]", "type": "image",
     "media": "F:\\TikTok\\@nasa\\Photo set_20260907_[333]",
     "gaps": ["meta"], "subtitles": 0,
     "id": "333", "title": "Photo set from Tokyo", "author": "taro", "nickname": "Taro",
     "date": "20260907", "duration": None, "views": 88000, "likes": 7000,
     "comments": 3, "shares": 8, "hashtags": [], "tags": ["街拍", "东京"],
     "summary": "东京街头随拍", "resolution": "",
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
window.tagResult = {ok:true, updated:1, failed:[], skipped:{no_meta:1,tagged:1,empty:0,filtered:0}, cancelled:false};
window.learningOptions = {apiKeySet:false, keyEncrypted:false, provider:'openai', api_base:'https://api.openai.com/v1', model:'gpt-4o-mini'};
window.pywebview = {api:(()=>{
  const explicit = {
    scan_assets: async f=>{window.calls.push('scan_assets:'+f);return window.scanResult},
    backfill_assets: async (f,u)=>{window.calls.push('backfill_assets:'+f+'|'+u);return window.backfillResult},
    cancel_asset_backfill: async()=>{window.calls.push('cancel_asset_backfill');return {ok:true}},
    tag_assets: async (f,paths)=>{window.calls.push('tag_assets:'+f+'|'+(paths||[]).join(','));return window.tagResult},
    cancel_asset_tagging: async()=>{window.calls.push('cancel_asset_tagging');return {ok:true}},
    get_learning_options: async()=>window.learningOptions,
    clear_learning_api_key: async()=>{window.calls.push('clear_learning_api_key');return {ok:true,removed:true}},
    choose_folder: async()=>{window.calls.push('choose_folder');return 'F:\\\\TikTok'},
    recent_profiles: async()=>[], refresh_recent_profiles: async()=>({ok:true,profiles:[]}),
    load_task_state: async()=>({ok:true,records:{}}), enrich: async()=>({updated:0}),
    get_update_info: async()=>({ok:true,current:'1.3.0',tokenSet:false}),
    set_cookie_options: async()=>({ok:true,hasSession:false}),
    get_cookie_status: async()=>({ok:true,hasSession:false}),
    get_filename_template: async()=>''
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

    # --- AI 打标签 -----------------------------------------------------

    def open_with_ai(self):
        self.page.evaluate("window.learningOptions={apiKeySet:true,provider:'deepseek'}")
        self.set_folder()
        self.open_modal()
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")

    def test_the_tag_button_is_greyed_out_without_an_api_key(self):
        self.set_folder()
        self.open_modal()                       # 默认 apiKeySet:false
        self.page.wait_for_function("document.getElementById('assetsList').textContent.includes('Moon')")
        self.assertTrue(self.page.locator("#assetsTag").is_disabled())
        self.page.evaluate("assetsTag()")
        self.page.wait_for_function("document.getElementById('assetsMessage').textContent.length>0")
        self.assertIn("API Key", self.page.locator("#assetsMessage").inner_text())
        self.assertEqual(self.page.evaluate(
            "window.calls.filter(c=>c.startsWith('tag_assets')).length"), 0)

    def test_rows_show_the_ai_tags_when_they_have_them(self):
        self.open_with_ai()
        self.assertIn("🏷 街拍 · 东京", self.listing())

    def test_search_matches_ai_tags(self):
        # 这正是打标签的意义：文案里没明写的内容也能搜到
        self.open_with_ai()
        self.search("街拍")
        listing = self.listing()
        self.assertIn("Photo set from Tokyo", listing)
        self.assertNotIn("Midnight pasta", listing)

    def test_the_untagged_filter_hides_tagged_assets(self):
        self.open_with_ai()
        self.page.check("#assetsOnlyUntagged")
        self.page.wait_for_timeout(150)
        listing = self.listing()
        self.assertIn("We are going back to the Moon", listing)
        self.assertNotIn("Photo set from Tokyo", listing)

    def test_tagging_sends_the_paths_of_exactly_what_is_shown(self):
        # 打标签认路径，所以搜索之后打的就真的只是搜出来那几条
        self.open_with_ai()
        self.search("pasta")
        self.page.wait_for_function("!document.getElementById('assetsTag').disabled")
        self.page.locator("#assetsTag").click()
        self.page.wait_for_function("window.calls.some(c=>c.startsWith('tag_assets'))")
        call = next(c for c in self.page.evaluate("window.calls")
                    if c.startswith("tag_assets"))
        self.assertIn("Midnight pasta_20260906.mp4", call)
        self.assertNotIn("Moon", call)
        self.assertEqual(self.errors, [])

    def test_assets_without_meta_are_not_tagged_and_are_reported(self):
        # 没 meta 就没标题文案，后端会跳过 —— 界面要把这件事说出来
        self.open_with_ai()
        self.page.wait_for_function("!document.getElementById('assetsTag').disabled")
        self.page.locator("#assetsTag").click()
        self.page.wait_for_function(
            "document.getElementById('assetsMessage').textContent.includes('打标签完成')")
        message = self.page.locator("#assetsMessage").inner_text()
        self.assertIn("成功 1 条", message)
        self.assertIn("还没补齐", message)

    def test_the_tag_button_is_greyed_when_everything_visible_is_tagged(self):
        self.open_with_ai()
        self.search("Photo set")            # 这条已经有标签了
        self.assertTrue(self.page.locator("#assetsTag").is_disabled())

    def test_the_tagging_progress_event_drives_the_message(self):
        self.set_folder()
        self.open_modal()
        self.page.evaluate("assetTagging({current:2,total:5,stem:'Clip',state:'working'})")
        self.assertIn("2/5", self.page.locator("#assetsMessage").inner_text())
        self.page.evaluate("assetTagging({done:true,updated:4,failed:1,total:5})")
        self.assertIn("成功 4 条", self.page.locator("#assetsMessage").inner_text())

    def open_ai_module(self, key_configured=True):
        """设置页是分类面板，AI 配置在「AI 大模型」下。"""
        if key_configured:
            self.page.evaluate(
                "window.learningOptions={apiKeySet:true,keyEncrypted:true,provider:'deepseek',"
                "api_base:'https://api.deepseek.com/v1',model:'deepseek-chat'}")
        self.page.locator("#settings").click()
        self.page.wait_for_selector("#settingsModal:not(.hidden)")
        self.page.locator('#settingsNav .settings-nav-item[data-module="ai"]').click()
        self.page.wait_for_selector('#settingsModal .settings-module[data-module="ai"].active')

    def test_switching_the_provider_fills_in_its_defaults(self):
        self.open_ai_module()
        self.page.select_option("#learningProvider", "deepseek")
        self.assertEqual(self.page.input_value("#learningBase"), "https://api.deepseek.com/v1")
        self.assertEqual(self.page.input_value("#learningModel"), "deepseek-chat")
        self.page.select_option("#learningProvider", "openai")
        self.assertEqual(self.page.input_value("#learningModel"), "gpt-4o-mini")

    def test_custom_provider_leaves_what_the_user_typed_alone(self):
        self.open_ai_module()
        self.page.fill("#learningBase", "https://my-gateway.local/v1")
        self.page.select_option("#learningProvider", "custom")
        self.assertEqual(self.page.input_value("#learningBase"), "https://my-gateway.local/v1")

    def test_the_key_state_is_explained_without_ever_showing_the_key(self):
        # 密钥是写只的：界面只说明"配没配、怎么存的"，永远显示不出值
        self.open_ai_module()
        note = self.page.locator("#learningKeyState").inner_text()
        self.assertIn("DPAPI", note)
        self.assertIn("留空表示不修改", note)
        self.assertEqual(self.page.input_value("#learningKey"), "")
        self.assertTrue(self.page.locator("#clearLearningKey").is_visible(),
                        "已经配了密钥就该给出显式的清除入口")

    def test_the_learning_options_include_the_grammar_checkbox(self):
        self.open_ai_module()
        labels = self.page.eval_on_selector_all(
            ".learning-checks label", "els => els.map(e => e.textContent.trim())")
        self.assertEqual(len(labels), 4, labels)
        self.assertTrue(any("语法点" in text for text in labels), labels)
        self.assertTrue(self.page.is_checked("#learnGrammar"), "默认应该勾上")
        self.assertIn("md", " ".join(labels), "标签上要写清楚会另存 .md")

    def test_the_grammar_checkbox_is_sent_to_the_backend(self):
        self.open_ai_module()
        self.page.uncheck("#learnGrammar")
        self.assertFalse(self.page.evaluate("learningInput().grammar"))
        self.page.check("#learnGrammar")
        self.assertTrue(self.page.evaluate("learningInput().grammar"))

    def test_the_grammar_checkbox_reflects_the_saved_value(self):
        self.page.evaluate(
            "window.learningOptions={apiKeySet:true,keyEncrypted:true,grammar:false,"
            "provider:'openai',api_base:'https://api.openai.com/v1',model:'gpt-4o-mini'}")
        self.open_ai_module(key_configured=False)
        self.page.wait_for_function("document.getElementById('learnGrammar').checked===false")
        self.assertEqual(self.errors, [])

    def test_clearing_the_key_calls_the_backend_and_updates_the_note(self):
        self.open_ai_module()
        self.page.locator("#clearLearningKey").click()
        self.page.wait_for_function("window.calls.includes('clear_learning_api_key')")
        self.assertIn("已清除", self.page.locator("#settingsMessage").inner_text())
        self.assertIn("尚未配置", self.page.locator("#learningKeyState").inner_text())
        self.assertFalse(self.page.locator("#clearLearningKey").is_visible(),
                         "清掉之后清除按钮该收起来")

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
