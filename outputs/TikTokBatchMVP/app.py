import os
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


def app_dir():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent


def find_chrome():
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for item in candidates:
        if item.is_file():
            return str(item)
    raise RuntimeError("未找到 Chrome 或 Edge，请先安装其中一个浏览器。")


def clean_profile_url(value):
    value = value.strip().split("?")[0].rstrip("/")
    match = re.search(r"https?://(?:www\.)?tiktok\.com/@[A-Za-z0-9._-]+", value)
    if not match:
        raise ValueError("请输入 TikTok 博主主页链接，例如 https://www.tiktok.com/@用户名")
    return match.group(0)


def collect_videos(profile_url, progress):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        last_error = ""
        for attempt in range(1, 4):
            found = {}
            context = None
            try:
                progress(f"启用备用读取（第 {attempt}/3 次）…")
                profile_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "browser-profile"
                profile_dir.mkdir(parents=True, exist_ok=True)
                context = p.chromium.launch_persistent_context(
                    str(profile_dir), executable_path=find_chrome(), headless=True,
                    viewport={"width": 1280, "height": 900},
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--window-position=-32000,-32000",
                        "--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--disable-features=CalculateNativeWinOcclusion",
                    ],
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(7000)
                unchanged = 0
                for _ in range(120):
                    rows = page.locator('a[href*="/video/"], a[href*="/photo/"]').evaluate_all(
                        """els => els.map(e => ({url:e.href.split('?')[0], title:e.getAttribute('aria-label') || e.innerText || ''}))"""
                    )
                    before = len(found)
                    for row in rows:
                        match = re.search(r"/(?:video|photo)/(\d+)", row["url"])
                        if match:
                            title = " ".join(row["title"].split()) or f"视频 {match.group(1)}"
                            found[match.group(1)] = (row["url"], title[:160], "", None, "image" if "/photo/" in row["url"] else "video")
                    progress(f"备用读取已获取 {len(found)} 条…")
                    unchanged = unchanged + 1 if found and len(found) == before else 0
                    if unchanged >= 10:
                        break
                    page.mouse.wheel(0, 5000)
                    page.wait_for_timeout(1000)
                if found:
                    return [(video_id, *found[video_id]) for video_id in found]
                last_error = "TikTok 未返回视频卡片"
            except Exception as exc:
                last_error = str(exc)
            finally:
                if context:
                    context.close()
    raise RuntimeError(f"备用读取连续失败：{last_error}")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TikTok 下载器")
        self.geometry("1306x872")
        self.minsize(980, 640)
        self.configure(bg="#0c0f15")
        self.items = {}
        self.busy = False
        self.colors = {"bg": "#0c0f15", "panel": "#151925", "card": "#1a2030", "line": "#2a3243", "text": "#f4f7ff", "muted": "#91a0ba", "red": "#ff5261", "green": "#52e0a4"}
        self._style()
        self._build()

    def _style(self):
        c = self.colors
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=c["bg"])
        style.configure("Panel.TFrame", background=c["panel"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"], font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", foreground=c["muted"])
        style.configure("Brand.TLabel", foreground=c["text"], font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Accent.TLabel", foreground=c["green"], font=("Microsoft YaHei UI", 8))
        style.configure("Panel.TLabel", background=c["panel"], foreground=c["text"])
        style.configure("PanelBrand.TLabel", background=c["panel"], foreground=c["text"], font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("PanelMuted.TLabel", background=c["panel"], foreground=c["muted"])
        style.configure("PanelAccent.TLabel", background=c["panel"], foreground=c["green"], font=("Microsoft YaHei UI", 8))
        style.configure("TButton", background="#182031", foreground=c["text"], bordercolor=c["line"], padding=(12, 8), font=("Microsoft YaHei UI", 9))
        style.map("TButton", background=[("active", "#263148"), ("disabled", "#242733")], foreground=[("disabled", "#777f91")])
        style.configure("Accent.TButton", background=c["red"], foreground="white", bordercolor=c["red"], font=("Microsoft YaHei UI", 10, "bold"), padding=(18, 10))
        style.map("Accent.TButton", background=[("active", "#ff6b76"), ("disabled", "#713a43")])
        style.configure("TEntry", fieldbackground="#111621", foreground=c["text"], bordercolor=c["line"], insertcolor="white", padding=9)
        style.configure("TCombobox", fieldbackground="#111621", background="#182031", foreground=c["text"], arrowcolor=c["text"], padding=7)
        style.configure("Dark.Treeview", background=c["bg"], fieldbackground=c["bg"], foreground=c["text"], rowheight=44, bordercolor=c["line"], font=("Microsoft YaHei UI", 9))
        style.configure("Dark.Treeview.Heading", background=c["panel"], foreground=c["muted"], bordercolor=c["line"], font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Dark.Treeview", background=[("selected", "#242e43")])

    def _build(self):
        c = self.colors
        header = ttk.Frame(self, style="Panel.TFrame", padding=(16, 12))
        header.pack(fill="x")
        logo = tk.Label(header, text="▶", bg=c["red"], fg="white", font=("Segoe UI", 14, "bold"), width=2, height=1)
        logo.pack(side="left", padx=(0, 9))
        brand = ttk.Frame(header, style="Panel.TFrame")
        brand.pack(side="left", padx=(0, 28))
        ttk.Label(brand, text="TikTok 下载器", style="PanelBrand.TLabel").pack(anchor="w")
        ttk.Label(brand, text="批量下载 · 无水印源优先", style="PanelAccent.TLabel").pack(anchor="w")
        self.url = tk.StringVar(value="https://www.tiktok.com/@volleyballqueen86")
        ttk.Entry(header, textvariable=self.url).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.read_button = ttk.Button(header, text="识别", style="Accent.TButton", command=self.read_list)
        self.read_button.pack(side="left", padx=(0, 8))
        self.queue_badge = ttk.Label(header, text="队列  0", style="PanelBrand.TLabel", padding=(12, 8))
        self.queue_badge.pack(side="left")

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True)
        main = ttk.Frame(body, padding=(16, 14))
        side = ttk.Frame(body, style="Panel.TFrame", padding=(12, 12))
        body.add(main, weight=3)
        body.add(side, weight=1)

        self.profile_bar = ttk.Frame(main)
        self.profile_bar.pack(fill="x", pady=(0, 10))
        self.avatar = tk.Label(self.profile_bar, text="♪", bg="#20293a", fg=c["red"], font=("Segoe UI", 18, "bold"), width=2, height=1)
        self.avatar.pack(side="left", padx=(0, 10))
        profile_text = ttk.Frame(self.profile_bar)
        profile_text.pack(side="left", fill="x", expand=True)
        self.profile_name = ttk.Label(profile_text, text="尚未识别账号", style="Brand.TLabel")
        self.profile_name.pack(anchor="w")
        self.profile_meta = ttk.Label(profile_text, text="粘贴 TikTok 博主主页链接并点击识别", style="Muted.TLabel")
        self.profile_meta.pack(anchor="w")

        tabs = ttk.Frame(main)
        tabs.pack(fill="x", pady=(0, 9))
        ttk.Button(tabs, text="全部").pack(side="left")
        ttk.Button(tabs, text="视频").pack(side="left", padx=6)
        ttk.Label(tabs, text="公开主页内容", style="Muted.TLabel").pack(side="left", padx=8)

        tools = ttk.Frame(main)
        tools.pack(fill="x", pady=(0, 8))
        ttk.Label(tools, text="视频列表", style="Brand.TLabel").pack(side="left")
        ttk.Button(tools, text="全选", command=lambda: self.set_all(True)).pack(side="right")
        ttk.Button(tools, text="全不选", command=lambda: self.set_all(False)).pack(side="right", padx=7)
        self.count = ttk.Label(tools, text="粘贴主页链接后点击识别", style="Muted.TLabel")
        self.count.pack(side="left", padx=16)

        filters = ttk.Frame(main)
        filters.pack(fill="x", pady=(0, 8))
        self.filter_text = tk.StringVar()
        search = ttk.Entry(filters, textvariable=self.filter_text)
        search.pack(side="left", fill="x", expand=True, padx=(0, 8))
        search.insert(0, "")
        search.bind("<KeyRelease>", self.filter_list)
        ttk.Label(filters, text="输入标题或视频 ID 筛选", style="Muted.TLabel").pack(side="left")

        self.empty = ttk.Frame(main)
        self.empty.place(relx=.5, rely=.46, anchor="center")
        ttk.Label(self.empty, text="📺", font=("Segoe UI Emoji", 34)).pack()
        ttk.Label(self.empty, text="粘贴博主主页链接开始", font=("Microsoft YaHei UI", 15, "bold")).pack(pady=(8, 4))
        ttk.Label(self.empty, text="自动读取主页视频，勾选后批量下载", style="Muted.TLabel").pack()

        table = ttk.Frame(main)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("check", "title", "id"), show="headings", selectmode="none", style="Dark.Treeview")
        self.tree.heading("check", text="选择")
        self.tree.heading("title", text="视频")
        self.tree.heading("id", text="ID")
        self.tree.column("check", width=58, anchor="center", stretch=False)
        self.tree.column("title", width=620)
        self.tree.column("id", width=180, stretch=False)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self.toggle_item)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

        side_top = ttk.Frame(side, style="Panel.TFrame")
        side_top.pack(fill="x")
        ttk.Label(side_top, text="下载队列", style="PanelBrand.TLabel").pack(side="left")
        self.queue_stats = ttk.Label(side_top, text="等待 0   完成 0   失败 0", style="PanelMuted.TLabel")
        self.queue_stats.pack(side="right")
        self.queue = tk.Listbox(side, bg=c["panel"], fg=c["text"], selectbackground="#263148", selectforeground="white", borderwidth=0, highlightthickness=0, font=("Microsoft YaHei UI", 9), activestyle="none")
        self.queue.pack(fill="both", expand=True, pady=(12, 0))

        footer = ttk.Frame(self, style="Panel.TFrame", padding=(16, 10))
        footer.pack(fill="x")
        ttk.Label(footer, text="画质", style="PanelMuted.TLabel").pack(side="left")
        self.quality = tk.StringVar(value="最佳画质")
        ttk.Combobox(footer, textvariable=self.quality, values=("最佳画质", "720p", "540p"), state="readonly", width=10).pack(side="left", padx=(7, 20))
        ttk.Label(footer, text="保存到", style="PanelMuted.TLabel").pack(side="left")
        self.output = tk.StringVar(value=str(Path.home() / "Downloads" / "TikTok"))
        ttk.Entry(footer, textvariable=self.output).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(footer, text="浏览", command=self.choose_folder).pack(side="left", padx=(0, 14))
        self.status = ttk.Label(footer, text="未选择任何视频", style="PanelMuted.TLabel")
        self.status.pack(side="left", padx=(0, 14))
        self.download_button = ttk.Button(footer, text="开始下载", style="Accent.TButton", command=self.download)
        self.download_button.pack(side="right")

    def later(self, fn, *args):
        self.after(0, fn, *args)

    def set_status(self, text):
        self.after(0, lambda value=text: self.status.config(text=value))

    def set_busy(self, value):
        self.busy = value
        state = "disabled" if value else "normal"
        self.read_button.config(state=state)
        self.download_button.config(state=state)

    def read_list(self):
        if self.busy:
            return
        try:
            profile = clean_profile_url(self.url.get())
        except ValueError as exc:
            messagebox.showerror("链接无效", str(exc))
            return
        self.set_busy(True)
        self.status.config(text="正在打开主页并读取视频…")
        threading.Thread(target=self._read_worker, args=(profile,), daemon=True).start()

    def _read_worker(self, profile):
        try:
            videos = collect_videos(profile, self.set_status)
            self.later(self._show_videos, videos)
        except Exception as exc:
            self.later(messagebox.showerror, "读取失败", str(exc))
            self.set_status("读取失败")
        finally:
            self.later(self.set_busy, False)

    def _show_videos(self, videos):
        self.empty.place_forget()
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        for video_id, url, title in videos:
            iid = self.tree.insert("", "end", values=("☑", title, video_id))
            self.items[iid] = {"selected": True, "url": url, "id": video_id, "title": title}
        match = re.search(r"/@([^/?]+)", self.url.get())
        username = "@" + match.group(1) if match else "TikTok 账号"
        self.profile_name.config(text=username)
        self.profile_meta.config(text=f"共读取 {len(videos)} 条公开视频 · 点击左侧方框加入下载队列")
        self.count.config(text=f"共 {len(videos)} 条，已选择 {len(videos)} 条")
        self.status.config(text="读取完成")
        self.refresh_queue()

    def toggle_item(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell" or self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid in self.items:
            selected = not self.items[iid]["selected"]
            self.items[iid]["selected"] = selected
            values = list(self.tree.item(iid, "values"))
            values[0] = "☑" if selected else "☐"
            self.tree.item(iid, values=values)
            self.update_count()

    def set_all(self, selected):
        for iid, item in self.items.items():
            item["selected"] = selected
            values = list(self.tree.item(iid, "values"))
            values[0] = "☑" if selected else "☐"
            self.tree.item(iid, values=values)
        self.update_count()

    def update_count(self):
        selected = sum(item["selected"] for item in self.items.values())
        self.count.config(text=f"共 {len(self.items)} 条，已选择 {selected} 条")
        self.refresh_queue()

    def refresh_queue(self):
        chosen = [(iid, item) for iid, item in self.items.items() if item["selected"]]
        self.queue.delete(0, "end")
        for iid, item in chosen:
            values = self.tree.item(iid, "values")
            title = values[1] if len(values) > 1 else item["id"]
            self.queue.insert("end", f"  {title[:42]}")
        self.queue_badge.config(text=f"队列  {len(chosen)}")
        self.queue_stats.config(text=f"等待 {len(chosen)}   完成 0   失败 0")
        if not self.busy:
            self.status.config(text=f"已选择 {len(chosen)} 个视频" if chosen else "未选择任何视频")

    def filter_list(self, _event=None):
        needle = self.filter_text.get().strip().lower()
        for position, (iid, item) in enumerate(self.items.items()):
            haystack = f"{item['title']} {item['id']}".lower()
            if not needle or needle in haystack:
                self.tree.reattach(iid, "", position)
            else:
                self.tree.detach(iid)

    def choose_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.output.get())
        if chosen:
            self.output.set(chosen)

    def download(self):
        selected = [item for item in self.items.values() if item["selected"]]
        if not selected:
            messagebox.showinfo("没有选择", "请先勾选要下载的视频。")
            return
        folder = Path(self.output.get()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        self.set_busy(True)
        threading.Thread(target=self._download_worker, args=(selected, folder), daemon=True).start()

    def _download_worker(self, selected, folder):
        import yt_dlp

        ok = 0
        failed = []
        total = len(selected)
        for index, item in enumerate(selected, 1):
            self.set_status(f"下载 {index}/{total}：{item['id']}")
            try:
                options = {
                    "outtmpl": str(folder / "%(upload_date)s_%(id)s_%(title).80B.%(ext)s"),
                    "format": self._format_choice(),
                    "noplaylist": True,
                    "retries": 3,
                    "continuedl": True,
                    "windowsfilenames": True,
                    "quiet": True,
                    "no_warnings": True,
                }
                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.download([item["url"]])
                ok += 1
            except Exception as exc:
                failed.append(f"{item['id']}: {exc}")
        self.later(self._download_done, ok, failed, folder)

    def _format_choice(self):
        choice = self.quality.get()
        if choice == "720p":
            return "best[height<=720][format_note!=watermarked]/best[height<=720]/best"
        if choice == "540p":
            return "best[height<=540][format_note!=watermarked]/best[height<=540]/best"
        return "best[format_note!=watermarked]/best"

    def _download_done(self, ok, failed, folder):
        self.set_busy(False)
        self.status.config(text=f"完成：成功 {ok}，失败 {len(failed)}")
        self.queue_stats.config(text=f"等待 0   完成 {ok}   失败 {len(failed)}")
        detail = f"成功下载 {ok} 条到：\n{folder}"
        if failed:
            detail += "\n\n失败项可再次勾选重试：\n" + "\n".join(failed[:8])
        messagebox.showinfo("下载完成", detail)


if __name__ == "__main__":
    App().mainloop()
