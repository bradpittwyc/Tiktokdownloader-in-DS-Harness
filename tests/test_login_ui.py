"""Exercise login settings in a real, isolated browser with a fake app bridge.

No TikTok request, saved browser profile, or real Cookie is used by this test.
"""

import os
from pathlib import Path
import shutil
import unittest

from playwright.sync_api import sync_playwright


UI_PATH = Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP/ui/index.html"

BRIDGE = """
window.testLoginCalls = 0;
window.cookieResult = {ok:true, count:0, hasSession:false, busy:false};
window.pywebview = {api:{
  recent_profiles:async()=>[],
  refresh_recent_profiles:async()=>({ok:true, profiles:[]}),
  set_cookie_options:async()=>window.cookieResult,
  get_cookie_status:()=>new Promise(resolve=>{window.resolveCookieStatus=resolve}),
  get_filename_template:async()=>'%(title)s_%(upload_date)s',
  get_learning_options:async()=>({enabled:false, apiKeySet:false}),
  set_filename_template:async()=>({ok:true}),
  set_learning_options:async()=>({ok:true}),
  login_tiktok:async()=>{window.testLoginCalls++;return {ok:true, busy:true}},
  cancel_tiktok_login:async()=>({ok:true})
}};
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


class LoginSettingsUITest(unittest.TestCase):
    def test_verification_is_explicit_and_resume_keeps_target(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path(), headless=True)
            try:
                page = browser.new_page(viewport={"width":1280,"height":900})
                runtime_errors=[]
                page.on("pageerror", lambda error: runtime_errors.append(str(error)))
                page.route("https://**/*", lambda route: route.abort())
                page.add_init_script(BRIDGE)
                page.goto(UI_PATH.as_uri())
                page.evaluate("""() => {
                    window.verifyCalls=[]; window.resumeCalls=[];
                    window.pywebview.api.verify_profile=async name=>{verifyCalls.push(name);return {ok:true,busy:true}};
                    recognize=async url=>resumeCalls.push(url);
                    document.getElementById('url').value='https://www.tiktok.com/@apple';
                    showScrapeResult({username:'apple',needsVerification:true,complete:false,warning:'需要验证'});
                }""")
                self.assertEqual(page.evaluate("verifyCalls.length"),0)
                page.locator("#verifyProfile").click()
                self.assertEqual(page.evaluate("verifyCalls"),["apple"])
                self.assertTrue(page.locator("#verifyProfile").is_disabled())
                page.evaluate("verificationStatus({username:'apple',ready:false,error:'用户取消'})")
                self.assertEqual(page.evaluate("resumeCalls.length"),0)
                self.assertIn("用户取消",page.locator("#scrapeNotice").inner_text())
                page.locator("#verifyProfile").click()
                page.evaluate("verificationStatus({username:'apple',ready:true})")
                self.assertEqual(page.evaluate("resumeCalls"),["https://www.tiktok.com/@apple"])
                self.assertEqual(runtime_errors,[])
            finally:
                browser.close()

    def test_login_settings_async_states(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path(), headless=True)
            self.addCleanup(browser.close)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            runtime_errors = []
            page.on("pageerror", lambda error: runtime_errors.append(str(error)))
            page.route("https://**/*", lambda route: route.abort())
            page.add_init_script(BRIDGE)
            page.goto(UI_PATH.as_uri())

            # An unresolved status API must not keep the whole settings panel hidden.
            page.locator("#settings").click()
            self.assertTrue(page.locator("#settingsModal").is_visible())
            page.evaluate("""window.resolveCookieStatus({
                ok:true,count:3,hasSession:false,source:'Chrome Cookie',busy:false
            })""")
            page.wait_for_function("document.getElementById('cookieStatus').textContent.includes('未检测到登录 Cookie')")

            # Calling a handler twice before the API returns opens only one login.
            page.evaluate("""document.getElementById('loginTikTok').onclick();
                document.getElementById('loginTikTok').onclick()""")
            page.wait_for_function("window.testLoginCalls===1")
            self.assertTrue(page.locator("#loginTikTok").is_disabled())
            self.assertTrue(page.locator("#cancelTikTokLogin").is_visible())

            # A mere status read does not overwrite a user's unsaved source choice.
            page.evaluate("""document.getElementById('cookieBrowser').value='edge';
                cookieStatus({ok:true,count:12,hasSession:true,
                source:'软件保存的 TikTok 登录态',browser:'saved',busy:false})""")
            self.assertEqual(page.locator("#cookieBrowser").input_value(), "edge")

            # Only the successful save event switches and persists the source.
            page.evaluate("""cookieStatus({ok:true,count:12,hasSession:true,
                source:'软件保存的 TikTok 登录态',browser:'saved',saved:true,busy:false})""")
            self.assertEqual(page.locator("#loginTikTok").inner_text(), "更新登录态")
            self.assertEqual(page.locator("#cookieBrowser").input_value(), "saved")
            self.assertEqual(page.evaluate("JSON.parse(localStorage.getItem('tiktok-prefs')).cookieBrowser"), "saved")

            # Import failures must not disappear behind a generic save success.
            page.evaluate("""window.cookieResult={ok:false,count:0,hasSession:false,
                busy:false,error:'导入失败：Chrome Cookie 无法解密'}""")
            page.locator("#useChromeLogin").click()
            page.wait_for_function("JSON.parse(localStorage.getItem('tiktok-prefs')).cookieBrowser==='chrome'")
            page.locator("#settingsSave").click()
            page.wait_for_function("document.getElementById('settingsMessage').textContent.includes('Chrome Cookie 无法解密')")
            self.assertTrue(page.locator("#settingsModal").is_visible())
            self.assertIn("Chrome Cookie 无法解密", page.locator("#status").inner_text())
            self.assertFalse(page.locator("#settingsSave").is_disabled())
            self.assertEqual(runtime_errors, [])
            browser.close()
            self._cleanups.clear()


if __name__ == "__main__":
    unittest.main()
