"""生成第三方开源组件声明，并把各组件自带的许可证全文收集到 installer/licenses/。

依赖变了就重跑一次：
    python installer/generate_notices.py
"""

import shutil
import sys
from importlib.metadata import distribution
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
LICENSE_DIR = HERE / "licenses"
NOTICES = HERE / "THIRD-PARTY-NOTICES.txt"

# 打包进 EXE 的运行时组件。（PyInstaller 只用于构建，但它的许可有特殊条款，一并列出。）
COMPONENTS = [
    ("yt-dlp", "yt-dlp", "抓取 TikTok 媒体地址、下载视频与字幕"),
    ("playwright", "Playwright (Python)", "驱动本机 Chrome 读取页面与翻页"),
    ("pywebview", "pywebview", "承载界面的桌面外壳（Windows 上走 WebView2）"),
    ("curl_cffi", "curl_cffi", "让 yt-dlp 的请求指纹与 Chrome 一致"),
    ("python-docx", "python-docx", "生成英文学习文档（.docx）"),
    ("requests", "requests", "下载图文帖的图片"),
    ("Pillow", "Pillow", "图片处理"),
]


def license_name(dist):
    meta = dist.metadata
    for key in ("License-Expression", "License"):
        value = (meta.get(key) or "").strip()
        if value:
            return value.splitlines()[0][:80]
    classifiers = [c.split("::")[-1].strip()
                   for c in (meta.get_all("Classifier") or []) if c.startswith("License")]
    return ", ".join(sorted(set(classifiers))) or "见项目主页"


def collect_license_files(dist, slug):
    """把发行版自带的许可证全文抄一份出来。"""
    written = []
    for entry in dist.files or []:
        name = Path(str(entry)).name.lower()
        if not name.startswith(("license", "licence", "copying", "notice")):
            continue
        if Path(str(entry)).suffix.lower() not in ("", ".txt", ".md", ".rst"):
            continue
        source = Path(dist.locate_file(entry))
        if not source.is_file() or source.stat().st_size == 0:
            continue
        target = LICENSE_DIR / f"{slug}-{Path(str(entry)).name}"
        shutil.copyfile(source, target)
        written.append(target.name)
    return written


def main():
    LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    for old in LICENSE_DIR.glob("*.txt"):
        old.unlink()

    rows = []
    for dist_name, title, purpose in COMPONENTS:
        dist = distribution(dist_name)
        version = dist.version
        slug = dist_name.lower().replace("_", "-")
        files = collect_license_files(dist, slug)
        rows.append({
            "title": title,
            "version": version,
            "license": license_name(dist),
            "purpose": purpose,
            "files": files,
            "home": (dist.metadata.get("Home-page")
                     or dist.metadata.get("Project-URL")
                     or ""),
        })

    lines = [
        "第三方开源组件声明",
        "=" * 60,
        "",
        "本软件包含以下开源组件，它们各自的版权与许可条款归原作者所有。",
        "各组件的许可证全文收录在本文件同级的 licenses 目录中。",
        "",
        "-" * 60,
    ]
    for row in rows:
        lines += [
            "",
            f"{row['title']} {row['version']}",
            f"  许可证 : {row['license']}",
            f"  用途   : {row['purpose']}",
        ]
        if row["home"]:
            lines.append(f"  项目   : {row['home']}")
        if row["files"]:
            lines.append(f"  全文   : licenses/{', licenses/'.join(row['files'])}")
    lines += [
        "",
        "-" * 60,
        "",
        "构建工具：PyInstaller（GPLv2 或更新版本，附带特殊例外条款，",
        "允许对其打包产物自由使用）。PyInstaller 不会进入最终产品的运行时。",
        "",
        "本软件使用 Microsoft Edge WebView2 运行时，该运行时由 Microsoft 提供，",
        "受其自身许可条款约束，不随本软件分发。",
        "",
        "本软件不包含、不修改上述组件的源代码。",
        "",
    ]
    NOTICES.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成 {NOTICES.name}")
    for row in rows:
        print(f"  {row['title']:<22} {row['version']:<12} {row['license'][:40]:<42} "
              f"许可全文 {len(row['files'])} 个")


if __name__ == "__main__":
    main()
