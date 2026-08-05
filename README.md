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

Configure either `GLM_API_KEY` or `AGNES_API_KEY` in `.env`, select the provider with
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

Each session is an append-only `session-YYYYMMDD-HHMMSS-xxxxxxxx.jsonl` file. Events
include CLI input/output, complete LLM messages and tool schemas, LLM responses and
errors, tool arguments, complete untruncated command stdout/stderr, elapsed time, HITL
requests and decisions, and final task results. Concurrent events include their thread
name and tool-call ID.

Credential-like fields and inline Bearer tokens are replaced with `[REDACTED]`. Base64
image payloads are omitted while their type and encoded size are retained. Trace files
still contain sensitive working context, so do not commit or share them casually.

## Implemented Features

- OpenAI-compatible GLM and Agnes chat clients with automatic text/vision routing
- Tool registry with JSON Schema definitions
- Built-in `read_file`, `write_file`, `delete_file`, `list_dir`, `execute_command`,
  `web_search`, `web_fetch`, and `search_code` tools
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
- DAG-layer parallelism for independent Plan-and-Execute tasks
- Four-tool default concurrency cap, batch timeout, and command-output truncation
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
historical image payload; an ordinary text prompt therefore returns to `glm-5.1`.
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

Restricted mode gathers dangerous-tool approvals serially before starting the approved
calls in parallel. This preserves the terminal's single `stdin` owner and prevents a
batch timeout from leaving an approval prompt running in the background. Command output
is capped at 8,000 characters before it is returned to the model.

Configure concurrency and the tool-batch timeout at startup:

```powershell
stellarcode --plan-workers 4 --max-parallel-tools 4 --tool-batch-timeout 90
```

The prompt asks the model to use focused `list_dir`, `read_file`, and `search_code`
exploration. Restricted mode also blocks obvious POSIX and Windows full-disk recursive
scan commands before approval. Full-access mode bypasses that policy and intentionally
keeps the unrestricted command behavior defined by its access contract.

## Memory

```text
/memory
/save 项目默认使用 Python 3.10
/recall Python 版本
/clear
```

`/clear` clears the current conversation and short-term memory, but keeps long-term
memory in `long_term_memory.json`.

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

StellarCode scans three Skill layers at startup. A later layer completely overrides an
earlier Skill with the same frontmatter `name`:

```text
1. <installed stellarcode package>/skills/<name>/SKILL.md   builtin
2. ~/.stellarcode/skills/<name>/SKILL.md                   user
3. <workspace>/.stellarcode/skills/<name>/SKILL.md         project
```

Only a compact name and description index is added to the system prompt. When a task
matches a description, the model calls the safe built-in `load_skill` tool. The full
body, capped at 5 KB, is queued and prepended once to the next user message under
`## 已加载 Skill：<name>`. This keeps normal turns small while making detailed guidance
available on demand. `/clear` discards pending Skill context.

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

Enable state is stored in `~/.stellarcode/skills.json` as a `disabled` list, so newly added
Skills are enabled by default. `/skill reload` rescans all three layers and applies to
the next LLM turn.

The built-in `web-access` Skill teaches the Agent to choose among `web_search`,
`web_fetch`, isolated Chrome DevTools, and a shared logged-in Chrome session. Its
`references/` directory contains a CDP quick reference and focused notes for GitHub,
WeChat articles, Zhihu, X, Xiaohongshu, and Juejin. A normal prompt is enough:

```text
> 阅读这个知乎链接并总结主要观点
```

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

The policy requires approval for `write_file`, `delete_file`, and the future-facing
`create_project` tool. `execute_command` is classified from its complete command text.
A strict allowlist lets common environment inspection commands run without approval,
including `conda env list`, `conda info`, `conda list`, `python --version`,
`conda run -n <env> python --version`, `pip list/show/freeze`, `Get-Command`,
`where.exe`, `Test-Path`, `Resolve-Path`, and `nvidia-smi`. Pipelines and semicolon
groups are safe only when every segment is allowlisted. `python -c`, package installs,
redirection, command substitution, filesystem changes, and all unrecognized commands
remain high risk. Read-only `read_file`, `list_dir`, `search_code`, `web_search`, and
`web_fetch` calls also run without a prompt.

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
paths. Concurrent Worker approval prompts are serialized so only one request reads
terminal input at a time.

Full access still runs with the permissions of the Windows/Linux/macOS account that
started StellarCode. Operating-system ACLs, firewalls, proxies, and endpoint security can
still deny an operation.

## Development

```powershell
pytest
python -m compileall src
ruff check src tests
```
