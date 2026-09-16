"""采集数据模型：候选视频的归一化、以及交给下载器的标准 job 形状。

关键约定（别改）：`job_to_downloader_video()` 的输出必须与
`web_app.Api.download(videos, ...)` 读的字段**逐字对应** —— id / url / title /
description / cover / duration / type / upload_date。它就是「Collector 生成
标准化 job，交给已有 downloader」这一步的全部内容：不新增协议、不复制下载器，
只把任务翻译成下载器本来就能吃的形状。
"""

from dataclasses import dataclass, field
import re

from . import dedupe

VIDEO = "video"
PHOTO = "photo"

# 下载器 / 内容库里的图片帖子类型写的是 "image"，采集成 "photo"（TikTok 的 /photo/ 链接）
PHOTO_ALIASES = ("photo", "image", "images")


@dataclass
class CandidateVideo:
    """一条候选内容（从创作者主页发现的 metadata，未下载）。"""

    source_video_id: str = ""
    source_url: str = ""
    title: str = ""
    description: str = ""
    cover: str = ""
    duration: int = 0
    kind: str = VIDEO
    source_type: str = "tiktok"
    creator_handle: str = ""
    upload_date: str = ""
    views: object = None
    raw: dict = field(default_factory=dict)

    @property
    def content_key(self):
        return dedupe.key_for(self.source_type, self.source_video_id, self.source_url)

    @property
    def downloader_kind(self):
        """下载器认的类型：图片帖子是 "image"。"""
        return "image" if self.kind in PHOTO_ALIASES else "video"

    def to_dict(self):
        data = {
            "source_video_id": self.source_video_id,
            "source_url": self.source_url,
            "title": self.title,
            "description": self.description,
            "cover": self.cover,
            "duration": self.duration,
            "kind": self.kind,
            "source_type": self.source_type,
            "creator_handle": self.creator_handle,
            "upload_date": self.upload_date,
            "views": self.views,
            "content_key": self.content_key,
        }
        return data


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


_COUNT_PATTERN = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([KkMmBb万亿]?)$")
_COUNT_FACTORS = {"": 1, "K": 10 ** 3, "M": 10 ** 6, "B": 10 ** 9, "万": 10 ** 4, "亿": 10 ** 8}


def parse_count(value):
    """把 TikTok 主页上的 "48.2K" / "1.2M" / "1,234" 转成整数。

    主页上的粉丝数是给人看的缩写文本，直接 int() 会得到 0 —— 那样 Creator
    的 followers 永远同步不上来。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return 0
    match = _COUNT_PATTERN.match(text)
    if not match:
        digits = re.sub(r"[^\d]", "", text)
        return _as_int(digits)
    amount = float(match.group(1).replace(",", ""))
    factor = _COUNT_FACTORS.get(match.group(2).upper(), 1)
    return int(amount * factor)


def normalize_candidate(raw, handle="", source_type="tiktok"):
    """把下载器给的任意形状归一成 CandidateVideo；认不出来返回 None。

    认得的形状：
    - dict：{"id", "url", "title", "cover", "views", "type", "duration", "upload_date"}
      （`Api.recognize()` 与本地档案 _load_profile_archive 都是这种）
    - tuple/list：(video_id, video_url, title, cover, views, type)（老档案的紧凑写法）
    """
    if isinstance(raw, (tuple, list)):
        if not raw:
            return None
        parts = list(raw) + [None] * 6
        raw = {"id": parts[0], "url": parts[1], "title": parts[2], "cover": parts[3],
               "views": parts[4], "type": parts[5]}
    if not isinstance(raw, dict):
        return None

    url = str(raw.get("url") or raw.get("source_url") or "").strip()
    ident = str(raw.get("id") or raw.get("source_video_id") or "").strip()
    if not ident:
        ident = dedupe.video_id_from_url(url)
    if not ident and not url:
        return None                                  # 既没 id 也没链接 → 无法建任务

    kind = str(raw.get("type") or raw.get("kind") or "").strip().lower()
    if not kind:
        kind = PHOTO if "/photo/" in url else VIDEO
    if kind in PHOTO_ALIASES:
        kind = PHOTO

    title = " ".join(str(raw.get("title") or "").split()) or f"作品 {ident or url}"
    return CandidateVideo(
        source_video_id=ident,
        source_url=url,
        title=title[:200],
        description=str(raw.get("description") or ""),
        cover=str(raw.get("cover") or raw.get("thumbnail") or ""),
        duration=_as_int(raw.get("duration")),
        kind=kind,
        source_type=source_type or "tiktok",
        creator_handle=str(raw.get("creator_handle") or handle or "").lstrip("@"),
        upload_date=str(raw.get("upload_date") or ""),
        views=raw.get("views"),
        raw=dict(raw) if isinstance(raw, dict) else {},
    )


def normalize_candidates(videos, handle="", source_type="tiktok"):
    """批量归一化，跳过认不出来的条目（不抛异常：一条坏数据不该毁掉整次采集）。"""
    result = []
    for raw in videos or []:
        candidate = normalize_candidate(raw, handle=handle, source_type=source_type)
        if candidate is not None:
            result.append(candidate)
    return result


def job_to_downloader_video(job):
    """标准 job -> `Api.download()` 能直接吃的视频条目。"""
    kind = str(job.get("kind") or VIDEO).lower()
    return {
        "id": str(job.get("source_video_id") or ""),
        "url": str(job.get("source_url") or ""),
        "title": str(job.get("title") or ""),
        "description": str(job.get("description") or ""),
        "cover": str(job.get("cover") or ""),
        "duration": _as_int(job.get("duration")),
        "type": "image" if kind in PHOTO_ALIASES else "video",
        "upload_date": str(job.get("upload_date") or ""),
    }


def job_payload(candidate, creator_id="", creator_handle="", batch_id="", priority="中",
                max_attempts=3, attempts=0, state="pending"):
    """CandidateVideo -> 入库用的 job 字典（列名与 collection_jobs 一致）。"""
    return {
        "creator_id": creator_id,
        "creator_handle": creator_handle or candidate.creator_handle,
        "source_type": candidate.source_type,
        "source_video_id": candidate.source_video_id,
        "source_url": candidate.source_url,
        "content_key": candidate.content_key,
        "title": candidate.title,
        "description": candidate.description,
        "cover": candidate.cover,
        "duration": candidate.duration,
        "kind": candidate.kind,
        "upload_date": candidate.upload_date,
        "priority": priority,
        "state": state,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "batch_id": batch_id,
    }
