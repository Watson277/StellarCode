# 每任务 Git worktree 隔离

状态：Desktop Runtime v1 已实现

StellarCode 不再让并发任务直接在同一个项目目录中执行内置文件修改和命令。每个任务
在接受前都会从该任务的 Side-Git PRE 快照创建一个独立 Git worktree；任务完成、失败
或取消后，再把这个 worktree 相对基线产生的改动安全合并回项目目录。

## 生命周期

1. `task.submit` 生成任务 ID。
2. Side-Git 以原始字节捕获项目 PRE 快照，不读取或修改用户仓库的 index、HEAD、分支或 refs。
3. Runtime 为任务创建一个私有 bare Git 仓库，只从共享 Side-Git 抓取该任务的 PRE ref。
   仓库和 worktree 分别位于 `<app-data>/.../workspace-protection/r/<short-hash>.git` 与
   `.../w/<short-hash>`。短目录名用于避开 Git for Windows 内部 `.git/worktrees/...`
   路径长度限制；协议、保护记录和 Git ref 中仍保存完整任务 ID。任务内执行的 `git commit`、
   `git branch` 或 `git update-ref` 只影响这个私有仓库，不能改写共享 Side-Git 或用户仓库。
4. ReAct、Plan 和 Team 的内置文件工具、`glob_files`、`grep_code` 以及
   `execute_command` 的 cwd 都路由到这个任务 worktree。命令参数中显式出现的项目根路径
   也会替换为 worktree 根路径。Runtime 会向 Agent 明确注入规范项目根目录与临时 worktree
   的区别；面向用户报告文件位置时必须映射回规范项目根目录，不能暴露临时路径。
5. 终态阶段等待该任务的工具完全退出，在项目级锁内暂存 worktree 改动并生成 binary patch。
6. Runtime 先捕获“合并前”项目快照，再执行 `git apply --check`。检查通过才应用整个 patch。
7. 合并后立即捕获 POST 快照，生成准确的任务 diff 与 rollback 元数据，然后清理 worktree。

这一顺序使不同会话的任务可以并行思考和执行，但对项目目录的最终写入是串行、可审计的。
两个任务修改不同文件或同一文件的不同上下文时可以依次合并；修改相同上下文时，后合并的
任务进入 `worktree_merge_conflict`，项目文件保持不变，冲突 worktree 会被保留供人工检查。
Runtime 启动时会清理由旧版本或中断创建遗留、且没有任何任务保护记录引用的私有仓库；
仍被 active、finalize-pending 或冲突记录引用的 worktree 与仓库不会被自动删除。
终态生成 patch 前还会重新校验 `.git` 指针，并把 Git 命令固定到记录中的任务私有仓库；
如果任务或外部程序改写了 Git 元数据，Runtime 会停止合并并保留现场，而不会跟随该指针。

隔离任务不允许通过 `Start-Process`、`Start-Job`、`nohup`、`setsid` 或 `start /b`
启动脱离工具生命周期的后台进程。这类进程会继续占用临时 worktree，导致合并后的目录无法
清理，并可能让开发服务器继续读取已经过期的副本。构建、测试等有限时长的前台命令不受影响。

## 自定义临时目录

桌面端可在 **设置 → 通用 → 临时 worktree 位置** 选择一个已存在的绝对目录。设置留空时，
worktree 继续位于系统盘的 Tauri 应用数据目录；例如选择 `E:\StellarCodeTemp` 后，新任务使用
`E:\StellarCodeTemp\projects\<project-id>\w\<task-hash>`。设置保存后需要重启 Runtime，且只影响
新创建的任务，不自动迁移已有任务或恢复现场。

该设置只移动任务的完整临时 worktree。压缩的 Side-Git 对象、任务记录、回滚元数据、会话、
Memory 与事件日志仍保存在应用数据目录。自定义目录不能位于项目工作区内部，也不能包含项目
工作区，以免内部临时文件被项目快照再次收集。

## 崩溃恢复

任务 checkpoint 同时保存 Side-Git snapshot ID 与 worktree 路径。Sidecar 重启后只有在以下
条件都成立时才允许恢复 Agent：

- 任务保护记录仍为 active；
- PRE Git tree 对象仍存在；
- worktree 位于 StellarCode 管理目录中且 `.git` 元数据有效。

合并状态会先持久化为 `applying`。如果进程在 `git apply` 后崩溃，重启会用 reverse-check
辨认 patch 是否已经应用，而不会盲目重复写入。POST snapshot ref 仍是终态提交依据。

## 隔离范围

隔离覆盖 StellarCode 内置的相对文件操作、精确代码搜索和命令 cwd。以下内容不在任务
worktree 中：

- `.git`、`.stellarcode`；
- `.venv`、`venv`、`node_modules` 和常见构建/缓存目录；
- `.env`、私钥和凭据类敏感路径；
- Windows junction/reparse point；
- 工作区外的绝对路径。

这些排除项与 Side-Git 修改保护范围一致。Python Runtime 已加载的模型配置不依赖 worktree
里的 `.env`；内置写入、补丁和删除工具会明确拒绝修改这些无法随终态 patch 合并的路径，
而不是先报告成功再静默丢弃。项目命令如需依赖目录，应使用系统/项目既有环境或在任务
worktree 内显式准备。

第三方 MCP、浏览器和外部进程有自己的路径与权限模型。StellarCode 不会声称能隔离它们对
工作区外资源、数据库、网络服务或其他应用的副作用；受限模式审批仍然是这些操作的授权边界。

## 终态语义

- `completed`：合并任务 worktree 的实际改动，然后记录成功终态。
- `failed`：保留并尝试合并失败前已经产生的工作区改动，与旧版“失败不自动回滚”一致。
- `cancelled`：停止工具后合并取消前的改动；用户仍可使用任务卡片的 Rollback。
- `worktree_merge_conflict`：不修改项目目录，任务终态改为 failed，保留 worktree。

成功合并后的 Undo 仍由 Side-Git 的逐路径哈希冲突检测完成，不使用用户仓库的
`git reset --hard`，也不会覆盖任务之后的同路径编辑。
