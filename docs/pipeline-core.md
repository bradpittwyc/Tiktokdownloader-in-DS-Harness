# 任务编排内核（Pipeline Core）

> 分支：`feature/content-factory-pipeline-core`
> 代码：`outputs/TikTokBatchMVP/content_factory/{jobs,queue,retry,orchestrator}.py`
> 测试：`tests/test_job_queue.py`、`tests/test_orchestrator.py`

这一层负责**让流水线的每个阶段按状态可靠执行**：排队、并发、失败重试、崩溃恢复、事件。
它**不**负责下载、ASR、AI 调用本身 —— 那些是通过适配器注入进来的。

---

## 一、为什么要有这一层

在它之前，「批量分析」的待办是 `ContentPipeline._queue` 这个**进程内列表**：

| 问题 | 后果 |
| --- | --- |
| 待办只在内存 | 用户排了 200 条，程序一关全丢 |
| 没有「谁正在跑」的记录 | 重启后无法知道哪些是中途断掉的，内容永远卡在「分析中」 |
| 没有并发保护 | 连点两次「批量分析」会给同一条内容排两个任务 |
| 没有重试策略 | 一次网络抖动就等于一条内容永久失败 |

现在这些都由 sqlite 里的 `jobs` 表 + 一份明确的状态机承担。

---

## 二、分层

```
jobs.py          Job 模型 + jobs 表（持久化，不管策略）
retry.py         重试策略：次数 / 指数退避 / 可重试判定（纯函数）
queue.py         队列：入队、去重、认领、完成/失败、恢复、并发执行、事件
orchestrator.py  编排：把队列接到真实阶段（下载 / 转写 / AI 标注）上，并同步内容状态
```

依赖方向是单向的：`orchestrator → queue → (jobs, retry)`。
`queue` 不认识内容库、界面、模型 API，所以它可以被单测直接怼。

---

## 三、Job 契约

### 状态机

```
pending ──▶ queued ──▶ running ──▶ done
              ▲           │
              │           ├── 还有额度 ──▶ retrying ──(退避到点)──▶ running
              │           └── 额度用尽 ──▶ failed
              └──── 恢复 / 重排 ──────────┘
                          cancelled（人为中止，终态）
```

- `pending` 还没到可执行时间（延迟入队）；
- `queued` 已就绪，可被 worker 认领；
- `running` 已被认领（带租约 `lease_owner` / `lease_expires_at`）；
- `retrying` 失败后等退避，到点后重新可认领；
- `done` / `failed` / `cancelled` 是终态。
- **活跃状态** = pending / queued / running / retrying。

### 字段

| 字段 | 说明 |
| --- | --- |
| `id` | `job_xxxxxxxxxxxx` |
| `seq` | 入队序号，FIFO 就靠它（在插入事务里取号，并发入队也不会错乱） |
| `kind` | 阶段名：`download` / `transcript` / `enrich`（队列不限制取值，编排层校验） |
| `content_id` | 对应的 `content_items.id` |
| `dedupe_key` | 默认 `kind:content_id`；活跃任务里这个键唯一 |
| `state` / `priority` / `attempts` / `max_attempts` | 调度用 |
| `payload` | 自由 JSON（编排层放 `chain`：跑完这一步接谁） |
| `result` / `error` | 结果与失败原因 |
| `available_at` | 到点才可被认领（退避用） |
| `lease_owner` / `lease_expires_at` / `run_token` | 租约与「哪次进程运行」的标记 |
| `created_at` / `updated_at` / `started_at` / `finished_at` | 时间戳 |

**attempts 语义**：认领任务时 +1，不是完成时。这样进程被 kill 也算一次尝试，
一条能把程序搞崩的任务不会变成无限重启的循环。`max_attempts=3` = 首次 + 两次重试。

---

## 四、Queue 契约

```python
queue = JobQueue(path=..., policies={...}, emit=...)

queue.enqueue(kind, content_id, payload=None, priority=0,
              max_attempts=None, dedupe_key=None, delay=0.0)
    -> {"ok", "created", "duplicate", "job"}      # 已有同阶段活跃任务时 created=False

queue.enqueue_many(requests) -> {"created", "duplicates", "rejected", "jobs"}
queue.claim(worker_id=None, kinds=None)          -> Job | None    # 原子认领并置 running
queue.finish(job, result=None, message="")       -> Job           # 置 done
queue.fail(job, error="", permanent=False)       -> {"job", "retry", "delay", "decision"}
queue.cancel(job_or_id, reason="")               -> Job
queue.requeue(job_or_id, delay=0.0, error="")    -> Job
queue.heartbeat(job, seconds=None)               -> Job           # 长任务续租
queue.recover(now=None)                          -> {"recovered", "abandoned", "jobs"}
queue.counts() / state_counts() / jobs(...) / status() / is_idle() / wait_for_idle(...)
queue.close()
```

### 三条硬保证

1. **同一内容的同一阶段不会重复排队**
   数据库层面用部分唯一索引兜底：
   `CREATE UNIQUE INDEX idx_jobs_active_dedupe ON jobs(dedupe_key) WHERE state IN (活跃状态)`。
   连点两次、两个线程同时入队、甚至两个进程同时入队，都只会有一条。

2. **同一条内容不会同时跑两个阶段**
   认领 SQL 里带 `NOT EXISTS(... o.state='running' AND o.content_id = j.content_id)`。
   下载还没跑完，转写不会被认领。

3. **并发上限 = worker 线程数**
   认领是 `BEGIN IMMEDIATE` 事务里的「先选后改」，不会因为两个线程同时抢而超发。

### 调度顺序

`ORDER BY priority DESC, seq ASC` —— 高优先级插队，同优先级严格 FIFO。

---

## 五、Retry 契约

```python
RetryPolicy(max_attempts=3, base_delay=0.5, factor=2.0, max_delay=60.0,
            jitter=0.0, retry_on=(), no_retry_on=())

policy.allows(attempts)                  # 还有额度吗
policy.delay_for(attempts)               # 退避：min(max_delay, base * factor**(attempts-1))
policy.decide(attempts, error=None)      # -> RetryDecision(retry, delay, reason, ...)
```

- **永久失败**：抛 `NonRetryableError`、或返回 `{"ok": False, "permanent": True}`、
  或失败原因命中 `orchestrator.PERMANENT_MARKERS`（没配 API Key、内容不存在、
  没装 ASR 后端、媒体还没下载……）→ 直接 `failed`，不消耗重试次数。
  这条很重要：不然「没配 Key」会被重试三次，用户等半天还是失败。
- 每个阶段一套默认策略（下载 3 次 / 转写 2 次 / 标注 3 次），
  可通过 `settings` 的可选 `jobs` 分区覆盖，不需要改动设置页契约：

```json
{ "jobs": { "base_delay": 1, "enrich": { "max_attempts": 5 } } }
```

---

## 六、恢复语义

`queue.recover()` 处理所有「认领了但没人管」的 running 任务：

| 情况 | 判定 | 处理 |
| --- | --- | --- |
| 上次进程留下的（`run_token` 不是本次运行） | 孤儿 | 还有额度 → 重新排队；额度用尽 → `failed`（写明是异常退出） |
| 同一次运行但租约过期 | worker 卡死/被杀 | 同上 |
| 队列里还是 queued / retrying 的 | 正常待办 | 不动，worker 起来自然接手 |

worker **执行期间会自动续租**（`_Heartbeat`，每 1/3 租约续一次），
所以跑很久的正常任务不会被自己的租约误伤 —— 否则就会出现「同一条内容被两个
worker 同时跑」，正是需求里明确要避免的事。

编排层订阅 `queueRecovered` 事件，把这些任务对应的内容状态一起收回来，
所以重启后不会有内容永远停在「分析中」。

---

## 七、事件契约

| 事件 | 时机 |
| --- | --- |
| `jobQueued` | 入队 / 重排 / 恢复 |
| `jobStarted` | 被认领（含第几次尝试） |
| `jobRetrying` | 失败但还能重试（带 delay、nextAttempt） |
| `jobFailed` | 终态失败 |
| `jobSucceeded` | 成功（带 result） |
| `jobCancelled` | 被取消 |
| `jobProgress` | 通用播报（不改状态） |
| `queueRecovered` | 启动恢复的汇总 |
| `queueIdle` | 队列空了 |

载荷（字段名与既有 `enrichProgress` 尽量对齐，方便以后做映射）：

```json
{ "event": "jobSucceeded", "jobId": "job_ab12", "id": "<contentId>", "contentId": "...",
  "kind": "enrich", "stage": "enrich", "state": "done", "stateLabel": "已完成",
  "attempt": 1, "maxAttempts": 3, "retriesLeft": 2, "priority": 0,
  "payload": {"chain": []}, "result": {...}, "message": "...", "error": "",
  "queue": {"queued": 1, "running": 0, "done": 4, "...": 0}, "at": 1770000000.0 }
```

事件发射**绝不抛异常**：界面/日志出问题不能把任务搞挂（沿用 `pipeline._report` 的教训）。

---

## 八、阶段与内容状态的映射

编排层把任务状态翻译回既有内容表的列，**不另造状态机**：

| 任务状态 | `download_status` | `transcript_status` | `ai_status` |
| --- | --- | --- | --- |
| pending / queued / retrying | `pending` | `pending` | `queued` |
| running | `running` | `running` | `running` |
| done | `done` | `done` | `done` |
| failed | `failed` | `failed` | `failed` |
| cancelled | `pending` | `pending` | `pending` |

（既有数据模型里只有 AI 阶段有 `queued`，下载/转写是 `pending -> running -> done|failed`。
这里顺着现状走，否则界面上的筛选立刻错位。）

写入是**事件驱动**的：任务表先写成功，事件才发出来，编排层再改内容表 ——
所以不会出现「任务被取消了，内容却被 handler 写成 done」这种互相矛盾的状态。
同步时会跳过没有变化的字段（少写库、也不搅乱界面的「最近更新」排序）。

链路：`discovered → download → transcript → enrich → ready`。
`submit_item()` 只入队第一个缺的阶段，并在 `payload["chain"]` 里写下剩下的；
每一步成功后自动接下一步，跳过已经完成的阶段。链路状态随任务持久化，重启不断。

编排放到队列上的入口：

| 方法 | 用途 |
| --- | --- |
| `submit_item(content_id, stages=None, priority=0, restart=False)` | 按内容当前状态排「还差哪些阶段」 |
| `submit_many(content_ids, ...)` | 批量，单条失败不影响其它 |
| `resubmit_failed(stages=("enrich",), limit=50)` | 把失败的内容重新排队（异常处理页的「重试」） |
| `run_once()` / `run_until_idle(timeout)` | 手动步进 / 等队列跑空 |
| `start()` / `stop()` / `close()` | 生命周期（`start` 内部先 `recover()` 再起 worker） |
| `status()` / `counts()` / `job(job_id)` / `content_jobs(content_id)` | 给界面看的运行状态 |

---

## 九、集成点（给 content_bridge / web_app 的接口需求）

本分支**没有**改动 `content_bridge.py` / `web_app.py` / `ui/app.js` / `ai.py` /
`transcript.py` / `factory_store.py` / `pipeline.py`。接入只需要三处：

**1. 构造（content_bridge.Api.__init__）**

```python
from content_factory.orchestrator import build_orchestrator

self.orchestrator = build_orchestrator(
    store=self._store, settings=self._settings, pipeline=self._pipeline,
    downloader=None,                 # 有下载器时注入（见下）
    emit=self._record_event,         # 直接复用既有事件队列，界面 subscribe 即可
    autostart=True,                  # 内部会先 recover() 再起 worker
)
```

**2. 桥接方法（新增可选，不改既有 `content_enrich*` 的行为）**

```python
def content_job_submit(self, item_id, stages=None, restart=False):
    return self._orchestrator.submit_item(item_id, stages=stages, restart=restart)

def content_job_status(self, job_id=""):
    return self._orchestrator.job(job_id) if job_id else self._orchestrator.status()

def content_job_cancel(self, job_id):
    return (self._orchestrator.queue.cancel(job_id) or {}) and {"ok": True}

def content_job_retry_failed(self, stages=None, limit=50):
    return self._orchestrator.resubmit_failed(stages=stages or ("enrich",), limit=limit)
```

**3. 下载适配器（collector / downloader 分支提供）**

```python
def download_job(job, item):          # 可调用对象，或带 download_job(job, item) 的对象
    ...
    return {"ok": True,
            "localVideoPath": "...", "localSubtitlePath": "...",   # 会回写内容表
            "duration": 37}
orchestrator.set_downloader(adapter)
```

**界面事件**：`self._record_event` 收到的 `job*` 事件已经是 JSON 友好的，
`ui/app.js` 只需要多订阅几个事件名。如果不想改界面，可以让编排器发兼容事件：

```python
build_orchestrator(..., legacy_events=True)   # 额外发 enrichProgress 形状的事件
```

默认关闭 —— 因为既有 `ContentPipeline` 自己已经在发 `enrichProgress`，
两边一起发会让界面收到重复状态。**接线时二选一。**

**退出**：`orchestrator.close()`（停 worker + 释放任务表连接）。

---

## 十、怎么跑 / 怎么测

```powershell
# 只跑编排内核（快，全部离线）
python -m unittest tests.test_job_queue tests.test_orchestrator

# 全量（含既有 309 个）
python -m unittest discover -s tests -t tests
```

覆盖到的行为：FIFO、优先级、成功、失败、退避重试、达到 max_attempts 后停止、
程序重启后 queued 任务继续、running 孤儿任务的回收与放弃、重复任务防护、
内容级互斥、并发上限、长任务续租、事件序列、内容状态映射、链路自动接续。

---

## 十一、已知取舍 / 后续建议

1. **任务表与内容库同一个 sqlite 文件**（`content-factory.db`），备份一个文件就够。
   任务表用 WAL + `synchronous=NORMAL`（应用崩溃不丢，掉电可能丢最后几条事务 ——
   队列本身有恢复机制）。**每次操作复用一条长连接**：实测慢盘上「开连接+提交+关连接」
   一次要 1.2s，复用后 20ms。`FactoryStore` 目前仍是每次操作开关连接，
   在慢盘上每次内容状态更新约 1.5s —— 建议后续把它也改成常驻连接（收益很大，
   但那是共享文件，本分支没有擅自改）。
2. **没有跨内容的全局限流**（比如「每分钟最多 20 次 AI 调用」）。现在的限流粒度是
   并发数。要加的话建议在 `WorkerPool._execute` 前面挂一个令牌桶，接口已经留好。
3. **取消不中断正在执行的那一次**：`cancel()` 把任务置为终态，正在跑的 handler
   结果会被丢弃（`finish`/`fail` 只接受 running 状态）。要真中断需要 handler 自己
   看取消标记（既有 `ContentPipeline.cancel` 就是这种模式）。
4. **`legacy_events` 只实现了 enrich 阶段**的兼容事件；如果界面也要下载/转写的
   进度，按同样的映射扩一行即可。
