# StellarCode Desktop RuntimeEvent Protocol

Status: draft v1

This document defines the boundary between the Tauri desktop process and the Python
StellarCode Runtime. It is intentionally independent from React, Tauri, Rich, and the
current CLI renderer so the same Runtime can serve both terminal and desktop clients.

The matching TypeScript contract lives in
`desktop/src/protocol/runtimeEvents.ts`.

## Transport

Version 1 uses UTF-8 JSON Lines over a local Sidecar process:

- Desktop writes one request object per line to Python stdin.
- Python writes one response or event object per line to stdout.
- Python writes human-readable logs only to stderr.
- A protocol object must never span multiple stdout lines.
- Local images and files are normally referenced by path. A clipboard image may be carried
  once as Base64 in a `task.submit` request, but Base64 is never emitted as a RuntimeEvent or
  stored in conversation/trace persistence.

The desktop owns Sidecar startup and shutdown. No TCP port is required in v1.

## Message kinds

Every object contains `protocol_version: 1` and one of three `kind` values:

| Kind | Direction | Purpose |
| --- | --- | --- |
| `request` | Desktop → Python | Start sessions/tasks, cancel, or resolve approval |
| `response` | Python → Desktop | Acknowledge a request by `request_id` |
| `event` | Python → Desktop | Stream ordered Runtime state and task progress |

Unknown additive fields must be ignored. An unknown `protocol_version`, message `kind`,
request `method`, or event `type` must produce an explicit protocol error rather than being
silently interpreted.

## State machine ownership

The desktop and Python Runtime use separate state machines. The desktop machine owns only
connection/focus/submit/cancel/replay UI state; Python owns live task routes and the
`accepted -> running/cancelling -> finalizing -> released` lifecycle. A successful request
acknowledges acceptance but does not make the desktop authoritative for task completion.
Only a matching terminal event or a reconciled `workspace.open` recovery snapshot releases
the frontend task route. `workspace.open.active_tasks[]` may include the additive `phase`
field (`accepted`, `running`, `cancelling`, or `finalizing`). Durable recovery continues to
come from checkpoints and EventJournal, not from either process's in-memory state. See
`docs/frontend-runtime-state-machines.md`.

## Request and response

Example request:

```json
{"kind":"request","protocol_version":1,"request_id":"req-01","method":"task.submit","params":{"session_id":"session-01","prompt":"Run the tests"}}
```

Example acknowledgement:

```json
{"kind":"response","protocol_version":1,"request_id":"req-01","ok":true,"result":{"task_id":"task-01"}}
```

Requests are idempotent only where explicitly documented. The desktop must not retry
`task.submit` or `approval.resolve` with a new `request_id` after an ambiguous transport
failure.

Initial request methods:

| Method | Result |
| --- | --- |
| `runtime.ping` | Runtime version and capabilities |
| `workspace.open` | Workspace/model metadata |
| `workspace.close` | Close acknowledgement |
| `runtime.set_access_mode` | Updated per-conversation `restricted` or `full-access` mode (`session_id` required) |
| `session.list` | Persisted conversations for the active project |
| `session.create` | New `session_id` |
| `session.open` | Conversation snapshot |
| `session.rename` | Updated conversation metadata |
| `session.delete` | Deleted conversation metadata |
| `session.reset` | Reset acknowledgement |
| `session.set_mode` | Updated `react`, `plan`, or `team` mode |
| `session.set_trace` | Updated per-conversation Trace state and log path |
| `prompt.snapshot` | Versioned Prompt layers, character/estimated-Token counts and SHA-256 hashes; Memory/summary bodies are redacted unless `include_memory=true` |

Access mode is isolated by conversation. A task freezes its conversation's mode when
execution begins, so changing an idle conversation cannot weaken or strengthen another
conversation that is already running. Full access remains process-local and resets when
the project Runtime is restarted.
| `mcp.list` | Current server states, safe configuration metadata, and discovered tools |
| `mcp.install` | Updated MCP snapshot after writing and starting a project server |
| `mcp.set_enabled` | Updated MCP snapshot after persisting a project enable override |
| `mcp.restart` | Updated MCP snapshot after restarting one server |
| `mcp.remove` | Updated MCP snapshot after removing a project server/override |
| `mcp.logs` | Bounded stderr log text for one server |
| `rag.snapshot` | Project sources, index counts, Embedding metadata, and rebuild state |
| `rag.add_sources` | Updated RAG snapshot after adding de-duplicated files/directories |
| `rag.remove_source` | Updated RAG snapshot after removing one source from the manifest |
| `rag.index` | Accepted background index job and its `job_id` |
| `rag.clear` | Clear generated project index after explicit confirmation; keep sources |
| `memory.list` / `memory.save` | Query or explicitly save project-scoped long-term facts |
| `memory.delete` / `memory.clear` | Delete one fact or confirmed-clear project memory |
| `skill.list` / `skill.get` | Structured Skill inventory and detail |
| `skill.set_enabled` / `skill.reload` | Persist global-by-name enablement or rescan Skill directories |
| `skill.diff` | Bounded current-to-bundled Diff plus optimistic current/bundled hashes |
| `skill.update` / `skill.restore_default` | Confirmed replacement after both reviewed hashes still match |
| `skill.keep_custom` | Preserve the local tree and acknowledge only the reviewed bundled release |
| `browser.snapshot` / `browser.probe` | Current Chrome session state or explicit local CDP probe |
| `browser.connect` / `browser.disconnect` | Confirmed shared/isolated Chrome transition |
| `browser.tabs` | Bounded Chrome DevTools tab output |
| `diagnostics.snapshot` | Last persisted workspace diagnostics and provider availability |
| `diagnostics.run` | Start a safe or explicitly confirmed build diagnostic job |
| `diagnostics.cancel` | Cancel the matching active diagnostic job |
| `event.replay` | Events for one session after a durable sequence number; optional `event_types` filters the journal |
| `task.submit` | New `task_id` |
| `task.recover` | Resume the matching unfinished task checkpoint |
| `task.cancel` | Cancellation accepted/rejected |
| `task.diff` | Bounded unified diff and persisted change summary for one finished task |
| `task.rollback` | Restore one task's protected workspace paths after confirmation and conflict checks |
| `approval.resolve` | Approval accepted/rejected |
| `runtime.shutdown` | Shutdown accepted |

## Concurrency and routing

Protocol v1 permits one Sidecar process to host multiple project Runtimes. `workspace.open`
selects (or lazily creates) a project Runtime; it does not close previously loaded projects.
Its result includes `active_tasks[]` and `recoveries[]` so the desktop can restore background
indicators without treating a live task as an interrupted task.

The scheduling unit is a conversation: each `session_id` may own at most one active `task_id`,
while different sessions may run concurrently within one project or across projects. Task
cancellation and approval resolution are routed by durable `task_id`/`session_id`, not by the
currently selected workspace. Per-task checkpoints are stored independently, and task-scoped
events always enter the owning project's journal even after the user switches projects.

Every project-scoped desktop request also carries an explicit `project_id`. The Sidecar validates
that `project_id`, `session_id`, and `task_id` resolve to the same owner before dispatch. After a
process restart the desktop reopens every project that owned active work, reconciles the union of
`active_tasks[]` and `recoveries[]`, and sends each `task.recover` to that explicit project.

Management jobs such as RAG indexing and diagnostics remain project-scoped. A project may
reject mutations while its own management job is active without blocking tasks in another
loaded project.

## Event envelope

```json
{
  "kind": "event",
  "protocol_version": 1,
  "event_id": "evt-01",
  "session_id": "session-01",
  "task_id": "task-01",
  "sequence": 7,
  "timestamp": "2026-08-05T07:30:01.123Z",
  "type": "tool.started",
  "data": {
    "tool_call_id": "call-01",
    "name": "read_file",
    "arguments": {"path": "src/stellarcode/agent.py"},
    "iteration": 1
  }
}
```

Rules:

1. `event_id` is globally unique for a Runtime process.
2. `sequence` is strictly increasing within a session, including events from parallel tasks.
3. `timestamp` is an ISO-8601 UTC timestamp and is display metadata, not an ordering key.
4. `task_id` is required for task-scoped events and omitted for Runtime/session events.
5. Deltas must be applied in `sequence` order.
6. Session events are appended to the project's durable journal before stdout delivery.
   On startup and before every append, the Runtime repairs a crash-truncated final JSONL
   record: a complete JSON object missing only its newline is preserved and delimited,
   while an incomplete fragment is truncated back to the last complete record. This keeps
   the next terminal event independently replayable after another restart.
7. After reconnect, `event.replay` returns events with `sequence > after_sequence`; the
   desktop de-duplicates by `event_id` and applies them in sequence order before accepting
   new live events for that session.
8. A normal `session.open` also replays the durable execution-detail event subset from the
   persisted event floor (zero for legacy conversations). The desktop ignores events before
   the last `session.reset`, merges the
   reconstructed task/tool/approval entries with the conversation snapshot by timestamp,
   and never replays assistant deltas over the authoritative persisted final answer.
9. Conversation schema v2 persists `event_floor_sequence`. `session.open` starts replay after
   this floor, so a reset remains authoritative even if the Sidecar exits after saving the
   cleared snapshot but before writing the `session.reset` journal marker.

## Event catalogue

### Runtime and session

- `runtime.ready`
- `runtime.shutdown`
- `workspace.opened`
- `workspace.closed`
- `access.mode_changed`
- `session.created`
- `session.opened`
- `session.renamed`
- `session.deleted`
- `session.snapshot`
- `session.reset`
- `trace.status_changed`

Trace recording is opt-in and persisted independently for each conversation. When enabled,
the Runtime writes redacted JSONL entries for LLM requests/responses, full tool results,
approval activity, incoming Runtime requests, and Runtime events. The active file path is
returned by `session.set_trace` and included in the conversation snapshot. Trace files are
stored under `<workspace>/.stellarcode/traces`.

Every shared Prompt assembly also adds a `prompt_assembled` Trace entry. It records the
Prompt version and each layer's role, character count, preflight Token estimate, sensitivity
flag, and SHA-256 hash without duplicating any layer body. This makes Prompt drift auditable
without adding Memory text to the new observability event.

### Task and assistant

- `workspace.snapshot.created`
- `task.started`
- `assistant.delta`
- `assistant.completed`
- `assistant.thinking`
- `usage.updated`
- `history.compaction.started`
- `history.compaction.finished`
- `history.compacted`
- `task.completed`
- `task.failed`
- `task.cancelled`
- `task.finalization.pending`
- `task.rollback.started`
- `task.rollback.completed`
- `task.rollback.failed`

`assistant.delta.data.text` values are concatenated in sequence order and rendered
immediately. The Python clients consume OpenAI-compatible SSE for GLM, DeepSeek, and Agnes;
the Agent coalesces very small provider chunks before emitting them so the durable event
journal is not flushed once per token. `assistant.delta.data.reset: true` removes provisional
text from a model round that ultimately requested a tool. `assistant.completed` remains the
authoritative final answer and replaces the accumulated stream. A terminal task event must
occur exactly once. `assistant.completed` is not a terminal task event because cleanup or
usage events may follow it.

`assistant.thinking` communicates activity and an optional short display summary. It is not
required to expose private chain-of-thought text.

Terminal task events carry `elapsed_ms`. For a recovered task this value describes only the
current Sidecar run, so the desktop derives total wall-clock duration from the original
`task.started.data.started_at` and terminal event timestamp. Older failed/cancelled events
without `elapsed_ms` use the same timestamp fallback.

`task.started.data.protection` 和三个终态事件的 `data.changes` 使用同一个
`TaskChangeSet` 结构。开始事件中的状态通常为 `active`；终态中的结构是持久化后的
最终事实，包含文件列表、增删统计、diff/rollback 可用性和错误状态。旧 Runtime 可能
省略这些可选字段，前端必须兼容，但宣告 `task_git_snapshots` capability 的 Runtime
必须在接受任务前成功创建保护快照。

`usage.updated` is emitted after every completed LLM request, including planner, worker,
reviewer, recovery, and history-summary calls. It carries the latest provider input/output,
cache and reasoning details, current-task totals, persisted conversation totals, provider,
model, operation, context capacity, and an `exact` flag. Provider usage is authoritative;
when it is absent the Runtime emits the same shape with `exact: false` from its estimator.
The event also carries call/task/conversation cost fields. Provider-reported cost is used
when available; otherwise the Runtime estimates cost from configured per-million-token
rates. Exact `deepseek-v4-flash` and `deepseek-v4-pro` names have narrow official USD defaults;
other models use provider-prefixed or generic `.env` pricing overrides.

`history.compaction.started` is emitted immediately before the potentially slow summary
request. The desktop shows an in-progress context status until the matching
`history.compaction.finished` arrives. The finished event is guaranteed by the Agent's
cleanup path even when compaction is cancelled or fails, so the UI cannot remain stuck.

`history.compacted` is emitted when the real provider message list is replaced with a marked
summary plus recent turns. Its before/after values are preflight estimates because compression
must happen before the next provider request. The event also reports compacted turn count,
method (`llm`, `fallback`, or `tool-result-truncation`), and cumulative compaction count.
Tool calls and their matching tool results are never split across the compaction boundary.

### Tools and attachments

- `tool.started`
- `tool.completed`
- `tool.failed`
- `attachment.available`

Each `tool_call_id` must have one `tool.started` and exactly one terminal tool event. Tool
arguments and result previews must pass the same redaction policy used by trace logging.
Large results remain in Python history and are represented by bounded previews in events.

`workspace.snapshot.created` 的 protection payload 可包含 `worktree_isolated`、
`worktree_path` 和 `merge_state`。`worktree_isolated=true` 表示该任务的内置文件操作和命令
cwd 正在独立 Git worktree 中执行。终态 `changes.merge_state=merged` 表示 patch 已安全合并；
`merge_conflict=true` 表示预检拒绝覆盖新项目状态，任务改为 failed 且 worktree 被保留。

`write_file`、`apply_patch` 和 `delete_file` 的 `tool.started.data.change_preview` 描述执行前的文件
状态，包括操作类型、路径、工作区范围、`rollback_protected`、`protection_reason`、
SHA-256、增删统计和有界 unified diff。`protection_reason` 的稳定值为
`outside_workspace`、`generated_or_internal_path`、`sensitive_path` 或 `preview_error`。
`write_file` 的完整替换内容和 `apply_patch` 的精确替换文本不会写入事件参数；后者只记录
编辑数量与字符统计。敏感路径隐藏 diff，二进制文件只报告
内容发生变化。未受保护路径仍可在策略允许时执行，但前端必须明确标记它不能由任务级
Side-Git 回滚。该预览是文件工具能力，不代表任意 shell/MCP 副作用都能在执行前转换为
逐文件 diff。

### Human approval

- `approval.requested`
- `approval.resolved`

After `approval.requested`, the corresponding tool call is paused. The desktop answers with
one `approval.resolve` request. Python validates the approval is still pending before it
continues. Closing the window or cancelling the task rejects pending approvals.

For a same-round tool batch, Runtime publishes every required `approval.requested` before
waiting for the individual decisions. The approvals are independent waiters and may be
resolved in any order. Cancelling one task rejects only approvals owned by that task; pending
approvals from other background conversations remain actionable. CLI input is still protected
by the terminal handler's single-reader lock.

`approval.requested.data.tool_call_id` is the original tool invocation ID, not the approval
ID. The desktop keeps pending approvals in arrival order and removes only the item whose
`approval_id` appears in `approval.resolved`. A tool card renders `WAIT` while its approval is
pending, `RUN` after approval, and then `OK` or `FAIL` after its terminal tool event. This
prevents a late resolution event for one approval from hiding the next approval in a batch.

During historical replay, approval events rebuild read-only decision cards and never enter
the actionable approval queue. A request that has no matching resolution is displayed as
`Interrupted`, and its associated tool is marked unconfirmed instead of remaining at `WAIT`.
Python validates the request's session/task against the active approval context and fsyncs
`approval.resolved` before waking the blocked tool thread, so an acknowledged decision cannot
be lost or appear after the tool outcome in durable history.

对于 `write_file`、`apply_patch` 和 `delete_file`，`approval.requested.data.change_preview` 与
`tool.started` 使用相同结构。批准后 Runtime 将预览时的路径和修改前哈希绑定到实际
操作；如果审批和执行之间目标文件发生变化，工具必须失败并重新读取，而不能执行已经
过期的覆盖操作。

### Plan and Multi-Agent

- `plan.planning.started` / `plan.planning.delta`
- `plan.created`
- `plan.step.started`
- `plan.step.delta`
- `plan.step.completed`
- `plan.step.failed`
- `plan.step.skipped`

Plan deltas are emitted from the same provider streaming interface as ReAct, but carry a
`step_id` and render only inside that step. Planner deltas are visible in the temporary
planning card until `plan.created` replaces it with the validated DAG. A reset removes a
provisional tool-call preamble rather than treating it as a step result.

Team collaboration adds `team.agent.delta`; it is scoped by Agent name and Team task id and
is rendered in that child Agent's expandable dialogue. The terminal MessageBus reply replaces
the accumulated child stream, so the durable result remains authoritative.

Parallel step events can interleave, but session sequence numbers provide a stable rendering
order. A Worker name is presentation metadata and must not be used as a task identifier.

### MCP

- `mcp.status_changed`

The event data is the complete safe server view: `name`, `status`, transport, configuration
source, command/URL metadata, environment/header *names*, discovered tools, error text,
uptime, process ID, and capabilities. Secret environment/header values are never emitted.
The desktop upserts this view immediately and reconciles it with `mcp.list` after opening a
workspace or completing a management request. Servers can remain in `starting`; one server
entering `error` does not remove tools registered by healthy servers.

`mcp.install` accepts exactly one of a local stdio `command` or an HTTP(S) `url` and stores it
under `<workspace>/.stellarcode/mcp.json`. Starting a local command requires
`confirmed: true`; the desktop must show the exact command and arguments before setting that
flag. Install, enable/disable, restart, and remove are rejected while an Agent task is active.
User-level configuration is read from `~/.stellarcode/mcp.json`; it can be disabled through
a project override but is not deleted by project management. `${VARIABLE}` values remain
unexpanded in persisted configuration and resolve only inside Python from the process,
project `.env`, or user `.env`.

### Code RAG

- `rag.index.started`
- `rag.index.progress`
- `rag.index.completed`
- `rag.index.failed`

Each desktop project persists its source manifest under
`<app-data>/runtime/projects/<project-id>/rag/sources.json`. The SQLite vector rows remain
namespaced by the active workspace even when explicitly selected sources are outside that
workspace. One rebuild collects and de-duplicates all supported files before atomically
replacing that workspace namespace; adding several sources therefore cannot cause the
last source to overwrite earlier ones.

`rag.index` runs on a dedicated background thread so JSONL requests and UI progress remain
responsive. Agent task submission, project switching, source mutation, MCP mutation, and a
second index build are rejected until the index job finishes. `rag.index.progress` carries
bounded discovery/file progress text. `rag.index.completed` carries the authoritative full
snapshot; `rag.index.failed` reports the exact provider/indexing error.

The snapshot exposes provider/model/Base URL and only a boolean indicating whether an
Embedding API key exists; secret values are never emitted. A source or Embedding-model
change sets `needs_rebuild`. The `automatic_retrieval` desktop setting changes the
`search_code` tool policy for ReAct, Plan, and Team: automatic mode encourages semantic
retrieval before codebase claims, while manual mode permits it only on an explicit request.

### Desktop management and Problems

Memory, Skill, and Browser requests return structured snapshots and are not persisted into the
execution event journal. This avoids duplicating long-term Memory content or Skill bodies in
session replay. Memory mutation and Skill/Browser transitions are rejected while another
workspace mutation is active. Clearing Memory and connecting shared Chrome require an explicit
confirmation flag; Browser snapshots never probe a TCP endpoint implicitly.

Bundled Skill snapshots expose versions, whole-tree SHA-256 hashes, customization flags, and
`current`, `customized`, `update_available`, `custom_kept`, or `error` upgrade state. The
Runtime automatically upgrades only copies that still equal their trusted installed hash.
Explicit update/restore requests require `confirmed: true` and the hashes returned by the latest
`skill.diff`; stale previews fail closed. `skill.keep_custom` carries the same hashes but does not
write Skill content. Bundled reconciliation is limited to the exact User-layer template path and
never mutates a same-name Project override.

Workspace diagnostics use these non-session events:

- `diagnostics.started`
- `diagnostics.progress`
- `diagnostics.completed`
- `diagnostics.failed`
- `diagnostics.cancelled`

They use the workspace session ID and carry only diagnostic results/status, not application
secrets. The safe profile parses/compiles Python without importing code and optionally runs Ruff
with no fixes and no cache. The build profile requires confirmation and is limited to a
project-local TypeScript compiler with `--noEmit` and Cargo with `--locked --offline`; it never
uses npm/npx package installation or arbitrary build scripts. Diagnostic processes use argv with
`shell=false`, bounded output/time, cancellable Windows process-tree cleanup, and do not follow
symlinks or reparse directories. Results are atomically persisted per project. A user may opt into
a local Python Language Server with an absolute executable and structured argv. The Runtime uses
bounded Content-Length JSON-RPC over stdio, opens only bounded workspace Python files, ignores
diagnostics outside that opened set, rejects every server request (including edits/commands),
isolates credentials, and terminates the process tree on timeout/cancel. It never installs or
auto-discovers a server from an untrusted project. LSP availability is reported honestly; a
syntax/lint/build result must not be presented as an LSP diagnostic.

### 工作区修改保护与任务回滚

支持该功能的 Runtime 在 `runtime.ready.data.capabilities` 中声明：

- `change_previews`
- `task_git_snapshots`
- `task_rollback`

完整设计和本地持久化布局见 `docs/task-modification-protection.md`。

#### `workspace.snapshot.created`

`task.submit` 在发送成功响应之前创建任务级 Side-Git 基线，并发送任务作用域事件：

```json
{
  "type": "workspace.snapshot.created",
  "task_id": "task-01",
  "data": {
    "snapshot_id": "snapshot-01",
    "task_id": "task-01",
    "session_id": "session-01",
    "backend": "side-git",
    "protected": true,
    "status": "active",
    "has_changes": false,
    "changed_files": [],
    "additions": 0,
    "deletions": 0,
    "diff_available": false,
    "rollback_available": false,
    "rolled_back": false
  }
}
```

事件名中的 `workspace` 表示保护对象是工作区；它仍必须携带 `task_id` 和实际会话
`session_id`。`task.started.data.protection` 会再次携带当前快照状态，使首次实时消费、
历史重放和崩溃恢复都能建立同一个任务卡片。`snapshot_id` 是回滚时的陈旧状态守卫，
不是 Git commit ID；协议不暴露内部 revision。

如果 Git 不可用或独立 Side-Git 无法初始化，Runtime 不得确认新任务。协议错误通过
`runtime_unavailable` 返回，而不是以 `protected: false` 静默执行。若任务结束时的
二次快照失败，终态仍可携带 `protected: false`、`status: "error"` 和 `error`，前端
必须关闭 diff/rollback 操作并显示保护失败。

#### 终态 `changes`

`task.completed`、`task.failed` 和 `task.cancelled` 在写入终态事件之前创建 after
snapshot。三个事件都可携带 `data.changes`：

```json
{
  "snapshot_id": "snapshot-01",
  "task_id": "task-01",
  "session_id": "session-01",
  "backend": "side-git",
  "protected": true,
  "status": "completed",
  "has_changes": true,
  "changed_files": [
    {"path":"src/app.py","status":"modified","additions":8,"deletions":2}
  ],
  "additions": 8,
  "deletions": 2,
  "diff_available": true,
  "rollback_available": true,
  "rolled_back": false,
  "error": "",
  "created_at": "2026-08-11T01:00:00.000Z",
  "completed_at": "2026-08-11T01:01:00.000Z",
  "rolled_back_at": null
}
```

`changed_files[].status` 为 `created`、`modified`、`deleted`、`type_changed` 或
`binary`。二进制增删行数为 0。完整 patch 不进入 RuntimeEvent journal；终态只保存
有界摘要。任务没有变化时 `has_changes`、`diff_available` 和
`rollback_available` 均为 `false`。

终态事件和后续 rollback 事件属于可恢复执行细节。前端执行 `session.open` 的过滤重放
时必须包含它们，以恢复文件列表、“查看差异”和“撤销任务修改”状态。

#### `task.diff`

请求：

```json
{"method":"task.diff","params":{"session_id":"session-01","task_id":"task-01","max_chars":120000}}
```

该请求只允许在 Runtime 空闲时执行。`session_id` 必须与快照记录匹配；任务必须已经
生成 before/after revision。`max_chars` 默认 80,000，Runtime 将其限制在
1,000 到 200,000 之间。成功结果为完整 `TaskChangeSet` 加：

```json
{"diff":"diff --git ...","diff_truncated":false}
```

diff 使用三行上下文、禁用 rename 检测和颜色。`diff_truncated: true` 只表示协议返回
内容被截断；Side-Git 中的任务快照仍是完整的。

#### `task.rollback`

请求：

```json
{
  "method":"task.rollback",
  "params":{
    "session_id":"session-01",
    "task_id":"task-01",
    "snapshot_id":"snapshot-01",
    "confirmed":true
  }
}
```

该请求只允许在 Agent 和 RAG 均空闲时执行，且必须显式 `confirmed: true`。
`task_id`、`session_id` 和 `snapshot_id` 必须与任务记录一致。成功响应为更新后的
`TaskChangeSet`，并额外包含 `restored_files`。已经回滚、没有受保护变化、仍在运行、
快照过期或快照已被保留策略清理的任务返回 `rollback_unavailable`。

事件顺序固定为：

1. `task.rollback.started`，数据为 `{ "snapshot_id": "..." }`；
2. Runtime 创建 rollback-safety 快照并完成冲突检测；
3. 成功时持久化任务记录，发送 `task.rollback.completed`，随后发送成功 response；
4. 失败时发送 `task.rollback.failed`，随后发送 error response。

`task.rollback.completed` 使用更新后的 `TaskChangeSet`，其中
`rolled_back: true`、`rollback_available: false`，并包含实际处理的
`restored_files`。`task.rollback.failed` 结构为：

```json
{
  "snapshot_id":"snapshot-01",
  "code":"workspace_changed",
  "message":"...",
  "conflicted_paths":["src/app.py"]
}
```

`code` 的主要值为 `workspace_changed` 和 `rollback_unavailable`。对应 response error
code 分别为 `rollback_conflict` 和 `rollback_unavailable`。

#### 冲突语义

回滚是任务路径级恢复，不是 `git reset --hard`。Runtime 比较任务 after snapshot 与
当前工作区，只检查该任务的 `changed_files`：

- 后续只修改其他路径时，回滚继续，其他路径原样保留；
- 任一任务路径在任务结束后又发生变化时，整个回滚在写文件前失败；
- `conflicted_paths` 返回相交路径；当前 v1 不尝试同文件的逐 hunk 自动合并；
- 无冲突时只恢复任务路径；任务新建文件删除，任务删除文件恢复，修改文件回到 before
  内容。

执行恢复前会创建 rollback-safety revision。可捕获的普通异常会触发补偿恢复并发送
失败事件。事件中的成功状态只能来自持久化完成的任务记录，不能根据 response 到达时间
自行推断。

如果进程在逐路径恢复时硬崩溃，下次 Runtime 启动先保存当前现场为 emergency revision，
并验证相关路径当前只能是 rollback-safety 或任务 before tree entry。符合时补偿回到
rollback-safety 并发送 `task.rollback.failed`（`rollback_interrupted_recovered`）；如果
重启前出现第三种人工修改，则不写工作区，发送 `rollback_recovery_failed`。待通知标记仅在
失败事件写入 journal 后清除。

#### Side-Git 边界

Side-Git 使用应用数据目录中的独立 bare repository 和 index；它不得修改用户仓库的
`HEAD`、分支、refs、index 或提交历史。Python 通过系统维护的明确排除规则安全遍历，
再用 `hash-object --no-filters` 按原始字节构树，不沿用用户 `.gitignore`、
`.gitattributes` 或 LFS clean filter 决定快照内容：

- 工作区外 `write_file` / `delete_file` 只可显示预览，不可通过任务 rollback 撤销；
- 被用户 `.gitignore` 忽略的普通源文件仍进入快照，避免静默失去撤销能力；
- 嵌套 Git 仓库的 `.git` 元数据排除，但普通工作树文件递归采集，不记录为 gitlink；
- Windows junction 与其他 reparse point 不递归采集，防止快照越过工作区；
- `.git`、`.stellarcode`、虚拟环境、依赖目录、缓存和常见构建输出被明确排除；
- `.env*`、常见密钥/证书、`.ssh`、`.aws` 和 `.azure` 等敏感路径被明确排除，预览以
  `rollback_protected: false` 和 `protection_reason` 告知前端；
- shell、MCP 或外部程序在工作区内产生的最终文件变化会出现在任务 diff 中，但数据库、
  网络、注册表、其他目录和远程系统副作用不受保护；
- `full-access` 不扩大 Side-Git 范围；
- 被采集文件的完整内容会保存在当前用户应用数据目录的 Side-Git 对象库中，操作系统
  文件权限仍是这部分本地数据的最终边界。

## Cancellation

`task.cancel` requests cooperative cancellation. The Runtime acknowledges whether the signal
was accepted, stops scheduling new tools/plan steps, terminates owned command processes where
possible, rejects pending approvals, stops waiting for blocking model requests, and finishes
with `task.cancelled`. A synchronous HTTP request already in flight may finish in a detached
daemon thread, but its result is ignored and cannot mutate the cancelled conversation.

Cancellation does not imply rollback. Before `task.cancelled` is journaled, the Runtime
creates the same final Side-Git snapshot used by other terminal states and stores the partial
workspace result in `task.cancelled.data.changes`. The UI must continue showing those changes
and may offer an explicit, separately confirmed `task.rollback` after cancellation.

## Sidecar crash recovery

The desktop treats an unexpected `runtime-exited` notification as an interruption, not an
immediate task failure. It preserves the task card and elapsed timer, restarts the Sidecar
with bounded exponential backoff, opens the same project, replays the active session from
its last applied sequence, opens the same conversation, and sends `task.recover` when
`workspace.open` reports an unfinished checkpoint.

The durable acceptance order for a new task is:

1. reserve the Runtime task slot;
2. create the Side-Git `before` revision and atomic task record;
3. fsync `workspace.snapshot.created` to the project event journal;
4. persist the user transcript entry;
5. atomically write
   `<app-data>/runtime/projects/<project-id>/recovery/active-task.json`, including the public
   `workspace_snapshot` record;
6. acknowledge `task.submit`, then start the worker and journal `task.started`.

Conversation messages are also checkpointed after every assistant message, every complete
tool-result batch, and history compaction. Completion uses a durable two-phase boundary:

1. persist `status=finalize_pending`, the intended completed/failed/cancelled outcome, and
   terminal data before POST-snapshot work;
2. create/reuse the idempotent Side-Git `after` revision and persist `workspace_finalized`;
3. fsync the terminal event with `data.changes` (the journal is the commit point);
4. remove `active-task.json` and release the active task.

If POST capture fails, `task.finalization.pending` tells the desktop that only workspace
finalization is being retried. Restart never invokes the Agent/model/tools for this state. If
the event journal append succeeds but stdout delivery or checkpoint cleanup fails, Runtime
recognizes the already-terminal task and performs cleanup without creating a second terminal
event.

ReAct recovery continues the persisted provider message history without adding the original
user turn a second time. If a crash leaves an assistant tool call without all matching tool
results, history repair inserts `TOOL_INTERRUPTED`: the operation is unconfirmed and the
Agent must inspect actual state before retrying. This is essential for commands and writes
whose side effects may have completed immediately before the process died.

Plan mode rebuilds its durable DAG, keeps confirmed completed/failed/skipped steps, and
continues pending steps. A step that was `running` at the crash boundary becomes pending with
an explicit inspect-before-repeat instruction. Team mode has no durable worker-local history,
so it replans from the persisted task/plan evidence with the same safety instruction rather
than blindly replaying an interrupted worker call. An answer already marked `answer_ready`
is returned from the checkpoint instead of calling the model again. Recovery never calls
`begin_task` a second time: `task.started.protection` is loaded from the original task record,
and finalization compares the single final `after` revision with the original `before`
revision. Before recovery starts, the Runtime validates that the checkpoint snapshot ID,
session ownership, task record, and referenced Git tree still match; otherwise it refuses to
resume the task without protection. If the terminal event was journaled but the checkpoint was not cleared before the
crash, `workspace.open` detects that terminal task in the journal and only deletes the stale
checkpoint.

Rollback crash recovery is separate from Agent task recovery and runs while the workspace
protection service is initialized. A task record with `rollback_state: "in_progress"` means
the previous Sidecar may have stopped after changing only some task paths. Before accepting
new work, the Runtime restores those task paths from `rollback_safety_revision`; the restore
is idempotent and may be retried after another hard stop. On success it creates a
`rollback-recovered` revision, marks the attempted rollback `failed`, and leaves rollback
available for an explicit retry. If compensation itself fails, the record becomes
`recovery_failed` and rollback is disabled pending manual inspection.

Before any file/directory type transition, rollback also scans every directory that would be
removed. Excluded secrets or generated paths, empty directories, junction/reparse points, and
any entry absent from the current safety tree cause an all-or-nothing conflict. The Runtime
never uses recursive deletion when it cannot prove every affected entry belongs to the
protected task result.

Both outcomes persist `rollback_recovery_event_pending: true`. After `workspace.open`, the
Sidecar fsyncs a `task.rollback.failed` event to the original session with code
`rollback_interrupted_recovered` or `rollback_recovery_failed`, and only then acknowledges
the notice by clearing the pending flag. A crash between journal append and acknowledgement
can therefore repeat the notice, so consumers must merge it idempotently by task and ordered
event sequence rather than treating it as another file mutation.

## Input attachments

`task.submit.params.attachments` contains user-selected local attachment metadata. Each item
uses a stable `id`, `kind`, `mime_type`, `display_name`, optional `local_path`, and optional
`size_bytes`. An image pasted into the chat box with `Ctrl+V` instead carries a temporary
`data_base64` field. The frontend limits clipboard images to 20 MB. The Sidecar independently
decodes and validates the image, stores its processed form under `~/.stellarcode/cache`, and
replaces the Base64 field with ordinary local metadata before emitting events or persisting
the transcript. The Base64 field is also explicitly omitted from Trace logs.

The Sidecar revalidates path-based attachments, file type, size, and count instead of trusting
frontend metadata. Image items enter the existing multimodal image pipeline. Text and source
files are inlined with bounded delimiters so an explicitly selected file outside the active
workspace can be inspected without weakening the normal PathGuard policy. Transcript entries
persist attachment metadata but never duplicate the inlined file contents.

If the primary client is text-only and a VLM is configured, image-bearing message histories
are dispatched by `VisionRoutingClient` to the configured vision provider. The router remains
on that VLM for dependent tool rounds while the active image is still present. Historical
image payloads are pruned before the next external user turn, allowing subsequent text-only
requests to return automatically to the primary model.

## Errors and process failure

Protocol errors use stable machine codes such as:

- `unsupported_protocol`
- `invalid_message`
- `unknown_method`
- `session_not_found`
- `task_not_found`
- `task_busy`
- `approval_not_pending`
- `runtime_unavailable`

Error `message` text is user-readable; logic branches on `code`, never on message text.

If stdout contains invalid JSON, the desktop records a transport diagnostic, skips that line,
and continues consuming later messages. If the Python process exits unexpectedly, the
automatic restart and replay flow above runs. After six failed restart attempts the Runtime
enters an error state and the UI offers the existing explicit Runtime restart.

The Tauri stdout bridge adds `runtime_pid` to each top-level event or response before it is
delivered to React. This field is transport metadata rather than part of Python's persisted
v1 envelope. React retires the old PID before an intentional Runtime stop and rejects messages
from retired or non-active PIDs before applying event-ID/sequence deduplication. This prevents
a late old-process `runtime.shutdown` event from making the new process's sequence-1
`runtime.ready` look stale during project or cross-project conversation switching.

## Security boundaries

- The frontend never invokes filesystem or shell operations directly.
- Access mode and approval policy remain authoritative in Python.
- `restricted` is the default mode. Risky operations use the Runtime HITL approval flow,
  and policy rejects broad recursive scans of filesystem roots.
- `full-access` bypasses Runtime approvals and the broad-scan restriction, including for
  paths outside the active workspace. It does not bypass operating-system ACLs,
  administrator requirements, file locks, or security software.
- Access mode is Runtime-local and is never persisted. Starting the app again or switching
  projects creates a Runtime in `restricted` mode.
- Windows workspace paths exposed to the Runtime and child shell processes use ordinary
  drive-letter or UNC syntax. Tauri's verbatim `\\?\` prefix is removed because CMD and
  Conda do not accept it as a current working directory.
- Tool child processes cannot inherit the Sidecar protocol stdin. Runtime-only Python import
  paths are removed and the user's pre-launch `PYTHONPATH` is restored before commands run.
- Event previews and stderr logs must redact configured secrets.
- Attachments are local paths validated by the Runtime; the UI does not accept arbitrary
  `file://` URLs from tool text.
- API keys are never included in protocol messages.

## Deferred from v1

- Rich Team streaming beyond text deltas (for example, structured per-token reasoning or
  provider-native tool-call deltas). Team text streaming is already exposed through
  `team.agent.delta` and rendered in the expandable child-Agent card.
- Packaged/frozen Python runtime distribution
- Multiple desktop clients attached to one Runtime
- Remote/network transport
- Binary streaming
- Cross-device or remote task migration
