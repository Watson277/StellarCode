# 前端与 Runtime 状态机边界

状态：Desktop Runtime v1 已实现

StellarCode 将“界面控制状态”和“Python 任务执行状态”拆成两个独立状态机。二者只通过
Runtime 协议的 request / response / event 同步，不共享对象，也不让前端推测后端任务是否
仍然存在。

## 前端状态机

实现：`desktop/src/runtime/frontendRuntimeMachine.ts`

前端状态机只管理可丢弃、可由 Runtime 重建的 UI 控制状态：

- Runtime 连接状态；
- 当前激活的 conversation；
- `session_id -> task_id` 和反向映射；
- `session_id -> submit request_id` 的短暂等待阶段，允许不同对话同时提交；
- 每任务 cancelling 状态；
- 当前 event replay 阶段。

`busy`、`activeTaskId`、`anyTaskRunning` 和 `Stopping...` 不再分别写入多个 React state/ref，
而是从上述状态确定性派生。`App.tsx` 收到 Runtime 消息后只发送状态机 action；状态转换函数
是纯函数，不执行 Tauri 调用、不写 transcript，也不读取 DOM。

项目切换只改变前端焦点，不会清除其他项目/对话的后台任务；只有整个 Sidecar 退出或显式
Runtime 重置才清空 live task routes。event replay 的 durable 数据仍来自 Python journal，前端
状态机仅表示 replay 是否正在进行。

## RuntimeClient：唯一 RPC 所有者

实现：`desktop/src/runtime/runtimeClient.ts`

所有 Sidecar request / response 关联均由 `RuntimeClient` 负责，包括 workspace、session、event
replay、task submit/recover/cancel、approval 和管理页请求。它统一提供：

- 强类型 method -> params -> result 映射；
- 请求超时、AbortSignal、重复 request id 防护；
- scope supersede 与按 scope prefix 中断；
- transport 断开时批量拒绝；
- 吞掉 timeout/切换项目之后迟到的旧 response，防止第二个 handler 误接管。

项目切换只中断 `project:{oldProject}:*` 和 `management:{oldProject}:*` 控制请求。后台
`task:*` 与 `approval:*` 动作属于全局控制面，不会因为界面切换项目而被取消。`App.tsx`
中的旧 response reducer 目前仅保留 UI projection 职责，已经不再拥有请求关联与生命周期。
`workspace.open` 是项目切换的提交点：打开失败时不更新最近项目，并自动重新打开上一个已确认
workspace，防止 UI project scope 与 Sidecar 实际 workspace 分叉。

## RuntimeEventProjector 与 per-session store

实现：

- `desktop/src/runtime/eventProjector.ts`
- `desktop/src/runtime/supervisionStore.ts`

`RuntimeEventProjector` 是纯函数，将 live 与 replay 的 RuntimeEvent 投影成规范化的 Task、
Approval 和 Session 实体。`SupervisionStore` 通过 `useSyncExternalStore` 向 React 暴露快照，
每个 session 只保存自己的 task/approval 索引，全局中心则通过 selector 聚合。

关键单调性约束：

- event id 与 sequence 去重，乱序旧事件不能让 terminal task 回退；
- `workspace.open.active_tasks` 是活跃路由权威快照，但不能复活已收到 terminal event 的 task；
- task terminal、Stop accepted、崩溃恢复都会终止旧 pending approval；
- `task.submit` 的业务拒绝才会成为 terminal failed；timeout/transport disconnect 先记录为
  “结果未知”的 recovering 占位，随后由 `task.started` 或 workspace 权威快照消解；
- journal replay 只过滤历史前缀，replay 期间缓冲的 live `assistant.delta/completed`、usage、
  access 和 trace event 必须原样回到 live 路径；
- replay 完成后再次用项目的 active task 快照对账，截断 journal 不会留下 zombie busy 或
  stale approval。

## 全局 Task Center 与 Approval Center

实现：`desktop/src/features/supervision/SupervisionCenter.tsx`

顶栏入口聚合所有已知项目与对话，不依赖当前打开的 transcript：

- Task Center 显示活动、耗时、Token、费用、模式和失败原因，可跳转、Stop、打开 Trace；
- Approval Center 显示项目/对话、项目工作区、风险、参数和文件 diff，可在后台对话中审批；
- 同一 task 的多个审批按队列展示；Stop 被接受后立即从可操作审批中移除；
- Drawer 支持 Escape、Tab focus trap、关闭后焦点恢复、计数 aria-live 和窄窗口全屏布局。

真正的工具实际执行目录可能是任务级临时 worktree，因此未由协议提供精确目录时 UI 明确标为
“项目工作区”，而不声称它一定是进程 Working Directory。

## Python Runtime 状态机

实现：`src/stellarcode/runtime/task_state.py`

Sidecar 的 live task route 使用以下阶段：

```text
accepted -> running -> finalizing -> released
    |           |
    +-> cancelling -> finalizing -> released
```

每个 task 只能属于一个 conversation，每个 conversation 同时只能有一个 live task。不同
conversation 和不同 project 可以拥有独立 route。状态机集中维护：

- `task_id -> runtime/project/session/phase`；
- `session_id -> task_id`；
- 注册、取消、终态 finalization 和释放的合法转换；
- 项目级活跃任务查询。

Sidecar 不再分别修改 `task_routes` 和 `session_tasks`。兼容别名仍为旧集成提供只读观察，所有
生产路径的写入都通过 `RuntimeTaskStateMachine`。

## Durable 状态和恢复

Python live 状态机只代表当前 Sidecar 进程，不替代 checkpoint：

- conversation transcript、active task checkpoint、Side-Git 保护记录和 `events.jsonl` 仍是
  崩溃恢复的权威数据；
- Sidecar 重启后从 checkpoint 注册 route；`finalize_pending` 直接进入 `finalizing`，不会重跑
  Agent；
- 前端重连后先读取 `workspace.open.active_tasks/recoveries`，再 replay journal；终态 event 才
  能释放对应 task；
- Sidecar 退出时前端保存所有活动任务所属的 `project_id + workspace`。新进程 ready 后先打开
  当前项目，再逐个显式打开后台项目，并用 `active_tasks + recoveries` 的并集分别对账；最后
  将 Sidecar 的兼容 active-project alias 重新指向当前可见项目；
- 恢复队列中的每一项都携带 `project_id`，`task.recover` 不依赖当前打开的 workspace。某个
  后台项目恢复失败只终止该项目任务，不影响其他项目；
- replay 中的历史事件只恢复展示，不会把已经结束的历史任务重新注册为 live task。

因此前端刷新、conversation 切换和 Python 进程重启不会依赖某个遗留的 `busy=true`，也不会
因为 UI 清除了 Stop 状态就让后端误以为任务已经结束。

## 所有权约定

| 状态 | 权威所有者 |
|---|---|
| 连接、焦点、submit/replay/cancel UI | FrontendRuntimeState |
| request/response 关联、timeout、scope | RuntimeClient |
| 跨 session Task/Approval 投影 | SupervisionStore |
| live task route 和合法阶段 | RuntimeTaskStateMachine |
| Agent/工具取消 Event | RuntimeSession |
| 崩溃恢复阶段和 terminal intent | TaskCheckpointStore |
| transcript / execution replay | conversation snapshot + EventJournal |
| 文件修改与 rollback | WorkspaceProtectionService |

新增任务阶段时，应先扩展 Python 状态机和协议事件，再让前端把事件映射成 action；不要重新在
`App.tsx` 中增加互相独立的布尔状态。
