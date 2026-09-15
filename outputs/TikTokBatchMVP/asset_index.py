"""素材库的本地索引层。

这一层**只读磁盘、不联网**：把一个下载目录扫成一条条"素材"，并算出每条
还缺哪些附属文件。联网补齐（重新提取元数据）在 web_app.py 的
`backfill_assets`，这一层不碰网络，所以可以完全离线测试。

为什么单独一个模块：`web_app.py` 已经是个 2500 行的单文件，
而"什么算媒体、什么算附属文件、一条素材由哪些文件组成"这套约定
是**素材工厂所有后续功能的地基**（素材库、自动打标签、转码、切片都要用），
值得有一个能脱离 Api 直接引用的地方。
"""

import datetime
import json
import re
from collections import Counter
from pathlib import Path


MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".jpg", ".jpeg", ".png", ".webp"}
SUBTITLE_EXTS = {".srt", ".vtt", ".ass", ".ttml", ".srv1", ".srv2", ".srv3", ".json"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# --- 素材工厂的落盘约定 ---------------------------------------------------
#
# 封面、音频、原始信息、素材元数据都跟视频同前缀，所以必须显式排除，否则会被
# 现有的分类逻辑误判，而且两种误判都很隐蔽：
#
#   * MEDIA_EXTS 含 .jpg —— 封面会被当成"媒体文件已存在"。下载前的跳过判断
#     （download 里 `if existing_media:`）会把**根本没下载过**的作品判成
#     已下载而静默跳过，用户只会看到"全部完成"却没有文件。
#   * SUBTITLE_EXTS 含 .json —— 元数据会被塞进 subtitles 列表，
#     让"有字幕就生成学习文档"的判断误触发，然后拿着空的字幕去问模型。
#
# 命名统一用"视频名 + 固定后缀"，不占用 yt-dlp 的 <名>.<语言>.<ext> 命名空间。
COVER_SUFFIX = ".cover.jpg"
INFO_SUFFIX = ".info.json"
META_SUFFIX = ".meta.json"
AUDIO_SUFFIX = ".audio.mp3"
SIDECAR_SUFFIXES = (COVER_SUFFIX, INFO_SUFFIX, META_SUFFIX, AUDIO_SUFFIX)
# 视频排在图片前面用：图文帖的 .jpg 和视频同名时，media[0] 必须是视频。
VIDEO_EXT_ORDER = (".mp4", ".mkv", ".webm", ".mov", ".avi")
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"}

# yt-dlp 的 info 里带着 cookies 和 http_headers。实测（2026-09-13，
# @nasa/video/7682095989578091790）连**匿名**提取都会带回
#     ttwid=1%7COEp_3ruJSA...; tt_csrf=...
# 注入登录态下载时更会带上 sessionid —— 原样落盘等于把登录凭证
# 写进用户的视频文件夹（那是会被同步、备份、发给别人的目录）。
SENSITIVE_INFO_KEYS = frozenset({"cookies", "http_headers"})

# 素材元数据的 schema 版本。改字段必须 +1，素材库按这个选解析器。
ASSET_SCHEMA = 1

# 图文帖的落盘目录名形如 <标题>_<日期>_[<id>]，一个目录就是一条素材。
POST_FOLDER_RE = re.compile(r"_\[(\d+)\]$")

HASHTAG_RE = re.compile(r"#([0-9A-Za-z_\u4e00-\u9fff\u3040-\u30ff]+)")

# 一条素材可能缺的附属文件。刻意不含 subtitles：
# 有的作品本来就没有字幕，缺了不代表"没补齐"，列进去只会制造噪音。
GAP_KINDS = ("cover", "audio", "info", "meta")


def sanitize_info(value):
    """Strip credential-bearing keys from anything read out of yt-dlp.

    Applied recursively: the top level carries `cookies`, and every entry in
    `formats` carries its own `http_headers`.
    """
    if isinstance(value, dict):
        return {key: sanitize_info(item) for key, item in value.items()
                if key not in SENSITIVE_INFO_KEYS}
    if isinstance(value, list):
        return [sanitize_info(item) for item in value]
    return value


def extract_hashtags(text):
    """TikTok 的话题只能从文案里解析。

    yt-dlp 对 TikTok **不提供** tags 字段（实测恒为 None），只在 description
    里以 #话题 的形式存在，所以素材库要的话题标签必须自己切。
    """
    tags, seen = [], set()
    for match in HASHTAG_RE.findall(str(text or "")):
        if match.lower() not in seen:
            seen.add(match.lower())
            tags.append(match)
    return tags


def is_sidecar(path):
    """True for the files this app writes next to the media (cover/audio/info/meta)."""
    return str(path).lower().endswith(SIDECAR_SUFFIXES)


def build_asset_meta(info, item=None, files=None):
    """素材库读的那一份元数据。

    刻意跟 yt-dlp 的原始 info 分开：原始 info 有 62 个字段、还会随 yt-dlp
    版本变，而素材库需要一个**稳定**的结构。字段名固定，改就要升 ASSET_SCHEMA。

    取值优先用 yt-dlp 刚拿到的 info（最新、最准），退回抓取阶段的 item
    （离线补写、或 yt-dlp 这次没给出的字段）。
    """
    info = info or {}
    item = item or {}
    files = files or {}

    def pick(*values):
        for value in values:
            if value not in (None, "", [], {}):
                return value
        return None

    description = pick(info.get("description"), info.get("title"), item.get("title")) or ""
    width = pick(info.get("width"), item.get("width"))
    height = pick(info.get("height"), item.get("height"))
    timestamp = pick(info.get("timestamp"), item.get("timestamp"))
    created_at = ""
    if timestamp:
        try:
            created_at = datetime.datetime.fromtimestamp(int(timestamp)).isoformat(timespec="seconds")
        except (ValueError, TypeError, OSError, OverflowError):
            created_at = ""
    return {
        "schema": ASSET_SCHEMA,
        "id": str(pick(info.get("id"), item.get("id")) or ""),
        "url": pick(info.get("webpage_url"), item.get("url")) or "",
        "type": item.get("type") or "video",
        "title": pick(info.get("title"), item.get("title")) or "",
        "description": description,
        "author": {
            "username": pick(info.get("uploader"), item.get("author")) or "",
            "nickname": pick(info.get("channel"), item.get("nickname")) or "",
            "id": str(pick(info.get("uploader_id")) or ""),
            "url": pick(info.get("uploader_url")) or "",
        },
        "created_at": created_at,
        "upload_date": pick(info.get("upload_date"), item.get("upload_date")) or "",
        "duration": pick(info.get("duration"), item.get("duration")),
        "video": {
            "width": width,
            "height": height,
            "resolution": pick(info.get("resolution"),
                               f"{width}x{height}" if width and height else None),
            "aspect_ratio": info.get("aspect_ratio"),
            "format_id": info.get("format_id"),
            "ext": info.get("ext"),
            "vcodec": info.get("vcodec"),
            "acodec": info.get("acodec"),
            "dynamic_range": info.get("dynamic_range"),
            "filesize": pick(info.get("filesize"), info.get("filesize_approx")),
            "tbr": info.get("tbr"),
        },
        "stats": {
            "views": pick(info.get("view_count"), item.get("views")),
            "likes": pick(info.get("like_count"), item.get("likes")),
            "comments": pick(info.get("comment_count"), item.get("comments")),
            "shares": pick(info.get("repost_count"), item.get("shares")),
            "saves": info.get("save_count"),
        },
        # TikTok 没有 tags 字段，只能从文案切。
        "hashtags": extract_hashtags(description),
        "music": {"title": info.get("track") or "", "artist": info.get("artist") or ""},
        # 全部是文件名，不含路径 —— 整个素材库目录可以整体搬走。
        "files": files,
        "source": {
            "extractor": pick(info.get("extractor_key"), "TikTok"),
            "captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
        },
    }


# --- 扫描 -----------------------------------------------------------------

def media_files(folder):
    """目录里真正的媒体文件（不含封面/音频/元数据），视频排在图片前。"""
    folder = Path(folder)
    files = [path for path in folder.iterdir()
             if path.is_file() and not is_sidecar(path)
             and path.suffix.lower() in MEDIA_EXTS]
    files.sort(key=lambda path: (path.suffix.lower() not in VIDEO_EXT_ORDER, path.name))
    return files


def subtitle_siblings(folder, stem):
    """跟某个媒体同名的字幕（yt-dlp 的 <名>.<语言>.<ext>）。"""
    folder = Path(folder)
    return sorted(str(path) for path in folder.iterdir()
                  if path.is_file() and path.name.startswith(stem + ".")
                  and not is_sidecar(path)
                  and path.suffix.lower() in SUBTITLE_EXTS)


def _nonempty(path):
    """存在且非空就返回它的字符串路径，否则 None。

    索引里的路径一律是字符串：这份结构要直接喂给前端 JSON，
    Path 对象必须先序列化，不如一开始就存字符串。
    """
    try:
        return str(path) if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def asset_gaps(asset):
    """这条素材缺哪些附属文件。"""
    return [kind for kind in GAP_KINDS if not asset.get(kind)]


def build_asset(folder, media_path):
    """把一条视频素材在磁盘上的实际情况汇总出来。"""
    folder = Path(folder)
    media_path = Path(media_path)
    stem = media_path.stem
    audio = sorted(str(path) for path in folder.iterdir()
                   if path.is_file() and path.suffix.lower() in AUDIO_EXTS
                   and path.name.startswith(stem + ".audio."))
    asset = {
        "type": "video",
        "stem": stem,
        "folder": str(folder),
        "media": str(media_path),
        "cover": _nonempty(folder / (stem + COVER_SUFFIX)),
        "audio": audio[0] if audio else None,
        "info": _nonempty(folder / (stem + INFO_SUFFIX)),
        "meta": _nonempty(folder / (stem + META_SUFFIX)),
        "subtitles": subtitle_siblings(folder, stem),
    }
    asset["gaps"] = asset_gaps(asset)
    return asset


def is_post_folder(folder):
    return bool(POST_FOLDER_RE.search(Path(folder).name))


def build_post_asset(folder):
    """图文帖：一个目录就是一条素材。

    只有 meta.json 是可补的 —— 第一张图就是封面，图片是直接抓的所以没有
    info.json，也没单独存音频轨。所以 gaps 只会是 meta。
    """
    folder = Path(folder)
    match = POST_FOLDER_RE.search(folder.name)
    images = sorted(path.name for path in folder.iterdir()
                    if path.is_file() and path.suffix.lower() in IMAGE_EXTS)
    meta = _nonempty(folder / "meta.json")
    asset = {
        "type": "image",
        "stem": folder.name,
        "id": match.group(1) if match else "",
        "folder": str(folder),
        "media": str(folder),
        "cover": None,
        "audio": None,
        "info": None,
        "meta": meta,
        "subtitles": [],
        "images": images,
    }
    asset["gaps"] = ["meta"] if not meta else []
    return asset


def scan_assets(root, recursive=True):
    """扫描一个目录，返回 (assets, problems)。

    遇到图文帖目录就整条收下、不再往里走 —— 否则每张 jpg 都会变成一条素材。
    """
    root = Path(root)
    assets, problems = [], []
    if not root.is_dir():
        return assets, [{"folder": str(root), "error": "目录不存在"}]
    stack = [root]
    while stack:
        folder = stack.pop()
        if is_post_folder(folder):
            try:
                assets.append(build_post_asset(folder))
            except OSError as exc:
                problems.append({"folder": str(folder), "error": str(exc)})
            continue
        try:
            for path in media_files(folder):
                assets.append(build_asset(folder, path))
        except OSError as exc:
            problems.append({"folder": str(folder), "error": str(exc)})
        if recursive:
            try:
                stack.extend(path for path in folder.iterdir() if path.is_dir())
            except OSError as exc:
                problems.append({"folder": str(folder), "error": str(exc)})
    assets.sort(key=lambda asset: (asset["folder"], asset["stem"]))
    return assets, problems


def load_meta(path):
    """读一条素材的 meta.json，读不出来就返回 None（不抛）。"""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def index_summary(assets):
    """给界面看的一行摘要。"""
    by_gap = Counter()
    total_bytes = 0
    for asset in assets:
        for kind in asset.get("gaps") or []:
            by_gap[kind] += 1
        for key in ("media", "cover", "audio"):
            path = asset.get(key)
            if isinstance(path, (str, Path)):
                try:
                    total_bytes += Path(path).stat().st_size
                except OSError:
                    pass
    complete = sum(1 for asset in assets if not asset.get("gaps"))
    return {
        "total": len(assets),
        "complete": complete,
        "incomplete": len(assets) - complete,
        "byGap": dict(by_gap),
        "bytes": total_bytes,
        "videos": sum(1 for asset in assets if asset["type"] == "video"),
        "posts": sum(1 for asset in assets if asset["type"] == "image"),
    }
