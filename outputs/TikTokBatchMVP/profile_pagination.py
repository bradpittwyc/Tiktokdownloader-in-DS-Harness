"""Target-scoped TikTok pagination state, independent of browser and credentials."""

import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


class ProfilePagination:
    def __init__(self, username):
        self.username = username.lower()
        self.sec_uid = None
        self.template_url = None
        self.pages = {}
        self.items = {}
        self.error = ""
        self.error_at = None
        self.refused = False
        self.revision = 0

    @staticmethod
    def request_parts(url):
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in ("www.tiktok.com", "tiktok.com")
                or parts.path.rstrip("/") != "/api/post/item_list"):
            return None
        return parts, parse_qs(parts.query, keep_blank_values=True)

    def belongs_to_target(self, url):
        parsed = self.request_parts(url)
        return bool(parsed and self.sec_uid and parsed[1].get("secUid", [None])[0] == self.sec_uid)

    def fail(self, message, refused=False):
        self.error = message
        if self.error_at is None:
            self.error_at = time.monotonic()
        self.refused = self.refused or refused

    def observe(self, url, data, http_status=200):
        parsed = self.request_parts(url)
        if not parsed:
            return False
        _, query = parsed
        sec_uid = query.get("secUid", [None])[0]
        scoped = bool(self.sec_uid and sec_uid == self.sec_uid)
        if self.sec_uid and not scoped:
            return False
        if scoped and http_status in (401, 403, 429):
            self.fail("TikTok 拒绝了分页请求，请检查登录状态或稍后重试", refused=True)
            return False
        if not isinstance(data, dict):
            if scoped:
                self.fail("TikTok 分页返回空内容或无效数据")
            return False
        rows = data.get("itemList")
        rows = rows if isinstance(rows, list) else None
        matches = [item for item in (rows or []) if isinstance(item, dict)
                   and isinstance(item.get("author"), dict)
                   and str(item["author"].get("uniqueId", "")).lower() == self.username]
        # Bind only after a successful response identifies this creator. An
        # unrelated feed's empty last page must never finish this collection.
        if not scoped:
            if not matches or not sec_uid:
                return False
            self.sec_uid = sec_uid
            scoped = True
        if http_status in (401, 403, 429) or str(data.get("statusCode", 0)) != "0":
            self.fail("TikTok 拒绝了分页请求，请检查登录状态或稍后重试", refused=True)
            return False
        if http_status >= 400 or rows is None:
            self.fail("TikTok 分页返回空内容或无效数据")
            return False
        # Mixed-author or owner-less nonempty pages cannot prove continuity.
        if rows and len(matches) != len(rows):
            self.fail("分页作品的博主身份不匹配，无法确认完整性")
            return False
        self.template_url = url
        requested = query.get("cursor", ["0"])[0] or "0"
        before = (dict(self.pages), len(self.items))
        for item in matches:
            if item.get("id") is not None:
                self.items[str(item["id"])] = item
        has_more = data.get("hasMore")
        if has_more in (False, 0, "0"):
            self.pages[requested] = None
        elif has_more in (True, 1, "1"):
            next_cursor = str(data.get("cursor", ""))
            if not next_cursor or next_cursor in ("None", requested):
                self.fail("TikTok 没有返回可继续读取的分页位置")
                return False
            self.pages[requested] = next_cursor
        else:
            self.fail("TikTok 没有返回分页结束标志")
            return False
        self.error = ""
        self.error_at = None
        if before != (self.pages, len(self.items)):
            self.revision += 1
        return True

    def next_cursor(self):
        cursor, seen = "0", set()
        while cursor in self.pages:
            if cursor in seen:
                self.fail("TikTok 重复返回相同分页位置")
                return cursor
            seen.add(cursor)
            cursor = self.pages[cursor]
            if cursor is None:
                return None
        return cursor

    @property
    def complete(self):
        return self.next_cursor() is None

    def continuation_url(self):
        if not self.template_url or self.refused:
            return None
        cursor = self.next_cursor()
        if cursor is None:
            return None
        parts, query = self.request_parts(self.template_url)
        # Request signatures cover the query. Changing cursor on an already
        # signed URL replays an invalid signature; let the page request its next
        # page normally instead. Never manufacture or strip security signatures.
        if cursor != query.get("cursor", ["0"])[0] and any(
                key.lower() in {"x-bogus", "x-gnarly", "_signature"} for key in query):
            return None
        query["cursor"] = [cursor]
        return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))
