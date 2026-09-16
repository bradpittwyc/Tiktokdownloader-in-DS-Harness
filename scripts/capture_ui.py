"""开发期截图工具：起真实窗口、逐个页面截图，用来和 UI 参考图逐页比对。

不是应用的一部分，只在手动跑 `python scripts/capture_ui.py <输出目录>` 时使用。

踩过的三个坑（都实测过，别再踩回去）：
1. ImageGrab.grab() 抓的是**屏幕**，窗口没被激活时截到的是压在下面的别的窗口
   （第一次截出来是 ChatGPT 页面）。而 SetForegroundWindow 对非前台进程会被
   Windows 静默忽略 —— 靠"提窗口 + 抓屏"不可靠。
   改用 PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)：直接让窗口把自己画到
   内存 DC 上，不需要窗口可见或被激活，后台也能截。
2. 不能用固定 sleep 猜"页面渲染好了"：要轮询 window.__bootError 与正文文案，
   否则截到的是「正在启动内容工厂…」。
3. 高 DPI 下窗口矩形是物理像素，PrintWindow 出来的位图也是物理像素，两者一致，
   所以按 rect 尺寸建位图即可，不要再用逻辑尺寸换算。
"""
import argparse
import ctypes
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "outputs" / "TikTokBatchMVP"
sys.path.insert(0, str(BASE))

PAGES = [
    ("dashboard", "01-dashboard"), ("creators", "02-creators"), ("collect", "03-collect"),
    ("pipeline", "04-pipeline"), ("ai", "05-ai"), ("publish", "06-publish"),
    ("errors", "07-errors"), ("settings", "08-settings-basic"),
]
SETTINGS_SUBS = [("collect", "09-settings-collect"), ("ai", "10-settings-ai"),
                 ("publish", "11-settings-publish"), ("storage", "12-settings-storage"),
                 ("notify", "13-settings-notify"), ("account", "14-settings-account")]

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020


def grab_window(title, path):
    """用 PrintWindow 抓指定标题的窗口，返回 (宽, 高) 或 None。"""
    from PIL import Image

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.FindWindowW.restype = wintypes.HWND
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return None

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 200 or height < 200:
        return None

    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT):
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height           # 负数 = 自上而下，省得再翻转
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0            # BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
            return None
        image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)
        image.convert("RGB").save(path)
        return image.size
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out", nargs="?", default="docs/ui-screenshots")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=920)
    parser.add_argument("--demo", action="store_true", help="先写入演示数据再截图")
    parser.add_argument("--only", default="", help="只截某几个页面名，逗号分隔")
    args = parser.parse_args()

    import webview
    import web_app

    api = web_app.Api()
    if args.demo:
        api.content_factory.content_seed_demo()

    out = Path(args.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parents[1] / out
    out.mkdir(parents=True, exist_ok=True)

    title = "Tony Content Engine 截图"
    # 主文档 URL 也要带版本串：WebView2 按 URL 缓存 file:// 主文档，
    # 不带的话改完 app.css 重新截图还是旧样式（实测踩过，见 web_app.start_ui 注释）。
    stamp = str(int(time.time()))
    window = webview.create_window(title, str(BASE / "ui" / "app.html") + f"?v={stamp}",
                                   js_api=api, x=40, y=40, width=args.width,
                                   height=args.height, background_color="#f5f7fb")

    def settled():
        for _ in range(160):
            try:
                errors = window.evaluate_js("JSON.stringify(window.__bootError||null)")
                if errors and errors not in ("null", "[]"):
                    print("启动期报错：", errors)
                ok = window.evaluate_js(
                    "(()=>{const h=document.getElementById('content');"
                    "return !!h && !h.textContent.includes('正在启动内容工厂');})()")
                if ok:
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def shoot():
        try:
            if not settled():
                print("界面没在超时内渲染完成，仍继续截图以便查看现场")
            time.sleep(1.0)
            wanted = {name for name in args.only.split(",") if name}
            todo = [(f"go('{page}')", name) for page, name in PAGES if not wanted or name in wanted]
            todo += [(f"go('settings','{sub}')", name) for sub, name in SETTINGS_SUBS
                     if not wanted or name in wanted]
            if not wanted or "15-library" in wanted:
                todo.append(("go('library')", "15-library"))
            for script, name in todo:
                window.evaluate_js(script)
                time.sleep(1.0 if name != "15-library" else 3.0)
                size = grab_window(title, out / f"{name}.png")
                print(("已保存 " + str(out / f"{name}.png") + f" {size}") if size
                      else f"抓不到窗口：{name}")
            print(f"截图完成：{out}")
        finally:
            window.destroy()

    threading.Thread(target=shoot, daemon=True).start()
    webview.start(private_mode=False, storage_path=str(web_app.webview_storage_path()))


if __name__ == "__main__":
    main()
