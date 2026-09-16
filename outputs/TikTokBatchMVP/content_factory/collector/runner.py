"""常驻轮询器：为 24/7 内容工厂留的最小接口。

这一版**只**做一个本地后台线程定时调用 `collector.tick()`：
没有云调度、没有分布式锁、没有 cron 表达式 —— 那些是后面阶段的事。
之所以现在就把接口定下来：界面上的「自动采集 / 24 小时运行」开关需要一个
真实可调用的东西，而不是一个永远返回 true 的假开关。

    runner = CollectorRunner(collector, interval=60)
    runner.start()      # 后台线程，随进程退出而结束（daemon）
    runner.status()     # {running, interval, rounds, lastTickAt, lastResult}
    runner.stop()

`interval` 是**轮询器自己的心跳**（多久看一次有没有到点的 Creator），
不是 Creator 的采集间隔 —— 后者由 CreatorMonitorService 的 next_check_at 决定。
"""

import threading
import time


class CollectorRunner:
    def __init__(self, collector, interval=60, limit=None, emit=None, on_round=None):
        self.collector = collector
        self.interval = max(5, int(interval or 60))
        self.limit = limit
        self._emit = emit or (lambda *_args, **_kwargs: None)
        self._on_round = on_round
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._rounds = 0
        self._last_at = ""
        self._last_result = None

    # ---- 生命周期 ------------------------------------------------------
    @classmethod
    def from_settings(cls, collector, settings, **kwargs):
        """心跳间隔取自「设置 → 工作模式」的 task_interval_sec（分钟级换算在调用方）。

        work_mode.mode 为 manual 时不自动跑 —— 这里只是把设置翻译成参数，
        是否 start() 由调用方（界面 / 启动流程）决定。
        """
        interval = 60
        if settings is not None:
            try:
                interval = int(settings.get("work_mode", "task_interval_sec") or 60)
            except (TypeError, ValueError):
                interval = 60
        return cls(collector, interval=interval, **kwargs)

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        with self._lock:
            if self.running:
                return {"ok": True, "running": True, "message": "已经在运行"}
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="ContentCollectorRunner")
            self._thread.start()
        self._report({"state": "started", "interval": self.interval})
        return {"ok": True, "running": True, "interval": self.interval}

    def stop(self, timeout=30):
        """停止并等待本轮跑完。

        默认等 30 秒而不是 5 秒：一轮 tick 里可能有真实抓取（几十秒），
        提前返回会让调用方以为已经停了，实际后台还在写库。
        """
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            stopped = not (thread and thread.is_alive())
            if stopped:
                self._thread = None
        self._report({"state": "stopped", "rounds": self._rounds, "stopped": stopped})
        return {"ok": True, "running": self.running, "rounds": self._rounds}

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:                      # 一轮失败不能让常驻循环死掉
                self._report({"state": "failed", "error": f"{type(exc).__name__}: {exc}"})
            self._stop.wait(self.interval)

    # ---- 单轮 ----------------------------------------------------------
    def run_once(self, force=False):
        result = self.collector.tick(limit=self.limit, force=force, trigger="runner")
        with self._lock:
            self._rounds += 1
            self._last_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self._last_result = {key: value for key, value in result.items() if key != "results"}
        self._report({"state": "tick", "rounds": self._rounds, "result": self._last_result})
        if callable(self._on_round):
            try:
                self._on_round(result)
            except Exception:
                pass
        return result

    def _report(self, payload):
        try:
            self._emit("collectRunner", payload)
        except Exception:
            pass

    def status(self):
        with self._lock:
            return {"running": self.running, "interval": self.interval, "rounds": self._rounds,
                    "lastTickAt": self._last_at, "lastResult": self._last_result,
                    "limit": self.limit}
