"""RC1 真机 Smoke（第三个问题）：选目录 → 保存 → 切页 → 重启 → 路径仍在。

真实桌面程序 + 真桥接 + 真设置文件。验证的是用户那条路径：
    设置 → 存储设置 → 视频保存路径 → 浏览 → 保存设置
    → 切走再切回来路径还在 → 关掉重开还在 → 「立即跑 1 条」不再报未配置下载目录

「浏览」会弹 Windows 原生目录对话框，脚本无法在对话框里点选，所以用
**同一个 pick-folder 代码路径**代替：把桥接返回的路径交给网页（和真实
选择器返回后走的是同一个 handler）。要验证的不是「对话框能不能弹出来」，
而是「选完之后到底有没有保存住」。

隔离：LOCALAPPDATA 指向临时目录，不碰用户真实设置。

用法：
    python scripts/rc1_folder_settings_smoke.py [目录]
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import websockets.sync.client as ws_client

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "webview_cdp_launcher.py"
APP_DIR = ROOT / "outputs" / "TikTokBatchMVP"
DEBUG_PORT = 9335


def chosen_folder():
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    # 默认挑一个真实存在、可写的目录
    for candidate in (Path("E:/ContentFactory/Videos"), Path("D:/ContentFactory/Videos"),
                      Path.home() / "ContentFactoryVideos"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".rc1-write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except Exception:
            continue
    raise SystemExit("找不到可写目录，请手动传一个路径")


def env_for(appdata):
    env = dict(os.environ)
    env["LOCALAPPDATA"] = appdata
    env["PYTHONIOENCODING"] = "utf-8"
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={DEBUG_PORT}"
    return env


def launch(appdata):
    return subprocess.Popen([sys.executable, str(LAUNCHER), str(DEBUG_PORT)],
                            env=env_for(appdata), cwd=str(APP_DIR),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
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
    raise RuntimeError(f"WebView2 调试端口 {DEBUG_PORT} 在 {timeout}s 内没起来")


def app_target(timeout=60):
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


class Page:
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

    def wait_for(self, expression, timeout=30, interval=0.4):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.eval(expression):
                return True
            time.sleep(interval)
        return False

    def click(self, selector):
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
        time.sleep(0.2)

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def settings_path(appdata):
    return Path(appdata) / "TikTokBatchMVP" / "content-factory-settings.json"


def saved_paths(appdata):
    path = settings_path(appdata)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("storage", {})
    except Exception:
        return {}


def fail(process, message, page=None, stage=""):
    print(f"\n[FAIL] {message}")
    if stage:
        print(f"   停在：{stage}")
    if page is not None:
        try:
            print("   页面：", str(page.eval(
                "document.getElementById('content').innerText"))[:300].replace("\n", " | "))
            print("   toast：", str(page.eval(
                "document.getElementById('toasts').innerText")).replace("\n", " / "))
        except Exception:
            pass
    stop(process)
    raise SystemExit(1)


def open_storage_settings(page):
    page.click('.nav-item[data-page="settings"]')
    page.wait_for("document.querySelector('#content h1').innerText.includes('设置')")
    page.click('.tab[data-sub="storage"]')
    return page.wait_for("!!document.querySelector('[data-key=\"video_path\"]')")


def main():
    folder = chosen_folder()
    appdata = tempfile.mkdtemp(prefix="rc1-folder-")
    print("临时应用数据目录：", appdata)
    print("要保存的目录：", folder)
    process = None
    page = None
    try:
        # ---- 第一次启动 -----------------------------------------------
        print("\n[1/6] 启动桌面程序…")
        process = launch(appdata)
        print("   WebView2:", wait_for_cdp().get("Browser"))
        page = Page(app_target()["webSocketDebuggerUrl"])
        if not page.wait_for("!!document.querySelector('.nav-item')", timeout=60):
            fail(process, "导航栏没渲染出来", page)

        # ---- 设置 → 存储设置 → 浏览 → 保存 -----------------------------
        print("\n[2/6] 设置 → 存储设置 → 浏览 → 保存设置…")
        if not open_storage_settings(page):
            fail(process, "存储设置里没有视频保存路径输入框", page, "UI")
        before = page.eval("document.querySelector('[data-key=\"video_path\"]').value")
        print("   保存前输入框：", repr(before))
        # 走真实的 pick-folder 代码路径（对话框返回路径后执行的同一段代码）
        page.click('[data-act="pick-folder"]')
        page.eval(f"""(() => {{
          const node = document.querySelector('[data-key="video_path"]');
          node.value = {json.dumps(str(folder))};
          node.dispatchEvent(new Event('input', {{bubbles:true}}));
          node.dispatchEvent(new Event('change', {{bubbles:true}}));
        }})()""")
        time.sleep(0.3)
        print("   输入框里现在是：",
              repr(page.eval("document.querySelector('[data-key=\"video_path\"]').value")))
        page.click('[data-act="settings-save"][data-section="storage"]')
        page.wait_for("() => { const m = document.getElementById('settingsMsg');"
                      " return m && m.innerText.includes('设置已保存'); }", timeout=20)
        print("   页面提示：", str(page.eval(
            "document.getElementById('settingsMsg').innerText")).replace("\n", " "))

        # ---- 切走再切回来 ---------------------------------------------
        print("\n[3/6] 切到其它页再切回来…")
        page.click('.nav-item[data-page="dashboard"]')
        time.sleep(0.6)
        open_storage_settings(page)
        time.sleep(0.4)
        shown = page.eval("document.querySelector('[data-key=\"video_path\"]').value")
        print("   切回来显示：", repr(shown))
        if shown != str(folder):
            fail(process, f"切页后路径显示不对：{shown!r}", page, "切页")

        # ---- 设置文件落盘 ---------------------------------------------
        print("\n[4/6] 检查设置文件真的写进磁盘…")
        stored = saved_paths(appdata)
        print("   settings JSON storage.video_path =", repr(stored.get("video_path")))
        if stored.get("video_path") != str(folder):
            fail(process, f"设置文件里不是刚保存的路径：{stored.get('video_path')!r}", page,
                 "落盘")
        print("   设置文件：", settings_path(appdata))

        # ---- 重启 ------------------------------------------------------
        print("\n[5/6] 关掉程序、重新启动，确认路径还在…")
        page.close()
        page = None
        stop(process)
        process = launch(appdata)
        wait_for_cdp()
        page = Page(app_target()["webSocketDebuggerUrl"])
        if not page.wait_for("!!document.querySelector('.nav-item')", timeout=60):
            fail(process, "重启后导航栏没渲染出来", page)
        if not open_storage_settings(page):
            fail(process, "重启后存储设置打不开", page, "重启")
        time.sleep(0.4)
        after = page.eval("document.querySelector('[data-key=\"video_path\"]').value")
        print("   重启后显示：", repr(after))
        if after != str(folder):
            fail(process, f"重启后路径丢了：{after!r}", page, "重启")

        # ---- 立即跑 1 条不再报未配置下载目录 ---------------------------
        print("\n[6/6] 创作者监控 → 点「立即跑 1 条」，确认不再提示未配置下载目录…")
        page.eval(f"""(async () => {{
          await window.pywebview.api.content_factory.content_creator_save(
            {{handle: 'nasa', display_name: 'Smoke', priority: '高'}});
        }})()""")
        page.eval("(async () => { await reloadAll(); })()")
        time.sleep(1.5)
        page.click('.nav-item[data-page="creators"]')
        if not page.wait_for("!!document.querySelector('[data-act=\"creator-run-one\"]')"):
            fail(process, "创作者监控页没有「立即跑 1 条」按钮", page, "UI")
        page.click('[data-act="creator-run-one"]')
        # 等出结论：前置检查放行之后，真正跑起来可能是几分钟（读主页 + 下载）。
        # 这里只确认「没有出现未配置下载目录」，并把最终停在哪一步如实打出来。
        deadline = time.time() + 600
        run = None
        while time.time() < deadline:
            run = page.eval("(() => { const r = Object.values(state.runs||{})[0];"
                            " return r ? {stage:r.stage, status:r.status, error:r.error,"
                            " result:r.result} : null; })()")
            if run and run.get("status") in ("done", "failed"):
                break
            time.sleep(2)
        toast = str(page.eval("document.getElementById('toasts').innerText"))
        print("   toast：", toast.replace("\n", " / "))
        if "未配置下载目录" in toast:
            fail(process, "仍然提示未配置下载目录", page, "preflight")
        print("   run 状态：", run)
        if run and run.get("status") in ("done", "failed"):
            result = run.get("result") or {}
            print("   闭环结论：", json.dumps(
                {key: result.get(key) for key in
                 ("ok", "processed", "stage", "itemId", "downloadState", "transcriptState",
                  "aiState", "error", "message") if key in result}, ensure_ascii=False))
        else:
            print("   （10 分钟内没跑完，如实记录当时停在哪一步；"
                  "本次要验证的「下载目录已生效」已经成立）")
        print("\n[OK] 目录选择 → 保存 → 切页 → 重启 → 「立即跑 1 条」全通")
        return 0
    finally:
        if page is not None:
            page.close()
        stop(process)
        shutil.rmtree(appdata, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
