"""TikTok-only login snapshots, protected by Windows DPAPI at rest."""
import ctypes
from ctypes import wintypes
from contextlib import closing
import json
import os
import sqlite3
import time
from pathlib import Path


SESSION_NAMES = {"sessionid", "sessionid_ss", "sid_tt"}


def tiktok_cookies(cookies, now=None):
    now = time.time() if now is None else now
    result = []
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lower().lstrip(".")
        if domain != "tiktok.com" and not domain.endswith(".tiktok.com"):
            continue
        if not cookie.get("name") or not cookie.get("value"):
            continue
        expires = cookie.get("expires", -1)
        if expires not in (None, -1, 0) and expires <= now:
            continue
        clean = {key: cookie[key] for key in
                 ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite")
                 if key in cookie}
        clean["path"] = clean.get("path") or "/"
        clean["expires"] = expires if expires and expires > 0 else -1
        if clean.get("sameSite") not in {"Strict", "Lax", "None"}:
            clean.pop("sameSite", None)
        result.append(clean)
    return result


def has_session(cookies):
    return any(c["name"] in SESSION_NAMES for c in tiktok_cookies(cookies))


def dpapi(data, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest)):
        raise OSError("Windows 无法读取本机加密的 TikTok 登录态，请重新登录")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        kernel.LocalFree(dest.data)


class SessionStore:
    def __init__(self, root=None):
        self.root = Path(root) if root else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP"
        self.path = self.root / "tiktok-session.dpapi"

    def save(self, cookies, user_agent=""):
        cookies = tiktok_cookies(cookies)
        if not has_session(cookies):
            raise ValueError("尚未检测到 TikTok 登录 Cookie，请先完成登录")
        payload = {"cookies": cookies, "user_agent": user_agent, "saved_at": time.time()}
        encrypted = dpapi(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, self.path)
        return cookies

    def load(self):
        if not self.path.exists():
            return {"cookies": [], "user_agent": ""}
        payload = json.loads(dpapi(self.path.read_bytes(), decrypt=True))
        payload["cookies"] = tiktok_cookies(payload.get("cookies", []))
        return payload


def chrome_app_bound(profile_root):
    """Inspect encryption type only; do not attempt to decrypt browser secrets."""
    database = Path(profile_root) / "Network" / "Cookies"
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            return bool(conn.execute(
                "SELECT 1 FROM cookies WHERE (host_key = 'tiktok.com' OR host_key LIKE '%.tiktok.com') "
                "AND name IN ('sessionid', 'sessionid_ss', 'sid_tt') "
                "AND substr(encrypted_value, 1, 3) = ? LIMIT 1", (b"v20",)
            ).fetchone())
    except sqlite3.Error:
        return False
