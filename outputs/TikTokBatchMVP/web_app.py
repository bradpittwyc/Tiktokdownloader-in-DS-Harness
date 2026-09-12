import json
import http.cookiejar
import csv
import os
import re
import sys
import threading
import time
import msvcrt
import shutil
import base64
import subprocess
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import webview
import yt_dlp
import curl_cffi
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from yt_dlp.networking.impersonate import ImpersonateTarget
from playwright.sync_api import sync_playwright

from app import clean_profile_url, find_chrome
from session_store import SessionStore, tiktok_cookies, has_session, chrome_app_bound


BASE = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent


class Api:
    def __init__(self):
        self._window = None
        self._scraper = None
        self._profile_avatar = ""
        self._profile_stats = {}
        self._pause_downloads = threading.Event()
        self._cancel_downloads = threading.Event()
        self._cookie_file = ""
        self._cookie_browser = ""
        self._cookie_error = ""
        self._cookie_code = ""
        self._cookie_snapshot = []
        self._cookie_loaded = False
        self._cookie_lock = threading.RLock()
        self._login_lock = threading.Lock()
        self._profile_lock = threading.Lock()
        self._verification_username = ""
        self._verification_status = {"busy": False}
        self._verification_started = threading.Event()
        self._collection_warning = ""
        self._collection_complete = False
        self._collection_needs_verification = False
        self._login_cancel = threading.Event()
        self._login_busy = False
        self._login_process = None
        self._session_store = SessionStore()
        self._session_user_agent = ""
        self._filename_template = self._load_filename_template()
        self._learning = self._load_learning_options()

    def _load_filename_template(self):
        path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "filename-template.txt"
        try:
            return path.read_text(encoding="utf-8").strip() or "%(title).150B_%(upload_date)s"
        except Exception:
            return "%(title).150B_%(upload_date)s"

    def get_filename_template(self):
        return self._filename_template

    def set_filename_template(self, value):
        value = str(value or "%(title).150B_%(upload_date)s").strip().replace("{title}", "%(title)s").replace("{date}", "%(upload_date)s")
        if "%(title)" not in value:
            value = "%(title).150B_" + value
        self._filename_template = value[:220]
        path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "filename-template.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._filename_template, encoding="utf-8")
        return {"ok": True, "template": self._filename_template}

    def _learning_file(self):
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP"
        root.mkdir(parents=True, exist_ok=True)
        return root / "learning.json"

    def _load_learning_options(self):
        defaults = {"enabled": False, "translation": True, "vocabulary": True,
                    "timestamps": True, "api_base": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini", "api_key": ""}
        try:
            saved = json.loads(self._learning_file().read_text(encoding="utf-8"))
            if isinstance(saved, dict): defaults.update(saved)
        except Exception:
            pass
        return defaults

    def get_learning_options(self):
        value = dict(self._learning)
        value["api_key"] = ""
        value["apiKeySet"] = bool(self._learning.get("api_key"))
        return value

    def set_learning_options(self, options):
        options = options or {}
        current = self._learning
        key = str(options.get("api_key") or "").strip()
        if key:
            current["api_key"] = key
        current.update({
            "enabled": bool(options.get("enabled")),
            "translation": bool(options.get("translation", True)),
            "vocabulary": bool(options.get("vocabulary", True)),
            "timestamps": bool(options.get("timestamps", True)),
            "api_base": str(options.get("api_base") or "https://api.openai.com/v1").strip().rstrip("/"),
            "model": str(options.get("model") or "gpt-4o-mini").strip(),
        })
        self._learning_file().write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        return self.get_learning_options()

    def test_learning_api(self, options):
        self.set_learning_options(options)
        if not self._learning.get("api_key"):
            return {"ok": False, "error": "请先填写 API Key"}
        try:
            text = self._call_learning_model("Reply with OK", max_tokens=8)
            return {"ok": True, "message": text[:80]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _call_learning_model(self, prompt, max_tokens=6000):
        base = self._learning["api_base"].rstrip("/")
        endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        response = requests.post(endpoint, headers={"Authorization": f"Bearer {self._learning['api_key']}",
            "Content-Type": "application/json"}, json={"model": self._learning["model"], "temperature": .25,
            "max_tokens": max_tokens, "messages": [{"role": "system", "content": "You are a precise English learning editor."},
            {"role": "user", "content": prompt}]}, timeout=180)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()

    @staticmethod
    def _subtitle_text(paths):
        candidates = [Path(x) for x in paths if Path(x).suffix.lower() in {".srt", ".vtt"}]
        candidates.sort(key=lambda p: (".en" not in p.name.lower(), p.suffix.lower() != ".srt"))
        if not candidates: return ""
        raw = candidates[0].read_text(encoding="utf-8", errors="ignore")
        lines = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.isdigit() or "-->" in line or line.startswith(("WEBVTT", "NOTE")): continue
            line = re.sub(r"<[^>]+>", "", line)
            if not lines or lines[-1] != line: lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _write_learning_docx(content, output, item):
        """Turn the model response into a Chinese and English formatted Word study sheet."""
        document = Document()
        section = document.sections[0]
        section.top_margin = section.bottom_margin = Pt(50)
        section.left_margin = section.right_margin = Pt(54)
        styles = document.styles
        styles["Normal"].font.name = "Times New Roman"
        styles["Normal"]._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        styles["Normal"].font.size = Pt(10.5)
        lines = content.replace("\r\n", "\n").split("\n")
        generated_title = next((line[2:].strip() for line in lines if line.startswith("# ")), "")
        title = document.add_paragraph(style="Title")
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = title.add_run(generated_title or item.get("title") or "TikTok English Study Notes")
        run.bold = True
        run.font.name = "Times New Roman"
        run.font.color.rgb = RGBColor(0, 0, 0)
        subtitle = document.add_paragraph()
        subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
        subtitle.add_run("TikTok English Learning Notes").italic = True
        subtitle.runs[0].font.name = "Times New Roman"
        subtitle.runs[0].font.color.rgb = RGBColor(90, 100, 115)
        def add_mixed(paragraph, text, bold=False):
            for part in re.findall(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+|[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+", text):
                run = paragraph.add_run(part)
                run.bold = bold
                if re.search(r"[\u4e00-\u9fff]", part):
                    run.font.name = "Microsoft YaHei"
                    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
                else:
                    run.font.name = "Times New Roman"

        def finish_paragraph(text, style=None):
            paragraph = document.add_paragraph(style=style) if style else document.add_paragraph()
            for part in re.split(r"(\*\*[^*]+\*\*)", text):
                add_mixed(paragraph, part.strip("*"), part.startswith("**") and part.endswith("**"))
            paragraph.paragraph_format.space_after = Pt(5)
            paragraph.paragraph_format.line_spacing = 1.28

        flowing_section, flowing = "", []
        def flush_flowing():
            nonlocal flowing
            if flowing:
                separator = "" if flowing_section == "中文对照" else " "
                finish_paragraph(separator.join(flowing))
                flowing = []

        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("# "):
                continue
            if line.startswith("## ") or line.startswith("### "):
                flush_flowing()
                heading = line[3:] if line.startswith("## ") else line[4:]
                paragraph = document.add_paragraph(style="Heading 2")
                add_mixed(paragraph, heading, True)
                color = RGBColor(227, 108, 10) if "视频主题" in heading else RGBColor(148, 54, 52)
                for run in paragraph.runs: run.font.color.rgb = color
                flowing_section = heading
                continue
            if flowing_section in {"英文原文", "中文对照"}:
                flowing.append(line)
                continue
            if line.startswith("### "):
                paragraph = document.add_paragraph(line[4:], style="Heading 3")
            elif line.startswith(("- ", "* ", "• ")):
                paragraph = document.add_paragraph(style="List Bullet")
                add_mixed(paragraph, line[2:])
            elif re.match(r"^\d+[.)]\s+", line):
                paragraph = document.add_paragraph(style="List Number")
                add_mixed(paragraph, re.sub(r"^\d+[.)]\s+", "", line))
            else:
                finish_paragraph(line)
                continue
            paragraph.paragraph_format.space_after = Pt(5)
            paragraph.paragraph_format.line_spacing = 1.28
        flush_flowing()
        document.save(output)

    def _generate_learning_document(self, item, target, subtitle_paths):
        try:
            text = self._subtitle_text(subtitle_paths)
            if not text:
                self._emit("learningProgress", {"id": item["id"], "state": "skipped", "message": "未找到可读的 SRT/VTT 字幕"})
                return
            self._emit("learningProgress", {"id": item["id"], "state": "working", "message": "正在生成英文学习文档…"})
            opts = self._learning
            sections = ["保留完整英文原文", "给出自然中文对照" if opts["translation"] else "不需要中文翻译",
                        "提炼重点词汇、音标/词性、中文义和例句" if opts["vocabulary"] else "不需要词汇表",
                        "讲解高价值句型、固定搭配和可模仿表达"]
            if opts["timestamps"]: sections.append("按内容段落标出对应时间码；字幕中没有时间码时按段落编号")
            prompt = "请基于以下 TikTok 英文字幕生成一份适合中国学习者的 Markdown 学习文档。" + "；".join(sections) + "。第一行必须是 # 后跟完整、自然、能概括视频主题的英文标题；自己根据字幕判断标题，不能截断，也不要复述文件名。后续严格使用：## 视频主题、## 英文原文、## 中文对照、## 重点词汇、## 句型与表达、## 跟读练习。英文原文和中文对照均按自然段连续书写，绝对不要按字幕逐行换行；只有跟读练习按短句逐行排列。不要编造字幕里不存在的内容。\n\n字幕：\n" + text[:30000]
            document = self._call_learning_model(prompt)
            generated_title = next((line[2:].strip() for line in document.splitlines() if line.startswith("# ")), item.get("title") or "英文学习")
            title = re.sub(r'[<>:"/\\|?*]+', '_', generated_title).strip(" .")[:140] or "英文学习"
            stamp = item.get("upload_date") or time.strftime("%Y%m%d")
            media_files, _ = self._files_for_item(Path(target), item["id"], item)
            base = media_files[0].stem if media_files else f"{title}_{stamp}"
            output = Path(target) / f"{base}.docx"
            self._write_learning_docx(document, output, item)
            self._emit("learningProgress", {"id": item["id"], "state": "done", "path": str(output), "message": "英文学习文档已保存"})
        except Exception as exc:
            self._emit("learningProgress", {"id": item["id"], "state": "failed", "message": str(exc)})

    def _queue_learning_document(self, item, target, subtitle_paths):
        if self._learning.get("enabled") and self._learning.get("api_key") and subtitle_paths:
            threading.Thread(target=self._generate_learning_document, args=(item, target, subtitle_paths), daemon=True).start()

    def set_cookie_options(self, cookie_file="", browser=""):
        with self._cookie_lock:
            self._cookie_file = str(cookie_file or "").strip()
            self._cookie_browser = str(browser or "").lower().strip()
            if self._cookie_browser not in {"", "chrome", "edge", "saved"}:
                self._cookie_browser = ""
            self._cookie_loaded = False
            self._session_user_agent = ""
            self._playwright_cookies()
            return self.get_cookie_status()

    def get_cookie_status(self):
        with self._cookie_lock:
            cookies = tiktok_cookies(self._cookie_snapshot)
            requested = bool(self._cookie_file or self._cookie_browser)
            return {"ok": not requested or (has_session(cookies) and not self._cookie_error),
                    "source": "cookies.txt" if self._cookie_file else self._cookie_source_label(),
                    "browser": self._cookie_browser, "count": len(cookies),
                    "hasSession": has_session(cookies), "error": self._cookie_error,
                    "code": self._cookie_code, "busy": self._login_busy}

    def _cookie_source_label(self):
        if self._cookie_browser == "chrome":
            return "Chrome Cookie"
        if self._cookie_browser == "saved":
            return "软件保存的 TikTok 登录态"
        return self._cookie_browser or "未使用"

    def login_tiktok(self):
        if not self._login_lock.acquire(blocking=False):
            return {"ok": True, "busy": True}
        if not self._profile_lock.acquire(blocking=False):
            self._login_lock.release()
            return {"ok": False, "busy": False, "error": "正在读取博主，请等本次读取结束后更新登录态"}
        self._login_busy = True
        self._login_cancel.clear()
        threading.Thread(target=self._login_tiktok_worker, daemon=True).start()
        return {"ok": True, "busy": True}

    def verify_profile(self, username):
        username = str(username or "").lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", username) or username != self._verification_username:
            return {"ok": False, "error": "请从当前抓取结果中的验证按钮打开"}
        if not self._login_lock.acquire(blocking=False):
            return {"ok": False, "error": "登录或验证窗口已经打开，请先完成或取消"}
        if not self._profile_lock.acquire(blocking=False):
            self._login_lock.release()
            return {"ok": False, "error": "正在读取主页，请稍候重试"}
        self._login_busy = True
        self._login_cancel.clear()
        self._verification_started.clear()
        self._verification_status = {"busy": True, "username": username,
                                     "message": "请完成验证并保持窗口打开，软件会在同一窗口继续抓取"}
        try:
            login_profile = self._login_profile_dir()
            login_profile.mkdir(parents=True, exist_ok=True)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                debug_port = sock.getsockname()[1]
            command = [find_chrome(), f"--user-data-dir={login_profile}", "--profile-directory=Default",
                       f"--remote-debugging-port={debug_port}", "--remote-debugging-address=127.0.0.1",
                       "--no-first-run", "--no-default-browser-check", "--new-window",
                       f"https://www.tiktok.com/@{username}"]
            self._login_process = subprocess.Popen(command)
        except Exception as exc:
            self._login_busy = False
            self._profile_lock.release()
            self._login_lock.release()
            return {"ok": False, "busy": False, "error": f"无法启动 Chrome：{exc}"}
        threading.Thread(target=self._verify_profile_worker, args=(username, debug_port), daemon=True).start()
        if not self._verification_started.wait(20):
            self._login_cancel.set()
            if self._login_process and self._login_process.poll() is None:
                self._login_process.terminate()
            return {"ok": False, "busy": False, "error": "20 秒内未能启动 Chrome 验证窗口，请重试"}
        state = self.get_verification_status()
        if state.get("stage") == "failed":
            return {"ok": False, "busy": False, "error": state.get("error") or "Chrome 验证窗口启动失败"}
        return {"ok": True, "busy": True, "stage": state.get("stage")}

    def get_verification_status(self):
        return dict(self._verification_status)

    def _verify_profile_worker(self, username, debug_port):
        """Keep the challenged page visible and continue collecting in that session."""
        error = ""
        result = None
        try:
            # Do not call evaluate_js here. This worker can start before the
            # verify_profile bridge call has returned; a synchronous callback
            # at that point deadlocks pywebview and Chrome is never launched.
            self._verification_status.update(stage="starting", message="正在打开 TikTok 验证窗口…")
            videos = self._collect_videos(f"https://www.tiktok.com/@{username}", interactive=True,
                                          lock_held=True, cdp_url=f"http://127.0.0.1:{debug_port}")
            result = self._store_profile_archive(username, videos, self._collection_complete)
        except Exception as exc:
            error = str(exc)
        finally:
            self._login_busy = False
            self._login_process = None
            self._profile_lock.release()
            self._login_lock.release()
            self._verification_status = {"busy": False, "username": username,
                                         "ready": bool(result), "error": error, "result": result,
                                         "stage": "finished" if result else "failed"}
            self._verification_started.set()
            self._emit("cookieStatus", self.get_cookie_status())
            self._emit("verificationStatus", self._verification_status)

    def cancel_tiktok_login(self):
        self._login_cancel.set()
        process = self._login_process
        if process and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
        return {"ok": True}

    def _login_tiktok_worker(self, verification_username=""):
        error = ""
        saved = False
        login_profile = self._login_profile_dir()
        target_url = f"https://www.tiktok.com/@{verification_username}" if verification_username else "https://www.tiktok.com/login"
        try:
            login_profile.mkdir(parents=True, exist_ok=True)
            command = [find_chrome(), f"--user-data-dir={login_profile}", "--profile-directory=Default",
                       "--no-first-run", "--no-default-browser-check", "--new-window",
                       "--disable-background-mode", target_url]
            # Google blocks sign-in in Playwright-controlled Chrome. Login is
            # performed in ordinary Chrome; once it closes, Chrome is reopened
            # headlessly with the same app-owned profile to export TikTok cookies.
            self._login_process = subprocess.Popen(command)
            self._emit("cookieStatus", {**self.get_cookie_status(), "busy": True,
                                       "error": "请在打开的博主主页手动完成验证，然后关闭该窗口" if verification_username else "请在普通 Chrome 中登录 TikTok；完成后关闭该窗口，软件会自动保存"})
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline and not self._login_cancel.is_set():
                if self._login_process.poll() is not None:
                    break
                time.sleep(.5)
            if self._login_cancel.is_set():
                error = "已取消登录，原登录设置保持不变"
            elif self._login_process.poll() is None:
                error = "等待登录超时，请关闭登录窗口后重试"
            else:
                time.sleep(1)
                with sync_playwright() as playwright:
                    context = playwright.chromium.launch_persistent_context(
                        str(login_profile), executable_path=find_chrome(), headless=True)
                    try:
                        page = context.pages[0] if context.pages else context.new_page()
                        cookies = tiktok_cookies(context.cookies())
                        if not has_session(cookies):
                            error = "没有检测到 TikTok 登录状态，请确认登录成功后再关闭窗口"
                        else:
                            user_agent = page.evaluate("navigator.userAgent")
                            with self._cookie_lock:
                                self._session_store.save(cookies, user_agent)
                                self._cookie_file = ""
                                self._cookie_browser = "saved"
                                self._cookie_snapshot = cookies
                                self._cookie_loaded = True
                                self._session_user_agent = user_agent
                                self._cookie_error = self._cookie_code = ""
                            saved = True
                    finally:
                        context.close()
        except Exception:
            error = "无法保存 TikTok 登录状态，请关闭登录窗口后重试；原设置保持不变"
        finally:
            self._login_process = None
            self._login_busy = False
            self._profile_lock.release()
            self._login_lock.release()
            status = self.get_cookie_status()
            status.update(saved=saved)
            if error:
                status.update(ok=False, error=error)
            self._emit("cookieStatus", status)
            if verification_username:
                self._emit("verificationStatus", {"username": verification_username, "ready": saved,
                                                   "error": error})

    def _chrome_profiles(self):
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
        try:
            ordered = []
            state = json.loads((root / "Local State").read_text(encoding="utf-8"))
            for name in state.get("profile", {}).get("last_active_profiles", []):
                if (root / name / "Network" / "Cookies").exists():
                    ordered.append(name)
            for folder in root.iterdir():
                if not folder.is_dir() or not (folder.name == "Default" or folder.name.startswith("Profile ")):
                    continue
                cookie_db = folder / "Network" / "Cookies"
                if cookie_db.exists() and folder.name not in ordered:
                    ordered.append(folder.name)
            return ordered
        except Exception:
            return []

    def choose_cookie_file(self):
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Cookie 文件 (*.txt;*.cookies)", "所有文件 (*.*)"),
        )
        return result[0] if result else None

    def _playwright_cookies(self):
        with self._cookie_lock:
            if self._cookie_loaded:
                self._cookie_snapshot = tiktok_cookies(self._cookie_snapshot)
                if (self._cookie_file or self._cookie_browser) and not has_session(self._cookie_snapshot) and not self._cookie_error:
                    self._cookie_error = "登录 Cookie 已过期或缺失，请更新 TikTok 登录态"
                    self._cookie_code = "no_session"
                return list(self._cookie_snapshot)
            self._cookie_error = self._cookie_code = ""
            self._cookie_snapshot = []
            self._cookie_loaded = True
            try:
                if self._cookie_file:
                    if not Path(self._cookie_file).is_file():
                        raise RuntimeError("找不到 cookies.txt 文件")
                    jar = http.cookiejar.MozillaCookieJar(self._cookie_file)
                    jar.load(ignore_discard=True, ignore_expires=False)
                    cookies = self._cookies_from_jar(jar)
                elif self._cookie_browser == "saved":
                    payload = self._session_store.load()
                    cookies = payload["cookies"]
                    self._session_user_agent = payload.get("user_agent", "")
                elif self._cookie_browser:
                    from yt_dlp.cookies import extract_cookies_from_browser
                    profiles = self._chrome_profiles() if self._cookie_browser == "chrome" else [None]
                    cookies = []
                    blocked_encryption = False
                    class QuietLogger:
                        def debug(self, *args, **kwargs): pass
                        def info(self, *args, **kwargs): pass
                        def warning(self, *args, **kwargs): pass
                        def error(self, *args, **kwargs): pass
                    for profile in profiles:
                        root = Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data"
                        if profile and chrome_app_bound(root / profile):
                            blocked_encryption = True
                            continue
                        try:
                            jar = extract_cookies_from_browser(self._cookie_browser, profile=profile, logger=QuietLogger())
                            candidate = self._cookies_from_jar(jar)
                            if has_session(candidate):
                                cookies = candidate
                                break
                        except Exception:
                            continue
                    if not cookies:
                        self._cookie_code = "app_bound" if blocked_encryption else "browser_unavailable"
                        message = ("Chrome 的 TikTok 登录 Cookie 使用应用绑定加密，无法直接导入；关闭 Chrome 也不能解决。"
                                   if blocked_encryption else "无法读取浏览器的 TikTok 登录 Cookie。")
                        raise RuntimeError(message + "请点击“登录 TikTok”保存软件登录态，或导入导出的 cookies.txt。")
                else:
                    return []
                self._cookie_snapshot = tiktok_cookies(cookies)
                if not has_session(self._cookie_snapshot):
                    self._cookie_code = "no_session"
                    raise RuntimeError("未检测到有效的 TikTok 登录 Cookie，请点击“登录 TikTok”或重新导入 cookies.txt")
            except RuntimeError as exc:
                self._cookie_error = str(exc)
            except Exception:
                # Never expose cookie contents or decoder input in an exception message.
                self._cookie_error = "登录态文件无法读取或格式不正确，请重新导入或登录 TikTok"
                self._cookie_code = "invalid_cookie_file"
            return list(self._cookie_snapshot)

    @staticmethod
    def _cookies_from_jar(jar):
        return tiktok_cookies([
            {"name": c.name, "value": c.value, "domain": c.domain,
             "path": c.path or "/", "secure": bool(c.secure),
             "expires": int(c.expires) if c.expires else -1}
            for c in jar
        ])

    def _require_cookies(self):
        cookies = self._playwright_cookies()
        if (self._cookie_file or self._cookie_browser) and (self._cookie_error or not has_session(cookies)):
            raise RuntimeError(self._cookie_error or "TikTok 登录态失效，请更新登录态")
        return cookies

    def _apply_cookie_options(self, options):
        # All downloaders use the same in-memory TikTok-only snapshot as Playwright.
        # Do not let yt-dlp independently select another browser profile.
        self._require_cookies()
        options.pop("cookiefile", None)
        options.pop("cookiesfrombrowser", None)
        return options

    def _youtube_dl(self, options):
        cookies = self._require_cookies()
        downloader = yt_dlp.YoutubeDL(options)
        for c in cookies:
            downloader.cookiejar.set_cookie(http.cookiejar.Cookie(
                version=0, name=c["name"], value=c["value"], port=None, port_specified=False,
                domain=c["domain"], domain_specified=True, domain_initial_dot=c["domain"].startswith("."),
                path=c.get("path") or "/", path_specified=True, secure=c.get("secure", False),
                expires=int(c["expires"]) if c.get("expires", -1) > 0 else None,
                discard=c.get("expires", -1) <= 0, comment=None, comment_url=None,
                rest={"HttpOnly": None} if c.get("httpOnly") else {}, rfc2109=False,
            ))
        return downloader

    def _files_for_item(self, folder, item_id, item=None):
        files = [path for path in Path(folder).iterdir() if path.is_file() and f"_[{item_id}]" in path.name]
        if not files and item:
            title = re.sub(r'[<>:"/\\|?*]+', '_', item.get("title") or "").strip(" .")
            stamp = item.get("upload_date") or ""
            if title:
                files = [path for path in Path(folder).iterdir() if path.is_file() and title[:60].lower() in path.stem.lower() and (not stamp or stamp in path.name)]
        media_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".jpg", ".jpeg", ".png", ".webp"}
        subtitle_exts = {".srt", ".vtt", ".ass", ".ttml", ".srv1", ".srv2", ".srv3", ".json"}
        media = [path for path in files if path.suffix.lower() in media_exts]
        subtitles = [path for path in files if path.suffix.lower() in subtitle_exts]
        return media, subtitles

    def _emit(self, function, value):
        if self._window:
            try:
                self._window.evaluate_js(f"{function}({json.dumps(value, ensure_ascii=False)})")
            except Exception:
                pass

    def recognize(self, raw_url):
        try:
            self._collection_complete = False
            self._collection_warning = ""
            self._collection_needs_verification = False
            url = clean_profile_url(raw_url)
            username = re.search(r"/@([^/?]+)", url).group(1)
            self._profile_avatar = ""
            self._profile_stats = {}
            cache_file = self._cache_file(username)
            if cache_file.exists():
                try:
                    early_cache = json.loads(cache_file.read_text(encoding="utf-8"))
                    if isinstance(early_cache, dict) and early_cache.get("avatar_owner") == username:
                        self._profile_avatar = early_cache.get("avatar", "")
                        self._profile_stats = early_cache.get("profile_stats", {})
                        self._emit("profileUpdate", {"username": username, "avatar": self._profile_avatar, "profileStats": self._profile_stats})
                except Exception:
                    pass
            try:
                videos = self._collect_videos(url)
                archived = self._store_profile_archive(username, videos, self._collection_complete)
                videos = archived["videos"]
            except Exception as live_error:
                # A requested login source must never degrade to an anonymous or
                # cached result that looks like a successful full-profile read.
                if (self._cookie_file or self._cookie_browser) and self._cookie_error:
                    raise live_error
                self._collection_warning = self._collection_warning or "本次读取失败，已显示本地缓存；尚未抓完。"
                if not cache_file.exists():
                    raise live_error
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    videos = cached.get("videos", [])
                    self._profile_avatar = cached.get("avatar", "") if cached.get("avatar_owner") == username else ""
                    self._profile_stats = cached.get("profile_stats", {}) if cached.get("avatar_owner") == username else {}
                else:
                    videos = cached
                self._emit("setStatus", f"TikTok 暂时限制访问，已载入本地缓存的 {len(videos)} 条视频")
            normalized = []
            for item in videos:
                if isinstance(item, dict):
                    item.setdefault("type", "image" if "/photo/" in item.get("url", "") else "video")
                    normalized.append(item)
                else:
                    video_id, video_url, title, *extra = item
                    normalized.append({"id": video_id, "url": video_url, "title": title,
                                       "cover": extra[0] if extra else "",
                                       "views": extra[1] if len(extra) > 1 else None,
                                       "type": extra[2] if len(extra) > 2 else "video"})
            return {"ok": True, "complete": self._collection_complete, "warning": self._collection_warning,
                    "needsVerification": self._collection_needs_verification,
                    "username": username, "avatar": self._profile_avatar, "profileStats": self._profile_stats, "videos": normalized}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def recognize_many(self, raw_urls):
        results = []
        for raw_url in raw_urls:
            value = self.recognize(raw_url)
            if value.get("ok"):
                results.append(value)
        if not results:
            return {"ok": False, "error": "没有成功读取任何博主"}
        return {"ok": True, "profiles": results}

    def save_task_state(self, username, record):
        try:
            root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "tasks"
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{re.sub(r'[^A-Za-z0-9._-]', '_', str(username))}.json"
            current = {}
            if path.exists():
                try: current = json.loads(path.read_text(encoding="utf-8"))
                except Exception: current = {}
            current[str(record.get("id"))] = record
            path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def load_task_state(self, username):
        try:
            root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "tasks"
            path = root / f"{re.sub(r'[^A-Za-z0-9._-]', '_', str(username))}.json"
            if not path.exists():
                return {"ok": True, "records": {}}
            records = json.loads(path.read_text(encoding="utf-8"))
            return {"ok": True, "records": records if isinstance(records, dict) else {}}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "records": {}}

    def recent_profiles(self):
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "cache"
        profiles = []
        if not root.exists():
            return profiles
        for cache_file in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if not isinstance(cached, dict):
                    continue
                username = cached.get("avatar_owner") or ""
                avatar = cached.get("avatar") or ""
                if not username:
                    continue
                profiles.append({"username": username, "avatar": avatar,
                                 "count": len(cached.get("videos") or [])})
                if len(profiles) >= 12:
                    break
            except Exception:
                continue
        return profiles

    def refresh_recent_profiles(self):
        """Repair stale avatar cache entries without opening a visible browser."""
        profiles = self.recent_profiles()
        seen, refresh = set(), []
        for profile in profiles:
            # A CDN avatar URL shared by multiple creators is stale cache data.
            key = profile["avatar"].split("?", 1)[0] if profile["avatar"] else f"empty:{profile['username']}"
            if not profile["avatar"] or key in seen:
                refresh.append(profile["username"])
            else:
                seen.add(key)
        if not refresh:
            return {"ok": True, "profiles": profiles}
        with sync_playwright() as playwright:
            for username in refresh:
                browser = context = None
                try:
                    # TikTok's SPA can retain the first profile while navigating in
                    # one tab. A fresh context makes the avatar belong to this ID.
                    browser, context = self._browser_context(playwright)
                    page = context.pages[0] if context.pages else context.new_page()

                    page.goto(f"https://www.tiktok.com/@{username}?lang=en", wait_until="domcontentloaded", timeout=45000)
                    # TikTok initially paints stale SPA content; wait for the
                    # profile image to settle before caching it.
                    page.wait_for_timeout(5000)
                    avatar = page.evaluate("""()=>{const image=document.querySelector('[data-e2e="user-avatar"] img');return image?(image.currentSrc||image.src||''):''}""")
                    if not avatar or "tiktokcdn.com" not in avatar:
                        path = self._cache_file(username)
                        if path.exists():
                            data = json.loads(path.read_text(encoding="utf-8"))
                            if isinstance(data, dict):
                                data["avatar"] = ""
                                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                        continue
                    path = self._cache_file(username)
                    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                    if isinstance(data, dict):
                        data["avatar"] = avatar
                        data["avatar_owner"] = username
                        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    continue
                finally:
                    if context:
                        try: context.close()
                        except Exception: pass
                    if browser:
                        try: browser.close()
                        except Exception: pass
        return {"ok": True, "profiles": self.recent_profiles()}

    def remove_recent_profile(self, username):
        try:
            clean_username = str(username).lstrip("@")
            cache_file = self._cache_file(clean_username)
            if cache_file.exists():
                cache_file.unlink()
            task_file = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "tasks" / f"{re.sub(r'[^A-Za-z0-9._-]', '_', clean_username)}.json"
            if task_file.exists():
                task_file.unlink()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _cache_file(self, username):
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "cache"
        return root / f"{re.sub(r'[^A-Za-z0-9._-]', '_', username)}.json"

    def _load_profile_archive(self, username):
        try:
            value = json.loads(self._cache_file(username).read_text(encoding="utf-8"))
            if isinstance(value, list):
                value = {"videos": value}
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _store_profile_archive(self, username, rows, complete=False):
        """Merge every observed batch atomically so interrupted runs lose no history."""
        path = self._cache_file(username)
        old = self._load_profile_archive(username)
        merged = {str(row.get("id")): row for row in old.get("videos", [])
                  if isinstance(row, dict) and row.get("id")}
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                ident = str(row["id"])
                merged[ident] = {**merged.get(ident, {}), **row}
        videos = sorted(merged.values(), key=lambda row: int(row.get("id", 0) or 0), reverse=True)
        history_complete = bool(old.get("history_complete") or complete)
        payload = {
            "schema": 2, "avatar": self._profile_avatar or old.get("avatar", ""),
            "avatar_owner": username, "profile_stats": self._profile_stats or old.get("profile_stats", {}),
            "videos": videos, "history_complete": history_complete,
            "last_sync": int(time.time()), "count": len(videos),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
        self._emit("archiveUpdate", {"username": username, "videos": videos,
                                     "complete": history_complete, "count": len(videos)})
        return {"ok": True, "complete": history_complete, "warning": self._collection_warning,
                "needsVerification": self._collection_needs_verification, "username": username,
                "avatar": payload["avatar"], "profileStats": payload["profile_stats"], "videos": videos}

    def _login_profile_dir(self):
        return self._session_store.root / "login-browser"

    def _browser_context(self, playwright, reuse_login=False, headless=True):
        cookies = self._require_cookies()
        if reuse_login and self._cookie_browser == "saved" and not self._cookie_file:
            # Use the exact Chrome profile where the user logged in / verified.
            # Copying cookies into a fresh context discards site storage and cache.
            context = playwright.chromium.launch_persistent_context(
                str(self._login_profile_dir()), executable_path=find_chrome(), headless=headless,
                viewport={"width": 1280, "height": 900})
            try:
                existing = tiktok_cookies(context.cookies())
                if not has_session(existing):
                    # Session cookies may be dropped by Chrome on normal shutdown.
                    # Restore only missing cookies; preserve newer verification data.
                    keys = {(c["domain"], c["path"], c["name"]) for c in existing}
                    missing = [c for c in cookies if (c["domain"], c["path"], c["name"]) not in keys]
                    if missing:
                        context.add_cookies(missing)
                return None, context
            except Exception:
                context.close()
                raise
        browser = playwright.chromium.launch(executable_path=find_chrome(), headless=True, args=["--disable-blink-features=AutomationControlled", "--disable-features=AutomationControlled"])
        try:
            # Match the saved login browser; avoid a stale hard-coded Chrome version.
            user_agent = self._session_user_agent or f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36"
            context = browser.new_context(
                viewport={"width": 1280, "height": 900}, locale="en-US",
                user_agent=user_agent,
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            if cookies:
                context.add_cookies(cookies)
            return browser, context
        except Exception:
            browser.close()
            raise

    def _collect_videos(self, url, interactive=False, lock_held=False, cdp_url=None):
        from profile_pagination import ProfilePagination

        # Reject an invalid requested login source before Playwright starts.
        self._require_cookies()
        self._collection_warning = ""
        self._collection_complete = False
        self._collection_needs_verification = False
        username_match = re.search(r"/@([^/?]+)", url)
        username = username_match.group(1) if username_match else ""
        archive = self._load_profile_archive(username)
        known_ids = {str(row.get("id")) for row in archive.get("videos", []) if isinstance(row, dict)}
        incremental = bool(archive.get("history_complete"))
        found, pagination = {}, ProfilePagination(username)
        browser = context = None

        def merge_api_items():
            for ident, item in pagination.items.items():
                kind = "image" if item.get("imagePost") else "video"
                stats, video = item.get("stats") or {}, item.get("video") or {}
                found[ident] = {
                    "id": ident, "url": f"https://www.tiktok.com/@{username}/{'photo' if kind == 'image' else 'video'}/{ident}",
                    "title": item.get("desc") or f"作品 {ident}", "cover": video.get("cover", ""),
                    "views": stats.get("playCount"), "likes": stats.get("diggCount"),
                    "comments": stats.get("commentCount"), "shares": stats.get("shareCount"),
                    "duration": video.get("duration"), "type": kind,
                }
                if item.get("createTime"):
                    try:
                        found[ident]["upload_date"] = time.strftime("%Y%m%d", time.localtime(int(item["createTime"])))
                    except (ValueError, TypeError, OverflowError):
                        pass
            if found:
                self._store_profile_archive(username, list(found.values()), False)

        def collect_response(response):
            if not pagination.request_parts(response.url):
                return
            try:
                pagination.observe(response.url, response.json(), response.status)
            except Exception:
                if pagination.belongs_to_target(response.url):
                    pagination.observe(response.url, None, response.status)

        acquired_here = False
        if not lock_held:
            if not self._profile_lock.acquire(blocking=False):
                raise RuntimeError("登录或验证窗口正在使用浏览器，请完成后关闭该窗口再继续")
            acquired_here = True
        try:
            if interactive:
                self._verification_status.update(stage="starting", message="正在启动 Chrome…")
            else:
                self._emit("setStatus", "正在后台读取主页…")
            with sync_playwright() as playwright:
                try:
                    if cdp_url:
                        deadline = time.monotonic() + 18
                        last_error = None
                        while time.monotonic() < deadline and not self._login_cancel.is_set():
                            try:
                                browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=3000)
                                context = browser.contexts[0] if browser.contexts else None
                                if context:
                                    break
                            except Exception as exc:
                                last_error = exc
                                time.sleep(.35)
                        if not context:
                            raise RuntimeError(f"Chrome 已启动，但软件无法连接验证窗口：{last_error or '连接超时'}")
                    else:
                        browser, context = self._browser_context(playwright, reuse_login=True, headless=not interactive)
                    if interactive:
                        self._verification_status.update(stage="opened", message="验证窗口已打开，请完成验证并保持窗口打开")
                        self._verification_started.set()
                    page = context.pages[0] if context.pages else context.new_page()
                    page.on("response", collect_response)
                    page.goto(url + "?lang=en", wait_until="domcontentloaded", timeout=60000)
                    try:
                        page.wait_for_selector('[data-e2e="followers-count"]', timeout=12000)
                    except Exception:
                        page.wait_for_timeout(1500)
                    profile_data = page.evaluate("""()=>{const img=document.querySelector('[data-e2e="user-avatar"] img');const text=e=>{const n=document.querySelector(e);return n?n.textContent.trim():null};return {avatar:img?(img.currentSrc||img.src||''):'',following:text('[data-e2e="following-count"]'),followers:text('[data-e2e="followers-count"]'),likes:text('[data-e2e="likes-count"]')}}""") or {}
                    self._profile_avatar = profile_data.pop("avatar", "") or ""
                    self._profile_stats = profile_data
                    self._emit("profileUpdate", {"username": username, "avatar": self._profile_avatar, "profileStats": self._profile_stats})
                    last_growth, previous, previous_revision = time.monotonic(), -1, -1
                    retries, last_continuation = {}, 0.0
                    while True:
                        rows = page.locator('a[href*="/video/"], a[href*="/photo/"]').evaluate_all("""els=>els.map(e=>{const c=e.closest('[data-e2e="user-post-item"]')||e.parentElement;const i=e.querySelector('img')||(c&&c.querySelector('img'));const v=c&&c.querySelector('[data-e2e="video-views"]');return {url:e.href.split('?')[0],title:e.getAttribute('aria-label')||(i&&i.alt)||e.innerText||'',cover:i?(i.currentSrc||i.src):'',views:v?v.textContent.trim():null}})""")
                        for row in rows:
                            match = re.search(r"/(?:video|photo)/(\d+)", row.get("url", ""))
                            if match and f"/@{username.lower()}/" in row.get("url", "").lower() and match.group(1) not in found:
                                title = " ".join((row.get("title") or "").split()) or f"作品 {match.group(1)}"
                                found[match.group(1)] = {"id": match.group(1), "url": row["url"], "title": title[:160], "cover": row.get("cover") or "", "views": row.get("views"), "type": "image" if "/photo/" in row.get("url", "") else "video"}
                        merge_api_items()
                        challenge = False
                        for frame in page.frames:
                            try:
                                text = frame.locator('body').inner_text(timeout=800).lower()
                                if any(message in text for message in ("drag the slider to fit the puzzle", "verify to continue", "拖动滑块", "完成下方验证")):
                                    challenge = True
                                    break
                            except Exception:
                                continue
                        if challenge:
                            if interactive:
                                self._emit("setStatus", "请在打开的 TikTok 窗口完成安全验证…")
                                deadline = time.monotonic() + 900
                                while challenge and time.monotonic() < deadline and not self._login_cancel.is_set():
                                    if page.is_closed():
                                        raise RuntimeError("验证窗口已关闭。请重新打开验证窗口，完成验证后保持窗口打开，软件会自动继续抓取。")
                                    page.wait_for_timeout(1000)
                                    challenge = False
                                    for frame in page.frames:
                                        try:
                                            text = frame.locator('body').inner_text(timeout=800).lower()
                                            if any(message in text for message in ("drag the slider to fit the puzzle", "verify to continue", "拖动滑块", "完成下方验证")):
                                                challenge = True
                                                break
                                        except Exception:
                                            continue
                                if self._login_cancel.is_set():
                                    raise RuntimeError("已取消验证")
                                if challenge:
                                    raise RuntimeError("等待 TikTok 验证超时")
                                last_growth = time.monotonic()
                                continue
                            self._collection_needs_verification = True
                            self._verification_username = username
                            self._collection_warning = "TikTok 要求安全验证，已有作品已保留。点击“打开验证窗口”，在博主主页完成验证并关闭窗口后自动继续。"
                            break
                        if pagination.refused:
                            self._collection_warning = pagination.error + "；已保留已有作品，尚未抓完。"
                            break
                        if pagination.complete:
                            self._collection_complete = True
                            break
                        # Once a complete archive exists, the first overlap proves
                        # we reached previously saved history; future syncs only
                        # need to collect the newer prefix.
                        if incremental and known_ids.intersection(found):
                            self._collection_complete = True
                            self._collection_warning = f"增量同步完成，本次发现 {len(set(found) - known_ids)} 条新作品。"
                            break
                        now = time.monotonic()
                        if len(found) != previous or pagination.revision != previous_revision:
                            last_growth = now
                        previous, previous_revision = len(found), pagination.revision
                        stalled = now - last_growth
                        if pagination.error_at is not None and now - pagination.error_at >= 12:
                            self._collection_warning = pagination.error + "；已保留已有作品，尚未抓完。"
                            break
                        if stalled >= 45:
                            self._collection_warning = "分页连续 45 秒没有前进，已保留已有作品；尚未抓完，请稍后重试。"
                            break
                        self._emit("setStatus", f"已读取 {len(found)} 条，{'正在恢复下一页…' if stalled >= 6 else '继续读取…'}")
                        # Reuse the original page and its login session. Continue
                        # only an observed, target-scoped API cursor; never reset
                        # to a new anonymous session after a stall.
                        continuation = pagination.continuation_url() if stalled >= 6 else None
                        if continuation and now - last_continuation >= 4:
                            cursor = pagination.next_cursor()
                            if retries.get(cursor, 0) >= 3:
                                self._collection_warning = "同一分页位置重试 3 次仍未前进，已保留已有作品；尚未抓完。"
                                break
                            retries[cursor] = retries.get(cursor, 0) + 1
                            last_continuation = now
                            try:
                                result = page.evaluate("""async url=>{const c=new AbortController();const timer=setTimeout(()=>c.abort(),20000);try{const r=await fetch(url,{credentials:'include',signal:c.signal});let data=null;try{data=await r.json()}catch{}return {status:r.status,data}}finally{clearTimeout(timer)}}""", continuation)
                                pagination.observe(continuation, result.get("data"), result.get("status", 0))
                            except Exception:
                                pagination.fail("下一页网络请求失败")
                            continue
                        # TikTok sometimes scrolls a nested content panel rather
                        # than window. Find the actual scroll parent of a card.
                        page.evaluate("""({username,back})=>{const cards=[...document.querySelectorAll('a[href*="/video/"],a[href*="/photo/"]')].filter(e=>e.href.toLowerCase().includes('/@'+username.toLowerCase()+'/'));let node=cards.at(-1);let scroller=null;while(node){const style=getComputedStyle(node);if(node.scrollHeight>node.clientHeight+80&&/(auto|scroll)/.test(style.overflowY)){scroller=node;break}node=node.parentElement}scroller=scroller||document.scrollingElement;if(scroller){if(back)scroller.scrollTop=Math.max(0,scroller.scrollTop-600);else scroller.scrollTop=scroller.scrollHeight}}""", {"username": username, "back": stalled >= 3})
                        if stalled >= 3:
                            page.wait_for_timeout(300)
                        page.mouse.wheel(0, 2500)
                        page.wait_for_timeout(1200)
                finally:
                    if context:
                        if self._cookie_browser == "saved" and not self._cookie_file:
                            try:
                                fresh_cookies = tiktok_cookies(context.cookies())
                                if has_session(fresh_cookies):
                                    with self._cookie_lock:
                                        self._session_store.save(fresh_cookies, self._session_user_agent)
                                        self._cookie_snapshot = fresh_cookies
                            except Exception:
                                pass
                        try: context.close()
                        except Exception: pass
                    if browser:
                        try: browser.close()
                        except Exception: pass
        except Exception:
            merge_api_items()
            if interactive:
                raise
            if not found:
                raise
            self._collection_warning = "读取连接中断，已保留已有作品；尚未抓完。"
        finally:
            if acquired_here:
                self._profile_lock.release()
        if not found and not self._collection_complete and not self._collection_needs_verification:
            raise RuntimeError(self._collection_warning or "TikTok 没有返回可读取的作品，尚未抓完。")
        return list(found.values())
    def enrich(self, videos):
        """Fill engagement metadata progressively; failures leave the fast list usable."""
        import yt_dlp

        if not videos:
            return {"updated": 0}
        username_match = re.search(r"/@([^/?]+)", videos[0].get("url", ""))
        username = username_match.group(1) if username_match else "unknown"
        cache_file = self._cache_file(username)
        avatar = ""
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    avatar = cached.get("avatar", "")
            except Exception:
                pass
        updated = 0
        options = {"quiet": True, "no_warnings": True, "skip_download": True,
                   "socket_timeout": 20, "retries": 1, "noplaylist": True}
        self._apply_cookie_options(options)
        for index, item in enumerate(videos, 1):
            if item.get("type") == "image" or "/photo/" in item.get("url", ""):
                if not item.get("upload_date"):
                    try:
                        item["upload_date"] = time.strftime("%Y%m%d", time.localtime(int(item["id"]) >> 32))
                    except Exception:
                        pass
                self._emit("metadataUpdate", item)
                self._emit("metadataStatus", {"current": index, "total": len(videos)})
                continue
            if item.get("likes") is not None and item.get("upload_date"):
                self._emit("metadataUpdate", item)
                continue
            try:
                with self._youtube_dl(options) as ydl:
                    info = ydl.extract_info(item["url"], download=False)
                item.update({
                    "title": info.get("description") or info.get("title") or item.get("title"),
                    "likes": info.get("like_count"),
                    "views": info.get("view_count") or item.get("views"),
                    "comments": info.get("comment_count"),
                    "upload_date": info.get("upload_date"),
                    "cover": info.get("thumbnail") or item.get("cover", ""),
                })
                updated += 1
                self._emit("metadataUpdate", item)
                if updated % 5 == 0:
                    cache_file.write_text(json.dumps({"avatar": avatar, "avatar_owner": username, "profile_stats": self._profile_stats, "videos": videos}, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps({"avatar": avatar, "avatar_owner": username, "profile_stats": self._profile_stats, "videos": videos}, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            self._emit("metadataStatus", {"current": index, "total": len(videos)})
            time.sleep(0.2)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"avatar": avatar, "avatar_owner": username, "profile_stats": self._profile_stats, "videos": videos}, ensure_ascii=False), encoding="utf-8")
        return {"updated": updated}

    def choose_folder(self):
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None

    def export_csv(self, rows, suggested_name="tiktok-works.csv"):
        try:
            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=suggested_name,
                file_types=("CSV 文件 (*.csv)",),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (list, tuple)) else result
            target = Path(path)
            if target.suffix.lower() != ".csv":
                target = target.with_suffix(".csv")
            columns = [
                "博主ID", "作品ID", "发布时间", "文案", "类型", "时长(秒)",
                "点赞数", "评论数", "分享数", "播放量", "点赞率", "评论率", "分享率",
                "本地文件夹", "原始TikTok链接", "下载状态", "学习状态", "标签", "学习笔记",
            ]
            with target.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)
            return {"ok": True, "path": str(target), "count": len(rows)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_folder(self, folder):
        try:
            target = Path(folder).expanduser().resolve()
            if not target.is_dir():
                return {"ok": False, "error": "下载文件夹不存在"}
            os.startfile(str(target))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _photo_urls(self, item):
        from playwright.sync_api import sync_playwright

        browser, context = None, None
        with sync_playwright() as playwright:
            try:
                browser, context = self._browser_context(playwright)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(item["url"] + "?lang=en", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(7000)
                urls = page.locator("img").evaluate_all("""els=>els.map(i=>({u:i.currentSrc||i.src||'',w:i.naturalWidth,h:i.naturalHeight})).filter(x=>x.u.includes('photomode')).map(x=>x.u)""")
                unique, signatures = [], set()
                for image_url in urls:
                    asset = re.search(r"/([0-9a-f]{20,})~", image_url, re.I)
                    signature = asset.group(1).lower() if asset else image_url.split("?")[0].split("~tplv-")[0]
                    if signature not in signatures:
                        signatures.add(signature)
                        unique.append(image_url)
                if not unique:
                    raise RuntimeError("页面未返回高清图片地址")
                return unique
            finally:
                if context:
                    context.close()
                if browser:
                    browser.close()
    def pause_downloads(self):
        self._pause_downloads.set()
        return True

    def resume_downloads(self):
        self._pause_downloads.clear()
        return True

    def cancel_downloads(self):
        self._cancel_downloads.set()
        self._pause_downloads.clear()
        return True

    def regenerate_learning_document(self, item, folder):
        """Refresh subtitle tracks and create a new study note without downloading media again."""
        try:
            target = Path(folder)
            if not target.is_dir():
                return {"ok": False, "error": "原下载文件夹不存在"}
            self._emit("learningProgress", {"id": item["id"], "state": "working", "message": "正在重新抓取字幕…"})
            options = {"outtmpl": str(target / (self._filename_template if "%(ext)" in self._filename_template else self._filename_template + ".%(ext)s")),
                       "skip_download": True, "noplaylist": True, "quiet": True, "no_warnings": True,
                       "writesubtitles": True, "writeautomaticsub": True, "subtitleslangs": ["all"],
                       "subtitlesformat": "srt/vtt/best", "overwrites": True,
                       "impersonate": ImpersonateTarget.from_str("chrome")}
            self._apply_cookie_options(options)
            with self._youtube_dl(options) as ydl:
                ydl.download([item["url"]])
            _, tracks = self._files_for_item(target, item["id"], item)
            paths = [str(path) for path in tracks]
            if not paths:
                return {"ok": False, "error": "该作品没有可下载的字幕轨"}
            if not self._learning.get("enabled") or not self._learning.get("api_key"):
                return {"ok": True, "subtitles": paths, "message": "字幕已刷新；请在设置中启用并配置学习文档 API"}
            threading.Thread(target=self._generate_learning_document, args=(item, target, paths), daemon=True).start()
            return {"ok": True, "subtitles": paths, "message": "字幕已刷新，正在生成学习文档"}
        except Exception as exc:
            self._emit("learningProgress", {"id": item.get("id", ""), "state": "failed", "message": str(exc)})
            return {"ok": False, "error": str(exc)}

    def download(self, videos, folder, quality, retry_count=3, concurrency=1, _worker=False):
        import yt_dlp

        concurrency = max(1, min(8, int(concurrency or 1)))
        if not _worker and concurrency > 1 and len(videos) > 1:
            self._cancel_downloads.clear()
            self._pause_downloads.clear()
            results = []
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="TikTokDownload") as pool:
                futures = {
                    pool.submit(self.download, [item], folder, quality, retry_count, 1, True): item
                    for item in videos
                }
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append({"ok": 0, "failed": [{"id": item["id"], "error": str(exc)}]})
            failed = [entry for result in results for entry in result.get("failed", [])]
            return {"ok": sum(result.get("ok", 0) for result in results), "failed": failed,
                    "folder": results[0].get("folder", folder) if results else folder}

        username_match = re.search(r"/@([^/?]+)", videos[0].get("url", "")) if videos else None
        username = username_match.group(1) if username_match else "unknown"
        safe_username = re.sub(r'[<>:"/\\|?*]', "_", username).strip(" .") or "unknown"
        target = Path(folder).expanduser() / f"@{safe_username}"
        target.mkdir(parents=True, exist_ok=True)
        try:
            free_bytes = shutil.disk_usage(str(target)).free
            if free_bytes < 200 * 1024 * 1024:
                raise RuntimeError(f"保存磁盘空间不足：仅剩 {free_bytes / 1024 / 1024:.0f} MB")
        except FileNotFoundError:
            raise RuntimeError("保存目录不可用")
        formats = {
            "720p": "best[height<=720][format_note!=watermarked]/best[height<=720]/best",
            "540p": "best[height<=540][format_note!=watermarked]/best[height<=540]/best",
        }
        fmt = formats.get(quality, "best[format_note!=watermarked]/best")
        ok, failed = 0, []
        if not _worker:
            self._cancel_downloads.clear()
            self._pause_downloads.clear()
        progress_context = {"id": "", "last_emit": 0.0, "last_percent": -1, "started": time.time()}
        def control_hook(data):
            while self._pause_downloads.is_set() and not self._cancel_downloads.is_set():
                time.sleep(0.15)
            if self._cancel_downloads.is_set():
                raise RuntimeError("用户取消下载")
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            percent = min(100, max(0, round(downloaded * 100 / total))) if total else 0
            now = time.time()
            if data.get("status") == "finished":
                percent = 100
            if percent != progress_context["last_percent"] and (now - progress_context["last_emit"] >= .15 or percent == 100):
                progress_context["last_emit"] = now
                progress_context["last_percent"] = percent
                elapsed = max(0.1, now - progress_context["started"])
                self._emit("downloadProgress", {"id": progress_context["id"], "state": "progress", "percent": percent,
                                                  "downloaded": downloaded, "totalBytes": total, "speed": downloaded / elapsed})
        for index, item in enumerate(videos, 1):
            if self._cancel_downloads.is_set():
                break
            progress_context.update({"id": item["id"], "last_emit": 0.0, "last_percent": -1, "started": time.time()})
            self._emit("downloadProgress", {"id": item["id"], "index": index, "total": len(videos), "state": "downloading", "percent": 0})
            try:
                existing_media, existing_subtitles = self._files_for_item(target, item["id"], item)
                if existing_media:
                    self._emit("downloadProgress", {"id": item["id"], "state": "skipped", "folder": str(target),
                                                     "subtitles": [str(path) for path in existing_subtitles]})
                    continue
                if item.get("type") == "image" or "/photo/" in item.get("url", ""):
                    import requests
                    urls = self._photo_urls(item)
                    stamp = item.get("upload_date") or time.strftime("%Y%m%d", time.localtime(int(item["id"]) >> 32))
                    title = re.sub(r'[<>:"/\\|?*]+', '_', item.get("title") or "图片").strip(" .")[:100]
                    post_folder = target / f"{title}_{stamp}_[{item['id']}]"
                    post_folder.mkdir(parents=True, exist_ok=True)
                    for photo_index, photo_url in enumerate(urls, 1):
                        if self._cancel_downloads.is_set():
                            raise RuntimeError("用户取消下载")
                        while self._pause_downloads.is_set() and not self._cancel_downloads.is_set():
                            time.sleep(.15)
                        response = None
                        last_photo_error = None
                        for photo_attempt in range(1, max(1, int(retry_count) + 1)):
                            try:
                                response = requests.get(photo_url, headers={"Referer": item["url"], "User-Agent": "Mozilla/5.0"}, timeout=45)
                                response.raise_for_status()
                                break
                            except Exception as photo_exc:
                                last_photo_error = photo_exc
                                if photo_attempt < max(1, int(retry_count) + 1):
                                    time.sleep(photo_attempt)
                        if response is None:
                            raise RuntimeError(f"图片下载失败：{last_photo_error}")
                        output = post_folder / f"{photo_index:02d}.jpg"
                        if output.exists() and output.stat().st_size > 0:
                            self._emit("downloadProgress", {"id": item["id"], "state": "progress", "percent": round(photo_index * 100 / len(urls))})
                            continue
                        output.write_bytes(response.content)
                        self._emit("downloadProgress", {"id": item["id"], "state": "progress", "percent": round(photo_index * 100 / len(urls))})
                    ok += 1
                    self._emit("downloadProgress", {"id": item["id"], "state": "done", "folder": str(target), "subtitles": []})
                    continue
                options = {
                    "outtmpl": str(target / (self._filename_template if "%(ext)" in self._filename_template else self._filename_template + ".%(ext)s")),
                    "format": fmt,
                    "noplaylist": True,
                    "retries": max(0, int(retry_count)),
                    "continuedl": True,
                    "windowsfilenames": True,
                    "quiet": True,
                    "no_warnings": True,
                    # TikTok returns a challenge page to generic HTTP clients.
                    # Use yt-dlp's curl_cffi handler to make the request match a
                    # current Chrome browser before extracting the media URLs.
                    "impersonate": ImpersonateTarget.from_str("chrome"),
                    "progress_hooks": [control_hook],
                    "writesubtitles": True,
                    "writeautomaticsub": True,
                    "subtitleslangs": ["all"],
                    "subtitlesformat": "srt/vtt/best",
                }
                self._apply_cookie_options(options)
                last_video_error = None
                for video_attempt in range(1, max(1, int(retry_count) + 1) + 1):
                    try:
                        with self._youtube_dl(options) as ydl:
                            ydl.download([item["url"]])
                        media_files, subtitle_files = self._files_for_item(target, item["id"], item)
                        if not media_files:
                            raise RuntimeError("下载器未生成目标文件")
                        last_video_error = None
                        break
                    except Exception as video_exc:
                        last_video_error = video_exc
                        if video_attempt < max(1, int(retry_count) + 1):
                            self._emit("downloadProgress", {"id": item["id"], "state": "retrying", "attempt": video_attempt + 1})
                            for _ in range(int(min(10, video_attempt * 3))):
                                if self._cancel_downloads.is_set():
                                    raise RuntimeError("用户取消下载")
                                while self._pause_downloads.is_set() and not self._cancel_downloads.is_set():
                                    time.sleep(0.15)
                                time.sleep(0.5)
                if last_video_error is not None:
                    raise last_video_error
                ok += 1
                _, subtitle_files = self._files_for_item(target, item["id"], item)
                subtitle_paths = [str(path) for path in subtitle_files]
                self._emit("downloadProgress", {"id": item["id"], "state": "done", "folder": str(target),
                                                 "subtitles": subtitle_paths})
                self._queue_learning_document(item, target, subtitle_paths)
            except Exception as exc:
                if self._cancel_downloads.is_set():
                    self._emit("downloadProgress", {"id": item["id"], "state": "cancelled"})
                    break
                failed.append({"id": item["id"], "error": str(exc)})
                self._emit("downloadProgress", {"id": item["id"], "state": "failed", "error": str(exc)})
        return {"ok": ok, "failed": failed, "folder": str(target)}


if __name__ == "__main__":
    if "--browser-probe" in sys.argv:
        report_path = Path(sys.argv[sys.argv.index("--browser-probe") + 1])
        report = {"ok": False}
        try:
            probe_api = Api()
            status = probe_api.set_cookie_options("", "saved")
            with sync_playwright() as probe_playwright:
                probe_browser, probe_context = probe_api._browser_context(
                    probe_playwright, reuse_login=True, headless=False)
                try:
                    probe_page = probe_context.pages[0] if probe_context.pages else probe_context.new_page()
                    probe_page.goto("https://www.tiktok.com/@apple", wait_until="domcontentloaded", timeout=60000)
                    report = {"ok": True, "session": bool(status.get("hasSession")),
                              "url": probe_page.url, "title": probe_page.title()}
                    time.sleep(3)
                finally:
                    probe_context.close()
                    if probe_browser:
                        probe_browser.close()
        except Exception as exc:
            report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        raise SystemExit(0 if report.get("ok") else 1)
    # Prevent accidental double launches from creating competing download windows.
    lock_path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "app.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = open(lock_path, "a+")
    try:
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock_handle.close()
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk(); root.withdraw(); messagebox.showinfo("TikTok 下载器", "程序已经在运行中，请切换到已有窗口。")
            root.destroy()
        except Exception:
            pass
        raise SystemExit(0)
    api = Api()
    api._window = webview.create_window(
        "TikTok 下载器",
        str(BASE / "ui" / "index.html"),
        js_api=api,
        width=1320,
        height=880,
        min_size=(1000, 650),
        background_color="#0b0e14",
    )
    webview.start(debug=False)

