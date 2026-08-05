# PaiCLI Java 原版与 StellarCode Python 版逐章对比报告

> 对比对象：`paicli-main` 当前 Java 实现与 `stellarcode` 当前 Python 实现  
> 对比时间：2026-08-04  
> 结论依据：以当前代码行为为准，PDF 和阶段文档仅用于解释设计背景。

## 一、总体结论

Python 版不是 Java 版的逐行翻译，而是围绕十二个教学章节重建的一套较轻量实现。两者的核心概念基本对齐，但产品定位不同：

| 维度 | Java 原版 | Python 版 |
|---|---|---|
| 定位 | 持续演进的产品级 Agent CLI | 便于学习、调试和扩展的章节化复刻 |
| 执行模型 | 流式 LLM、JLine Renderer、长上下文治理 | 同步 LLM 请求、Rich/标准终端、结构更直接 |
| 模型生态 | GLM、DeepSeek、Step、Kimi、讯飞、Agnes 等 | 当前主要接入 GLM，文本与视觉模型自动路由 |
| 安全边界 | HITL 之外还有 PathGuard、CommandGuard、AuditLog | 两种访问模式和 HITL，但没有同等级文件沙箱 |
| 工具体系 | 11 个内置工具、MCP、资源、实时代码搜索 | 8 个主要内置工具、MCP 核心、RAG |
| 工程成熟度 | 上下文压缩、TUI、LSP、快照、Runtime API 等较完整 | 十二章主功能完整，代码短小，测试和二次开发更轻便 |

因此，Python 版已经覆盖课程主线，但不能简单理解为“Java 版换了一种语言”。Java 版更重视长期运行、交互体验和防护边界；Python 版更重视可读性、快速实验，以及对本项目使用方式的定制。

---

## 第一章：ReAct 与 Tool Call

### 共同点

两版都采用相同的基本闭环：用户输入进入 LLM，模型决定直接回答或输出 `tool_calls`，Agent 执行工具，把结果作为 `tool` 消息回灌，再由模型继续推理。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| 循环预算 | `AgentBudget` 默认硬上限 50 次，并检测连续 3 次相同调用 | 默认最多 8 次；连续两次相同失败会提前终止 |
| 达到上限 | 由预算和停滞保护终止 | 额外发起一次禁用工具的最终总结请求 |
| LLM 输出 | 支持流式 content 和 reasoning 展示 | 当前以同步 HTTP 响应为主 |
| 工具基础集 | 包含 `glob_files`、`grep_code`、`create_project`、`revert_turn` | 基础文件、命令、RAG、Web 工具，更精简 |
| 终端反馈 | Renderer 展示 thinking、工具状态、token、耗时 | 输出每轮调用、成功/失败及停止原因，结构更朴素 |
| 工具返回 | 统一工具结果并支持文本/图片内容 | `ToolOutput` 同样支持文本和图片内容块 |

### 影响与判断

- Java 版适合复杂长任务，容许更多迭代，同时用停滞检测防止无限循环。
- Python 版的 8 轮更易观察和控制成本，但搜索、浏览器这类多跳任务更容易触顶。
- Python 版曾经只显示 `Stopped after 8 iterations`，当前已经补充每轮调用、错误和最终总结，因此调试体验比最初版本完整。
- Java 的实时流式渲染更像成熟 Code Agent；Python 的同步结构更适合阅读 ReAct 主循环。

**本章结论：** 核心机制等价，Java 版的运行时治理和交互成熟度更高，Python 版更容易学习和修改。

---

## 第二章：Plan-and-Execute 与 DAG

### 共同点

两版都有 Planner、ExecutionPlan、任务依赖、并行批次和执行结果汇总。模型先生成结构化计划，再依据依赖关系执行。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| 拓扑处理 | `ExecutionPlan` 使用 DFS 检测环并生成拓扑序 | 使用 Kahn 思路，不断提取入度为 0 的可执行批次 |
| 计划审阅 | 执行前有 Enter 执行、`I` 补充、ESC 取消、Ctrl+O 展开 | 当前生成计划后直接进入执行 |
| 重规划 | 任务失败后可调用 `Planner.replan(...)` | 主要记录失败并阻断依赖任务，没有完整交互式重规划 |
| 并发批次 | 同层无依赖任务最多 4 个并发执行 | 同样按 ready batch 并行，默认最多 4 个 |
| 计划解析 | Java record/类模型配合 Jackson | Python dataclass 和 JSON 归一化，自动修正任务 ID |

### 影响与判断

Python 的 Kahn 批次算法天然贴近“哪一批可以立即并发”的问题，教学上更直观；Java 的优势在于执行前人工审阅和失败重规划，能降低模型生成错误计划后直接落地的风险。

**本章结论：** DAG 核心能力相当；Java 版在计划生命周期上更完整，Python 版在算法表达上更简洁。

---

## 第三章：Memory 系统

### 共同点

两版都区分短期记忆和长期记忆，长期数据落盘为 JSON，并提供状态、保存和检索能力。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| 长期记忆写入 | 只通过 `/save` 或用户明确要求保存 | 支持 `/save`，同时压缩器仍可按“记住、偏好、项目”等标记启发式提取事实 |
| 作用域 | 支持 project/global 作用域 | 当前主要使用一个配置目录下的长期记忆文件 |
| 管理能力 | list/search/delete/clear，强调可审计、可删除 | `/memory`、`/save`、`/recall` 为主 |
| 上下文压缩 | 短期记忆压缩与 conversation history 压缩是两条独立链路，支持 `/compact` | 主要实现短期记忆 token 预算和摘要压缩 |
| 项目级规则 | `PAI.md`、`PAI.local.md` 分层注入，可用 `/init` 生成 | 暂无同等级项目规则文件系统 |
| 检索 | 中文分词和关键词相关性 | 同样使用中文分词/关键词检索，结构较轻量 |

### 影响与判断

- Java 当前坚持长期记忆由用户明确授权保存，更可控，也避免错误事实自动持久化。
- Python 的启发式提取演示了“自动记忆”，但可能误把临时信息保存为长期事实，产品化时应改成候选记忆确认或完全手动保存。
- Java 真正解决了长会话历史接近模型窗口的问题；Python 的短期记忆摘要不能完全替代 conversation history compaction。

**本章结论：** Python 完成了 Memory 教学模型，Java 在作用域、治理和长上下文稳定性上明显更强。

---

## 第四章：Multi-Agent 协作

### 共同点

两版都采用 Planner、Worker、Reviewer 三类角色：Planner 拆任务，多个 Worker 执行，Reviewer 判断结果是否通过，失败任务最多重试两次。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| 默认 Worker | 2 个 | 2 个 |
| 角色上下文 | 每个角色有独立对话历史，但共享 ToolRegistry 和 MemoryManager | 同样保留角色历史，并共享工具与记忆服务 |
| Skill 缓冲区 | Orchestrator 当前给多个角色共享一个 `SkillContextBuffer` | 每个角色显式拥有独立 `SkillContextBuffer` |
| 输出体验 | 通过统一 Renderer 流式展示角色状态 | 控制台输出角色和任务状态 |
| 中断/状态 | 与 Java 全局 renderer、snapshot、token 状态联动 | 聚焦规划、执行、审查主流程 |

### 影响与判断

Python 版在 Skill 缓冲区隔离上反而更稳妥：并发 Worker 不会消费其他角色刚加载的 Skill。Java 的共享缓冲区实现更简单，但在并发加载 Skill 时存在角色间内容串用的可能。

**本章结论：** Multi-Agent 主算法基本对齐；Java 的产品集成更成熟，Python 的 Skill 上下文隔离设计更合理。

---

## 第五章：RAG 代码检索

### 共同点

两版都包含代码分块、Embedding、SQLite 持久化、内存余弦相似度、中文关键词匹配和语义/关键词混合排序。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| 语言结构解析 | JavaParser 解析 Java 类、方法和关系 | Python `ast` 解析类、函数、调用和 import 关系 |
| 其他文件 | 非 Java 文件按文本分块 | 非 Python 文件按文本分块 |
| Embedding | 以 Ollama/OpenAI 兼容服务为主 | Ollama、OpenAI 兼容接口，并提供本地 hash embedding 兜底 |
| 精确代码探索 | `glob_files` + `grep_code` + `read_file` 为默认路径，`rg` 不可用时回退 Java 扫描 | 没有独立 glob/grep 内置工具，更依赖 list/read 和 `search_code` |
| 排序增强 | 语义、关键词、类型权重和关系图能力较完整 | 语义与关键词双命中加分，并限制单文件结果数量，减少结果扎堆 |
| 文档类型 | 支持 `.md`，不包含 `.txt` | 同样支持 `.md`，当前也不包含 `.txt` |
| 图查询 | 支持 `/graph` 和代码关系存储 | 同样实现关系存储及 `/graph` 查询 |

### 影响与判断

两版都针对各自实现语言做了正确适配。Java 的关键优势不是单纯向量检索，而是把 RAG 定位为模糊检索的辅助，再用 glob/grep/read 做精确定位；Python 当前缺少这组实时搜索工具。Python 的本地 hash 方案不需要下载模型，适合测试流程，但检索质量不能替代真实 Embedding。

**本章结论：** 语言适配程度相当；Java 的代码探索工具链更完整，Python 的离线可测试性更好。

---

## 第六章：HITL 人工审批

### 共同点

两版都按工具风险等级决定是否审批，支持本次批准、会话内批准、拒绝、跳过和修改参数；并用锁保证 Multi-Agent 并发场景下审批框串行显示。拒绝原因都会作为工具结果返回给 Agent。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| 默认模式 | HITL 与策略层共同生效 | 启动默认 restricted，可运行时切换 full-access |
| 文件边界 | `PathGuard` 强制限制在项目根目录 | 按用户需求取消 workspace 限制，可操作绝对路径和外部目录 |
| 命令策略 | `CommandGuard` 拦截危险命令 | restricted 下主要依赖风险审批和少量明显危险模式检查 |
| 策略优先级 | 策略拒绝不可由用户审批绕过 | full-access 会跳过人工审批，边界更宽松 |
| 审计 | `AuditLog` 覆盖安全相关调用 | MCP 有审计，内置工具审计覆盖不如 Java 完整 |
| 文件限制 | 写入大小、命令超时和输出预算等约束较系统 | 有工具级超时与输出控制，但不是完整沙箱 |

### 影响与判断

这里是两版最重要的语义差异。Python 的“限制模式”是审批模式，不是强隔离沙箱；“完全访问模式”则是主动选择的高权限模式。Java 的设计是审批层之外仍保留不可绕过的工作区策略，因此更适合默认安全场景。

**本章结论：** Python 的两模式切换更符合本项目的本机 Agent 实验需求；Java 的纵深防御更适合产品默认值。两者不能只按“是否弹审批框”判断安全性。

---

## 第七章：多并发执行

### 共同点

两版都把同一轮多个工具调用提交给线程池执行，默认最大并发数为 4，并按模型原始 tool-call 顺序返回结果，避免并发完成顺序扰乱消息配对。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| 并发设施 | `ExecutorService` / `CompletableFuture` 风格 | `ThreadPoolExecutor` / futures |
| 三条执行路径 | ReAct、Plan、Multi-Agent 统一走 `executeTools()` | 核心 ToolRegistry 统一批量执行，Plan/Multi-Agent 复用 |
| 审批并发 | synchronized 串行审批 | `RLock` 串行预审批 |
| 上下文传播 | Java 线程上下文由对象和调用参数显式共享 | 使用 `contextvars.copy_context()` 向工作线程传播角色/审批上下文 |
| 超时 | 工具自身限制与执行器治理结合 | 批次默认 90 秒，并保留每个工具自己的超时 |

### 影响与判断

两版的用户可见语义基本一致。Python 的 `contextvars` 处理对日志、角色身份和审批状态传播很关键；Java 则依靠更显式的对象生命周期和统一执行入口。

**本章结论：** 本章能力接近，没有明显功能缺口；实现方式体现了各自语言的惯用并发模型。

---

## 第八章：联网搜索

### 共同点

两版都把搜索和抓取拆成 `web_search`、`web_fetch` 两步，由模型自主决定何时搜索、搜索后抓取哪些 URL，并对请求做 SSRF 和响应大小防护。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| 搜索来源 | 原生 SearchProvider，并可在 Step 模型下优先转发 StepSearch MCP | Zhipu、SerpAPI、SearXNG，另有 Wikipedia 兜底 |
| 质量判断 | 主要依赖 provider 结果和模型后续选择 | `SmartSearchProvider` 显式计算相关结果数量，标注 high/low quality |
| 搜索策略提示 | 已知 URL 先 fetch；失败再考虑 Chrome | 搜索结果会提示优先抓取前 3 个高质量页面，并限制单任务搜索次数 |
| 抓取安全 | NetworkPolicy、重定向和内容预算 | 每次 DNS/重定向重新检查公网地址，最多 5 次重定向、5 MB 响应 |
| JavaScript 页面 | 回退 Chrome DevTools MCP | 同样可由模型继续调用 Chrome MCP |
| 模型适配 | Step 模型和 StepSearch MCP 有专门联动 | 与 GLM 工具调用链结合，provider 选择更独立 |

### 影响与判断

Python 版在普通搜索结果质量诊断、Wikipedia 兜底和错误可见性上做了针对性增强，能解释“为什么搜不到”。Java 版的优势是 Step 模型、StepSearch MCP、Renderer 和浏览器回退之间的整体联动。

**本章结论：** Python 的原生搜索策略更透明；Java 的模型和 MCP 搜索生态整合更深。

---

## 第九章：MCP 接入与 Chrome DevTools MCP

### 共同点

两版都由 Agent 主进程充当 MCP Client，启动外部 MCP Server，完成 initialize、工具发现、动态注册和 `tools/call`。工具名统一映射为 `mcp__{server}__{tool}`，配置支持 stdio 和 Streamable HTTP。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| MCP 核心 | 自行实现较完整的 client、transport 和 server manager | 基于 Python MCP SDK 封装 client/session，代码量更小 |
| 启动策略 | CLI 最多等待 8 秒，慢 Server 保持 STARTING 并在后台继续 | 当前 `start_all()` 等待配置的 Server 初始化完成，超时会直接报告错误 |
| 动态更新 | 支持 `tools/list_changed` 全量替换 | 启动时发现工具为主，缺少完整动态刷新链路 |
| Resources | 虚拟工具和 `@server:uri` mention 双通道 | 重点实现 tools，Resources 能力不如 Java 完整 |
| 结果类型 | text、image、structured content，并接入审计/HITL | 同样支持 text、image、structured content 和 MCP 审计 |
| Schema 兼容 | 有 schema 归一化和兼容处理 | 也会清理模型不接受的 JSON Schema 字段 |
| 示例服务 | 可接任意配置的 MCP Server | 额外提供易读的 Python weather MCP Server 示例 |

### 影响与判断

Python 版借助官方 SDK，把 JSON-RPC、stdio framing 和会话管理隐藏在库内部，适合理解 MCP 的职责边界；Java 原版自己承担更多协议和生命周期细节，因此具备后台启动、资源引用、通知刷新等高级能力。

Chrome DevTools MCP 本身不是写死在 ToolRegistry 里的静态工具。两版都是先启动 `chrome-devtools-mcp` Server，再把它上报的 schema 动态注册到 ToolRegistry。

**本章结论：** Python 已完成 MCP 工具调用主链路；Java 是更完整的 MCP Host 实现。

---

## 第十章：CDP 会话复用

### 共同点

两版都支持 isolated 和 shared 两种浏览器会话：isolated 使用独立浏览器数据目录，shared 通过 CDP 连接用户已登录的 Chrome；`--autoConnect` 用于让 Chrome DevTools MCP 自动发现可连接实例。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| 会话配置 | `/browser` 命令、自动连接、legacy port 和状态展示较完整 | 通过 CLI/MCP 配置切换 shared/isolated，并生成对应启动参数 |
| 标签页归属 | `BrowserGuard` 记录 Agent 创建的页面，只关闭自己的标签页 | 实现相同的“只清理 Agent 页面”核心规则 |
| 敏感页面 | `SensitivePagePolicy` 内置银行、支付、账号等规则，用户可追加规则 | Chrome 工具当前整体设为安全免审批，没有独立敏感页面升级策略 |
| 审批复用 | 敏感页面上的修改操作强制单次审批，不复用会话全批准 | 依据用户要求，Chrome DevTools 工具不触发 HITL |
| 终端集成 | `/browser status/connect/disconnect` 与 Renderer 联动 | 命令和错误输出较轻量 |

### 影响与判断

shared 模式能读取私有仓库，是因为 Agent 复用了 Chrome 的登录 Cookie，而不是 GitHub API 自动获得权限。Python 版目前更便利，但把全部 Chrome 工具视为安全，会让点击、填写、提交等写操作失去最后确认。Java 的敏感页面策略更适合长期保留 shared 模式。

**本章结论：** 会话复用主功能已对齐；Java 的页面归属和敏感操作治理更完整，Python 当前偏向调试便利。

---

## 第十一章：web-access Skill

### 共同点

两版都实现三层 Skill 目录、启动时索引、`load_skill` 工具、`SkillContextBuffer` 和一次性注入。索引只向 system prompt 暴露名称与描述，完整 `SKILL.md` body 在模型调用 `load_skill` 后才加载。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| Skill 来源 | 内置、用户级、项目级，后加载层覆盖前层 | 相同三层覆盖顺序 |
| 索引预算 | 最多 20 个、4 KB，描述限制 500 字符 | 同样设置数量和字符预算 |
| body 预算 | 单个约 5 KB，缓冲区最多 3 个 | 同样限制 body 和缓冲数量 |
| 注入方式 | `load_skill` 后 push，下一条 user message 构造时 drain | 相同的 buffer + drain 机制 |
| 并发隔离 | Multi-Agent 当前共享一个 buffer | Planner、Worker、Reviewer 使用独立 buffer |
| 异常可见性 | 有 Skill 注册和状态展示 | 后续补齐 warning 展示及状态文件写入异常处理 |

### 影响与判断

完整 Skill body 不放进 system prompt，是为了让稳定的 system 前缀更容易复用服务端 prompt cache，并避免所有 Skill 每轮占用 token。`drain()` 表示内容注入一次后立即从缓冲区移除，防止后续无关问题继续携带旧 Skill。

两版当前都把 body 作为 user message 的前置内容，而不是永久改变 system prompt。Python 的独立缓冲区解决了多个 Worker 抢占同一 Skill 内容的问题，是这一章最明确的 Python 增强。

**本章结论：** 单 Agent 设计基本一致；Python 的 Multi-Agent Skill 隔离优于 Java 当前共享实现。

---

## 第十二章：多模态能力

### 共同点

两版都把消息抽象为文本和图片内容块，支持本地图片引用、MCP 返回图片、图片大小校验、压缩缩放和历史图片裁剪。

### 主要差异

| 项目 | Java 原版 | Python 版 |
|---|---|---|
| 图片输入 | `@image:path`，JLine 补全和终端集成更完整 | 支持 `@image:path`，并新增 Windows `@clipboard` 剪贴板读取 |
| 模型选择 | 通常通过 `/model` 或配置切换到 GLM-5V 等视觉模型 | 检测到 `image_url` 后自动从 `GLM_MODEL` 路由到 `GLM_VISION_MODEL` |
| API Key | 跟随所选 provider/model 配置 | 可独立设置 `GLM_VISION_API_KEY`，适配文本和视觉服务账号不同的情况 |
| Provider | 多 provider 抽象，并声明各 provider 是否支持图片 | 当前主要围绕 GLM 文本/视觉双路由 |
| 图像处理 | 文件类型、尺寸、编码和历史回收 | Pillow 处理透明通道、最长边 2000、源文件 50 MB、编码后约 5 MB |
| 429 处理 | 通用 provider 错误与流式链路 | 针对视觉接口补充 429、余额/并发类业务错误提示和有限重试 |
| MCP 图片 | 工具图片作为额外 user-role 内容回灌 | 采用相同策略，避免某些 API 拒绝 tool-role 图片块 |

### 影响与判断

Python 版解决了“有图片时手动切视觉模型、没图片时再切回来”的使用痛点，自动路由是明显的易用性提升。Java 的优势是多模型、多 provider、流式输出和终端补全体系更完整。

`Ctrl+V` 直接把位图作为普通终端文本粘贴并不可靠，因此 Python 使用显式 `@clipboard`；这比依赖终端对图片剪贴板的非标准行为更可控。

**本章结论：** Python 在 GLM 单生态下的自动多模态体验更方便；Java 的通用模型抽象和交互基础设施更强。

---

## 二、十二章差异速览

| 章节 | Python 复刻程度 | Java 的主要优势 | Python 的主要优势 |
|---|---|---|---|
| 1. ReAct | 核心完整 | 流式、长任务预算、Renderer | 主循环简洁、失败信息清楚 |
| 2. Plan/DAG | 核心完整 | 人工审阅、补充和重规划 | Kahn 批次表达直观 |
| 3. Memory | 基础完整 | 作用域、审计、双重压缩、PAI.md | 结构轻量，便于实验自动提取 |
| 4. Multi-Agent | 核心完整 | 产品状态和渲染集成 | 角色 Skill 缓冲区隔离 |
| 5. RAG | 核心完整 | 实时代码搜索链和类型增强 | Python AST、本地 hash 兜底 |
| 6. HITL | 功能完整但安全语义不同 | PathGuard、策略不可绕过、审计 | restricted/full-access 动态切换 |
| 7. 并发 | 基本对齐 | 三路径统一和成熟运行治理 | `contextvars` 上下文传播 |
| 8. Web | 功能完整 | StepSearch/MCP/浏览器联动 | 质量评分、Wikipedia 兜底 |
| 9. MCP | 核心完整 | Resources、通知、后台启动 | 官方 SDK 封装、示例清晰 |
| 10. CDP | 主功能完整 | SensitivePagePolicy | 默认使用更直接 |
| 11. Skill | 基本对齐 | 启动与 UI 集成 | Multi-Agent 独立 buffer |
| 12. 多模态 | 功能完整 | 多 provider 和终端能力 | 自动视觉路由、剪贴板和错误诊断 |

## 三、Python 版尚未覆盖的 Java 产品能力

以下能力不属于十二章主线，或只在 Python 版中部分实现：

1. JLine inline renderer、底部状态栏、实时 Markdown、输入高亮和补全。
2. 对话历史自动压缩、`/compact` 和按模型窗口计算的安全阈值。
3. `PAI.md` 项目级共享规则、`/init`、`/export`。
4. LSP 诊断、Side-Git 快照和 `revert_turn`。
5. Runtime API、durable task、微信 iLink 通道。
6. MCP Resources、resource mention、动态工具变更通知和后台启动恢复。
7. 浏览器敏感页面策略和不可复用的一次性审批。
8. 多 provider 切换、reasoning 流式保留、prompt cache/token/cost 统计。

这些差异说明 Python 版当前更适合作为“从零理解 Agent 架构”的实现，而 Java 原版已经进入“长期运行的 CLI 产品”阶段。

## 四、后续演进建议

按收益和风险排序，Python 版下一阶段更值得补齐的是：

1. **先补 conversation history compaction。** 它直接决定长会话是否会因上下文窗口溢出而失败。
2. **加入 `glob_files` 和 `grep_code`。** 精确代码定位不应全部依赖向量检索。
3. **重新划定 restricted 安全语义。** 可以保留 full-access，但 restricted 应增加不可审批绕过的 PathGuard 和更完整审计。
4. **完善 Plan 审阅与失败重规划。** 这是计划模式区别于普通 ReAct 的关键价值。
5. **补 MCP 后台启动和动态刷新。** 避免单个慢 MCP Server 阻塞整个 CLI。
6. **保留 Python 已有增强。** 自动视觉模型路由、搜索质量诊断和独立 Skill buffer 不应在追齐 Java 时丢失。

最终目标不应是机械复制 Java，而应是：以 Java 版的成熟治理能力补足 Python 版，同时保留 Python 版更清晰、更自动化的设计。

## 五、主要代码入口

### Python 版

- `src/stellarcode/agent.py`：ReAct 主循环
- `src/stellarcode/planner.py`、`execution_plan.py`、`plan_execute_agent.py`：Plan/DAG
- `src/stellarcode/memory/`：短期与长期记忆
- `src/stellarcode/multi_agent.py`：Multi-Agent
- `src/stellarcode/rag/`：RAG
- `src/stellarcode/hitl/`、`src/stellarcode/access.py`：审批和访问模式
- `src/stellarcode/tools/`：内置工具、并发、Web
- `src/stellarcode/mcp/`：MCP Client 和 Server 管理
- `src/stellarcode/browser/`：CDP 会话
- `src/stellarcode/skills/`：Skill 系统
- `src/stellarcode/image_input.py`、`src/stellarcode/llm.py`：图片处理与模型路由

### Java 原版

- `src/main/java/com/paicli/agent/Agent.java`：ReAct 主循环
- `src/main/java/com/paicli/agent/PlanExecuteAgent.java`：Plan 执行与审阅
- `src/main/java/com/paicli/plan/`：DAG 和 Planner
- `src/main/java/com/paicli/memory/`：Memory 与上下文压缩
- `src/main/java/com/paicli/agent/AgentOrchestrator.java`：Multi-Agent
- `src/main/java/com/paicli/rag/`：RAG
- `src/main/java/com/paicli/hitl/`、`policy/`：审批、安全策略和审计
- `src/main/java/com/paicli/tool/ToolRegistry.java`：内置工具与并发入口
- `src/main/java/com/paicli/web/`：联网搜索与抓取
- `src/main/java/com/paicli/mcp/`：MCP Host
- `src/main/java/com/paicli/browser/`：浏览器会话和敏感页面策略
- `src/main/java/com/paicli/skill/`：Skill
- `src/main/java/com/paicli/image/`、`llm/`：多模态和多模型
