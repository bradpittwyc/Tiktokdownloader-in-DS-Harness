"""RC1 真机 Smoke Test：启动真实桌面程序，走一遍「创作者监控 → 添加创作者」。

不是模拟：起的是 outputs/TikTokBatchMVP/web_app.py（真 pywebview 窗口 + 真桥接 +
真 sqlite），通过 WebView2 的调试端口连进去，在真实页面上填表单、点真实的
「＋ 添加创作者」「保存」按钮，然后直接查库确认落盘，关掉进程再起一次确认数据还在。

为什么用原始 CDP 而不是 Playwright：实测 Playwright 的 connect_over_cdp 对
WebView2 只能看到 about:blank 与 devtools:// 页面（应用页面明明在 /json/list 里），
而且 WebView2 拒绝 Target.createBrowserContext（Not allowed）。所以这里直接用
websockets 说 CDP 协议：Runtime.evaluate 取元素、派发真实鼠标事件、点真实按钮。

隔离：LOCALAPPDATA 指向临时目录，所以不会碰用户本机的
%LOCALAPPDATA%/TikTokBatchMVP/content-factory.db。

用法：
    python scripts/rc1_creator_smoke.py
"""

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import websockets.sync.client as ws_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, "scripts", "webview_cdp_launcher.py")
APP_DIR = os.path.join(ROOT, "outputs", "TikTokBatchMVP")
DEBUG_PORT = 9333


# ---------------------------------------------------------------- 进程

def env_for(appdata):
    env = dict(os.environ)
    env["LOCALAPPDATA"] = appdata
    env["PYTHONIOENCODING"] = "utf-8"
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={DEBUG_PORT}"
    return env


def db_path(appdata):
    return os.path.join(appdata, "TikTokBatchMVP", "content-factory.db")


def launch(appdata):
    return subprocess.Popen([sys.executable, LAUNCHER, str(DEBUG_PORT)],
                            env=env_for(appdata), cwd=APP_DIR,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def wait_for_cdp(timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version",
                                        timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.5)
    raise RuntimeError(f"WebView2 的调试端口 {DEBUG_PORT} 在 {timeout}s 内没起来")


def app_target(timeout=60):
    """找到内容工厂主页面这个 target。

    debug 窗口会额外出现在 /json/list 里，所以要按 URL 挑；
    app.html 既可能是 file://（打包版）也可能是 http://127.0.0.1:<port>/app.html
    （pywebview 起的内置服务器），所以按路径匹配而不是按协议匹配。
    """
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/list",
                                        timeout=5) as response:
                targets = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            targets = []
        for target in targets:
            url = target.get("url") or ""
            seen.append(url)
            if urlparse(url).path.endswith("/app.html") and target.get("webSocketDebuggerUrl"):
                return target
        time.sleep(0.5)
    raise RuntimeError(f"没找到内容工厂页面；当前 target：{seen}")


# ---------------------------------------------------------------- CDP

class Page:
    """最小 CDP 客户端：够用就好（求值 / 派发真实鼠标事件 / 截图）。"""

    def __init__(self, ws_url):
        self._ws = ws_client.connect(ws_url, max_size=64 * 1024 * 1024)
        self._id = 0

    def send(self, method, **params):
        self._id += 1
        message_id = self._id
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method} 失败：{message['error']}")
                return message.get("result", {})

    def eval(self, expression):
        result = self.send("Runtime.evaluate", expression=expression, returnByValue=True,
                           awaitPromise=True)
        if result.get("exceptionDetails"):
            raise RuntimeError(f"页面脚本报错：{result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def wait_for(self, expression, timeout=20, interval=0.3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.eval(expression):
                return True
            time.sleep(interval)
        return False

    def click(self, selector):
        """派发真实鼠标事件（不是 element.click()）：走的是浏览器自己的命中测试。"""
        box = self.eval(f"""(() => {{
          const node = document.querySelector({json.dumps(selector)});
          if (!node) return null;
          node.scrollIntoView({{block:'center'}});
          const r = node.getBoundingClientRect();
          return {{x: r.left + r.width/2, y: r.top + r.height/2}};
        }})()""")
        if not box:
            raise RuntimeError(f"页面上找不到元素：{selector}")
        for kind in ("mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", type=kind, x=box["x"], y=box["y"],
                      button="left", clickCount=1)
        time.sleep(0.15)

    def type_text(self, selector, text):
        self.click(selector)
        self.eval(f"""(() => {{
          const node = document.querySelector({json.dumps(selector)});
          node.value = {json.dumps(text)};
          node.dispatchEvent(new Event('input', {{bubbles:true}}));
          node.dispatchEvent(new Event('change', {{bubbles:true}}));
        }})()""")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 断言辅助

def fail(process, message, page=None):
    print(f"[FAIL] {message}")
    if page is not None:
        try:
            print("   页面文本：", str(page.eval(
                "document.getElementById('content').innerText"))[:300].replace("\n", " | "))
            print("   弹窗文本：", str(page.eval(
                "document.getElementById('modalHost').innerText"))[:200].replace("\n", " | "))
        except Exception as exc:                                    # pragma: no cover
            print("   读页面失败：", exc)
    stop(process)
    raise SystemExit(1)


def query_creators(appdata):
    path = db_path(appdata)
    if not os.path.isfile(path):
        return []
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT handle, display_name, priority, poll_interval, category "
            "FROM creators ORDER BY created_at").fetchall()
    finally:
        connection.close()


def main():
    appdata = tempfile.mkdtemp(prefix="rc1-smoke-")
    print(f"临时应用数据目录：{appdata}")
    process = None
    page = None
    try:
        # ---- 第一次启动 --------------------------------------------------
        print("\n[1/6] 启动桌面程序…")
        process = launch(appdata)
        print("   WebView2:", wait_for_cdp().get("Browser"))
        target = app_target()
        print("   应用页面：", target["url"])
        page = Page(target["webSocketDebuggerUrl"])
        if not page.wait_for("!!document.querySelector('.nav-item')", timeout=40):
            fail(process, "导航栏没渲染出来", page)
        print("   窗口标题：", page.eval("document.title"))

        # ---- 创作者监控 → 添加创作者 → @nasa → 保存 --------------------
        print("\n[2/6] 创作者监控 → ＋ 添加创作者 → 输入 @nasa → 保存…")
        page.click('.nav-item[data-page="creators"]')
        if not page.wait_for("document.querySelector('#content h1').innerText.includes('创作者监控')"):
            fail(process, "没进到创作者监控页", page)
        page.click('[data-act="creator-add"]')
        if not page.wait_for("!!document.querySelector('#creatorHandle')"):
            fail(process, "「＋ 添加创作者」没有打开添加表单", page)
        page.type_text("#creatorHandle", "@nasa")
        page.type_text("#creatorName", "NASA")
        page.eval("""(() => {
          const p = document.querySelector('#creatorPriority'); p.value = '高';
          const i = document.querySelector('#creatorInterval'); i.value = '30 分钟';
        })()""")
        page.click('[data-act="creator-save"]')
        page.wait_for("!document.querySelector('#creatorHandle')", timeout=20)
        if page.eval("!!document.querySelector('#creatorHandle')"):
            fail(process, "保存后弹窗没关闭", page)
        if not page.wait_for("document.querySelector('#content').innerText.includes('@nasa')"):
            fail(process, "页面上没有出现 @nasa", page)
        toasts = page.eval("document.getElementById('toasts').innerText")
        print("   toast:", str(toasts).strip().replace("\n", " / "))
        if "已添加 @nasa" not in str(toasts):
            fail(process, "没有出现「已添加 @nasa」提示", page)
        print("   ✓ 页面上已出现 @nasa")

        # ---- 重复添加 ----------------------------------------------------
        print("\n[3/6] 重复添加 @nasa…")
        page.click('[data-act="creator-add"]')
        page.wait_for("!!document.querySelector('#creatorHandle')")
        page.type_text("#creatorHandle", "nasa")
        page.click('[data-act="creator-save"]')
        page.wait_for("!!document.querySelector('#creatorFormMsg').innerText.trim()")
        message = str(page.eval("document.getElementById('creatorFormMsg').innerText"))
        print("   弹窗提示：", message.strip().replace("\n", " "))
        if "已存在" not in message:
            fail(process, "重复添加没有提示「已存在」", page)
        if not page.eval("!!document.querySelector('#creatorHandle')"):
            fail(process, "重复添加时弹窗不该关闭", page)
        page.click('[data-act="close-modal"]')
        page.wait_for("!document.querySelector('#creatorHandle')")
        print("   ✓ 明确提示已存在，弹窗保留")

        # ---- 非法 handle --------------------------------------------------
        print("\n[4/6] 非法 handle…")
        page.click('[data-act="creator-add"]')
        page.wait_for("!!document.querySelector('#creatorHandle')")
        page.type_text("#creatorHandle", "bad handle!")
        page.click('[data-act="creator-save"]')
        page.wait_for("!!document.querySelector('#creatorFormMsg').innerText.trim()")
        message = str(page.eval("document.getElementById('creatorFormMsg').innerText"))
        print("   弹窗提示：", message.strip().replace("\n", " "))
        if "不合法" not in message and "非法" not in message:
            fail(process, "非法 handle 没有给出明确错误", page)
        page.click('[data-act="close-modal"]')
        page.wait_for("!document.querySelector('#creatorHandle')")
        print("   ✓ 非法 handle 被拒绝")

        shot = page.send("Page.captureScreenshot", format="png")
        with open(os.path.join(ROOT, "benchmark-results", "rc1-creator-smoke.png"), "wb") as handle:
            handle.write(base64.b64decode(shot["data"]))

        # ---- 落盘核对 ----------------------------------------------------
        print("\n[5/6] 关掉程序，直接查数据库…")
        page.close()
        page = None
        stop(process)
        process = None
        rows = query_creators(appdata)
        print("   creators 表：", rows)
        if len(rows) != 1 or rows[0][0] != "nasa":
            fail(None, f"creators 表内容不对（应恰好一条 nasa）：{rows}")
        print("   ✓ 库里恰好一条 nasa：", rows[0])

        # ---- 重启后再确认 ------------------------------------------------
        print("\n[6/6] 重新启动程序，确认 @nasa 还在…")
        process = launch(appdata)
        wait_for_cdp()
        target = app_target()
        page = Page(target["webSocketDebuggerUrl"])
        if not page.wait_for("!!document.querySelector('.nav-item')", timeout=40):
            fail(process, "重启后导航栏没渲染出来", page)
        page.click('.nav-item[data-page="creators"]')
        page.wait_for("document.querySelector('#content h1').innerText.includes('创作者监控')")
        content = str(page.eval("document.getElementById('content').innerText"))
        print("   页面文本片段：", content[:200].replace("\n", " | "))
        if "@nasa" not in content:
            fail(process, "重启后页面上没有 @nasa", page)
        print("   ✓ 重启后 @nasa 仍在")

        page.close()
        page = None
        stop(process)
        process = None
        print("\n[OK] 真机闭环通过：添加 → 落库 → 重启仍在")
        return 0
    finally:
        if page is not None:
            page.close()
        stop(process)
        shutil.rmtree(appdata, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
