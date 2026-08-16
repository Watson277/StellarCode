# Windows 指令执行异常与审批卡死问题复盘

> 日期：2026-08-09  
> 项目：StellarCode Desktop  
> 分支：`desktop-client`  
> 涉及模块：Tauri Runtime、Python Sidecar、`execute_command`、HITL 审批、React 工具状态展示

## 1. 问题摘要

用户在 StellarCode 中要求 Agent 执行 `conda activate llm1`。Agent 进行了多轮环境检查，最后同时提交了两条工具调用。界面中的两个工具一直显示为 `Running`，看起来像 Conda 或 Python 再次卡死，直到用户点击 Stop 才结束。

最终确认这是两个问题叠加后的结果：

1. Agent 前几轮生成了不适用于 Windows PowerShell 或不能跨进程生效的 Conda 命令，因此进行了过多的恢复和排查。
2. 最后两条命令并没有真正启动，而是被 HITL 审批批次阻塞。第二个审批已经产生，但被第一个审批稍后到达的 `approval.resolved` 事件从前端错误清除，形成了“工具显示 Running、界面却没有审批按钮”的假死状态。

因此，最后的卡死并不是 Python 解释器、Conda 或操作系统命令本身运行超时，而是一个前端审批状态竞态问题。

## 2. 用户观察到的现象

问题发生期间出现过以下现象：

- `nvidia-smi`、`Get-ChildItem`、`Get-Content` 等命令可以正常执行。
- `conda activate llm1`、`pip list`、`py_compile` 曾出现失败或看似超时。
- 部分命令输出包含乱码或异常的 Windows 工作目录表示。
- Agent 为一个简单的激活环境请求连续执行了多轮命令。
- 最后两个工具卡片同时显示 `Running`，但长时间没有输出。
- 点击 Stop 后，工具才返回取消结果。

这些现象容易让人误以为“所有 Python 解释器在启动阶段卡死”，但 Trace 表明最后一次故障发生时，两条命令根本没有进入子进程执行阶段。

## 3. 故障过程还原

本次会话中，Agent 最多允许 8 次 ReAct 迭代。主要执行过程如下：

| 迭代 | Agent 行为 | 结果 |
|---|---|---|
| 1 | 生成 `execute_command` 参数 | `command` 值没有正确加引号，工具参数不是合法 JSON |
| 2 | 使用 `&&`、`which`、`$CONDA_DEFAULT_ENV` | 使用了 Bash 语法，与 Windows PowerShell 5.1 不兼容 |
| 3 | 直接运行 `conda activate llm1` | 命令进程结束后仍显示 `base`，Python 仍是 StellarCode `.venv` |
| 4 | 执行 `conda env list` | 成功定位 `llm1`：`E:\envs\llm1` |
| 5 | 再次直接执行 `conda activate` | 仍未得到期望环境 |
| 6 | 执行 `conda run` 和 Conda 信息检查 | 命令很快结束，但解释器结果不符合 Agent 预期，因此继续排查 |
| 7 | 检查 `E:\envs\llm1` 目录 | 确认环境及 `python.exe` 实际存在 |
| 8 | 并行提交两条最终检查命令 | 卡在审批阶段，命令没有真正启动 |

最后一次迭代的关键事件时间线如下：

```text
16:17:29.302  两个 tool.started 事件到达前端
16:17:29.303  第一个 approval.requested
              用户允许第一个审批
16:17:34.614  第二个 approval.requested
16:17:34.614  第一个 approval.resolved
              前端错误清空当前审批，第二个审批从界面消失
16:20:31      用户点击 Stop
              第二个审批因任务取消而拒绝，整个批次结束
```

Trace 中没有这两条命令的真实子进程执行结果。第一个工具虽然已批准，但工具注册表会先收集整个批次的审批；第二个工具未完成审批，因此第一个工具也没有开始执行。

## 4. 根因分析

### 4.1 Agent 生成命令不符合当前 Shell

StellarCode Desktop 在 Windows 上使用 PowerShell 执行字符串命令，但模型先后生成了 Bash 风格的：

```text
command1 && command2
which conda
$CONDA_DEFAULT_ENV
```

PowerShell 5.1 不支持这里使用的 Bash `&&` 语义，`which` 也不是标准 PowerShell 定位命令。正确形式应使用 PowerShell 语法，例如分号、`Get-Command` 和 `$env:CONDA_DEFAULT_ENV`。

第一次调用还包含非法 JSON，使得实际排查尚未开始就浪费了一次迭代。

### 4.2 对 `conda activate` 的进程边界理解错误

`conda activate` 修改的是当前 Shell 进程的环境变量。StellarCode 每次 `execute_command` 都会创建独立子进程，因此：

```text
工具调用 A：conda activate llm1
工具调用 B：python -V
```

调用 A 中的环境修改不会保留到调用 B。即使调用 A 成功退出，也不能据此认为后续工具已经进入 `llm1`。

如果只需要运行目标环境中的 Python，最稳定的方式是直接调用：

```powershell
& "E:\envs\llm1\python.exe" -m pip list
```

如果确实要在同一个 PowerShell 子进程内激活并继续执行，则所有步骤必须放在同一次工具调用中，并先加载 Conda PowerShell hook：

```powershell
conda shell.powershell hook | Out-String | Invoke-Expression
conda activate llm1
python -V
```

这种激活仍然只在该次工具调用的子进程内有效。

### 4.3 Windows 工作目录和 Runtime 环境曾污染工具子进程

Tauri 对 Windows 路径进行规范化时可能保留 `\\?\` 前缀。Python 可以使用这种路径，但 CMD、PowerShell 和 Conda 有时会把它当作特殊或 UNC 风格路径处理，引发工作目录异常。

另外，Tauri 为启动 Python Sidecar 设置的 Runtime 专用 `PYTHONPATH` 如果继续传给工作区命令，会污染由 `execute_command` 启动的 Python/Conda 子进程。工具子进程也不应该继承 Sidecar 的标准输入管道，否则交互式程序可能等待永远不会到来的输入。

这些问题不是最后两个工具“假死”的直接原因，但它们会导致前几轮检查结果不稳定，促使 Agent 继续进行不必要的环境诊断。

### 4.4 单值审批状态导致事件竞态

旧前端只保存一个审批：

```tsx
const [approval, setApproval] = useState<ApprovalEvent | null>(null);

approval.requested -> setApproval(message)
approval.resolved  -> setApproval(null)
```

这里没有检查 `approval_id`。当事件以如下顺序到达时：

```text
第二个 approval.requested
第一个 approval.resolved
```

第二个审批刚被保存，就会被第一个审批的 resolved 事件无条件清空。

这是一种典型的异步事件竞态：事件本身都合法，但前端把“某一个审批已结束”错误解释成“所有审批都结束了”。

### 4.5 审批事件没有携带真实工具调用 ID

旧 Runtime 将 `approval_id` 同时填入 `tool_call_id`：

```text
tool_call_id = approval_id
```

因此前端无法把审批与原始工具卡片可靠关联。通过名称或参数查找也不安全，因为同一批次可能包含名称和参数相同的多个工具调用。

### 4.6 工具卡片过早且错误地显示 Running/OK

旧流程在进入审批前就发送 `tool.started`，前端收到后立即显示 `Running`。实际上该工具可能仍在等待批准。

同时，工具卡片左侧状态文本被固定写成 `OK`，不论工具是在等待、运行、成功还是失败，都会产生误导。

## 5. 修复方案

### 5.1 统一 Windows 子进程工作目录

新增 `subprocess_safe_path()`，在路径进入 Runtime 和工具系统时移除 Windows verbatim 前缀，确保传给 Shell/Conda 的路径使用普通盘符形式：

```text
E:\study2\LLM Internship\pro1
```

而不是：

```text
\\?\E:\study2\LLM Internship\pro1
```

Tauri 项目存储也会迁移已有的异常路径记录。

### 5.2 隔离 Sidecar 与工作区命令环境

Tauri 启动 Sidecar 时分别记录：

- Runtime 自己需要的 `PYTHONPATH`
- 用户启动 StellarCode 前原有的 `PYTHONPATH`

执行工作区命令时会移除 Runtime 专用路径，并恢复用户原始环境。`execute_command` 同时使用：

```python
stdin=subprocess.DEVNULL
```

防止工作区程序读取 Sidecar 的 JSONL 标准输入通道。Windows 命令还会使用独立进程组，便于超时或 Stop 时终止完整进程树。

### 5.3 审批状态由单值改为队列

前端现在保存：

```tsx
const [approvalQueue, setApprovalQueue] = useState<ApprovalEvent[]>([]);
```

新的 `approval.requested` 按到达顺序加入队列，并按 `approval_id` 去重。UI 每次展示队首审批，处理完成后自动显示下一项。

### 5.4 resolved 只删除匹配审批

前端不再执行全局 `setApproval(null)`，而是仅删除 ID 相同的审批：

```tsx
setApprovalQueue(current =>
  current.filter(item => item.data.approval_id !== resolvedApprovalId)
);
```

因此，即使第二个 requested 先于第一个 resolved 到达，第二个审批仍会保留在队列中。

### 5.5 审批关联真实 `tool_call_id`

`ApprovalRequest` 新增工具调用 ID。`HitlToolRegistry` 创建审批时传入原始 `ToolInvocation.id`，Runtime 再将其放入 `approval.requested.data.tool_call_id`。

现在两个标识职责明确：

| 标识 | 用途 |
|---|---|
| `approval_id` | 识别和解决某一条审批 |
| `tool_call_id` | 识别并更新某一个工具调用 |

即使同时执行两个完全相同的命令，也不会更新错工具卡片。

### 5.6 新增完整工具状态机

工具卡片现在使用以下四种状态：

| 内部状态 | UI 标记 | 含义 |
|---|---|---|
| `waiting_approval` | `WAIT` | 工具正在等待用户审批 |
| `running` | `RUN` | 已获批准或无需审批，正在执行 |
| `completed` | `OK` | 工具执行成功 |
| `failed` | `FAIL` | 工具失败、被拒绝或被跳过 |

`approval.requested` 将对应工具改为 `WAIT`；批准后改为 `RUN`；最终由 `tool.completed` 或 `tool.failed` 更新为 `OK` 或 `FAIL`。

工具卡片按 `tool_call_id` 原地更新，不再为 started 和 completed 分别创建重复卡片。

前端还增加了状态防倒退保护：如果一个很快结束的工具已经收到 `tool.completed`，稍后到达的 `approval.resolved` 不会把 `OK` 覆盖回 `RUN`。

## 6. 修复后的事件流程

```text
tool.started
    │
    ├─ 无需审批 ───────────────> RUN -> OK / FAIL
    │
    └─ approval.requested ─────> WAIT
                                  │
                                  ├─ approve -> RUN -> OK / FAIL
                                  └─ reject  -> FAIL
```

批量审批时：

```text
approval A requested -> queue [A]
approval A approved
approval B requested -> queue [A, B]
approval A resolved  -> queue [B]
UI 自动展示 approval B
```

旧竞态中最关键的 `[B] -> []` 错误清空已经不会再发生。

## 7. 是否破坏原有设计

本次修复没有通过关闭审批或降低安全策略来绕过问题，原有设计仍然成立：

- Normal 模式下，高风险操作仍必须审批。
- Full access 模式仍按原设计绕过 Runtime 审批。
- PathGuard、命令策略和工具执行边界不变。
- 并行工具仍然可以并行执行。
- HITL 注册表仍可在执行批次前收集必要审批。
- Stop 仍会拒绝待处理审批并终止已启动的命令进程树。
- RuntimeEvent 仍是 UI 与 Python Agent 之间的唯一状态事实来源。

修复只是让 UI 能准确表示 Runtime 的真实状态，并让每个审批和工具调用拥有稳定的一一关联。

## 8. 验证结果

修复完成后执行了以下验证：

```text
前端 TypeScript/Vite 构建：通过
Python 全量测试：194 passed
审批与并行工具针对性测试：21 passed
Rust/Tauri 测试：4 passed
git diff --check：通过
```

用户随后重新运行客户端并确认多审批流程已经成功，不再出现第二个审批消失和工具永久显示 Running 的情况。

## 9. 后续防止同类问题的建议

### 9.1 Runtime/UI 协议

- 所有异步对象都必须使用稳定 ID 更新，不能用“收到结束事件就清空全部状态”的方式处理。
- 终态事件不得把已经完成的状态倒退为中间状态。
- 新增并行工具能力时，必须测试事件乱序或近同时到达的情况。
- `tool.started`、等待审批和真正开始执行的语义可以在后续协议版本中进一步拆分得更严格。

### 9.2 Windows 命令生成

- System prompt 应明确当前 Shell 类型和版本。
- Windows PowerShell 中不要生成 Bash 的 `which`、`$VAR` 或不兼容的 `&&`。
- 对“执行一条明确命令”的请求限制无意义的探索次数；语法修复后仍失败时，应向用户报告证据，而不是自动耗尽 8 轮。
- 运行 Conda 环境程序时优先使用目标环境解释器绝对路径，避免依赖跨工具调用的 `conda activate` 状态。

### 9.3 回归测试

后续至少应长期保留这些测试场景：

1. 同一批次产生两个需要审批的工具。
2. 第二个 `approval.requested` 早于第一个 `approval.resolved` 到达前端。
3. 两个工具名称和参数完全相同，但 `tool_call_id` 不同。
4. 工具极快完成，terminal event 早于 resolved event 被前端处理。
5. 等待审批期间点击 Stop。
6. Windows 工作区路径包含空格、中文或历史 `\\?\` 前缀。
7. Sidecar 设置 Runtime `PYTHONPATH` 后，工作区 Python 仍只收到用户原始环境。

## 10. 最终结论

这次问题表面上表现为“Conda/Python 命令卡死”，实际由两层原因组成：前半段是 Agent 对 Windows Shell 和 Conda 进程边界处理不正确，导致执行步骤过多；最后的永久等待则是前端单审批状态与异步事件顺序共同造成的审批丢失。

通过统一 Windows 子进程路径和环境、使用审批队列、严格匹配 `approval_id`、传递真实 `tool_call_id`，并建立 `WAIT/RUN/OK/FAIL` 工具状态机，问题已经从根因上修复，同时保留了 Normal 模式下原有的安全审批设计。
