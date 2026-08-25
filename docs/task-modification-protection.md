# StellarCode 任务修改保护设计

状态：Desktop Runtime v1 已实现

本文说明 StellarCode 桌面端如何在 Agent 修改项目时提供写入前差异预览、任务级
Side-Git 快照、任务完成后的差异查看，以及带冲突检测的一键回滚。对应运行时实现位于
`src/stellarcode/protection/`，桌面协议见
`docs/desktop-runtime-event-protocol.md`。

## 目标

修改保护必须满足以下约束：

1. Agent 开始执行工具前，先持久化整个工作区的任务基线。
2. `write_file`、`apply_patch` 和 `delete_file` 在执行前生成可审阅的文件级 diff。
3. 成功、失败和取消的任务都记录实际产生的工作区变化。
4. 用户可以查看任务 diff，并按任务撤销这次任务修改过的文件。
5. 回滚不能覆盖任务结束后产生的同路径修改；遇到冲突时必须停止。
6. 快照系统不得修改用户仓库的 `HEAD`、分支、refs、暂存区或提交历史。
7. Sidecar 崩溃恢复必须继续使用原任务基线，不能把崩溃后的部分修改当成新基线。
8. 并发任务必须在独立 Git worktree 中执行，最终改动通过冲突预检后串行合并。

## 三层保护

任务 worktree 是三层保护之前的执行隔离层。其完整生命周期和排除范围见
`docs/task-git-worktree-isolation.md`；以下 Side-Git PRE/POST 与 rollback 仍是合并回项目后
的审计和撤销依据。

### 1. 写入前文件预览

内置 `write_file`、`apply_patch` 和 `delete_file` 都配置了 previewer。工具调用开始前，Runtime
根据当前文件和目标内容生成 `change_preview`：

| 字段 | 含义 |
| --- | --- |
| `operation` | `create`、`modify`、`delete`、`no_change` 或 `unknown` |
| `path` | 工作区相对路径；工作区外文件使用绝对路径 |
| `workspace_scoped` | 该路径是否位于当前工作区内 |
| `rollback_protected` | 该路径是否属于任务级 Side-Git 快照和回滚范围 |
| `protection_reason` | 未保护原因：`outside_workspace`、`generated_or_internal_path`、`sensitive_path` 或 `preview_error` |
| `sensitive` | 是否命中敏感文件名、后缀或目录规则 |
| `binary` | 内容是否无法作为 UTF-8 文本比较 |
| `before_sha256` / `after_sha256` | 修改前后内容哈希；不存在的版本为 `null` |
| `additions` / `deletions` | 文本行增删统计 |
| `diff` | 最多 24,000 字符的 unified diff |
| `truncated` | diff 是否被截断 |
| `error` | 预览失败时的错误说明 |

预览会同时进入 `tool.started` 和受限模式下的 `approval.requested`。完整替换内容和
`apply_patch` 的精确 `old_text` / `new_text` 不会写进工具事件参数；事件中只保留长度
说明、编辑统计和有界 diff。

以下内容不会直接显示：

- `.env`、`.env.*`、`credentials.json`、`secrets.json`；
- `.key`、`.pem`、`.p12`、`.pfx`；
- `.ssh`、`.aws`、`.azure` 目录下的文件；
- 二进制内容。

敏感文件仍显示路径、操作类型和统计，但 `diff` 会替换为隐藏提示。二进制文件只显示
“内容已变化”，不生成文本 diff。工作区外路径、生成/内部路径和敏感路径还会显示为
“不受任务回滚保护”；这不阻止已获授权的工具执行，但用户不能误以为 Side-Git 可以撤销
这些路径的修改。

受限模式中，用户批准文件修改后，Runtime 会把预览时的路径和 `before_sha256` 作为
内部守卫传给文件工具。工具在文件修改锁内重新读取目标；如果审批后、真正写入前文件
已经变化，本次操作失败，并要求 Agent 重新读取和重新申请审批。这避免用户批准的是 A
版本，实际覆盖的却是稍后出现的 B 版本。`write_file` 与 `apply_patch` 使用临时文件、
`fsync` 和 `os.replace` 原子替换目标。`apply_patch` 只修改已存在的 UTF-8 文本文件；
每项 `old_text` 默认必须精确匹配一次，只有显式设置 `replace_all=true` 才允许批量替换。

完全访问模式不等待审批；此时预览用于展示，不代表用户确认。

### 2. 任务级 Side-Git 快照

每次 `task.submit` 都必须成功创建任务基线，否则 Runtime 拒绝接受任务。快照发生在
任务响应确认和任何 Agent 工具副作用之前。

快照数据位于：

```text
<app-data>/runtime/projects/<project-id>/workspace-protection/
├── objects.git             # 独立 bare Git object database 和 refs
├── snapshot.index          # 独立 index
├── workspace-protection.lock # 项目级跨进程 Side-Git 所有权锁
└── tasks/
    └── <task-token>.json   # 一个任务一份原子更新的元数据
```

每个任务使用独立 refs：

```text
refs/stellarcode/tasks/<task-token>/before
refs/stellarcode/tasks/<task-token>/after
refs/stellarcode/tasks/<task-token>/rollback-safety
refs/stellarcode/tasks/<task-token>/rollback-result
refs/stellarcode/tasks/<task-token>/rollback-recovered
refs/stellarcode/tasks/<task-token>/rollback-emergency
refs/stellarcode/tasks/<task-token>/rollback-cas
```

Side-Git 通过独立的 `GIT_DIR` 和 `GIT_INDEX_FILE` 保存对象和树。Python 按安全排除
规则遍历工作区，用 `git hash-object --no-filters` 按原始字节写入 blob，再通过独立 index
构建 tree/commit。它不会运行用户仓库的 `git add`、`git commit`、`reset` 或 `checkout`，
也不受用户 `.gitignore`、`.gitattributes`、LFS clean filter 或嵌套仓库 gitlink 语义影响，
因此既不会污染用户暂存区，也不会创建用户可见的提交。

任务结束时，无论结果是 `completed`、`failed` 还是 `cancelled`，Runtime 都会创建
`after` 快照并计算基线到结果的变化。任务终态事件携带 `changes` 摘要：

取消或工具批次超时可能先让 Agent 停止等待，但 Runtime 会在 POST 快照前等待所有已启动
工具 handler 退出。这样后台命令/MCP/通用工具不能在任务终态快照之后继续写入工作区；
如果第三方工具自身永久不返回，终态会相应等待，而不会虚假宣称已形成稳定回滚边界。

| 字段 | 含义 |
| --- | --- |
| `snapshot_id` | 前端防止对陈旧任务记录执行回滚的稳定 ID |
| `task_id` / `session_id` | 快照所属任务和会话 |
| `backend` | 当前为 `side-git` |
| `protected` | 是否成功建立保护；新任务要求为 `true` |
| `status` | `active` 或任务终态 |
| `has_changes` | 受保护工作区内是否存在变化 |
| `changed_files` | 路径、状态和逐文件增删统计 |
| `additions` / `deletions` | 总文本行统计；二进制文件按 0 计 |
| `diff_available` | 是否可请求任务 diff |
| `rollback_available` | 当前是否允许回滚 |
| `rolled_back` | 是否已经成功回滚 |
| `rollback_state` | `idle`、`in_progress`、`completed`、`failed` 或 `recovery_failed` |
| `rollback_recovery_event_pending` | 硬崩溃恢复结果是否仍等待写入事件日志 |
| `error` | 快照或回滚失败说明 |

文件状态为 `created`、`modified`、`deleted`、`type_changed` 或 `binary`。为避免大块代码
进入事件日志，完整任务 patch 不写入 `events.jsonl`，而是在用户点击“查看差异”后通过
`task.diff` 按需生成。

非 active 任务最多保留 100 条记录；超过限制时删除最旧记录及其任务 refs。active
记录不会因普通保留策略被删除。发生淘汰时还会执行 Side-Git 对象清理，避免已删除 refs
对应的不可达 blob 长期占用磁盘。

同一个项目的 `snapshot.index`、refs 和任务记录只能由一个 Runtime 进程持有。Runtime 启动
时获取 `workspace-protection.lock` 的操作系统文件锁，正常切换项目/退出时显式释放；进程硬
崩溃时由操作系统自动释放。第二个客户端若同时打开同一个项目，可以读取项目，但新任务
会因无法建立可靠基线而拒绝执行，不会让两套 Side-Git 事务交错。

### 3. 显式任务回滚

取消任务不会自动回滚。成功、失败和取消的任务只要有受保护变化，都可由用户点击
“撤销任务修改”或“回滚任务修改”发起 `task.rollback`。

回滚前必须满足：

- 当前没有 Agent 任务或 RAG 索引任务运行；
- `task_id`、`session_id` 和 `snapshot_id` 与持久化记录匹配；
- 请求显式携带 `confirmed: true`；
- 任务已经结束、存在受保护变化，且尚未回滚。

Runtime 首先创建 `rollback-safety` 快照，然后比较任务 `after` 与当前工作区。冲突检测
只关注该任务修改过的路径：

- 当前工作区只修改了无关路径：允许回滚，无关修改保持不变；
- 任务修改过的任一路径在任务结束后又发生变化：返回 `rollback_conflict`，不写入任何
  文件；
- 当前实现使用保守的路径级冲突判断，不尝试自动合并同一文件中的不同代码块。

第一次检查之后、真正修改工作区之前，Runtime 还会创建 `rollback-cas` 现场并再次比较
字节和文件类型。二次检查发现编辑器刚写入的新内容时，回滚在零写入状态下停止，且不会
运行补偿覆盖这次编辑。Windows 上，受影响文件或目录出现 Side-Git 无法表示的 NTFS
alternate data stream（ADS）时也按冲突处理。每次破坏性操作前还会拒绝经过 symlink、
junction 或其他 reparse-point 父目录的路径，避免目录别名把回滚重定向到其他位置。
多文件回滚还会在每一个删除/替换动作紧邻执行前重新核对该路径的 Git blob、类型和父
目录；如果外部编辑器在上一文件恢复后修改了下一文件，系统停止后续写入并进入安全补偿
或人工恢复状态，不会把晚到的内容当作任务结果删除。

没有冲突时，Runtime 只恢复任务 `changed_files` 中的路径：

- 任务新建的文件会被删除；
- 任务删除的文件会从 `before` 快照恢复；
- 任务覆盖的文件、可执行位和符号链接恢复为任务开始前状态；
- 不在任务变化集合内的文件不会被重写。

删除任务新建文件后不会顺带清理其空父目录。Git 无法证明目录归属，也不能表示 NTFS
目录 ADS；保留一个可能为空的目录，比误删任务之后添加的目录元数据更安全。

文件与目录互换时，Runtime 会在第一处写入前递归检查待删除目录。如果目录中存在
Side-Git 未采集的敏感文件、生成目录、junction/reparse point、空目录或其他无法证明属于
任务结果的内容，整次回滚返回冲突且不会修改任何文件。这避免恢复一个旧文件时顺带删除
后来放入同名目录的 `.env` 等内容。

文件恢复使用临时文件和原子替换。普通异常发生时，Runtime 尝试使用
`rollback-safety` 快照补偿已经写入的路径；成功后创建 `rollback-result`，将记录标为
`rolled_back: true`，并关闭再次回滚入口。

补偿前 Runtime 会先创建 `rollback-emergency` 并验证现场仍只可能是任务 `before` 与
`rollback-safety` 的组合。如果结果快照阶段恰好又出现第三方编辑、ADS 或无法证明安全
删除的内容，系统保留现场、关闭自动重试并要求人工检查，而不会用 safety 快照覆盖新内容。

## Runtime 时序

### 新任务

```text
Desktop task.submit
  -> Runtime 占用 task slot
  -> Side-Git 创建 before commit 和任务记录
  -> journal: workspace.snapshot.created
  -> 保存 user transcript
  -> 写 recovery/active-task.json（包含 workspace_snapshot）
  -> task.submit response
  -> journal: task.started（再次携带 protection）
  -> Agent / tools
  -> Side-Git 创建 after commit，原子更新任务记录
  -> journal: task.completed | task.failed | task.cancelled（携带 changes）
  -> 删除 active-task.json
```

事件先写入项目 `events.jsonl` 并 `fsync`，再发送给前端。终态的 `changes` 在终态事件
入日志前已经持久化，因此重放不会出现“显示可回滚、但找不到快照”的正常完成状态。
Runtime 在启动时和每次追加事件前都会检查 JSONL 尾部：如果最后一条 JSON 已完整、只是
缺少换行符，就补上分隔符；如果是进程崩溃留下的半条 JSON，就截断到上一条完整记录并
`fsync`。因此后续终态事件不会与残片拼接，下一次重启仍可把终态日志作为提交点。

### Sidecar 崩溃后的任务恢复

如果崩溃发生在任务终态之前：

1. Tauri 重启 Sidecar 并重新打开同一项目；
2. Runtime 读取 `active-task.json` 和原任务的 `workspace_snapshot`；
3. 前端先重放缺失事件，再打开原会话；
4. 前端发送 `task.recover`；
5. 恢复任务沿用原 `before` revision，不创建第二个基线；
6. 任务再次结束时，相对原始基线生成唯一的最终 `after` 和 `changes`。

恢复开始前还会校验 checkpoint 中的 `snapshot_id`、会话归属、任务记录与实际 Git tree
对象都仍然存在；任一项缺失时拒绝恢复，避免把崩溃后的部分工作区状态误当成新基线。

如果终态事件已经持久化、但 Sidecar 在删除 `active-task.json` 前退出，重新打开项目时
Runtime 根据事件日志确认该任务已经终止，只清理 active checkpoint，不重复执行任务。
此时 Side-Git 任务记录已在终态事件之前落盘。

如果 Agent 已经产生最终回答，但创建 `after` 快照暂时失败，Runtime 不会伪造正常终态，
也不会删除 `active-task.json`。它把 checkpoint 标为 `finalize_pending`，写入
`task.finalization.pending`，并仅重试 POST 快照；不会重新调用模型或工具。成功后才写入
原本的 `task.completed` / `task.failed` / `task.cancelled` 并清理 checkpoint。若 Sidecar
在此期间退出，下一次打开项目会从同一 checkpoint 继续最终快照步骤。

如果普通 Python 异常发生在回滚过程中，回滚函数会尝试用 `rollback-safety` 立即补偿。
如果进程在 `rollback_state=in_progress` 时被硬终止，下一次打开项目时
`WorkspaceProtectionService` 会先于任务恢复扫描任务记录。它先采集当前现场为
`rollback-emergency` revision，再验证每个相关 tree entry 只能等于回滚前 safety 状态
或任务 before 状态。只有这两种状态才可能是本次半回滚产生的结果，随后才会用
`rollback_safety_revision` 恢复该任务的 `changed_files`。如果重启前用户又修改了相关
文件，当前内容属于第三种状态，Runtime 会保留现场并标为 `recovery_failed`，绝不自动
覆盖人工修改。安全补偿本身是幂等的；如果恢复过程再次崩溃，下次启动可以继续验证和
执行相同恢复。

自动补偿成功时，Runtime 创建 `rollback-recovered` revision，将回滚记录改为 `failed`、
重新开放 `rollback_available`，并持久化 `rollback_recovery_event_pending: true`。这表示
原回滚没有完成，但工作区已恢复到发起回滚前，用户可以重新尝试。自动补偿失败时，记录
改为 `recovery_failed`、关闭回滚入口，并要求用户先人工检查受影响文件。

Sidecar 打开工作区后把上述待通知结果写成 `task.rollback.failed`：补偿成功使用
`code: "rollback_interrupted_recovered"`，补偿失败使用
`code: "rollback_recovery_failed"`。只有该事件已写入 `events.jsonl` 后，Runtime 才清除
pending 标志。因此如果 Sidecar 在“写日志”和“确认通知”之间再次退出，下次启动会重试
通知；前端必须按任务和事件序列幂等归并，不能把重试解释为第二次文件回滚。

## 协议与会话持久化

以下事件会进入项目事件日志，并包含在桌面执行细节重放集合中：

- `workspace.snapshot.created`
- `task.finalization.pending`
- `task.completed` / `task.failed` / `task.cancelled` 中的 `changes`
- `task.rollback.started`
- `task.rollback.completed`
- `task.rollback.failed`

前端重开会话后，通过重放终态和 rollback 事件恢复文件列表、diff 按钮、回滚按钮及
冲突状态。完整 diff 不随会话 snapshot 持久化；需要时重新调用 `task.diff`。

任务实际修改或成功回滚工作区后，Runtime 会清除当前 RAG 索引元数据，避免
`search_code` 继续返回修改前的语义索引。用户需要重新构建索引。

## Side-Git 安全边界

### 同一工作区的并发任务

不同对话可以同时运行，但两个任务若在时间上重叠并共享同一项目工作区，Side-Git
无法可靠判断某个文件变化究竟属于哪一个任务。Runtime 会把双方记录标记为
`change_attribution: shared_workspace_overlap`，保存重叠的 `concurrent_task_ids`，并将
`diff_available` 和 `rollback_available` 设为 `false`。界面仍可展示检测到的总体变化，
但不会提供可能覆盖另一任务结果的单任务 diff 或一键撤销。

不同项目有独立工作区与 Side-Git，因此不受此降级影响。若需要同一项目中完全独立、
可分别回滚的并发写任务，后续应采用每任务 Git worktree/容器隔离，而不是在共享目录中
猜测文件归属。

Side-Git 是工作区内容保护，不是系统级事务，也不是沙箱：

- 只覆盖当前工作区中 Side-Git 明确采集的文件；工作区外路径不在任务 rollback 范围内。
- Side-Git 使用 Python 安全遍历和 `hash-object --no-filters`，不会沿用用户 `.gitignore`
  决定保护范围。即使源文件被用户 ignore，它仍会进入任务快照，避免静默失去撤销能力；
  `.gitattributes` / Git LFS clean filter 也不会改变快照字节。
- 嵌套 Git 仓库的 `.git` 元数据仍排除，但其中普通工作树文件作为普通文件递归采集，
  不会退化成只记录 gitlink 而遗漏未提交内容。
- Windows junction 和其他 reparse point 不会被递归采集，防止快照越过工作区边界读取外部目录。
- 回滚不会通过 symlink/reparse-point 父目录执行；受影响路径上的 NTFS ADS 会触发安全冲突。
- 系统明确排除 `.git`、`.stellarcode`、虚拟环境、依赖目录、缓存和常见构建产物，包括
  `.venv`、`venv`、`node_modules`、`__pycache__`、`.pytest_cache`、`.mypy_cache`、
  `.ruff_cache`、`.tox`、`.nox`、`.gradle`、`.next`、`.turbo`、`target`、`dist` 和
  `build`。
- 系统也明确排除 `.env*`、常见密钥/证书文件、`.ssh`、`.aws` 和 `.azure` 等敏感路径，
  防止秘密写入 Side-Git 对象库。写入预览会以 `rollback_protected: false` 和稳定的
  `protection_reason` 明示这些路径不能通过任务回滚恢复。
- `execute_command`、MCP 工具和外部程序在工作区内留下的最终文件变化可被任务快照
  发现，但它们对数据库、网络服务、注册表、其他目录或远程系统造成的副作用无法撤销。
- `full-access` 只改变审批策略，不扩大 Side-Git 的回滚范围。
- Side-Git 需要本机可用的 Git。无法初始化独立仓库时，Desktop Runtime 拒绝新任务，
  不会静默降级为无保护写入。
- 快照对象包含被采集文件的完整内容，保存在当前用户的本地应用数据目录；上述敏感路径
  被显式排除。用户仍应通过操作系统权限保护应用数据目录。
- Side-Git 通过项目级操作系统锁防止多个 Runtime 同时管理同一个项目保护库。其他编辑器或
  外部进程仍可修改工作区；StellarCode 通过提交前/回滚前二次快照和冲突检测保守地拒绝
覆盖可见的晚期修改，但这不等价于操作系统级文件事务或容器隔离。
- Git 不表示空目录：如果一个任务只创建或删除空目录而没有任何文件变化，这个目录本身
  不会出现在任务 diff 中；在文件/目录类型回滚时发现未知空目录则会保守地拒绝删除。

因此，界面中的“撤销任务修改”准确含义是：在没有同路径后续修改的前提下，将该任务
改变过的受保护工作区路径恢复到任务开始前。它不等价于撤销整台电脑上的所有副作用。
