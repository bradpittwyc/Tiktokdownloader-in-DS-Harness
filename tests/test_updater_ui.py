"""UI 层：设置里的「软件更新」区。

真实浏览器 + 假 bridge，零网络。重点锁住接线：
点检查更新要调到后端、有新版本要显示说明和安装按钮、下载要走对方法。
"""

import json
import os
from pathlib import Path
import shutil
import unittest

from playwright.sync_api import sync_playwright


UI_PATH = Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP/ui/index.html"

BRIDGE = """
window.calls = [];
window.updateInfo = {ok:true, current:'1.0.2', repo:'owner/repo', tokenSet:false};
window.checkResult = {ok:true, current:'1.0.2', latest:'1.0.3', tag:'v1.0.3',
  hasUpdate:true, notes:'## 新版本\\n- 修了某个问题', publishedAt:'2026-09-13T00:00:00Z',
  htmlUrl:'https://github.com/owner/repo/releases/tag/v1.0.3',
  asset:{name:'TikTokBatchMVP-Setup-1.0.3.exe', size:104857600,
         url:'https://api.github.com/x', download:'https://github.com/x'}};
window.downloadResult = {ok:true, path:'F:\\\\Temp\\\\setup.exe', version:'1.0.3', size:104857600};
window.pywebview = {api:(()=>{
  const explicit = {
    recent_profiles: async()=>[], refresh_recent_profiles: async()=>({ok:true,profiles:[]}),
    load_task_state: async()=>({ok:true,records:{}}), enrich: async()=>({updated:0}),
    get_update_info: async()=>{window.calls.push('get_update_info');return window.updateInfo},
    check_update: async()=>{window.calls.push('check_update');return window.checkResult},
    set_update_token: async t=>{window.calls.push('set_update_token:'+t);return {ok:true,tokenSet:!!t}},
    download_update: async()=>{window.calls.push('download_update');return window.downloadResult},
    install_update: async p=>{window.calls.push('install_update:'+p);return {ok:true}},
    open_release_page: async u=>{window.calls.push('open_release_page:'+u);return {ok:true}}
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


class UpdaterUITest(unittest.TestCase):
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
        # 更新区在默认隐藏的设置弹窗里，等它挂载即可，不能等可见
        self.page.wait_for_selector("#checkUpdate", state="attached")
        # 启动时那次静默检查会发请求，先清掉记录
        self.page.wait_for_timeout(200)
        self.page.evaluate("window.calls=[]")

    def _stop(self):
        self.browser.close()
        self.playwright.stop()

    def open_settings(self):
        self.page.locator("#settings").click()
        self.page.wait_for_selector("#settingsModal:not(.hidden)")

    def test_settings_shows_the_current_version(self):
        self.open_settings()
        self.page.wait_for_function(
            "document.getElementById('updateCurrent').textContent.includes('1.0.2')")
        self.assertIn("get_update_info", self.page.evaluate("window.calls"))
        self.assertIn("当前版本", self.page.locator("#updateCurrent").inner_text())

    def test_checking_reports_a_new_version_with_notes(self):
        self.open_settings()
        self.page.locator("#checkUpdate").click()
        self.page.wait_for_function("window.calls.includes('check_update')")
        self.page.wait_for_selector("#doUpdate")
        text = self.page.locator("#updateBox").inner_text()
        self.assertIn("发现新版本 1.0.3", text)
        self.assertIn("修了某个问题", text, "release 说明要显示出来")
        self.assertEqual(self.errors, [])

    def test_no_update_says_so(self):
        self.page.evaluate("window.checkResult={ok:true,current:'1.0.3',latest:'1.0.3',hasUpdate:false}")
        self.open_settings()
        self.page.locator("#checkUpdate").click()
        self.page.wait_for_function(
            "document.getElementById('updateBox').textContent.includes('最新版本')")
        self.assertEqual(self.page.locator("#doUpdate").count(), 0)

    def test_missing_token_is_explained(self):
        self.page.evaluate("""window.checkResult={ok:false,needsToken:true,
            current:'1.0.2',error:'读不到 release。仓库如果是私有的，需要在下面填 GitHub Token。'}""")
        self.open_settings()
        self.page.locator("#checkUpdate").click()
        self.page.wait_for_function(
            "document.getElementById('updateBox').textContent.includes('检查失败')")
        text = self.page.locator("#updateBox").inner_text()
        self.assertIn("GitHub Token", text)
        self.assertIn("仓库设为公开", text)

    def test_install_button_downloads_then_installs(self):
        self.open_settings()
        self.page.locator("#checkUpdate").click()
        self.page.wait_for_selector("#doUpdate")
        self.page.locator("#doUpdate").click()
        self.page.wait_for_function("window.calls.some(c=>c.startsWith('install_update:'))")
        calls = self.page.evaluate("window.calls")
        self.assertIn("download_update", calls)
        self.assertTrue(any(c.startswith("install_update:") for c in calls),
                        "下载完必须调 install_update 并带上路径")
        self.assertIn("下载完成", self.page.locator("#updateBox").inner_text())

    def test_progress_events_drive_the_bar(self):
        self.open_settings()
        self.page.locator("#checkUpdate").click()
        self.page.wait_for_selector("#doUpdate")
        self.page.evaluate("""() => {
            updateBox('<div class="update-bar"><i id="updateBar"></i></div><div id="updateText"></div>');
            updateProgress({downloaded: 52428800, total: 104857600, percent: 50});
        }""")
        self.assertEqual(self.page.evaluate("document.getElementById('updateBar').style.width"),
                         "50%")
        self.assertIn("50.0 / 100.0 MB", self.page.locator("#updateText").inner_text())

    def test_saving_a_token_calls_the_backend_and_clears_the_field(self):
        self.open_settings()
        self.page.fill("#updateToken", "ghp_secret_value")
        self.page.locator("#saveUpdateToken").click()
        self.page.wait_for_function("window.calls.some(c=>c.startsWith('set_update_token:'))")
        calls = self.page.evaluate("window.calls")
        self.assertIn("set_update_token:ghp_secret_value", calls)
        self.assertEqual(self.page.input_value("#updateToken"), "",
                         "保存后要把输入框清掉，别把密钥留在屏幕上")

    def test_release_page_button_passes_the_url(self):
        self.open_settings()
        self.page.locator("#checkUpdate").click()
        self.page.wait_for_selector("#openReleasePage")
        self.page.locator("#openReleasePage").click()
        self.page.wait_for_function("window.calls.some(c=>c.startsWith('open_release_page:'))")
        calls = self.page.evaluate("window.calls")
        self.assertTrue(any("releases/tag/v1.0.3" in c for c in calls))


if __name__ == "__main__":
    unittest.main()
