"""真机 Smoke Test 的启动器：起真实窗口，但额外打开 WebView2 的调试端口。

为什么不直接改 web_app.py：那是产品代码，为了测试往里加 `debug=True` 属于
「顺手改别的」—— 本阶段只修创作者添加流程。

为什么需要它：pywebview 6.2.1 只认 `webview.settings['REMOTE_DEBUGGING_PORT']`
（edgechromium.py:87-90 把它拼进 WebView2 的 AdditionalBrowserArguments），
而这个设置默认是 None，web_app.py 也不会去改它。所以这里在进程内把它填上，
其余一切照旧 —— 跑起来的仍然是真实应用：真窗口、真桥接、真 sqlite。
顺带把 debug 打开（方便人肉看一眼），但调试端口只靠那个设置，不靠 debug。

用法（由 scripts/rc1_creator_smoke.py 调起，也可手动跑）：
    python scripts/webview_cdp_launcher.py <调试端口>
"""

import os
import runpy
import sys
from pathlib import Path

import webview

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "outputs" / "TikTokBatchMVP" / "web_app.py"

_original_start = webview.start


def start_with_debug(*args, **kwargs):
    kwargs["debug"] = True
    return _original_start(*args, **kwargs)


def main():
    if len(sys.argv) < 2:
        print("用法：python scripts/webview_cdp_launcher.py <调试端口>")
        return 2
    port = int(sys.argv[1])
    # 这条环境变量是 WebView2 自己的开关，pywebview 不会覆盖它，双保险。
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                          f"--remote-debugging-port={port}")
    # 关键：必须在 create_window 之前设置 —— WebView2 的 CreationProperties
    # 是在建窗的时候定下来的，之后再改 webview.settings 已经晚了。
    webview.settings["REMOTE_DEBUGGING_PORT"] = port
    webview.start = start_with_debug
    sys.argv = [str(APP)]                     # 别让下游把调试端口当成应用参数
    sys.path.insert(0, str(APP.parent))
    runpy.run_path(str(APP), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
