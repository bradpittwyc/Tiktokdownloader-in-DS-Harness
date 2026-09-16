"""RC1 真机 Smoke Test（第二轮）：点「立即跑 1 条」，把一条新内容真的跑到底。

跑的是真实桌面程序（真 pywebview 窗口 + 真桥接 + 真 sqlite + 真 TikTok 下载器）：

    Creator Monitor → [立即跑 1 条] → 读取创作者主页 → 下载 1 条 → 文件落盘
      → 自动入内容库 → 读字幕（没有则本机 ASR）→ AI 标注 → AI 加工页能看到结果

隔离：LOCALAPPDATA 指向临时目录，不碰用户本机的内容库。
下载目录也指向临时目录，验证「文件真的落盘」时不会污染用户磁盘。

真实 TikTok 可能因为风控 / 安全验证失败 —— 那种情况**如实报告停在哪一步**，
不伪造成功。

用法：
    python scripts/rc1_run_one_smoke.py [handle]      # 默认 nasa
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
from pathlib import Path
from urllib.parse import urlparse

import websockets.sync.client as ws_client

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "webview_cdp_launcher.py"
APP_DIR = ROOT / "outputs" / "TikTokBatchMVP"
DEBUG_PORT = 9334
TIMEOUT_SECONDS = 900


# ---------------------------------------------------------------- 进程

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


# ---------------------------------------------------------------- CDP

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

    def type_text(self, selector, text):
        self.click(selector)
        self.eval(f"""(() => {{
          const node = document.querySelector({json.dumps(selector)});
          node.value = {json.dumps(text)};
          node.dispatchEvent(new Event('input', {{bubbles:true}}));
          node.dispatchEvent(new Event('change', {{bubbles:true}}));
        }})()""")

    def screenshot(self, path):
        shot = self.send("Page.captureScreenshot", format="png")
        Path(path).write_bytes(base64.b64decode(shot["data"]))

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def fail(process, message, page=None, stage=""):
    print(f"\n[FAIL] {message}")
    if stage:
        print(f"   停在：{stage}")
    if page is not None:
        for selector, label in (("#content", "页面"), ("#toasts", "toast")):
            try:
                text = str(page.eval(f"document.querySelector('{selector}').innerText"))
                print(f"   {label}：", text[:400].replace("\n", " | "))
            except Exception:
                pass
    stop(process)
    raise SystemExit(1)


# ---------------------------------------------------------------- 数据

def db_path(appdata):
    return Path(appdata) / "TikTokBatchMVP" / "content-factory.db"


def query(appdata, sql, params=()):
    path = db_path(appdata)
    if not path.is_file():
        return []
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def real_api_key():
    """拿本机已有的 AI 凭据，让真机 smoke 能真的跑完 AI 这一段。

    顺序：环境变量 -> 用户本机 learning.json（应用「设置 → AI 加工设置」里的
    「导入已有配置」读的就是它）。只做一次单条视频的标注调用；拿不到就如实
    报告停在 AI 前置检查，不伪造成功。
    """
    key = str(os.environ.get("CONTENT_FACTORY_SMOKE_API_KEY") or "").strip()
    if key:
        return key, "环境变量 CONTENT_FACTORY_SMOKE_API_KEY"
    path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "learning.json"
    if not path.is_file():
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "", ""
    key = str(data.get("api_key") or "").strip()
    return (key, "本机 learning.json") if key else ("", "")


def main():
    handle = (sys.argv[1] if len(sys.argv) > 1 else "nasa").lstrip("@")
    appdata = tempfile.mkdtemp(prefix="rc1-runone-")
    download_dir = Path(appdata) / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    print(f"临时应用数据目录：{appdata}")
    print(f"临时下载目录：{download_dir}")
    print(f"测试 Creator：@{handle}")
    process = None
    page = None
    try:
        # ---- 启动 + 准备设置 --------------------------------------------
        print("\n[1/7] 启动桌面程序…")
        process = launch(appdata)
        print("   WebView2:", wait_for_cdp().get("Browser"))
        page = Page(app_target()["webSocketDebuggerUrl"])
        if not page.wait_for("!!document.querySelector('.nav-item')", timeout=60):
            fail(process, "导航栏没渲染出来", page)
        print("   窗口标题：", page.eval("document.title"))

        print("\n[2/7] 配置下载目录 + AI Key + 添加 Creator…")
        key, key_from = real_api_key()
        print("   AI Key：", f"已找到（{key_from}）" if key else "未找到（AI 阶段会明确失败）")
        ai_section = {"api_key": key} if key else {}
        if key:
            ai_section.update({"model": "deepseek-chat", "api_base": "https://api.deepseek.com/v1"})
        # 走真实桥接写设置（设置 → 存储设置 / AI 加工设置）
        setup = page.eval(f"""(async () => {{
          const api = window.pywebview.api.content_factory;
          const saved = await api.content_save_settings('storage', {{
            video_path: {json.dumps(str(download_dir))} }});
          if ({json.dumps(bool(key))}) {{
            await api.content_save_settings('ai', {json.dumps(ai_section)});
          }}
          const made = await api.content_creator_save({{
            handle: {json.dumps(handle)}, display_name: 'Smoke Test',
            priority: '高', poll_interval: '30 分钟' }});
          const now = await api.content_settings();
          return {{saved: saved && saved.ok, made: made, folder: saved && saved.settings
                   && saved.settings.storage && saved.settings.storage.video_path,
                   aiKeySet: !!(now && now.ai && now.ai.apiKeySet)}};
        }})()""")
        print("   设置已保存：", setup.get("saved"), " 下载目录：", setup.get("folder"),
              " AI Key 已配：", setup.get("aiKeySet"))
        if not setup.get("saved"):
            fail(process, "下载目录没保存成功", page)
        made = setup.get("made") or {}
        if not made.get("ok") and not made.get("duplicate"):
            fail(process, f"添加 Creator 失败：{made.get('error')}", page)
        print("   Creator：", "新建" if made.get("ok") else "已存在（复用）")
        # 设置和 Creator 是直接走桥接写的，页面上的 state 还是旧的 —— 刷一次，
        # 后面才是「用户真的看到并点了按钮」。
        page.eval("(async () => { await reloadAll(); })()")
        time.sleep(1.5)
        print("   页面已刷新，创作者数：",
              page.eval("(state.creators || []).length"))

        # ---- 点真实按钮 --------------------------------------------------
        print("\n[3/7] 创作者监控 → 点击「立即跑 1 条」…")
        page.click('.nav-item[data-page="creators"]')
        if not page.wait_for(
                "document.querySelector('#content h1').innerText.includes('创作者监控')"):
            fail(process, "没进到创作者监控页", page)
        if not page.wait_for(f"""!!document.querySelector(
                '[data-act="creator-run-one"]')"""):
            fail(process, "没有「立即跑 1 条」按钮", page, "UI")
        page.click('[data-act="creator-run-one"]')
        time.sleep(1.0)
        started = page.eval("JSON.stringify(Object.values(state.runs || {})[0] || null)")
        print("   按钮已点击，run 状态：", str(started)[:200])

        # ---- 等它跑完（状态是后端真的写出来的）----------------------------
        print("\n[4/7] 等待闭环跑完（发现 → 下载 → 字幕/ASR → AI）…")
        deadline = time.time() + TIMEOUT_SECONDS
        last_stage = ""
        while time.time() < deadline:
            run = page.eval("(() => { const r = Object.values(state.runs||{})[0];"
                            " return r ? {stage:r.stage, status:r.status,"
                            " label:r.stageLabel, steps:(r.steps||[]).map(s=>s.stage),"
                            " error:r.error, result:r.result} : null; })()")
            if run and run.get("stage") != last_stage:
                last_stage = run.get("stage")
                print(f"   → {last_stage} ({run.get('label')})")
            if run and run.get("status") in ("done", "failed"):
                break
            time.sleep(2)
        else:
            fail(process, f"{TIMEOUT_SECONDS}s 内没有结束", page, last_stage or "未知")

        run = page.eval("(() => { const r = Object.values(state.runs||{})[0];"
                        " return r ? {stage:r.stage, status:r.status, error:r.error,"
                        " result:r.result} : null; })()")
        print("   步骤轨迹：", run.get("result", {}).get("steps") and
              [s.get("stage") for s in run["result"]["steps"]] or
              page.eval("JSON.stringify((Object.values(state.runs||{})[0]||{}).steps||[])"))
        result = (run or {}).get("result") or {}
        print("   结果：", json.dumps({key: result.get(key) for key in
                                       ("ok", "processed", "stage", "itemId", "sourceVideoId",
                                        "downloadState", "transcriptState", "aiState",
                                        "needsApiKey", "needsAsr", "needsVerification",
                                        "error", "message") if key in result},
                                      ensure_ascii=False))

        # ---- 核对落盘与入库 ----------------------------------------------
        print("\n[5/7] 核对文件落盘 / 内容库 / 状态…")
        files = sorted(str(path) for path in download_dir.rglob("*") if path.is_file())
        print("   下载目录里的文件：", files)
        items = query(appdata, "SELECT id, source_video_id, download_status, transcript_status,"
                               " ai_status, local_video_path, local_subtitle_path FROM content_items")
        for row in items:
            print("   内容库：", row)
        media_exists = any(str(row[5]) and Path(str(row[5])).is_file() for row in items)
        # 没有视频也可能是仅图片作品（photo post），那种情况下载器会写 jpg
        has_media = media_exists or any(path.endswith((".mp4", ".webm", ".jpg", ".jpeg", ".png"))
                                       for path in files)
        print("   媒体文件真的在磁盘上：", has_media)
        jobs = query(appdata, "SELECT source_video_id, state, content_item_id, last_error "
                              "FROM collection_jobs")
        print("   采集任务：", jobs)

        page.screenshot(ROOT / "benchmark-results" / "rc1-run-one-smoke.png")

        # ---- AI 加工页能看到结果 ------------------------------------------
        item_id = result.get("itemId") or (items[0][0] if items else "")
        if result.get("aiState") == "done" and item_id:
            print("\n[6/7] 打开「AI 加工」页确认能看到标注结果…")
            page.click('.nav-item[data-page="ai"]')
            page.wait_for("document.querySelector('#content h1').innerText.includes('AI 加工')")
            page.eval(f"(() => {{ state.ai.selected = {json.dumps(item_id)}; "
                      "state.ai.status='all'; renderPage(); })()")
            time.sleep(0.8)
            content = str(page.eval("document.getElementById('content').innerText"))
            markers = [marker for marker in ("内容分析结果", "主题", "难度", "重点句", "推荐任务")
                       if marker in content]
            print("   AI 加工页命中：", markers)
            page.screenshot(ROOT / "benchmark-results" / "rc1-run-one-ai.png")
            if not markers:
                fail(process, "AI 加工页看不到标注结果", page, "AI 加工页")
        else:
            print("\n[6/7] AI 阶段没跑完（如实报告，不伪造）：",
                  {key: result.get(key) for key in
                   ("stage", "aiState", "transcriptState", "error", "needsApiKey", "needsAsr")})

        # ---- 结论 ---------------------------------------------------------
        print("\n[7/7] 结论")
        stage = result.get("stage") or last_stage
        if result.get("ok") and result.get("processed") == 1 and result.get("aiState") == "done":
            print("   [OK] 单条闭环真的跑完了：下载 → 落盘 → 入库 → 转写 → AI")
            if not has_media:
                fail(process, "AI 成功了但没找到下载文件，落盘这一步存疑", page, stage)
            return 0
        if result.get("ok") and result.get("processed") == 1:
            print(f"   [PARTIAL] 下载与入库成功，但下游没走完：stage={stage} "
                  f"transcript={result.get('transcriptState')} ai={result.get('aiState')}")
            print(f"   原因：{result.get('error') or result.get('message')}")
            return 2
        if result.get("ok") and result.get("processed") == 0:
            print(f"   [NO-NEW-CONTENT] 没有发现新内容：{result.get('message')}")
            return 3
        print(f"   [FAIL] 停在 {stage}：{result.get('error') or (run or {}).get('error')}")
        return 1
    finally:
        if page is not None:
            page.close()
        stop(process)
        shutil.rmtree(appdata, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
