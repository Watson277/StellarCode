# StellarCode Desktop：多项目与多对话设计

状态：Phase A-C 已实现，等待 UI 验收与 Phase D 完整回归  
目标分支：`desktop-client`

## 1. 目标

桌面客户端需要支持：

- 注册已有本地目录为项目；
- 在指定父目录下创建新项目目录；
- 保存最近打开的多个项目；
- 每个项目创建、切换、重命名和删除多个对话；
- 切换对话后恢复可见 transcript 和 Python Agent 上下文；
- 项目之间严格隔离 workspace、短期记忆、RAG、MCP 和审批状态；
- 从项目列表移除项目时默认不删除用户的真实目录。

本阶段不支持同时执行多个项目中的任务。一个桌面窗口只有一个 active project 和
一个 active conversation；后台多项目并发留到后续版本。

## 2. 所有权边界

| 数据/能力 | 权威拥有者 | 原因 |
|---|---|---|
| 项目注册表、最近打开时间 | Tauri/Rust | Runtime 启动前也必须可读取 |
| 目录选择、目录创建、文件树 | Tauri/Rust | 使用原生路径能力并集中校验 |
| Agent messages、工具历史、短期记忆 | Python Runtime | 避免 React 保存一份不完整的 Agent 上下文 |
| 对话 transcript | Python Runtime | 与 Agent 上下文以同一事务持久化 |
| 当前 UI 选择和临时展开状态 | React | 仅属于显示层，可随时重建 |
| API Key、模型配置 | `.env`/Python | 不进入项目注册表或对话文件 |

React 不直接读写项目文件，也不把自己的消息数组当作长期数据源。UI 展示的数据来自
Runtime 的 session snapshot 与 RuntimeEvent。

## 3. 数据模型

### 3.1 ProjectRecord

```json
{
  "id": "project-uuid",
  "name": "stellarcode",
  "path": "E:\\study2\\LLM Internship\\PaiCLI\\stellarcode",
  "canonical_path": "e:\\study2\\llm internship\\paicli\\stellarcode",
  "created_at": "2026-08-05T10:00:00.000Z",
  "last_opened_at": "2026-08-05T10:30:00.000Z"
}
```

规则：

- `id` 使用 UUID，不从路径计算，允许用户移动后重新绑定；
- Windows 路径去除末尾分隔符并进行大小写无关的重复检测；
- 项目路径必须是已存在的可读目录；
- “创建项目”只创建一个新的空目录，不自动生成代码模板；
- “移除项目”只删除注册记录，不触碰真实项目目录。

### 3.2 ConversationRecord

```json
{
  "schema_version": 1,
  "id": "session-uuid",
  "project_id": "project-uuid",
  "title": "实现登录页面",
  "mode": "react",
  "created_at": "2026-08-05T10:05:00.000Z",
  "updated_at": "2026-08-05T10:20:00.000Z",
  "transcript": [],
  "agent_messages": [],
  "short_term_memory": {},
  "last_sequence": 0
}
```

标题规则：新对话初始名为 `New conversation`，第一次提交任务后使用用户提示的首行生成
标题，最多 48 个字符；用户重命名后不再自动覆盖。

`transcript` 是适合 UI 恢复的有限展示记录；`agent_messages` 是模型下一轮真正需要的上下文，
二者不能互相替代。工具结果可以被截断展示，但 Agent 上下文继续遵守现有压缩规则。

## 4. 本地存储

Tauri 通过 `app.path().app_data_dir()` 确定应用数据根目录，不硬编码用户目录。

```text
<app-data>/
├── projects.json
└── runtime/
    └── projects/
        └── <project-id>/
            ├── project-state.json
            └── conversations/
                └── <conversation-id>.json
```

- `projects.json` 由 Rust 原子写入；
- 对话文件由 Python 原子写入：写临时文件、flush 后 `os.replace()`；
- 不把对话记录写进用户项目仓库，避免污染 Git；
- 对话可能包含源码、提示和工具结果，默认视为敏感本地数据；
- 删除对话只删除 app-data 中对应文件，不修改项目文件；
- v1 不加密本地对话文件，后续可增加系统密钥链与静态加密。

## 5. Runtime 结构

```text
SidecarServer
└── ActiveWorkspaceRuntime (最多一个)
    ├── workspace path / project id
    ├── ToolRegistry / HITL / MCP / RAG / Browser
    └── ConversationRuntime (多个)
        ├── Agent / PlanAgent / TeamAgent
        ├── MemoryManager
        ├── transcript
        └── active task (最多一个)
```

同一项目的对话共享耗时较高、且与 workspace 绑定的工具与服务；每个对话独立拥有模型历史和
短期记忆。长期记忆是否跨对话共享继续沿用项目级 `MemoryManager` 约定，但短期对话内容不能串线。

切换项目执行以下顺序：

1. 拒绝当前待审批请求；
2. 若有正在执行的任务，拒绝切换；
3. 持久化所有 dirty conversations；
4. 关闭 MCP、Browser、Trace 等 workspace 资源；
5. 校验新目录并创建新的 `ActiveWorkspaceRuntime`；
6. 返回对话列表，并按需打开最近对话。

## 6. Tauri 命令

项目注册表使用 Tauri command，不经过 Python JSONL：

| 命令 | 用途 |
|---|---|
| `project_list` | 返回按最近打开时间排序的项目 |
| `project_register` | 注册已有目录，重复路径返回已有项目 |
| `project_create` | 在父目录下创建并注册新目录 |
| `project_remove` | 仅移除注册记录 |
| `project_touch` | 更新最近打开时间 |
| `workspace_list_entries` | 读取当前项目的一层目录内容 |

目录选择使用 Tauri Dialog 插件；插件只把用户选择的路径交给 Rust，最终仍由
`project_register` 做规范化和目录校验。

文件树采用按需加载而不是启动时递归扫描：展开目录时调用 `workspace_list_entries`，默认忽略
`.git`、`.venv`、`node_modules`、`target`、`__pycache__` 等重目录。

## 7. Runtime 协议扩展

### 7.1 请求

```text
workspace.open
workspace.close
session.list
session.create
session.open
session.rename
session.delete
session.reset
session.set_mode
task.submit
```

关键参数：

```json
{
  "method": "workspace.open",
  "params": {
    "project_id": "project-uuid",
    "workspace": "E:\\code\\project",
    "data_dir": "<app-data>\\runtime"
  }
}
```

`session.create` 不再隐式创建整个 workspace Runtime；必须先成功执行 `workspace.open`。

### 7.2 事件

```text
workspace.opened
workspace.closed
session.listed
session.created
session.opened
session.renamed
session.deleted
session.snapshot
```

`session.snapshot` 用于首次打开或切换对话，携带可展示 transcript；实时增量继续使用现有
`task.*`、`assistant.*`、`tool.*` 和 `approval.*` 事件。

## 8. 并发与一致性

- 一个 conversation 同时最多一个 task；
- active workspace 同时最多一个 task，先保持与当前 Runtime 的共享 Agent 资源兼容；
- task 运行期间禁止项目切换和对话删除；
- 允许查看其他对话元数据，但 v1 不在任务运行期间切换 active conversation；
- 所有 RuntimeEvent 继续按 session 独立递增 `sequence`；
- UI 只接受当前 active conversation 的 transcript 事件，其他会话事件保存在状态表中；
- 写盘失败时任务结果仍显示，但对话标记为 `persistence_error`，不能伪装成已保存。

## 9. 安全约束

- Rust 对所有传入路径执行规范化，文件树子路径必须位于 project root；
- 不提供“递归删除项目目录”命令；
- 创建项目时项目名不能包含路径分隔符、`.` 或 `..`；
- Python 收到 `workspace.open` 后再次解析路径并建立自己的 workspace 边界；
- 项目切换清空会话审批白名单；
- transcript 写盘前应用密钥脱敏；
- 项目注册表不存 API Key、环境变量值或完整工具输出。

## 10. 实现阶段

### Phase A：项目注册表与真实文件树

- Rust `ProjectStore`；
- Dialog 目录选择；
- 注册、创建、移除项目；
- 懒加载文件树；
- React 项目切换状态。

### Phase B：Runtime 多对话

- 拆分 `ActiveWorkspaceRuntime` 与 `ConversationRuntime`；
- 实现 session CRUD/open/snapshot；
- 保持 ReAct、Plan、Team 与 HITL 功能。

### Phase C：持久化与恢复

- 原子保存 conversation；
- 启动时加载对话列表；
- 自动标题、重命名、删除；
- 恢复 transcript 和 Agent messages。

### Phase D：验收

- 两个项目互相切换不串 workspace；
- 每个项目至少创建三个对话；
- 重启客户端后项目、对话和上下文可恢复；
- 删除注册记录不删除真实目录；
- 非法路径、重复路径和任务中切换均有明确错误。
