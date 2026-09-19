# StellarCode
<img src="https://picui.ogmua.cn/s1/2026/08/25/6a8d63cfc9d12.webp" alt="stellar.png" title="stellar.png" />


StellarCode 是一个本地运行的 Code Agent，当前默认分支为 `desktop-client`。项目保留了
Python CLI，同时提供基于 **Tauri 2 + React + TypeScript** 的桌面客户端。

桌面版不是用 Rust 重写 Python Agent：

```text
React / TypeScript UI
        │ Tauri Command + RuntimeEvent
        ▼
Rust 桌面宿主
        │ JSONL stdin/stdout
        ▼
Python Runtime Sidecar
        │
        ├─ ReAct / Plan / Team
        ├─ Tools / Approval / Git worktree
        ├─ Memory / Context compression
        ├─ RAG / MCP / Skills / Browser
        └─ LLM / VLM providers
```

开发时，Tauri 会自动启动仓库根目录 `.venv` 中的 Python Runtime。发行构建则使用
PyInstaller 将 Runtime 打包进安装程序，最终用户不需要单独安装 Python、Node.js 或 Rust。
## Windows客户端安装
/desktop/src-tauri/target/release/bundle/msi/StellarCode_0.1.0_x64_en-US.msi
## 1. 环境要求

推荐在 Windows 10/11 x64 上开发。

| 环境 | 推荐版本 | 用途 |
|---|---:|---|
| Git | 最新稳定版 | 克隆和版本管理 |
| Python | 3.10+ | Agent、Runtime Sidecar、测试 |
| Node.js | 20.19+ 或 22.12+ | React、TypeScript、Vite |
| npm | 随 Node.js 安装 | 前端依赖和脚本 |
| Rust | stable，MSVC toolchain | Tauri 后端和桌面程序 |
| Visual Studio Build Tools | 2022 | Windows C++ 编译工具和 Windows SDK |
| WebView2 Runtime | Windows 通常已内置 | 渲染桌面 UI |

Visual Studio Installer 中至少勾选：

- `Desktop development with C++`（使用 C++ 的桌面开发）
- MSVC v143 C++ x64/x86 build tools
- Windows 10 或 Windows 11 SDK

检查环境：

```powershell
git --version
python --version
node --version
npm --version
rustc --version
cargo --version
```

## 2. 获取项目

```powershell
git clone https://github.com/Watson277/StellarCode.git
cd StellarCode
git switch desktop-client
```

如果已经克隆过：

```powershell
git switch desktop-client
git pull origin desktop-client
```

## 3. 配置 Python Runtime

所有 Python 命令都在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

如果 PowerShell 阻止激活脚本，可只为当前终端临时放开：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

也可以完全不激活虚拟环境，直接使用：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

验证 Python 包：

```powershell
.\.venv\Scripts\python.exe -c "import stellarcode; print('StellarCode Python Runtime OK')"
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```

## 4. 配置模型和 `.env`

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

`.env` 已被 `.gitignore` 忽略，不要提交 API Key。至少配置一个文本模型。

### 通用文本模型配置

```dotenv
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://your-provider.example/v1
LLM_MODEL_NAME=your_model_name
```

文本接口只需要兼容 OpenAI Chat Completions，不再选择或限制服务商。`LLM_API_KEY` 对
无需鉴权的本地接口可以留空。

### 通用视觉模型配置

```dotenv
VISION_API_KEY=your_vision_api_key
VISION_BASE_URL=https://your-vision-provider.example/v1
VISION_MODEL_NAME=your_vision_model_name
```

视觉模型可以和文本模型来自不同服务商。不需要图片能力时，将 `VISION_BASE_URL` 和
`VISION_MODEL_NAME` 留空。

### 通用 Embedding 配置

Code RAG 使用独立的 Embedding 配置：

```dotenv
EMBEDDING_API_KEY=your_embedding_api_key
EMBEDDING_BASE_URL=https://your-embedding-provider.example/v1
EMBEDDING_MODEL_NAME=your_embedding_model_name
```

`EMBEDDING_BASE_URL` 留空时使用无需 API Key 和网络下载的内置确定性向量；远程接口
统一使用 OpenAI-compatible `/embeddings`，Ollama 可填写其 `/v1` 兼容地址。

旧版本中的 provider 及其前缀变量仅作为迁移回退，新配置不再需要 provider。

RAG、Web Search、MCP 等可选配置都写在 [`.env.example`](./.env.example) 中。没有使用
某项能力时，不需要配置对应变量。

环境变量读取顺序以已有系统变量为先，并支持用户级、项目级 `.env`。桌面发行版可以在
`Settings → Models → Show .env` 中打开用户配置文件。

## 5. 安装前端依赖

进入 `desktop` 目录安装锁定的依赖：

```powershell
cd desktop
npm ci
```

首次安装需要联网。以后 `package-lock.json` 没有变化时通常无需重复执行。

只运行浏览器中的 React UI：

```powershell
npm run dev
```

这只启动 Vite 页面，不会提供完整的 Tauri 能力，也不会正常管理 Python Sidecar。完整开发
请使用下一节的 `npm run tauri dev`。

## 6. 启动桌面客户端

确认当前目录是 `desktop`：

```powershell
cd "E:\study2\LLM Internship\PaiCLI\stellarcode\desktop"
npm run tauri dev
```

该命令会同时启动：

1. Vite 前端开发服务器（默认 `http://localhost:1420`）；
2. Rust/Tauri 桌面宿主；
3. 仓库根目录 `.venv\Scripts\python.exe` 中的 Python Runtime Sidecar。

首次执行需要编译 Tauri/Rust 依赖，耗时较长是正常现象。之后 Cargo 会复用增量缓存；只有
清理 `target`、切换 toolchain、修改依赖或编译配置时才会大规模重编译。

如果需要显式指定 Python：

```powershell
$env:STELLARCODE_PYTHON = "E:\study2\LLM Internship\PaiCLI\stellarcode\.venv\Scripts\python.exe"
npm run tauri dev
```

这个变量只影响当前 PowerShell 窗口。如需长期配置，也可以在客户端
`Settings → Data & Diagnostics` 中选择 Python 可执行文件。

## 7. 运行 Python CLI

CLI 与桌面版共用 Agent 核心。在仓库根目录执行：

```powershell
cd "E:\study2\LLM Internship\PaiCLI\stellarcode"
.\.venv\Scripts\Activate.ps1
stellarcode --workspace .
```

也可以不激活环境：

```powershell
.\.venv\Scripts\python.exe -m stellarcode.cli --workspace .
```

直接检查 JSONL Runtime Sidecar：

```powershell
.\.venv\Scripts\python.exe -m stellarcode.runtime.sidecar --workspace .
```

Sidecar 启动后会等待 JSONL 协议输入，因此没有普通 CLI 提示符是正常现象；使用
`Ctrl+C` 退出。

### SWE-bench 非交互适配器

`swebench-agent` 用于在外部 runner 已准备好的干净仓库中执行单道 SWE-bench 任务。
它允许仓库内代码读写和有限时长的命令执行，但不加载 Web、MCP 或 Memory 工具；题目
文件与结果文件必须位于仓库工作区之外，防止混入最终 Git patch。
模型调用使用流式响应，SWE-bench 专用读取超时为 180 秒。

```powershell
.\.venv\Scripts\python.exe -m stellarcode.cli swebench-agent `
  --workspace E:\path\to\clean-repository `
  --prompt-file E:\path\outside-repository\prompt.md `
  --result-file E:\path\outside-repository\agent-result.json `
  --max-iterations 40 `
  --temperature 0.2
```

完整的数据脱敏、仓库 worktree 隔离和 `predictions.jsonl` 收集流程位于同级
`SWE-bench` 评测目录。

## 8. 测试和构建检查

### Python 测试

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

`tests/` 是项目的自动化测试源码，应当提交到 Git；`.pytest_cache/`、覆盖率报告和其他
测试缓存已经被忽略。

### React/TypeScript 构建

```powershell
cd desktop
npm run build
```

可单独运行桌面状态机和 UI 数据逻辑测试：

```powershell
npm run test:runtime-machine
npm run test:runtime-supervision
npm run test:session-drafts
npm run test:diff-parser
npm run test:workspace-layout
npm run test:code-answer
npm run test:transcript-order
```

### Rust/Tauri 检查

```powershell
cd desktop\src-tauri
cargo test --no-default-features --locked
cargo build --no-default-features --locked
```

依赖已经下载完成且希望禁止联网时：

```powershell
cargo test --no-default-features --locked --offline
cargo build --no-default-features --locked --offline
```

## 9. 将 Cargo 缓存放到其他磁盘（可选）

Cargo 默认把注册表、源码和工具缓存放在 `%USERPROFILE%\.cargo`。如果系统盘空间不足，可以
在新的 PowerShell 中设置：

```powershell
[Environment]::SetEnvironmentVariable("CARGO_HOME", "E:\cargo", "User")
[Environment]::SetEnvironmentVariable("CARGO_TARGET_DIR", "E:\cargo-target\stellarcode", "User")
```

关闭并重新打开终端后检查：

```powershell
$env:CARGO_HOME
$env:CARGO_TARGET_DIR
cargo fetch --manifest-path "E:\study2\LLM Internship\PaiCLI\stellarcode\desktop\src-tauri\Cargo.toml"
```

`CARGO_HOME` 是依赖缓存；`CARGO_TARGET_DIR` 是编译产物。不要把它们提交到仓库。

## 10. 构建 Windows 发行版

发行构建会先使用 PyInstaller 冻结 Python Sidecar，再由 Tauri 生成安装包。构建机器仍然
需要完整的 Python、Node.js、Rust 和 MSVC 环境，但最终用户不需要这些开发环境。

在仓库根目录安装发行依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,release]"
```

执行打包：

```powershell
cd desktop
.\scripts\build-windows-release.ps1
```

如果尚未安装 PyInstaller，也可以允许脚本安装：

```powershell
.\scripts\build-windows-release.ps1 -InstallBuildTools
```

生成结果位于：

```text
desktop\src-tauri\target\release\bundle\
├─ msi\   # MSI 安装包
└─ nsis\  # EXE 安装包（目标可用时）
```

重新修改代码后，直接重新运行同一个脚本即可。脚本会重新打包 Python Runtime、构建前端并
编译 Tauri 应用。

## 11. 常见问题

### `npm ERR! ENOENT ... package.json`

`npm` 命令运行目录不对。根目录没有 `package.json`，应进入：

```powershell
cd "E:\study2\LLM Internship\PaiCLI\stellarcode\desktop"
npm run tauri dev
```

### 一直显示 `Runtime starting`

先确认 Python Runtime 可以导入：

```powershell
cd "E:\study2\LLM Internship\PaiCLI\stellarcode"
.\.venv\Scripts\python.exe -c "import stellarcode.runtime.sidecar; print('OK')"
```

然后确认 `.venv` 位于仓库根目录，而不是 `desktop` 目录。必要时通过
`STELLARCODE_PYTHON` 显式指定解释器。

### Cargo 每次都重新编译大量依赖

不要删除 `desktop\src-tauri\target` 或自定义的 `CARGO_TARGET_DIR`，也不要在不同终端频繁
切换 `CARGO_TARGET_DIR`、Rust toolchain 或编译参数。`cargo build` 与 `tauri dev` 使用不同
参数时可能分别产生一套缓存。

### `cargo fetch --locked` 提示没有 `Cargo.lock`

首次生成锁文件时不要使用 `--locked`：

```powershell
cd desktop\src-tauri
cargo fetch
```

仓库已有 `Cargo.lock` 后再使用 `--locked`。

### GitHub 推送出现 `Recv failure: Connection was reset`

这通常是 VPN、代理或 HTTPS 链路问题，不会丢失本地提交：

```powershell
git config --global http.version HTTP/1.1
git push -u origin desktop-client
```

仍然失败时关闭 VPN 后重试，或配置 GitHub SSH Key 后改用 SSH remote。

## 12. 仓库结构

```text
stellarcode/
├─ src/stellarcode/          # Python Agent 与 Runtime Sidecar
│  ├─ agent.py               # ReAct Agent 主循环
│  ├─ plan/                  # Plan-and-Execute 与 DAG
│  ├─ multi_agent/           # Team 编排、Worker 与 MessageBus
│  ├─ runtime/               # 桌面 JSONL Runtime 与事件协议实现
│  ├─ memory/                # 长期记忆提取、检索与模型上下文压缩
│  ├─ rag/                   # 代码索引和检索
│  ├─ mcp/                   # MCP 客户端与工具接入
│  ├─ skill/                 # Skill 发现、版本和渐进式披露
│  └─ tools/                 # 文件、搜索、命令及修改工具
├─ desktop/
│  ├─ src/                   # React + TypeScript 前端
│  ├─ src-tauri/             # Rust/Tauri 后端
│  └─ scripts/               # 桌面测试及 Windows 打包脚本
├─ tests/                    # Python 自动化测试
├─ docs/                     # 协议与架构设计文档
├─ scripts/                  # Python Sidecar/评测辅助脚本
├─ .env.example              # 环境变量模板
└─ pyproject.toml            # Python 包与依赖配置
```

## 13. 运行数据位置

开发工作区中的本地状态主要包括：

- `<workspace>/.stellarcode/traces/`：按会话记录的 Trace；
- `<workspace>/.stellarcode/mcp.json`：项目 MCP 配置；
- `.stellarcode-memory/`：长期记忆；
- `.stellarcode-rag/` 或配置的 RAG 目录：代码索引。

记忆系统只维护模型上下文和长期记忆，不再使用独立短期记忆副本、预算或压缩。
对话级长期记忆使用关键词与 Embedding 向量混合检索；用户级长期记忆按设计全部注入。
模型上下文达到窗口的 80% 时压缩，保留最近 3 轮及工具调用配对；压缩时自动提取
长期记忆，也可在聊天框手动提取。桌面端直接读取会话记录中的用户原话，并保存
已提取消息 ID。旧会话仍可加载，新保存的会话不再包含 `short_term_memory`。

Windows 桌面客户端的项目注册、会话、设置、Runtime journal、任务快照和临时 worktree 默认
保存在：

```text
%APPDATA%\com.stellarcode.desktop\
```

这些目录可能包含对话、源码片段、命令输出和项目路径，不应提交或随意共享。

## 14. 进一步阅读

- [桌面 RuntimeEvent 协议](docs/desktop-runtime-event-protocol.md)
- [前端与 Runtime 状态机](docs/frontend-runtime-state-machines.md)
- [任务级 Git worktree 隔离](docs/task-git-worktree-isolation.md)
- [任务修改保护](docs/task-modification-protection.md)
- [上下文压缩与 Token usage](docs/context-compression-and-token-usage.md)
- [Windows 命令执行与审批竞态复盘](docs/windows-command-execution-and-approval-race-postmortem.md)

## License

本仓库当前未声明开源许可证。在添加明确的 `LICENSE` 文件前，请勿假定代码可被自由复制、
修改或再分发。
