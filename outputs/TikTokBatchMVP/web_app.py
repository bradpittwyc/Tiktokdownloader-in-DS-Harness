import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import webview

from app import clean_profile_url, find_chrome


BASE = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent


class Api:
    def __init__(self):
        self._window = None
        self._scraper = None
        self._profile_avatar = ""
        self._profile_stats = {}
        self._pause_downloads = threading.Event()
        self._cancel_downloads = threading.Event()

    def _emit(self, function, value):
        if self._window:
            try:
                self._window.evaluate_js(f"{function}({json.dumps(value, ensure_ascii=False)})")
            except Exception:
                pass

    def recognize(self, raw_url):
        try:
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
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps({"avatar": self._profile_avatar, "avatar_owner": username, "profile_stats": self._profile_stats, "videos": videos}, ensure_ascii=False), encoding="utf-8")
            except Exception as live_error:
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
            return {"ok": True, "username": username, "avatar": self._profile_avatar, "profileStats": self._profile_stats, "videos": normalized}
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
                if not username or not avatar or "tiktokcdn.com" not in avatar:
                    continue
                profiles.append({"username": username, "avatar": avatar,
                                 "count": len(cached.get("videos") or [])})
                if len(profiles) >= 12:
                    break
            except Exception:
                continue
        return profiles

    def _cache_file(self, username):
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "cache"
        return root / f"{re.sub(r'[^A-Za-z0-9._-]', '_', username)}.json"

    def _browser_context(self, playwright):
        browser = playwright.chromium.launch(executable_path=find_chrome(), headless=True, args=["--disable-blink-features=AutomationControlled"])
        return browser, browser.new_context(viewport={"width": 1280, "height": 900})

    def _collect_videos(self, url):
        from playwright.sync_api import sync_playwright

        username_match = re.search(r"/@([^/?]+)", url)
        username = username_match.group(1) if username_match else ""
        last_error = ""
        for attempt in range(1, 4):
            found, browser, context = {}, None, None
            try:
                self._emit("setStatus", f"后台读取主页（第 {attempt}/3 次尝试）…")
                with sync_playwright() as playwright:
                    browser, context = self._browser_context(playwright)
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(url + "?lang=en", wait_until="domcontentloaded", timeout=60000)
                    try:
                        page.wait_for_selector('[data-e2e="followers-count"]', timeout=12000)
                    except Exception:
                        page.wait_for_timeout(1500)
                    profile_js = """()=>{const img=document.querySelector('[data-e2e=\"user-avatar\"] img');const text=e=>{const n=document.querySelector(e);return n?n.textContent.trim():null};return {avatar:img?(img.currentSrc||img.src||''):'',following:text('[data-e2e=\"following-count\"]'),followers:text('[data-e2e=\"followers-count\"]'),likes:text('[data-e2e=\"likes-count\"]')}}"""
                    profile_data = page.evaluate(profile_js) or {}
                    self._profile_avatar = profile_data.pop("avatar", "") or ""
                    self._profile_stats = profile_data
                    self._emit("profileUpdate", {"username": username, "avatar": self._profile_avatar, "profileStats": self._profile_stats})
                    unchanged, previous = 0, -1
                    for _ in range(120):
                        rows = page.locator('a[href*="/video/"], a[href*="/photo/"]').evaluate_all("""els=>els.map(e=>{const c=e.closest('[data-e2e=\"user-post-item\"]')||e.parentElement;const i=e.querySelector('img')||(c&&c.querySelector('img'));const v=c&&c.querySelector('[data-e2e=\"video-views\"]');return {url:e.href.split('?')[0],title:e.getAttribute('aria-label')||(i&&i.alt)||e.innerText||'',cover:i?(i.currentSrc||i.src):'',views:v?v.textContent.trim():null}})""")
                        for row in rows:
                            match = re.search(r"/(?:video|photo)/(\d+)", row.get("url", ""))
                            if match:
                                title = " ".join((row.get("title") or "").split()) or f"作品 {match.group(1)}"
                                found[match.group(1)] = {"id": match.group(1), "url": row["url"], "title": title[:160], "cover": row.get("cover") or "", "views": row.get("views"), "type": "image" if "/photo/" in row.get("url", "") else "video"}
                        self._emit("setStatus", f"已读取 {len(found)} 条，继续加载…")
                        unchanged = unchanged + 1 if found and len(found) == previous else 0
                        previous = len(found)
                        if unchanged >= 10:
                            break
                        page.mouse.wheel(0, 5000)
                        page.wait_for_timeout(800)
                    if found:
                        return list(found.values())
                    last_error = "TikTok 页面没有返回公开作品"
            except Exception as exc:
                last_error = str(exc)
            finally:
                if context:
                    try: context.close()
                    except Exception: pass
                if browser:
                    try: browser.close()
                    except Exception: pass
            time.sleep(2)
        raise RuntimeError(f"后台读取连续失败：{last_error}")
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
                with yt_dlp.YoutubeDL(options) as ydl:
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
            self._emit("metadataStatus", {"current": index, "total": len(videos)})
            time.sleep(0.35)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"avatar": avatar, "avatar_owner": username, "profile_stats": self._profile_stats, "videos": videos}, ensure_ascii=False), encoding="utf-8")
        return {"updated": updated}

    def choose_folder(self):
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None

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

    def download(self, videos, folder, quality):
        import yt_dlp

        username_match = re.search(r"/@([^/?]+)", videos[0].get("url", "")) if videos else None
        username = username_match.group(1) if username_match else "unknown"
        safe_username = re.sub(r'[<>:"/\\|?*]', "_", username).strip(" .") or "unknown"
        target = Path(folder).expanduser() / f"@{safe_username}"
        target.mkdir(parents=True, exist_ok=True)
        formats = {
            "720p": "best[height<=720][format_note!=watermarked]/best[height<=720]/best",
            "540p": "best[height<=540][format_note!=watermarked]/best[height<=540]/best",
        }
        fmt = formats.get(quality, "best[format_note!=watermarked]/best")
        ok, failed = 0, []
        self._cancel_downloads.clear()
        self._pause_downloads.clear()
        progress_context = {"id": "", "last_emit": 0.0, "last_percent": -1}
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
                self._emit("downloadProgress", {"id": progress_context["id"], "state": "progress", "percent": percent,
                                                  "downloaded": downloaded, "totalBytes": total})
        for index, item in enumerate(videos, 1):
            if self._cancel_downloads.is_set():
                break
            progress_context.update({"id": item["id"], "last_emit": 0.0, "last_percent": -1})
            self._emit("downloadProgress", {"id": item["id"], "index": index, "total": len(videos), "state": "downloading", "percent": 0})
            try:
                existing = [path for path in target.iterdir() if path.is_file() and f"_[{item['id']}]" in path.name]
                if existing:
                    self._emit("downloadProgress", {"id": item["id"], "state": "skipped", "folder": str(target)})
                    continue
                if item.get("type") == "image" or "/photo/" in item.get("url", ""):
                    import requests
                    urls = self._photo_urls(item)
                    stamp = item.get("upload_date") or time.strftime("%Y%m%d", time.localtime(int(item["id"]) >> 32))
                    title = re.sub(r'[<>:"/\\|?*]+', '_', item.get("title") or "图片").strip(" .")[:100]
                    post_folder = target / f"{title}_{stamp}_[{item['id']}]"
                    post_folder.mkdir(parents=True, exist_ok=True)
                    if any(post_folder.glob("*.jpg")):
                        self._emit("downloadProgress", {"id": item["id"], "state": "skipped", "folder": str(target)})
                        continue
                    for photo_index, photo_url in enumerate(urls, 1):
                        if self._cancel_downloads.is_set():
                            raise RuntimeError("用户取消下载")
                        while self._pause_downloads.is_set() and not self._cancel_downloads.is_set():
                            time.sleep(.15)
                        response = requests.get(photo_url, headers={"Referer": item["url"], "User-Agent": "Mozilla/5.0"}, timeout=30)
                        response.raise_for_status()
                        output = post_folder / f"{photo_index:02d}.jpg"
                        output.write_bytes(response.content)
                        self._emit("downloadProgress", {"id": item["id"], "state": "progress", "percent": round(photo_index * 100 / len(urls))})
                    ok += 1
                    self._emit("downloadProgress", {"id": item["id"], "state": "done", "folder": str(target)})
                    continue
                options = {
                    "outtmpl": str(target / "%(title).100B_%(upload_date)s_[%(id)s].%(ext)s"),
                    "format": fmt,
                    "noplaylist": True,
                    "retries": 3,
                    "continuedl": True,
                    "windowsfilenames": True,
                    "quiet": True,
                    "no_warnings": True,
                    "progress_hooks": [control_hook],
                }
                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.download([item["url"]])
                ok += 1
                self._emit("downloadProgress", {"id": item["id"], "state": "done", "folder": str(target)})
            except Exception as exc:
                if self._cancel_downloads.is_set():
                    self._emit("downloadProgress", {"id": item["id"], "state": "cancelled"})
                    break
                failed.append({"id": item["id"], "error": str(exc)})
                self._emit("downloadProgress", {"id": item["id"], "state": "failed", "error": str(exc)})
        return {"ok": ok, "failed": failed, "folder": str(target)}


if __name__ == "__main__":
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

