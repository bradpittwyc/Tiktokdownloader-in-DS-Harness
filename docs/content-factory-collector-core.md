# 自动采集核心（Creator Monitor + Collector）

> 分支：`feature/content-factory-collector-core`
> 基线：`78f75fe8f5f28cece4b512585af192a3f86011b3`
> 范围：**内核**（创作者监控 + 自动采集判断 + 任务排队），不含复杂 UI。

---

## 一、这条链路做了什么

```
Creator 配置（creators 表，沿用原 contract）
   │  CreatorMonitorService：增删改查 / 唯一 handle / 启用停用 / poll_interval / priority
   ▼
定期检查：due_creators() → poll_creator(creator)         ← 到点了吗 / 上次结果如何
   │  带租约的抢占（同一 Creator 不会被并发检查两次）
   ▼
候选发现：source.discover() → Api.recognize()             ← 复用下载器，零复制
   │  归一化成 CandidateVideo
   ▼
去重：source_video_id / source_url / content_key 三层      ← 批次内 + 跨批次
   ▼
判断：DownloadPolicy.decide()                             ← 库里已有？队列已有？规则过滤？
   ▼
排队：collection_jobs（部分唯一索引兜底，同一内容不会有两条未完成任务）
   ▼
交给已有下载器：job_to_downloader_video() → Api.download() ← 不新协议、不复制下载器
   ▼
收尾：reconcile() 读内容库实际结果 → job done / failed     ← 入库仍走原 content_register_download
```

**职责边界**：Collector 只做「发现 + 判断 + 排队」；下载器继续只做「下载」。
抓取复用 `Api.recognize()`，下载复用 `Api.download()`，入库复用 `content_register_download`，
一个字节的下载/抓取逻辑都没有复制。

---

## 二、新增文件

```
outputs/TikTokBatchMVP/content_factory/
  creator_monitor/                 创作者监控（纯本地服务，不 import 下载器）
    __init__.py                    对外只暴露 CreatorMonitorService
    intervals.py                   poll_interval 解析 / 优先级权重 / 时间戳换算（纯函数）
    store.py                       creator_monitor_state 表 + creators 契约列的受限回写
    service.py                     Creator CRUD / 启用停用 / 到期计算 / 检查状态 / 抢占
  collector/                       自动采集内核
    __init__.py                    build_collector / download_jobs / CollectorRunner …
    models.py                      CandidateVideo、归一化、标准 job ↔ 下载器形状互转
    dedupe.py                      URL 归一化 + content_key + 批次去重表
    policy.py                      DownloadPolicy（要不要下载）与原因文案
    sources.py                     CandidateSource 接口 + 下载器适配 + 静态来源
    store.py                       collection_jobs / collection_runs 两张表
    service.py                     ContentCollector（poll / tick / plan / claim / reconcile）
    handoff.py                     job → Api.download() → 按结果收尾（唯一的下载接缝）
    runner.py                      CollectorRunner：本地常驻轮询（24/7 预留接口）
    bridge_api.py                  CollectorApi：给 content_bridge.py 一行转发用的实现
tests/
  test_creator_monitor.py          32 个
  test_collector_core.py           54 个
  test_collector_bridge_api.py      8 个
```

**没有改动**：`web_app.py` / `content_bridge.py` / `ui/*` / `pipeline.py` / `factory_store.py` /
`ai_enrichment.py` / `transcript.py` / `settings_store.py`。

---

## 三、数据模型（新增两张表，不动原有三张表）

```sql
-- 监控状态：与 creators 一对一，Creator contract 一个字段都没改
creator_monitor_state(
    creator_id PK, enabled, poll_interval_seconds,
    last_check_at, next_check_at, last_state, last_error,
    last_discovered, last_queued, consecutive_failures,
    total_checks, total_queued, running_since, run_owner, created_at, updated_at)

-- 采集任务：一个内容一条任务
collection_jobs(
    id PK, batch_id, creator_id, creator_handle, source_type, source_video_id,
    source_url, content_key, title, description, cover, duration, kind, upload_date,
    priority, state, attempts, max_attempts, content_item_id, last_error,
    created_at, updated_at, scheduled_at, finished_at)
-- 关键：同一 content_key 只允许存在一条未完成任务
CREATE UNIQUE INDEX idx_collection_jobs_active
    ON collection_jobs(content_key) WHERE state IN ('pending','running');

-- 每次检查一条记录（成功 / 部分 / 失败 / 忙碌 / 跳过 都要留痕）
collection_runs(id PK, batch_id, creator_id, creator_handle, trigger, state,
    started_at, finished_at, discovered, fresh, queued, duplicates, filtered,
    existing, error, message, warning, needs_verification, complete)
```

- job 状态机：`pending → running → done | failed`，另有 `skipped` / `cancelled`。
- run 状态：`success | partial | failed | busy | skipped`。
- 存储位置不变：`%LOCALAPPDATA%/TikTokBatchMVP/content-factory.db`（与内容库同库）。

---

## 四、公开接口（服务层）

### CreatorMonitorService（`content_factory.creator_monitor`）

```python
create_creator(handle, **fields)      # handle 唯一（忽略大小写）；返回 {ok, creatorId, creator}
update_creator(creator_id, **fields)  # 可改 handle（重查唯一）/ 展示字段 / poll_interval
delete_creator(creator_id)            # 同时取消该创作者未完成的任务
creator(id) / creator_by_handle(handle) / creators(enabled, search, category, status, due_only, limit)
enable(id) / disable(id) / set_enabled(id, bool)
set_poll_interval(id, "30 分钟")      # 同时写 contract 文本与秒数
set_priority(id, "高")
state(id)                             # 监控状态原始行
due_creators(limit, now)              # 到点该检查的（高→中→低，再按最久没查）
mark_checked(id, state, error, discovered, queued)   # 记录结果 + 排下一次（失败退避 ×2，封顶 6h）
begin_check(id, owner, lease_seconds) / end_check(id, owner) / is_checking(id)
expire_leases(lease_seconds)          # 清掉进程崩溃留下的死租约
stats()                               # total/enabled/disabled/due/checking/failing
```

### ContentCollector（`content_factory.collector`）

```python
poll_creator(creator_id, trigger, newest_only, force)   # 检查一个创作者（同步，不抛异常）
check_now(creator_id)                                   # 立即检查（忽略排期，仍受并发保护）
tick(limit, force, trigger)                             # 一轮：把所有到点的创作者检查一遍
plan(limit, creator_id)                                 # 待办任务 → 下载器形状（不改状态）
claim(limit, creator_id)                                # 同上，但标记 running（attempts+1）
mark_running / mark_done / mark_failed                  # 执行方回写
reconcile(limit)                                        # 按内容库实际结果收尾任务
recover_stale(seconds)                                  # running 卡太久 → 退回 pending
status() / jobs_view() / runs_view() / failures_view()
```

### 模块级工具

```python
build_collector(store, downloader=None, settings=None, source=None, emit=None)
download_jobs(collector, downloader, jobs=None, folder="", quality="", limit=20, ...)
CollectorRunner(collector, interval=60)   # start() / stop() / run_once() / status()
CollectorApi(store, settings, downloader, emit)   # 桥接门面（content_* 方法）
```

### 事件（emit 回调 / 桥接层转发给界面）

| 事件 | 载荷 |
|---|---|
| `collectProgress` | `{creatorId, handle, state(running/done/partial/failed), message, summary, runId, nextCheckAt}` |
| `collectJob` | `{creatorId, handle, state(queued), job, contentKey, title}` |
| `collectRunner` | `{state(started/stopped/tick/failed), interval, rounds, result}` |

---

## 五、Integration Agent 需要做的连接（都不在本分支范围内）

### 1. `content_bridge.py`：一行转发（实现已在 `collector/bridge_api.py`）

```python
from content_factory.collector.bridge_api import CollectorApi
...
# __init__ 里：
self.bridge_collector = _Hidden(CollectorApi(
    store=unwrap(self.bridge_store), settings=unwrap(self.bridge_settings),
    downloader=downloader, emit=self._record_event))
...
# 类体里逐个加（名字必须写在类体里，见下面的坑）：
def content_creator_list(self, enabled=None, search="", due_only=False, limit=200):
    return unwrap(self.bridge_collector).content_creator_list(enabled, search, due_only, limit)
def content_creator_save(self, values=None): ...
def content_creator_delete(self, creator_id): ...
def content_creator_toggle(self, creator_id, enabled=True): ...
def content_creator_set_interval(self, creator_id, value): ...
def content_creator_set_priority(self, creator_id, value): ...
def content_creator_check_now(self, creator_id, background=True): ...
def content_collect_tick(self, force=False, limit=None): ...
def content_collector_status(self): ...
def content_collection_jobs(self, state="all", creator_id="", limit=100): ...
def content_collection_runs(self, creator_id="", state="all", limit=100): ...
def content_collection_failures(self, limit=50): ...
def content_collect_download(self, limit=10, folder="", quality="", background=True): ...
def content_collector_start(self, interval=None, limit=None): ...
def content_collector_stop(self): ...
```

⚠️ **坑**：`ContentFactoryApi.__dir__` 只列 `vars(type(self))` 里的名字，
所以**不能用 mixin 继承**加方法 —— 继承来的方法不会出现在 `dir()` 里，pywebview 发现不了。
必须在类体里显式写这一行行转发（`test_collector_bridge_api.py` 锁住了这 15 个名字）。

### 2. `web_app.py`：启动时清死租约 / 可选启动常驻采集

```python
# Api.__init__ 或窗口创建之后（一行即可）：
self.content_factory.content_collector_start()   # 仅在「工作模式 = auto24 / timed」时
```

死租约已经在 `CollectorApi.__init__` 里自动清理，不需要额外调用。

### 3. `ui/app.js`：创作者监控页 / 自动采集页接真实数据

- 创作者列表：`content_creator_list()` → 每条含 `enabled / is_due / next_check_at /
  poll_interval_text / last_state / last_error / consecutive_failures / item_count`。
- 新增/编辑：`content_creator_save({handle, display_name, category, priority, poll_interval})`。
- 启停：`content_creator_toggle(id, enabled)`；立即检查：`content_creator_check_now(id)`。
- 采集看板：`content_collector_status()` / `content_collection_jobs("pending")` /
  `content_collection_runs()` / `content_collection_failures()`。
- 实时刷新：轮询 `content_events(since)` 里的 `collectProgress` / `collectJob`。

### 4. 与 Pipeline / 下载器队列的关系

采集任务（collection_jobs）只是「**待下载**」队列；下载完成后仍然由下载器写
`content_items`，之后才是 pipeline 的转写 / AI 标注。两套队列不要合并：
Collector 的 job 在 `reconcile()` 之后会自动收敛成 `done`。

---

## 六、怎么跑 / 怎么测

```powershell
python -m unittest tests.test_creator_monitor tests.test_collector_core tests.test_collector_bridge_api
```

96 个新测试全部离线（浏览器、网络、yt-dlp 都是替身）；其中一个用例用**真实**的
`Api.download`（只替换 yt-dlp）跑通「采集任务 → 下载 → 入库 → 任务收尾」。

---

## 七、这一版刻意没做

云端调度、分布式锁、cron 表达式、多平台来源（YouTube / 小红书）、
指纹/音频级查重（`settings.collect` 里的 dedupe_method 仍是偏好项）、
采集页面的完整 UI。接口都留好了：换调度只需换 `CollectorRunner`，
接新平台只需再写一个 `CandidateSource`。
