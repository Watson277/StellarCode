# StellarCode 上下文压缩死锁故障复盘

## 1. 故障摘要

StellarCode Desktop 在执行一个较长任务时，成功生成并写入文件后触发了上下文压缩。
压缩摘要的 LLM 请求已经返回，但任务长时间没有继续执行，直到用户点击 `Stop` 后才完成
取消流程。

本次故障不是两个操作系统进程之间的死锁，而是同一个 Python Sidecar 进程中的两个线程
互相等待：

- `stellarcode-runtime-task`：运行 Agent、维护消息历史并执行上下文压缩；
- `stellarcode-cancellable-call`：执行同步 LLM HTTP 请求，并在响应后记录 Token Usage。

根因是 `ConversationHistoryCompactor.maybe_compact()` 在持有压缩器锁的情况下等待 LLM
线程结束；LLM 线程返回摘要后又通过 Usage 持久化路径读取 `history_snapshot()`，而该读取
需要获得同一个压缩器锁。两个线程因此形成循环等待。

## 2. 影响范围

满足以下条件时可能触发：

1. 当前 Agent 历史达到上下文压缩阈值；
2. Runtime 为模型请求提供了 `cancellation_event`，因此同步 LLM 请求被放到独立线程；
3. 模型返回后，`TracingChatClient` 同步执行 Token Usage 回调；
4. Usage 回调保存完整对话，其中包含 `history_snapshot()`。

故障期间已经完成的工具操作不会自动回滚。例如本次任务中的 `yolo.py` 已成功写入，但
Agent 没有继续进行语法检查、运行验证和最终回复。

## 3. 现场日志证据

用户任务：

```text
帮我写一个yolo模型在这个项目下
```

实际任务记录位于：

```text
E:\study2\LLM Internship\pro1\.stellarcode\traces\
session-20260809-195545-4f1cbb27.jsonl
```

关键时间线：

| 本地时间 | 事件 | 说明 |
| --- | --- | --- |
| 19:56:46 | `task.started` | YOLO 任务开始 |
| 19:56:53 | 首轮 `llm_response` | 模型决定读取目录和 `cnn.py` |
| 20:00:09 | 第二轮 `llm_response` | 约 196 秒后生成 `write_file` 调用 |
| 20:00:09 | `tool.completed: write_file` | 成功写入 `yolo.py`，20,896 个字符 |
| 20:00:09 | `history.compaction.started` | 达到 25,000 Token 上下文配置的压缩阈值 |
| 20:00:29 | 压缩 `llm_response` | 摘要模型已返回，耗时约 19 秒 |
| 20:00:29—20:31:02 | 无后续事件 | 模型已返回，但 Usage 回调无法完成 |
| 20:31:02 | `task.cancel` | 用户点击 Stop |
| 20:31:02 | `history.compaction.finished` | Runtime 退出等待并释放压缩锁 |
| 20:31:02 | `usage.updated` | 被阻塞的模型线程取得锁后完成 Usage 保存 |
| 20:31:02 | `task.cancelled` | 任务取消完成 |

`llm_response` 已出现但 `usage.updated` 延迟到取消后才出现，是定位故障区间的关键证据。
这说明网络请求已经结束，阻塞发生在记录响应之后、模型工作线程返回之前。

用户最初提供的 `session-20260809-203105-bcbd5a13.jsonl` 只有 `session_start`、
`session.opened` 和 `session.snapshot` 三条记录。它是在任务取消后重新打开会话时创建的，
任务的真实执行链位于前一个 Trace 文件。

## 4. 相关线程和创建关系

Tauri 首先启动 Python Sidecar 进程：

```text
Tauri desktop.exe
└── Python Sidecar python.exe
    ├── MainThread
    ├── stellarcode-runtime-task
    └── stellarcode-cancellable-call
```

### 4.1 Sidecar MainThread

Sidecar 主线程读取 Tauri 发送的 JSONL 请求。收到 `task.submit` 后，它在
`src/stellarcode/runtime/sidecar.py` 中创建 `stellarcode-runtime-task`：

```python
threading.Thread(
    target=self._run_task,
    args=(runtime, session_id, task_id, prompt, attachments),
    name="stellarcode-runtime-task",
    daemon=True,
).start()
```

任务没有直接运行在 Sidecar 主线程中，因此主线程在 Agent 忙碌或卡住时仍能接收
`task.cancel` 和 `approval.resolve`。

### 4.2 Runtime 任务线程

`stellarcode-runtime-task` 负责：

- 执行 ReAct/Plan/Team 任务；
- 调用工具；
- 维护 Agent 消息；
- 判断是否需要压缩上下文；
- 触发摘要 LLM 请求；
- 生成最终回答。

### 4.3 模型工作线程

Runtime 调用同步模型客户端时，`src/stellarcode/cancellation.py` 中的
`cancellable_call()` 创建 `stellarcode-cancellable-call`：

```python
threading.Thread(
    target=lambda: context.run(invoke),
    name="stellarcode-cancellable-call",
    daemon=True,
).start()
```

模型调用在工作线程中执行，Runtime 任务线程每隔约 50ms 检查一次取消事件。这样用户可以
在同步 HTTP 请求仍未返回时点击 Stop，而不必等待网络超时。

## 5. 死锁是怎样产生的

### 5.1 错误的锁范围

旧版 `maybe_compact()` 使用一个 `RLock` 包住整个压缩流程：

```python
def maybe_compact(...):
    with self._lock:
        # 检查阈值、整理历史
        summary = self._summarize(...)
        # 更新摘要和压缩次数
```

`_summarize()` 内部会调用 `cancellable_call()`，启动模型工作线程，然后等待该线程彻底
结束。由于整个 `maybe_compact()` 仍处于 `with self._lock` 中，Runtime 任务线程在等待
模型时一直持有压缩器锁。

### 5.2 模型返回后的同步 Usage 回调

模型工作线程中的调用顺序是：

```text
client.chat()
→ 记录 llm_response
→ usage_callback(...)
→ 返回 ChatResult
→ 模型线程结束
```

Usage 回调进入 `RuntimeSession._record_usage()`。为了让统计数据跨重启持久化，旧逻辑会
立即调用 `_save_conversation()` 保存整份对话：

```text
TracingChatClient.usage_callback
→ RuntimeSession._record_usage
→ RuntimeSession._save_conversation
→ conversation.agent.history_snapshot
→ conversation.agent.history_compactor.snapshot
→ with self._lock
```

`snapshot()` 的锁本身是合理的，它保证 `summary`、`compaction_count` 和
`last_compacted_at` 不会被读取成互相不一致的组合。问题是另一个线程持有该锁等待当前
模型线程结束。

### 5.3 循环等待

最终等待关系为：

```text
Runtime任务线程：
  持有 compactor._lock
  等待模型工作线程完成

模型工作线程：
  已经获得LLM摘要
  正在执行Usage持久化
  等待 compactor._lock
```

即：

```text
Runtime任务线程等待模型线程
          ↑              ↓
模型线程等待Runtime持有的压缩锁
```

两个必要条件同时成立：

1. 双方各自持有或等待对方完成所需的资源；
2. 没有任何一方能够自行继续执行并释放条件。

因此形成死锁。

### 5.4 为什么 `RLock` 没有解决

`threading.RLock` 只允许持锁的**同一个线程**重复进入：

```text
Runtime线程持锁 → Runtime线程再次加锁：允许
Runtime线程持锁 → 模型线程申请该锁：阻塞
```

这次第二次申请来自另一个线程，因此 `RLock` 与普通互斥锁一样会阻塞。

### 5.5 为什么点击 Stop 后突然继续

点击 Stop 后，Sidecar MainThread 设置任务的 `cancellation_event`。Runtime 任务线程在
`cancellable_call()` 的轮询中检测到取消，抛出 `TaskCancelledError`，退出
`maybe_compact()` 并释放压缩器锁。

模型线程随后取得锁，完成：

```text
history_snapshot
→ 对话持久化
→ usage.updated
```

所以日志中会在 `task.cancel` 之后突然出现此前缺失的 `usage.updated`。这不是模型在点击
Stop 后才返回，而是模型早已返回，只是返回后的同步回调一直被锁阻塞。

## 6. 修复方案

修复原则是：

> 锁只保护共享状态的读取和提交，不能在持锁期间等待网络、磁盘或其他线程。

修复后的 `maybe_compact()` 分为三个阶段。

### 6.1 第一阶段：短暂读取共享状态

```python
with self._lock:
    previous_summary = self.summary
    generation = self._generation
```

读取后立即释放锁。`previous_summary` 作为本次压缩的输入快照，`generation` 用于判断
模型返回时该状态是否仍然有效。

### 6.2 第二阶段：在锁外调用 LLM

```python
effective_summary = self._summarize(
    compacted,
    client,
    cancellation_event,
    previous_summary,
)
```

摘要网络请求、消息序列化、Token 估算和候选历史构建都在锁外完成。模型工作线程现在可以
在 Usage 回调中自由调用 `history_snapshot()`，因为 Runtime 没有持有压缩器锁。

`_summarize()` 不再直接读取 `self.summary`，而是使用传入的 `previous_summary`，避免模型
线程在压缩过程中访问可变共享状态。

### 6.3 第三阶段：短暂提交结果

```python
with self._lock:
    if self._generation != generation:
        return None

    self.summary = effective_summary
    self.compaction_count += 1
    self.last_compacted_at = compacted_at
    self._generation += 1
```

模型返回后重新获得锁，一次性提交摘要、压缩次数和时间。

`_generation` 用于防止以下竞态：

```text
压缩请求开始
→ 用户Reset或其他压缩更新状态
→ 旧压缩请求返回
→ 旧摘要覆盖新状态
```

如果版本发生变化，本次旧结果直接丢弃。

### 6.4 Reset 使旧请求失效

```python
def reset(self) -> None:
    with self._lock:
        self.summary = ""
        self.compaction_count = 0
        self.last_compacted_at = None
        self._generation += 1
```

这样即使 Reset 发生时摘要请求仍在网络中，返回的旧摘要也无法重新写回。

### 6.5 保留 `snapshot()` 的锁

修复没有删除 `snapshot()` 中的锁：

```python
def snapshot(self) -> dict[str, Any]:
    with self._lock:
        return {
            "summary": self.summary,
            "compaction_count": self.compaction_count,
            "last_compacted_at": self.last_compacted_at,
            "context_window": self.context_window,
        }
```

直接删除该锁虽然可以掩盖本次死锁，但会让持久化读到部分更新的压缩状态，因此不是正确
修复。

## 7. 修复后的调用顺序

```text
Runtime线程短暂读取压缩状态
→ 释放压缩锁
→ 创建模型工作线程
→ 模型返回摘要
→ 模型线程执行Usage回调
→ history_snapshot短暂获取压缩锁
→ Usage持久化完成
→ 模型线程结束
→ Runtime线程短暂获取锁并提交新摘要
→ 发送history.compacted
→ 继续下一轮Agent请求
```

预期 Trace 顺序：

```text
history.compaction.started
llm_request
llm_response
usage.updated
history.compacted
history.compaction.finished
下一轮 llm_request
```

## 8. 验证结果

修复后使用了一个专门的跨线程最小复现：

1. 使用 16,000 Token 上下文窗口和足够大的历史强制触发压缩；
2. 传入 `threading.Event()`，确保 `cancellable_call()` 创建模型工作线程；
3. Fake Client 在模型工作线程中主动调用 `agent.history_snapshot()`；
4. 使用 2 秒有界 `join()` 检查 Runtime 线程能否结束。

结果：

```text
THREAD_FINISHED=True
CLIENT_CALLS=2
RESULT=[{'role': 'assistant', 'content': 'done'}]
ERRORS=[]
```

这条路径在旧实现中会稳定死锁，修复后能够正常完成。

项目回归结果：

```text
压缩模块测试：4 passed
Python全量测试：213 passed
Ruff静态检查：通过
Python编译检查：通过
Git whitespace检查：通过
```

## 9. 排查方法总结

本次定位采用“最后一个成功事件”方法：

1. 将 JSONL Trace 按时间压缩成事件时间线；
2. 找到最后一个明确完成的事件 `llm_response`；
3. 找到下一个本应出现但缺失的事件 `usage.updated`；
4. 只检查两者之间的同步调用；
5. 沿 `usage_callback → _save_conversation → history_snapshot → snapshot` 跟踪；
6. 与压缩线程的 `lock → cancellable_call → wait` 路径组合；
7. 画出线程等待环，确认死锁。

遇到类似卡死问题时，可以使用 `faulthandler.dump_traceback_later()` 或 `py-spy dump`
抓取所有 Python 线程堆栈。旧故障预期显示：

```text
stellarcode-runtime-task
  Agent._chat
  ConversationHistoryCompactor.maybe_compact
  ConversationHistoryCompactor._summarize
  cancellable_call
  completed.wait

stellarcode-cancellable-call
  TracingChatClient.chat
  RuntimeSession._record_usage
  RuntimeSession._save_conversation
  Agent.history_snapshot
  ConversationHistoryCompactor.snapshot
  RLock.acquire
```

## 10. 后续防范规则

1. 不得在持锁期间执行 LLM、HTTP、子进程、磁盘写入或等待其他线程。
2. 所有锁的临界区应只包含短时间的内存读取或原子状态提交。
3. 并发操作跨越外部请求时，使用版本号或 generation 防止旧结果覆盖新状态。
4. 测试必须覆盖生产环境的真实线程边界；传入 `None` 导致的同步路径不能替代线程测试。
5. 对 `started` 类型事件必须保证在成功、失败和取消路径上都有对应的结束事件。
6. Trace 分析优先寻找“最后一个成功事件”和“第一个缺失事件”，缩小排查区间。
7. Token Usage 回调应保持轻量，避免在模型工作线程中引入新的长时间阻塞操作。

