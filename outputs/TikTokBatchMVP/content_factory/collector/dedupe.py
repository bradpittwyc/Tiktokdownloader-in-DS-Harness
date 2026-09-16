"""候选内容去重：source_video_id / source_url / content_key 三层。

为什么是三层而不是一层：
- source_video_id 最准（TikTok 的作品 id 永不变），但候选里可能没有它
  （有的来源只给了链接）；
- source_url 用来兜住「有链接没 id」的情况，所以必须先归一化 ——
  带 ?is_from_webapp=1、#anchor、m.tiktok.com 主机名的同一条内容
  必须算作同一条，否则同一个视频会被反复排队；
- content_key 是入库时的唯一键（与 factory_store.content_key 同一套写法），
  用它把「候选」和「内容库里已有的内容」对齐。

注意：这里不碰数据库，也不判断「该不该下载」—— 那是 policy.py 的事。
"""

import re

VIDEO_ID_PATTERN = re.compile(r"/(?:video|photo)/(\d+)")

# 手机端 / 短链主机名统一到 www，否则同一条内容两种写法会被当成两条。
HOST_ALIASES = {
    "m.tiktok.com": "www.tiktok.com",
    "tiktok.com": "www.tiktok.com",
}

_URL_PATTERN = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<host>[^/]+)(?P<path>.*)$")


def normalize_url(url):
    """去掉查询串 / 锚点 / 末尾斜杠，主机名小写并统一到 www。"""
    text = str(url or "").strip()
    if not text:
        return ""
    text = text.split("#", 1)[0].split("?", 1)[0].strip()
    match = _URL_PATTERN.match(text)
    if match:
        host = match.group("host").lower()
        host = HOST_ALIASES.get(host, host)
        text = f"{match.group('scheme').lower()}://{host}{match.group('path')}"
    return text.rstrip("/")


def video_id_from_url(url):
    """从 /@user/video/7312… 或 /@user/photo/7312… 里取出作品 id。"""
    match = VIDEO_ID_PATTERN.search(str(url or ""))
    return match.group(1) if match else ""


def key_for(source_type, source_video_id="", source_url=""):
    """统一的 content_key。

    有作品 id 就与 factory_store.content_key 完全一致（tiktok:7312）；
    只有链接时退化成 tiktok:url:<归一化链接>，这样「只有链接」的候选
    也能和别的候选、以及库里已有的内容对上。
    """
    kind = source_type or "tiktok"
    ident = str(source_video_id or "").strip() or video_id_from_url(source_url)
    if ident:
        return f"{kind}:{ident}"
    normalized = normalize_url(source_url)
    return f"{kind}:url:{normalized}" if normalized else ""


class DedupeIndex:
    """一次采集批次内的去重表（跨批次 / 跨库的去重由 policy + 唯一索引负责）。"""

    def __init__(self):
        self.keys = set()
        self.urls = set()

    def check(self, content_key, source_url=""):
        """返回 None 表示没重复（并登记）；否则返回重复原因。"""
        key = str(content_key or "").strip()
        url = normalize_url(source_url)
        if key and key in self.keys:
            return "duplicate_key"
        if url and url in self.urls:
            return "duplicate_url"
        if key:
            self.keys.add(key)
        if url:
            self.urls.add(url)
        return None

    def __len__(self):
        return len(self.keys)
