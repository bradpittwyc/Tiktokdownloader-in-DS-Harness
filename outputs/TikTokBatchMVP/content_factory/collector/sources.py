"""候选内容来源：Collector 只依赖这个小接口，不直接依赖下载器。

为什么要有这一层：
- 生产环境用的是 `DownloaderCandidateSource` —— 它**调用现有下载器的
  `Api.recognize()`**，不复制任何抓取逻辑（Chrome 启动、分页、cookie、
  安全验证、本地档案增量同步全都在下载器里，一个字都不重写）。
- 测试和离线场景用 `StaticCandidateSource`，不需要浏览器也不需要网络。
- 以后要接别的平台（YouTube / 小红书），只需要再实现一个 discover()。

顺带说明「增量」：`Api.recognize()` 内部在本地档案标记为完整之后会自动
转成增量读取（`_collect_videos` 里的 incremental 分支），所以反复轮询
不会每次把整个主页翻一遍 —— 这里不需要也不应该再实现一套增量逻辑。
"""

from dataclasses import dataclass, field


def profile_url(handle):
    text = str(handle or "").strip().lstrip("@")
    return f"https://www.tiktok.com/@{text}" if text else ""


@dataclass
class DiscoveryResult:
    """一次「读取创作者主页」的结果。"""

    ok: bool = False
    videos: list = field(default_factory=list)
    error: str = ""
    warning: str = ""
    complete: bool = False
    needs_verification: bool = False
    cancelled: bool = False
    cached: bool = False
    avatar: str = ""
    profile_stats: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def state(self):
        """成功 / 部分成功 / 失败 —— 直接决定下次检查是等一个周期还是退避。"""
        if not self.ok:
            return "failed"
        return "success" if self.complete else "partial"


class CandidateSource:
    """候选项来源接口。"""

    name = "source"

    def discover(self, creator, newest_only=False):        # pragma: no cover - 接口
        raise NotImplementedError


class StaticCandidateSource(CandidateSource):
    """写死的来源：单测 / 离线演示用。"""

    name = "static"

    def __init__(self, videos=None, error="", complete=True, warning="",
                 needs_verification=False, cancelled=False, avatar="", profile_stats=None,
                 by_handle=None):
        self.videos = list(videos or [])
        self.error = error
        self.complete = complete
        self.warning = warning
        self.needs_verification = needs_verification
        self.cancelled = cancelled
        self.avatar = avatar
        self.profile_stats = dict(profile_stats or {})
        self.by_handle = dict(by_handle or {})
        self.calls = []

    def discover(self, creator, newest_only=False):
        handle = creator.get("handle") if isinstance(creator, dict) else str(creator)
        self.calls.append({"handle": handle, "newestOnly": bool(newest_only)})
        if self.error:
            return DiscoveryResult(ok=False, error=self.error, videos=[],
                                   needs_verification=self.needs_verification)
        videos = self.by_handle.get(handle, self.videos)
        return DiscoveryResult(ok=True, videos=list(videos), complete=self.complete,
                               warning=self.warning, needs_verification=self.needs_verification,
                               cancelled=self.cancelled, avatar=self.avatar,
                               profile_stats=dict(self.profile_stats))


class DownloaderCandidateSource(CandidateSource):
    """把现有下载器的 `recognize()` 包成 Collector 的候选项来源。

    这是「复用下载器能力」的唯一接口点：
    - 下载器为 None / 没有 recognize（例如精简环境）→ 返回明确的失败原因，
      而不是假装成功；
    - `recognize()` 抛异常 → 包成 failed 结果（采集失败必须留下记录）；
    - `ok=False` 的返回原样透传（例如需要安全验证、读取被限制）。
    """

    name = "downloader"

    def __init__(self, downloader, url_for=None):
        self._downloader = downloader
        self._url_for = url_for or (lambda creator: profile_url(
            creator.get("handle") if isinstance(creator, dict) else creator))

    def discover(self, creator, newest_only=False):
        reader = getattr(self._downloader, "recognize", None)
        url = self._url_for(creator)
        if not callable(reader):
            return DiscoveryResult(ok=False, error="当前下载器不支持读取创作者主页（缺少 recognize）")
        if not url:
            return DiscoveryResult(ok=False, error="创作者缺少 handle，无法读取主页")
        try:
            payload = reader(url)
        except Exception as exc:                            # 浏览器/网络/锁 异常都收在这里
            return DiscoveryResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        if not isinstance(payload, dict):
            return DiscoveryResult(ok=False, error="下载器返回了无法识别的结果")
        if not payload.get("ok"):
            return DiscoveryResult(
                ok=False,
                error=str(payload.get("error") or "读取创作者主页失败"),
                needs_verification=bool(payload.get("needsVerification")),
                cancelled=bool(payload.get("cancelled")))
        return DiscoveryResult(
            ok=True,
            videos=list(payload.get("videos") or []),
            warning=str(payload.get("warning") or ""),
            complete=bool(payload.get("complete")),
            needs_verification=bool(payload.get("needsVerification")),
            cancelled=bool(payload.get("cancelled")),
            cached=bool(payload.get("cached")),
            avatar=str(payload.get("avatar") or ""),
            profile_stats=dict(payload.get("profileStats") or {}),
            raw=payload)
