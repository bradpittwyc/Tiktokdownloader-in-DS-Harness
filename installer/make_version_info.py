"""从 outputs/TikTokBatchMVP/VERSION 生成 PyInstaller 的版本资源文件。

为什么单独用 Python 生成、而不是写在 build.ps1 里：
**Windows PowerShell 5.1 会把没有 BOM 的 .ps1 当 ANSI(GBK) 读**，
脚本里一旦出现中文（比如产品名「TikTok 下载器」）就会变成乱码，
乱码里混出的引号会让整个脚本语法崩溃。实测踩过。
所以 build.ps1 保持纯 ASCII，中文一律交给 Python 写。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "version_info.txt"

PRODUCT = "TikTok 下载器"
PUBLISHER = "TikTokBatchMVP"
COPYRIGHT = f"Copyright (C) 2026 {PUBLISHER}"

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({quad}), prodvers=({quad}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
      StringStruct('CompanyName', '{publisher}'),
      StringStruct('FileDescription', '{product}'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'TikTokBatchMVP'),
      StringStruct('LegalCopyright', '{copyright}'),
      StringStruct('OriginalFilename', 'TikTokBatchMVP.exe'),
      StringStruct('ProductName', '{product}'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [0x0804, 1200])])
  ]
)
"""


def main():
    version = (sys.argv[1] if len(sys.argv) > 1
               else (HERE.parent / "outputs" / "TikTokBatchMVP" / "VERSION")
               .read_text(encoding="utf-8").strip())
    parts = (version.split(".") + ["0", "0", "0", "0"])[:4]
    # PyInstaller 通过 miscutils.decode() 读取，识别 BOM 与编码声明，
    # 所以直接用带 BOM 的 UTF-8，Windows 上最保险。
    TARGET.write_text(
        TEMPLATE.format(quad=", ".join(parts), version=version,
                        product=PRODUCT, publisher=PUBLISHER, copyright=COPYRIGHT),
        encoding="utf-8-sig")
    print(f"version_info.txt <- {version}  ({TARGET.stat().st_size} 字节)")


if __name__ == "__main__":
    main()
