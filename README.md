# StellarCode Python

This project rebuilds the core StellarCode stages in Python:

```text
Chapter 1: User -> ReAct Agent -> tool calls -> final answer
Chapter 2: User goal -> planner JSON -> DAG -> dependency-layer execution
Chapter 3: Conversation -> short-term memory -> retrieval -> long-term facts
Chapter 4: Goal -> Planner -> parallel Workers -> Reviewer -> retries -> summary
RAG phase: Source -> AST chunks -> embeddings -> SQLite -> hybrid retrieval
HITL phase: risky tool -> human decision -> execute, modify, reject, or skip
Parallel phase: one LLM round -> ordered concurrent tools -> bounded results
Web phase: current query -> search provider -> result URLs -> safe readable fetch
MCP phase: config -> initialize -> tools/list -> namespaced tools -> tools/call
Skill phase: three-layer discovery -> prompt index -> load_skill -> next-turn context
Multimodal phase: image/clipboard/MCP screenshot -> preprocessing -> vision message
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

## Desktop client

The `desktop/` Tauri application starts the same Python Agent through the JSONL Runtime
Sidecar. During development it automatically uses `.venv\Scripts\python.exe`:

```powershell
cd desktop
npm install
npm run tauri dev
```

The CLI remains available and does not need to be replaced. The desktop transport and its
typed messages are documented in `docs/desktop-runtime-event-protocol.md`.

### Windows installer (no Python / Node / Rust required for end users)

The release installer bundles a PyInstaller `stellarcode-sidecar.exe` with the Tauri app.
It does **not** use the developer virtual environment, source tree, or `.env` after
installation. On the release build machine, run:

```powershell
cd desktop
.\scripts\build-windows-release.ps1 -InstallBuildTools
```

The command first packages the Python Runtime, then produces a Windows MSI installer under
`src-tauri\target\release\bundle\msi\` (and an NSIS installer too when that Tauri target is
available). End users install the generated installer and launch
StellarCode directly; they do not install Python, Node.js, Rust, npm, or Cargo.

API keys cannot be bundled. On first launch, StellarCode safely creates a user-owned `.env`
beside `settings.json` in its Tauri application-data directory. Use **Settings → Models →
Show .env** to add a key, then restart Runtime. Project `.env` files can still supply
project-specific configuration when no user or system value is already set.

Unexpected Sidecar exits are recovered automatically. The desktop restarts Python with
bounded backoff, replays missed per-session events from the project journal, restores the
unfinished task checkpoint, and continues ReAct history from the last safe message/tool
boundary. Interrupted side-effecting tools are marked unconfirmed and verified before any
retry instead of being executed blindly.

One desktop Sidecar now keeps independently loaded project Runtimes alive. Each conversation
owns its own task slot, cancellation signal, checkpoint, short-term context, Skill buffer,
usage ledger, and Trace target, so tasks in different projects or conversations can continue
in the background while the user switches the visible workspace. Every accepted task now runs
built-in project file operations and commands in its own Side-Git-backed Git worktree. Final
patches are checked and merged into the project serially; conflicting tasks fail without
overwriting the newer project state and retain their worktree for inspection. The sidebar
marks running conversations and project task counts. A conversation still accepts only one
task at a time. See `docs/task-git-worktree-isolation.md` for lifecycle and scope.

Desktop Plan mode displays the generated DAG as a persistent live plan card. Overall
progress and each step's dependencies, running state, result, failure, or skipped state are
updated through structured RuntimeEvents and restored when the conversation is reopened.

Every desktop task is protected before execution by an isolated Side-Git snapshot stored in
StellarCode application data. It never stages, commits, resets, or changes the user's Git
repository. `write_file`, `apply_patch`, and `delete_file` approvals include a bounded
pre-change diff and verify the approved file hash again under the mutation lock before an
atomic replace/unlink. `apply_patch` performs ordered exact-text edits, refuses ambiguous
matches unless `replace_all` is explicit, and preserves the file's dominant newline style.
After completed, failed, and cancelled tasks, the transcript shows the protected file set,
lazy full diff, and a task-level Undo action. Undo restores only that task's paths, refuses
to overwrite later edits, preserves unrelated changes, and uses its own crash-recoverable
safety snapshot. See `docs/task-modification-protection.md` for the exact scope and limits.
Windows junctions/reparse points are never followed into snapshots, and rollback refuses a
file/directory replacement when it would remove excluded or otherwise unprotected children.

Desktop conversation reopening now combines the persisted transcript with a filtered replay
of the project's durable RuntimeEvent journal. Tool cards, approval decisions, task status,
and elapsed time therefore survive project switches and application restarts while historical
pending approvals remain read-only and reset boundaries discard older execution details. A
persisted per-conversation event floor makes reset durable across Sidecar crash boundaries.

The Runtime now compacts the actual provider message history before it reaches the configured
context limit. It preserves recent turns and complete tool-call/result boundaries, stores an
LLM-generated summary with the conversation, and uses a deterministic fallback if the summary
request fails. Provider-reported Token usage is accumulated per call, task, and conversation;
APIs that omit usage are shown explicitly as estimates. See
`docs/context-compression-and-token-usage.md` for the memory boundaries and lifecycle.

The desktop top bar exposes a unified **Manage** and **Settings** center. Settings persist
non-secret General, Appearance, Models, Agent, and Diagnostics configuration in the Tauri
application-data directory. Management pages audit/edit shared project Memory, enable or reload
Skills, manage MCP/RAG, and explicitly connect Browser sessions without returning secret values.
The Problems panel now uses persisted real analyzer output rather than a fixed placeholder:
safe runs check Python syntax and Ruff and can query an explicitly configured local Python
Language Server over read-only LSP JSON-RPC; confirmed build runs are limited to project-local
TypeScript `--noEmit` and Cargo `--locked --offline`. Runs are asynchronous and cancellable.
The LSP client never installs a server, uses a shell, accepts workspace edits/commands, or reports
diagnostics for files it did not open; when no server is configured it is shown as unavailable
instead of simulating LSP output. The MCP Servers section reads live project Runtime state, displays discovered tools
and schemas, and can manage or add project-local stdio/HTTP servers.
General includes a persisted Simplified Chinese/English language selector that localizes all
built-in desktop navigation, dialogs, approvals, task/tool/Plan states, and settings pages;
model replies, project files, and third-party MCP content keep their original language.
General also lets users choose a custom root for temporary task worktrees. An empty value keeps
the default Tauri application-data location on the system drive; changing it restarts Runtime
and affects new task worktrees without moving conversations, snapshots, or existing task data.
Appearance includes
fixed dark/light presets, custom accent, background, panel, and font colors, automatically
dimmed secondary text, and client-wide font scaling. Model and Agent overrides are passed to
the Python Sidecar on restart; API keys remain in `.env` or system environment variables,
and Full access is never persisted as a default.

Configure `DEEPSEEK_API_KEY`, `GLM_API_KEY`, or `AGNES_API_KEY` in `.env`, select the provider with
`LLM_PROVIDER`, then run:

```powershell
stellarcode --memory-dir .stellarcode-memory
```

StellarCode starts in restricted mode, so risky operations ask for approval automatically.
To run without StellarCode approval, path, or network restrictions, start it explicitly with:

```powershell
stellarcode --mode full-access
```

Python 3.10 and newer are supported.

## Aider Polyglot benchmark Agent interface

For an end-to-end StellarCode evaluation, the benchmark runner must give the
Agent a clean per-case workspace and keep tests and reference implementations
private. The non-interactive interface is:

```powershell
stellarcode benchmark-agent `
  --workspace E:\bench\case-001 `
  --prompt-file E:\bench\case-001\PROMPT.md `
  --editable-file affine_cipher.py `
  --result-file E:\bench-results\case-001.json `
  --max-iterations 30
```

The workspace may contain only the task prompt and source files. This mode
exposes `read_file`, `list_dir`, `glob_files`, `grep_code`, `write_file`, and
`apply_patch`; it blocks network, shell, MCP, memory, project-generation, and
paths outside the workspace. `write_file` and `apply_patch` may modify only
the files named with `--editable-file`. The result JSON contains termination,
elapsed time, changed-file hashes, and a tool-event trail. The external runner
must run hidden tests after this command returns and calculate pass rates.

Use `benchmark-run` to do that end-to-end without copying files by hand:

```powershell
stellarcode benchmark-run `
  --clean-root "E:\study2\LLM Internship\PaiCLI\polyglot-python-agent-clean" `
  --tests-root "E:\study2\LLM Internship\PaiCLI\polyglot-benchmark-main\python\exercises\practice" `
  --results-dir "E:\study2\LLM Internship\PaiCLI\polyglot-results" `
  --case affine-cipher `
  --tries 1
```

It creates disposable work and private-test copies under `--results-dir`, calls
the Agent, copies only declared Python source files into the private copy, then
runs `python -m pytest -q`. It never changes either supplied dataset directory.
Omit `--case` to run all cleaned Python cases. `summary.json` contains pass rates
and the complete per-case agent/test records.

If a C++ run already generated answers but testing could not start (for example,
CMake was installed afterward), test the saved `private-tests` copies without
calling the Agent again:

```powershell
.\scripts\test_saved_polyglot_cpp_results.ps1 `
  -ResultsRoot "E:\study2\LLM Internship\PaiCLI\polyglot-results-cpp-deepseek-v4-flash"
```

It writes `cpp-test-summary-attempt-1.json` under the results directory. By
default it removes only each saved attempt's `build` directory to force a clean
compile; pass `-KeepBuild` to retain existing build outputs.
The script adjusts the disposable private copy's CMake exercise-name lookup;
this is required because its folder is named `private-tests` rather than the
exercise name, and it does not change the saved Agent source files.

## Complete Trace Mode

Trace mode records a complete, timestamped execution trail for debugging. It is off by
default because the log can contain user conversations, source code, file contents,
commands, and command output. Enable it at startup with:

```powershell
stellarcode --trace
```

The default output directory is `<workspace>/.stellarcode/traces`. Choose another directory
when needed:

```powershell
stellarcode --trace --trace-dir E:\stellarcode-logs
```

Trace mode can also be changed while StellarCode is running:

```text
/trace
/trace on
/trace off
/trace status
```

In the desktop client, use the `Trace On/Off` button beside the composer controls. The
desktop setting is stored independently for every conversation and restored when that
conversation is opened again.

Each session is an append-only `session-YYYYMMDD-HHMMSS-xxxxxxxx.jsonl` file. Events
include CLI input/output, complete LLM messages and tool schemas, LLM responses and
errors, tool arguments, complete untruncated command stdout/stderr, elapsed time, HITL
requests and decisions, and final task results. Concurrent events include their thread
name and tool-call ID.

Prompt assembly emits a separate `prompt_assembled` event containing the Prompt version,
mode, and per-layer character count, estimated Token count, sensitivity flag, and SHA-256
hash. This metadata event never contains the layer bodies. The desktop **Settings → Prompt**
page presents the same snapshot for the current conversation; retrieved Memory and compacted
summary bodies are omitted by Runtime unless the user explicitly enables their temporary
local display.

Credential-like fields and inline Bearer tokens are replaced with `[REDACTED]`. Base64
image payloads are omitted while their type and encoded size are retained. Trace files
still contain sensitive working context, so do not commit or share them casually.

## Implemented Features

- OpenAI-compatible GLM and Agnes chat clients with automatic text/vision routing
- Tool registry with JSON Schema definitions
- Built-in `read_file`, `write_file`, `apply_patch`, `delete_file`, `list_dir`,
  `glob_files`, `grep_code`, `execute_command`, `web_search`, `web_fetch`, and
  `search_code` tools
- ReAct loop with max-iteration protection
- Plan-and-Execute with DAG dependencies and topological ordering
- Short-term memory, persistent long-term JSON memory, retrieval, and compression
- Multi-Agent orchestrator with Planner, Worker, and Reviewer roles
- Two-worker parallel execution for independent DAG steps
- Conservative review parsing and up to two feedback-driven retries per step
- Isolated SubAgent histories with shared LLM, tools, and memory retrieval
- Python AST code chunks and import/inheritance/containment/call relations
- SQLite vector storage with project isolation and hybrid semantic/keyword ranking
- Human approval for dangerous tools with serialized Multi-Agent prompts
- Ordered parallel tool execution shared by ReAct, Plan tasks, and Multi-Agent Workers
- Per-task Git worktree isolation with checked, serialized terminal merges
- Separate frontend control and Python Runtime task state machines
- DAG-layer parallelism for independent Plan-and-Execute tasks
- Four-tool default concurrency cap, cooperative batch cancellation, command process-tree
  cleanup on timeout, and command-output truncation
- Persistent conversation history repair for interrupted assistant/tool-call rounds
- Persistent desktop task, tool, approval, Plan, and elapsed-time execution details
- Isolated task Side-Git snapshots, approval-time file diffs, atomic writes, conflict-safe
  task rollback, and interrupted-rollback recovery without touching the user's Git state
- Provider-history compression with tool-call-safe turn grouping and persisted summaries
- Exact provider Token usage with estimated fallback and per-task/conversation ledgers
- SSE streaming for ReAct, Plan, and Team for DeepSeek, GLM, and Agnes; Plan step and
  Team child-Agent deltas render in their own cards with authoritative final-message
  reconciliation, live context occupancy, cache/reasoning usage, and cost accounting
- Automatic Zhipu, SerpAPI, or SearXNG web search provider selection
- SSRF-protected, rate-limited web fetching with HTML-to-Markdown extraction
- Current local date and timezone injected into ReAct, Plan, and Multi-Agent prompts
- Live tool-call progress plus detailed iteration-limit and request-failure diagnostics
- Opt-in JSONL trace mode for complete LLM, tool, approval, console, and task results
- MCP 2025-03-26 client with stdio and Streamable HTTP transports
- Parallel MCP server startup, namespaced dynamic tools, HITL, and JSONL auditing
- Default isolated Chrome DevTools MCP with browser fallback and DOM snapshots
- Three-layer Skill registry, persistent enable state, lazy `load_skill`, and the
  built-in `web-access` decision guide with site-specific references
- Local image and clipboard references, bounded image preprocessing, GLM-5V content
  arrays, MCP screenshot attachments, and historical Base64 pruning

## Multimodal Image Input

To use DeepSeek V4 Flash for text and tool-calling tasks, configure:

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your_deepseek_api_key_here
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

DeepSeek V4 Flash uses the OpenAI-compatible Chat Completions API. StellarCode preserves
the returned `reasoning_content` in assistant history so subsequent tool rounds remain
valid. The DeepSeek endpoint itself is treated as text-only. When a separate vision
provider is not configured, local images and screenshot attachments are replaced by an
explicit text notice before the request is sent.

To keep DeepSeek for code and tool calling while routing image-bearing turns through the
existing GLM vision pipeline, configure:

```dotenv
LLM_PROVIDER=deepseek
VISION_PROVIDER=glm
GLM_API_KEY=your_glm_api_key_here
GLM_VISION_MODEL=glm-5v-turbo
# GLM_VISION_API_KEY=optional_standard_bigmodel_api_key
```

`VISION_PROVIDER` accepts `auto`, `glm`, `agnes`, or `disabled`. With `auto` (also the
default when the variable is omitted), StellarCode selects the first configured VLM in
GLM then Agnes order. Text-only requests continue using `DEEPSEEK_MODEL`; a request whose
active message history contains an image uses the VLM for the complete image/tool round.
After historical image bytes are pruned on the next user turn, routing returns to DeepSeek.
Provider-specific DeepSeek `reasoning_content` fields are removed from requests sent to GLM.

StellarCode routes each request automatically. With the GLM provider, normal text tasks use
`GLM_MODEL`; a request whose active context contains an image uses `GLM_VISION_MODEL`.
Leave `GLM_BASE_URL` unset so StellarCode can also select the matching endpoint:

```dotenv
GLM_MODEL=glm-5.1
GLM_VISION_MODEL=glm-5v-turbo
# GLM_VISION_API_KEY=optional_standard_api_key
# GLM_BASE_URL=https://your-compatible-endpoint/v1/chat/completions
```

Attach one or more local images directly in a normal prompt:

```text
> 描述 @image:docs/screenshot.png
> 对比 @image:<C:\Users\me\Desktop\before image.png> 和 @image:"after image.png"
> 分析我刚复制的截图 @clipboard
```

Relative paths are resolved from `--workspace`. Angle brackets or quotes are required
when a path contains spaces. `@clipboard` reads the current GUI clipboard image and
caches a PNG under `~/.stellarcode/cache`.

In the desktop client, focus the chat box and press `Ctrl+V` after copying an image or taking
a screenshot. The image appears as a removable attachment before it is sent, is cached under
`~/.stellarcode/cache`, and follows the same automatic VLM routing as attached image files.
Clipboard images are limited to 20 MB. Normal copied text continues to paste as text.

Images are decoded and validated rather than trusted by extension. Sources are limited
to 50 MB; payloads over the API's 5 MB Base64 limit are resized to at most 2000x2000
and re-encoded. Transparent images are flattened onto white. Previous-turn image data
is removed before the next user task so conversation history does not repeatedly send
large Base64 payloads.

MCP image results, including Chrome DevTools screenshots, remain structured image
attachments. StellarCode first returns the normal tool text message, then adds a user-role
image message for providers that reject images in `tool` messages. For page text and
DOM inspection, `take_snapshot` is still preferred over a screenshot.

No manual switching is needed. Local images and MCP screenshots select
`glm-5v-turbo` for that task. Before the next external user task, StellarCode removes the
historical image payload; an ordinary text prompt therefore returns to `glm-5.1`, or to the
separate primary text provider when cross-provider vision routing is enabled.
Set `GLM_VISION_MODEL=disabled` only when image routing must be turned off; image bytes
will then be omitted with an explicit notice.

`GLM_VISION_API_KEY` is optional and otherwise falls back to `GLM_API_KEY`. Configure
it separately when the text key belongs to a Coding Plan that cannot call the standard
multimodal API.

To use Agnes for both text and image requests, configure:

```dotenv
LLM_PROVIDER=agnes
AGNES_API_KEY=your_agnes_api_key_here
AGNES_MODEL=agnes-2.0-flash
AGNES_VISION_MODEL=agnes-2.0-flash
AGNES_BASE_URL=https://apihub.agnes-ai.com/v1
```

The default model and endpoint follow the Java PaiCLI Agnes integration. Agnes uses the
OpenAI-compatible `/chat/completions` request shape, including tool calls and image
content blocks. If the Agnes model list for your account exposes a separate vision
model, put that exact model ID in `AGNES_VISION_MODEL`; no code change is required.
Set `AGNES_VISION_MODEL=disabled` to omit images explicitly.

## ReAct And Plan

Use ReAct mode by default, or run a one-shot plan:

```text
/plan 创建 hello.txt，写入 Hello StellarCode，然后读取并验证内容
```

Switch modes explicitly:

```text
/plan
创建 demo 文件，写入内容，然后执行命令验证文件存在
/react
```

Preview a plan without executing it:

```text
/preview-plan 创建一个 README 草稿，然后检查内容
```

## Parallel Execution

When one LLM response contains several independent `tool_calls`, StellarCode executes them
concurrently through one shared `ToolRegistry.execute_tools()` path. A single tool call
stays on the calling thread, while larger batches use up to four daemon workers by
default. Results are always returned to the model in the original invocation order,
even when faster calls finish first.

Plan-and-Execute groups the task DAG into dependency layers. Independent tasks in one
layer run together; dependent tasks wait for the previous layer. Multi-Agent keeps its
Worker pool and each Worker can also execute independent tool calls concurrently.

Restricted mode creates every dangerous-tool approval in a batch before waiting for the
decisions. Desktop therefore displays the whole approval queue immediately and starts the
approved calls together after the batch is resolved. The terminal handler keeps its own
single-`stdin` lock, so CLI prompts remain serialized even though they use the same batching
code. Command output is capped at 8,000 characters before it is returned to the model.

Configure concurrency and the tool-batch timeout at startup:

```powershell
stellarcode --plan-workers 4 --max-parallel-tools 4 --tool-batch-timeout 90
```

The bundled `code-exploration` Skill owns the cross-tool workflow: discover candidates with
`glob_files`, locate exact symbols or strings with `grep_code`, read focused source with
`read_file`, and use `search_code` only for fuzzy semantic retrieval. Tool schemas describe
only each tool's own contract, while the system prompt retains global safety and verification
rules. Both exact-search tools prefer local `rg` and fall back to bounded Python scanning when
`rg` is unavailable. Restricted mode also blocks obvious POSIX and Windows full-disk recursive
scan commands before approval. Full-access mode bypasses that policy and intentionally keeps
the unrestricted command behavior defined by its access contract.

## Memory

```text
/memory
/save 项目默认使用 Python 3.10
/recall Python 版本
/clear
```

`/clear` clears the current conversation and short-term memory, but keeps long-term
memory in `long_term_memory.json`.

In the desktop Runtime, short-term memory and Token accounting remain isolated per
conversation. All loaded conversations in one project share a synchronized project-level
long-term memory service, so newly saved facts are visible across conversations without a
Runtime restart and concurrent writes cannot replace newer facts with a stale copy.
The **Manage → Memory** page exposes this same project store for audit, filtering, explicit
save/delete, and confirmed clear operations; it does not mix conversation history into the
long-term fact list.

Retrieved Memory and compacted conversation summaries are not placed in the system prompt.
They are attached to the relevant turn as standard `user` messages containing a versioned
`stellarcode.context/v1` JSON envelope marked `trusted=false`. The stable system policy treats
these records as evidence rather than instructions, and Runtime-only type metadata is removed
before provider requests. This keeps query-specific content out of the cached system prefix
and prevents Memory text from changing permissions or task scope.

## Multi-Agent

Run one task immediately:

```text
/team 创建一个 Python 计算器模块，补充单元测试，并运行测试验证
```

Or arm Multi-Agent mode for the next input only:

```text
/team
创建一个待办事项 CLI，包含新增、查看和删除功能，并写测试验证
```

After that task finishes, the CLI automatically returns to ReAct mode. The team uses
one Planner, two Workers, and one Reviewer by default. Independent DAG steps run in
parallel; dependent steps wait for their prerequisites. Configure the team with:

```powershell
stellarcode --team-workers 2 --team-retries 2
```

Only Workers receive tool schemas. Planner and Reviewer return structured JSON and do
not call tools. The orchestrator injects completed dependency results into the next
Worker's task context, truncated to 500 characters per dependency.

Team roles communicate through a per-run, append-only JSONL **MessageBus**.  The Lead
produces task/review requests into recipient mailboxes; Workers and Reviewers claim them
with a lease, append structured replies to the Lead mailbox, and acknowledge the original
message only after the reply is durable.  Mailbox lines are never deleted during a run:
expired leases are retried, exhausted messages enter `dead-letter.jsonl`, and completed
messages remain available for audit.  Desktop mailboxes are stored under the project Runtime
data directory; CLI mailboxes are local state in `.stellarcode/team-message-bus/`.

In the desktop client, the same collaboration is emitted as durable `team.*` RuntimeEvents.
The main transcript therefore shows one compact Team card; expand it to inspect each
Planner, Worker, and Reviewer request, reply, tool invocation, and status without mixing
their activity into the main assistant conversation.

## Code RAG

The default embedding provider follows the PDF chapter and uses Ollama with
`nomic-embed-text:latest`:

```powershell
ollama pull nomic-embed-text
$env:EMBEDDING_PROVIDER = "ollama"
stellarcode --rag-dir .stellarcode-rag
```

Inside StellarCode, build the index before searching:

```text
/index
/search Where is the ReAct tool-call loop implemented?
/graph Agent
```

The desktop client no longer requires a trip back to `/index`. Open **Settings > Code RAG**,
add one or more files/folders (or the full workspace), and select **Build index**. Sources are
stored per desktop project and are de-duplicated before one atomic rebuild, so adding a second
file does not replace the first file's index. The page reports live progress, indexed files,
chunks, relations, the active Embedding provider/model, and whether a rebuild is required.
It also supports rebuilding and clearing the generated SQLite index while retaining the
source list.

Desktop RAG settings can override the non-secret `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, and
`EMBEDDING_BASE_URL` values for the next Runtime. `EMBEDDING_API_KEY` remains in `.env` or the
system environment and is never copied into desktop settings. Automatic retrieval is enabled
by default and is injected once as Runtime capability state for ReAct, Plan, and Team workers;
it can be switched to explicit-request-only mode. The static `search_code` schema contains no
retrieval workflow. The `code-exploration` Skill interprets the mode and does not require an
RAG index for exact `glob_files` or `grep_code` exploration.

`/index [path]` rebuilds the index for the workspace or a specified directory. Python files are parsed with the
standard-library `ast` module; supported non-Python files use line-based chunks. The
index records chunks and Python code relations in SQLite. `/search` merges embedding
similarity with `jieba` keyword matches, then returns real source snippets with paths
and line numbers. The ReAct agent, Plan task agents, and Multi-Agent Workers can use the
same `search_code` tool.

An absolute `/index` path can point anywhere the current operating-system user can read.
File tools also accept absolute paths and `..` paths outside the working directory.
`--workspace` now selects only the starting directory for relative paths, commands, and
the default RAG project; it is not a security boundary.

For an offline smoke test that needs no model download, use the deterministic local
hash embedding. It is convenient for development but less capable than a real
embedding model:

```powershell
$env:EMBEDDING_PROVIDER = "local"
stellarcode --rag-dir .stellarcode-rag
```

OpenAI-compatible embedding endpoints are also supported with
`EMBEDDING_PROVIDER=openai` (or `zhipu`/`glm`) plus `EMBEDDING_MODEL`,
`EMBEDDING_BASE_URL`, and `EMBEDDING_API_KEY`.

## Web Search And Fetch

The Agent decides whether to call web tools from the normal conversation. Use
`web_search` when the answer depends on current public information, then use
`web_fetch` to read a selected result or a URL supplied by the user:

```text
> 搜索 Python 3.14 最近发布了哪些版本，并给出来源
> 阅读 https://docs.python.org/3/whatsnew/3.14.html 并总结主要变化
```

No slash command is required. The system prompt receives the current local date and
timezone so relative words such as "today" and "latest" have a concrete reference.

Search configuration is read from `.env`. An explicit provider wins:

```dotenv
SEARCH_PROVIDER=zhipu
ZHIPU_SEARCH_ENGINE=search_std
```

Supported values are `zhipu`, `serpapi`, and `searxng`. Without
`SEARCH_PROVIDER`, StellarCode selects the first available configuration in this order:
`GLM_API_KEY`, `SERPAPI_KEY`, then `SEARXNG_URL`. Zhipu reuses the normal GLM key.

The selected provider is the primary source, not the only source. StellarCode scores results
against the stable entity terms in the query. When the primary results are off-topic, it
tries any other configured provider and then the public Wikimedia API, which needs no
API key. Search output includes a quality assessment and up to three `web_fetch`
candidates. A single Agent or Multi-Agent Worker task can make at most four
`web_search` calls, preventing slightly different queries from consuming every tool
round without producing an answer.

`web_fetch` accepts only public HTTP/HTTPS destinations. It resolves and checks every
redirect target, blocks loopback/private/link-local addresses, limits the response body
to 5 MB, allows 30 logical fetches per 60 seconds, and returns up to 8,000 characters
by default. HTML pages are reduced to readable Markdown. Pages that require JavaScript
rendering or block automation may not expose readable static content.

## Skills And Web Access

StellarCode scans two editable Skill layers at startup. Project Skills completely
override User Skills with the same frontmatter `name`:

```text
1. ~/.stellarcode/skills/<name>/SKILL.md                   user
2. <workspace>/.stellarcode/skills/<name>/SKILL.md         project
```

Packaged default Skills are templates rather than a third registry layer. On first
startup, each missing template (for example `web-access`) is copied into the User
directory. StellarCode records the bundled version plus a SHA-256 hash of the complete
Skill tree, including `SKILL.md` and `references/`. A copy that still matches its trusted
installed hash is upgraded automatically with the application. A changed or legacy copy
without a trusted baseline is never overwritten: the desktop marks it as customized and,
when bundled content changes, shows **New version available**.

The Settings → Skills page can display a bounded text Diff from the current User copy to
the new bundled copy. **Update** and **Restore default** require confirmation and send both
Diff hashes back to Runtime; the operation is rejected if either tree changed after the
preview. **Keep custom** acknowledges only that exact local/bundled pair without changing
files, and a later bundled release appears as a new update again. Directory replacement and
the baseline state update are serialized across CLI/desktop processes; if baseline persistence
fails, the previous Skill directory is restored. Project Skills remain ordinary overrides and
are never changed by the bundled updater.

Only a compact name and description index is added to the system prompt. When a task
matches a description, the model calls the safe built-in `load_skill` tool. The full
body, capped at 5 KB, is supplied once as user-role context under
`## 已加载 Skill：<name>` for the next model round of the same task. This keeps normal
turns small while making detailed guidance available on demand without waiting for another
user message. `/clear` discards pending Skill context.

Each Multi-Agent role owns an independent Skill context buffer. The active buffer is
carried through `contextvars` into parallel ToolRegistry threads, so concurrent Workers
cannot consume one another's loaded Skill. Registry/frontmatter warnings are shown at
startup and by `/skill` or `/skill reload`. A failed `skills.json` update is reported by
the command without terminating the CLI or leaving a temporary state file behind.

Manage Skills without restarting StellarCode:

```text
/skill
/skill show web-access
/skill off web-access
/skill on web-access
/skill reload
```

Enable state and bundled version/hash baselines are stored in
`~/.stellarcode/skills.json`; newly added Skills are enabled by default. `/skill reload`
installs missing packaged templates, safely reconciles clean copies, rescans the User and
Project layers, and applies to the next LLM turn.

The built-in `web-access` Skill teaches the Agent to choose among `web_search`,
`web_fetch`, isolated Chrome DevTools, and a shared logged-in Chrome session. Its
`references/` directory contains a CDP quick reference and focused notes for GitHub,
WeChat articles, Zhihu, X, Xiaohongshu, and Juejin. A normal prompt is enough:

```text
> 阅读这个知乎链接并总结主要观点
```

The built-in `code-exploration` Skill is the single workflow source for file discovery,
exact search, semantic RAG, focused reading, editing, and verification. System and Tool
Schema layers deliberately do not repeat those sequencing rules.

To create a project Skill, add `.stellarcode/skills/code-review/SKILL.md`:

```markdown
---
name: code-review
description: Review Python changes for correctness, regressions, and missing tests.
version: "1.0.0"
tags: [review, python]
---

# Code Review

Inspect the diff, identify findings by severity, and run focused tests.
```

Then run `/skill reload`. The directory name is used as a fallback when `name` is
omitted, but explicit kebab-case names are recommended.

## MCP Servers

StellarCode loads MCP configuration from two locations. User configuration is loaded first,
then project configuration overrides servers with the same name:

```text
~/.stellarcode/mcp.json
<workspace>/.stellarcode/mcp.json
```

When the user-level file does not exist, StellarCode creates it once with the default Google
Chrome DevTools MCP server. An existing file is never modified; if it lacks the browser
entry, startup prints a README hint instead.

The format is compatible with the common `mcpServers` configuration shape. A complete
starting point is available in `docs/mcp.example.json`. For example, a local filesystem
server can be configured with stdio:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "${PROJECT_DIR}"]
    }
  }
}
```

A remote Streamable HTTP server uses `url` and optional headers:

```json
{
  "mcpServers": {
    "remote": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ${MCP_API_KEY}"}
    }
  }
}
```

`${PROJECT_DIR}`, `${HOME}`, process environment variables, project `.env`, and
`~/.env` values are expanded independently for each server. One broken server enters
the `error` state without blocking the others. Configured servers start concurrently,
perform the MCP initialize handshake, discover `tools/list`, sanitize input schemas,
and register names such as `mcp__filesystem__read_file` in the shared ToolRegistry.
ReAct, Plan, and Multi-Agent Workers can all call the discovered tools.

Desktop users can manage the same runtime from **Settings → MCP Servers**. The page displays
live `starting`/`ready`/`error`/`disabled` state, safe endpoint metadata, all discovered
namespaced tools and input schemas, and bounded stderr logs. It supports project-local stdio
and Streamable HTTP additions, enable/disable, restart, and removal. Adding stdio requires an
explicit confirmation of the command that will run with the current Windows account. Secret
environment/header values are not returned to the UI; store `${VARIABLE}` placeholders in
the MCP config and define their values in the system environment or `.env`.

Manage servers while StellarCode is running:

```text
/mcp
/mcp restart filesystem
/mcp logs filesystem
/mcp disable filesystem
/mcp enable filesystem
```

In restricted mode, MCP tools require approval by default because third-party tools may
have side effects not visible from their names. The trusted `chrome-devtools` server is
an explicit exception: every `mcp__chrome-devtools__*` tool runs without approval.
Full-access mode skips approval for all tools.
Executed MCP calls are appended to `.stellarcode/audit/mcp-tools.jsonl`; argument keys that
look like passwords, tokens, secrets, API keys, or authorization values are redacted.

The project-local weather server exposes `mcp__weather__query_weather` through the
official UAPI Python SDK. Put `UAPI_TOKEN` in `.env`, keep the `weather` entry in
`.stellarcode/mcp.json`, and restart StellarCode. The Agent can then answer requests such as
`查询北京天气` automatically. Set `forecast`, `hourly`, `extended`, `minutely`, or
`indices` when richer weather data is needed; `adcode` takes precedence over `city`.

### Chrome DevTools MCP

The default browser server runs in isolated mode so Agent activity does not reuse the
cookies, logins, local storage, or cache from the user's daily Chrome profile:

```json
{
  "mcpServers": {
    "chrome-devtools": {
      "command": "npx",
      "args": ["-y", "chrome-devtools-mcp@latest", "--isolated=true"]
    }
  }
}
```

Node.js 20.19 or a newer supported LTS release is required by current versions of
`chrome-devtools-mcp`. The first start may be slow while `npx` downloads the package and
Chrome starts. StellarCode allows 60 seconds for MCP initialization and prints a waiting line
every five seconds. Use `/mcp logs chrome-devtools` for the underlying npm, Node, or
Chrome error when startup fails.

For normal public URLs the Agent tries `web_fetch` once. It falls back to browser tools
for JavaScript-rendered pages, blocked or empty static responses, forms, console logs,
and network inspection. WeChat article pages may go directly to the browser. Browser
reading prefers `mcp__chrome-devtools__take_snapshot`, which returns readable DOM text;
`take_screenshot` is reserved for explicit visual requests.

All `mcp__chrome-devtools__*` tools are classified as safe and run without an approval
prompt, including navigation, snapshots, clicks, form interactions, JavaScript
evaluation, and network inspection. Other MCP servers keep the normal restricted-mode
approval policy.

### CDP Session Reuse

Chrome DevTools starts in isolated mode by default. This temporary profile cannot see
the cookies, login state, or tabs from the user's normal Chrome. Inspect and switch the
runtime browser session with:

```text
/browser
/browser status
/browser connect
/browser tabs
/browser disconnect
```

For Chrome 144 or newer, open `chrome://inspect/#remote-debugging`, enable remote
debugging, and run `/browser connect`. StellarCode restarts only the in-memory
`chrome-devtools` server arguments with `--autoConnect`; Chrome then displays its own
permission dialog. The change is not written to `~/.stellarcode/mcp.json`, so a new StellarCode
process returns to the configured default.

The legacy `/browser connect 9222` form first checks
`http://127.0.0.1:9222/json/version`. It changes the MCP arguments only after that check
succeeds, using `--browser-url=http://127.0.0.1:9222`. A failed switch restores the
previous arguments and restarts the previous browser mode.

The Agent also receives `browser_status`, `browser_connect`, `browser_tabs`, and
`browser_disconnect` tools. It can therefore switch to a shared login session when an
isolated page reaches a login wall. In shared mode, StellarCode may close only tabs opened by
StellarCode during the current shared session; attempts to close the user's existing tabs
are blocked. All other Chrome tools remain approval-free as configured above.

Remote debugging gives the MCP server access to the selected Chrome profile, including
open tabs, cookies, and site data. Enable it only for a trusted local StellarCode process and
disconnect when shared access is no longer needed.

MCP resources, prompts, OAuth, sampling, and broader browser automation policies remain
outside this phase.

## Human Approval

StellarCode has two startup access modes. Restricted mode is the default and automatically
requires confirmation before file writes, deletion, state-changing or unknown command
execution, or project creation. `web_search`, `web_fetch`, and
`mcp__chrome-devtools__*` are treated as safe and run without an approval prompt. Other
dynamically loaded `mcp__*` tools require approval:

```powershell
stellarcode
# Equivalent explicit form:
stellarcode --mode restricted
```

The policy requires approval for `write_file`, `apply_patch`, `delete_file`, and the
future-facing `create_project` tool. `execute_command` is classified from its complete
command text.
A strict allowlist lets common environment inspection commands run without approval,
including `conda env list`, `conda info`, `conda list`, `python --version`,
`conda run -n <env> python --version`, `pip list/show/freeze`, `Get-Command`,
`where.exe`, `Test-Path`, `Resolve-Path`, and `nvidia-smi`. Pipelines and semicolon
groups are safe only when every segment is allowlisted. `python -c`, package installs,
redirection, command substitution, filesystem changes, and all unrecognized commands
remain high risk. Read-only `read_file`, `list_dir`, `glob_files`, `grep_code`,
`search_code`, `web_search`, and `web_fetch` calls also run without a prompt.

Each approval request shows the tool, localized risk level, risk description, and JSON
arguments in a bordered Rich terminal panel. Long argument values are truncated and
retain their original character count.

Available decisions are:

```text
y or Enter  approve this call
a           approve this tool for the current session
n           reject and optionally provide a reason to the Agent
s           skip this call
m           replace the call arguments with a JSON object
```

Full-access mode removes StellarCode's approval prompts and allows file tools to use any path
available to the current operating-system account. It also permits direct HTTP/HTTPS
fetches and unrestricted commands without StellarCode confirmation:

```powershell
stellarcode --mode full-access
```

The access mode can also be inspected or changed while StellarCode is running:

```text
/mode
/mode restricted
/mode full-access
```

Switching to full access requires typing `FULL ACCESS` at a second confirmation prompt.
Switching back to restricted mode is immediate. Every mode change clears session-level
"approve all" choices. The old `/hitl on` and `/hitl off` commands only show a migration
hint and do not change permissions. ReAct, Plan task agents, and Multi-Agent Workers
share the same live approval registry, so a mode change applies to all three execution
paths. Desktop approval requests can wait concurrently; terminal prompts are serialized so
only one request reads terminal input at a time.

Full access still runs with the permissions of the Windows/Linux/macOS account that
started StellarCode. Operating-system ACLs, firewalls, proxies, and endpoint security can
still deny an operation.

## Development

```powershell
pytest
python -m compileall src
ruff check src tests
```
