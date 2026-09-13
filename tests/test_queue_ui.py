"""下载队列分页 + 下载历史。

真实浏览器 + 假 bridge，零网络。
"""

import json
import os
from pathlib import Path
import shutil
import unittest

from playwright.sync_api import sync_playwright


UI_PATH = Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP/ui/index.html"

SEED_RECORDS = {
    "apple": {
        "1002": {"item": {"id": "1002", "title": "较新的作品", "cover": ""},
                 "state": "done", "folder": "F:\\TikTok\\@apple", "subtitles": [], "error": ""},
        "1001": {"item": {"id": "1001", "title": "较旧的作品", "cover": ""},
                 "state": "failed", "folder": "", "subtitles": [],
                 "error": "下载器未生成目标文件"},
    },
    "banana": {
        "2001": {"item": {"id": "2001", "title": "别家的作品", "cover": ""},
                 "state": "skipped", "folder": "F:\\TikTok\\@banana", "subtitles": [], "error": ""},
    },
}

BRIDGE = """
window.openedFolders = [];
try{ localStorage.setItem('tiktok-download-records', JSON.stringify(__SEED__)) }catch(e){}
window.pywebview = {api:(()=>{
  const explicit = {
    recent_profiles: async()=>[],
    refresh_recent_profiles: async()=>({ok:true,profiles:[]}),
    open_folder: async path=>{window.openedFolders.push(path);return {ok:true}},
    load_task_state: async()=>({ok:true,records:{}}),
    enrich: async()=>({updated:0})
  };
  return new Proxy(explicit,{get(target,prop){
    if(typeof prop!=='string') return undefined;
    if(prop in target) return target[prop];
    return async()=>({ok:true});
  }});
})()};
""".replace("__SEED__", json.dumps(SEED_RECORDS, ensure_ascii=False))


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


class QueueAndHistoryUITest(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(executable_path=chrome_path(),
                                                       headless=True)
        self.addCleanup(self._stop)
        self.page = self.browser.new_page(viewport={"width": 1600, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("https://**/*", lambda route: route.abort())
        self.page.add_init_script(BRIDGE)
        self.page.goto(UI_PATH.as_uri())
        self.page.wait_for_selector("#queuePageInfo")

    def _stop(self):
        self.browser.close()
        self.playwright.stop()

    def seed_queue(self, count):
        self.page.evaluate("""count => {
            videos = Array.from({length: count}, (_, i) => ({
                id: String(1000 + i), title: '作品' + i, type: 'video'}));
            selected = new Set(videos.map(v => v.id));
            states = {}; folders = {}; subtitles = {}; errors = {}; queuePage = 1;
            renderQueue();
        }""", count)

    # ---------- 队列分页 ----------

    def test_eight_cards_per_page(self):
        self.seed_queue(20)
        self.assertEqual(self.page.locator("#queue .q-card").count(), 8)
        self.assertEqual(self.page.locator("#queuePageInfo").inner_text(), "1/3")

    def test_first_page_cannot_go_back(self):
        self.seed_queue(20)
        self.assertTrue(self.page.locator("#queuePrev").is_disabled())
        self.assertFalse(self.page.locator("#queueNext").is_disabled())

    def test_paging_forward_walks_the_selection(self):
        self.seed_queue(20)
        self.page.locator("#queueNext").click()
        self.assertEqual(self.page.locator("#queuePageInfo").inner_text(), "2/3")
        self.assertEqual(self.page.locator("#queue .q-card").count(), 8)
        self.assertFalse(self.page.locator("#queuePrev").is_disabled())

        self.page.locator("#queueNext").click()
        self.assertEqual(self.page.locator("#queuePageInfo").inner_text(), "3/3")
        self.assertEqual(self.page.locator("#queue .q-card").count(), 4,
                         "最后一页只有 20-16=4 个")
        self.assertTrue(self.page.locator("#queueNext").is_disabled())

        self.page.locator("#queuePrev").click()
        self.assertEqual(self.page.locator("#queuePageInfo").inner_text(), "2/3")

    def test_a_selection_that_fits_has_one_disabled_page(self):
        self.seed_queue(8)
        self.assertEqual(self.page.locator("#queuePageInfo").inner_text(), "1/1")
        self.assertEqual(self.page.locator("#queue .q-card").count(), 8)
        self.assertTrue(self.page.locator("#queuePrev").is_disabled())
        self.assertTrue(self.page.locator("#queueNext").is_disabled())

    def test_shrinking_the_selection_clamps_the_page(self):
        self.seed_queue(20)
        self.page.locator("#queueNext").click()
        self.page.locator("#queueNext").click()
        self.assertEqual(self.page.locator("#queuePageInfo").inner_text(), "3/3")
        # 只剩 5 个 -> 1 页，页码要收回来而不是空着
        self.page.evaluate("""() => {
            selected = new Set(videos.slice(0, 5).map(v => v.id)); renderQueue();
        }""")
        self.assertEqual(self.page.locator("#queuePageInfo").inner_text(), "1/1")
        self.assertEqual(self.page.locator("#queue .q-card").count(), 5)

    def test_an_empty_queue_still_shows_the_footer(self):
        self.page.evaluate("() => { selected = new Set(); videos = []; renderQueue(); }")
        self.assertIn("勾选视频后", self.page.locator("#queue").inner_text())
        self.assertTrue(self.page.locator("#downloadHistory").is_visible(),
                        "没勾选也要能看历史")

    # ---------- 底部工具条的位置 ----------

    def test_history_button_is_bottom_left_and_pager_bottom_right(self):
        self.seed_queue(20)
        foot = self.page.locator(".queue-foot").bounding_box()
        queue = self.page.locator("#queue").bounding_box()
        history = self.page.locator("#downloadHistory").bounding_box()
        prev = self.page.locator("#queuePrev").bounding_box()
        panel = self.page.locator(".queue-panel").bounding_box()

        self.assertGreater(foot["y"], queue["y"] + queue["height"] - 1,
                           "工具条应该在队列列表下方")
        self.assertLess(history["x"], prev["x"], "历史按钮在左、翻页在右")
        self.assertLess(history["x"] - panel["x"], panel["width"] / 2,
                        "历史按钮靠左半区")
        self.assertGreater(prev["x"] - panel["x"], panel["width"] / 2,
                           "翻页按钮靠右半区")
        self.assertAlmostEqual(foot["y"] + foot["height"], panel["y"] + panel["height"],
                               delta=20, msg="工具条贴在面板底部")

    # ---------- 下载历史 ----------

    def test_history_lists_every_profile_newest_first(self):
        self.page.locator("#downloadHistory").click()
        self.assertTrue(self.page.locator("#historyModal").is_visible())
        rows = self.page.locator("#historyList .history-row")
        self.assertEqual(rows.count(), 3)
        # 作品 id 是雪花号，越大越新
        self.assertIn("别家的作品", rows.nth(0).inner_text())
        self.assertIn("较新的作品", rows.nth(1).inner_text())
        self.assertIn("较旧的作品", rows.nth(2).inner_text())
        self.assertEqual(self.errors, [])

    def test_history_shows_state_and_profile(self):
        self.page.locator("#downloadHistory").click()
        text = self.page.locator("#historyList").inner_text()
        self.assertIn("@apple", text)
        self.assertIn("@banana", text)
        self.assertIn("已完成", text)
        self.assertIn("已存在，跳过", text)
        self.assertIn("失败", text)

    def test_history_opens_the_folder_of_a_finished_download(self):
        self.page.locator("#downloadHistory").click()
        self.assertEqual(self.page.locator("#historyList .h-open").count(), 2,
                         "只有带 folder 的记录才有「打开」")
        self.page.locator("#historyList .h-open").first.click()
        self.page.wait_for_function("window.openedFolders.length===1")
        self.assertEqual(self.page.evaluate("window.openedFolders"),
                         ["F:\\TikTok\\@banana"])

    def test_history_reports_the_total(self):
        self.page.locator("#downloadHistory").click()
        self.assertIn("3 条", self.page.locator("#historySummary").inner_text())

    def test_history_closes(self):
        self.page.locator("#downloadHistory").click()
        self.assertTrue(self.page.locator("#historyModal").is_visible())
        self.page.locator("#historyClose").click()
        self.assertTrue(self.page.locator("#historyModal").is_hidden())

    def test_history_empty_state(self):
        self.page.evaluate("() => { records = {}; openHistory(); }")
        self.assertTrue(self.page.locator("#historyModal").is_visible())
        self.assertIn("还没有下载记录", self.page.locator("#historyList").inner_text())
        self.assertEqual(self.page.locator("#historyList .history-row").count(), 0)

    def test_history_ignores_records_without_an_item(self):
        self.page.evaluate("""() => {
            records = {apple: {1: {state:'done', folder:'X'}}}; openHistory();
        }""")
        self.assertEqual(self.page.locator("#historyList .history-row").count(), 0)


if __name__ == "__main__":
    unittest.main()
