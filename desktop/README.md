# StellarCode Desktop

Tauri 2 + React desktop client for the local StellarCode Python coding agent.

The desktop owns Sidecar fault recovery: an unexpected Python exit keeps the current task
visible, restarts the Runtime with exponential backoff, replays missed ordered events, and
continues unfinished durable task checkpoints after reopening their conversations.
Normal project switching no longer retires Python. One Sidecar lazily hosts a Runtime per
loaded project, and each Runtime can run one task per conversation concurrently. Tauri still
tags every forwarded message with the originating process ID so late messages from an actual
restart are discarded before sequence de-duplication.

## Development

```powershell
npm install
npm run tauri dev
```

`npm run tauri dev` starts the Python Runtime automatically from the repository `.venv`.
The Tauri backend owns the child process and forwards the JSONL RuntimeEvent stream to React.
Set `STELLARCODE_PYTHON` when a different Python executable is required.
The child process is forced to UTF-8 mode so Chinese prompts and responses remain valid JSONL
on Windows systems whose default console encoding is GBK.

The composer Stop button sends cooperative `task.cancel` to ReAct, Plan, or Team tasks.
Pending approvals are rejected and active command process trees are terminated.
All dangerous approvals in one tool batch are published before Runtime waits for any single
decision, and the desktop queue remains actionable across project/conversation switches. Each approval is correlated by
`approval_id`, while its tool card is correlated by `tool_call_id` and displays
`WAIT`, `RUN`, `OK`, or `FAIL` according to its actual lifecycle.
Consecutive tool calls and their progress updates are rendered in one compact StellarCode
activity message instead of creating a separate conversation row for every event.
Assistant responses are rendered as GitHub-flavored Markdown, including headings, lists,
tables, links, blockquotes, inline code, fenced code blocks, and task lists.
DeepSeek, GLM, and Agnes answers arrive incrementally through `assistant.delta`; the final
`assistant.completed` event reconciles the displayed Markdown with the persisted answer.
The Context budget panel uses provider Token counts when the final stream includes usage,
marks estimator fallbacks explicitly, and shows task/conversation cost when provider or
configured pricing is available.

Each submitted task also gets one compact live Agent status bar above its response. The bar
shows a running `mm:ss`/`h:mm:ss` timer, the latest bounded operational summary, approval and
tool phases, and a prominent `正在压缩上下文` state while history compaction is active. It
uses Runtime progress summaries rather than exposing raw model reasoning. The timer stops at
the Runtime-reported elapsed duration when the task completes, or at the local cancellation/
failure time when no terminal duration is supplied.

Plan mode renders the generated DAG as a live execution card in the transcript. The card
shows task types and dependencies, overall completion, and per-step pending, running,
completed, failed, skipped, or cancelled state. Independent steps may update concurrently;
their tool calls continue to use the shared compact activity cards and approval queue.

Before `task.submit` is acknowledged, Runtime creates an isolated Side-Git baseline in the
application-data directory. The user's repository HEAD, index, branches, and worktree Git
configuration are never changed. File-write approvals show a bounded diff; approved writes
are revalidated against their original hash and performed with same-directory atomic replace.
Every task terminal card lists its protected file changes and offers lazy `View diff` and
`Undo task changes` actions. Undo is path-scoped and all-or-nothing: if any task file has been
edited later it reports a conflict instead of overwriting it, while unrelated edits remain
untouched. A pre-rollback safety revision lets Runtime repair an interrupted partial rollback
on restart. Workspace-external, sensitive, generated, dependency, and Runtime-internal paths
are explicitly labelled outside this rollback boundary; network, database, process, and other
external side effects cannot be undone by a workspace Git snapshot.
Windows junctions/reparse points are excluded from traversal. File/directory type rollback
also stops before writing if recursive replacement would remove an excluded or unprotected
child such as a later `.env` file.

## Settings and desktop management

The top bar has separate **Manage** and **Settings** entry points. They open the same unified
desktop center at the relevant section, so project services and application preferences do not
require CLI commands. The center includes:

- General controls the persisted client language (Simplified Chinese or English), reopening
  the last project, Enter versus Ctrl+Enter sending, conversation font size, and compact
  tool/Plan rendering. Language selection covers built-in navigation, dialogs, approvals,
  task/tool/Plan states, Settings, empty states, and diagnostics; model replies, workspace
  content, and third-party MCP output remain in their source language. These options apply
  after saving without a Runtime restart.
- Appearance switches between fixed dark and light color presets, supports custom accent,
  base-background, panel, and primary-font colors, and scales the entire desktop client from
  10 to 16 px. Panel color controls the gray surface family used by sidebars, bars, the
  composer, selected rows, and raised controls. Switching themes restores that theme's
  complete preset before further customization. Appearance changes apply after saving
  without restarting Runtime; conversation text can still be sized independently under
  General.

The selected font color is the source for all client typography. Solid accent buttons use an
automatic high-contrast foreground for readability; primary labels and message
content use it directly; secondary labels, timestamps, sidebar metadata, input text, and
placeholders use automatically mixed soft and faint variants. Success, warning, and error
states retain semantic borders, backgrounds, and indicator dots while their text still
follows the selected font color.

Conversation messages no longer reserve an avatar gutter for the user or Agent. Assistant
Markdown uses the selected primary font color consistently for headings, list markers,
links, emphasis, and code. Table headers and cells use the same base background as the
client, while borders preserve the table structure.
- Models configures provider-free OpenAI-compatible text and optional vision endpoints.
  Text uses `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_NAME`; a separate visual endpoint
  uses the matching `VISION_*` names. The Settings page contains no provider selector.
  API keys are detected for status display but are never copied into the settings file.
- Agent configures the default mode for new conversations, maximum iterations, parallel
  tools, tool timeout, Plan workers, Team workers, Team retries, and the model context window
  used by the Runtime's preflight compression threshold.
- Memory audits the active project's shared long-term facts and supports filtering, explicit
  saves, deletion, and confirmed clearing. Short-term conversation context remains isolated
  and is not mixed into this page.
- Skills lists user and project Skills with their source, metadata, load warnings, and current
  enabled state. Packaged defaults are installed into the User directory without overwriting
  existing copies. User/project directories can be opened for custom installs; Reload discovers
  new `SKILL.md` files without restarting the desktop.
- Code RAG selects files, folders, or the complete workspace as project-scoped semantic
  search sources. It shows live background-index progress and persisted file/chunk/relation
  counts, and supports source removal, rebuild, and generated-index clearing. Embedding
  model/Base URL overrides are passed to the next Runtime as `EMBEDDING_MODEL_NAME` and
  `EMBEDDING_BASE_URL`, while `EMBEDDING_API_KEY` remains in
  `.env`. Automatic retrieval lets the Agent call `search_code` proactively;
  disabling it restricts RAG use to an explicit user request. Changing an Embedding model
  requires a Runtime restart and rebuild.
- MCP Servers shows every user/project server, live startup/error state, transport, discovered
  `mcp__server__tool` names, descriptions, and input schemas. Servers can be refreshed,
  enabled/disabled, restarted, inspected for stderr logs, or removed when project-owned.
  The custom-server form writes project-local stdio or Streamable HTTP configuration and
  starts it immediately. A local stdio command is shown in an explicit confirmation dialog
  before execution. Environment/header values are never returned to React; secrets should
  use `${VARIABLE}` placeholders resolved from environment or `.env` files.
- Browser manages the Chrome DevTools MCP session: isolated/shared state, autoConnect, explicit
  legacy CDP probing, shared-tab listing, and disconnect. Connecting to signed-in Chrome always
  shows a warning because page content may contain sensitive data.
- Data & Diagnostics selects an optional Python executable, reports every available analyzer,
  and opens local data paths. The Problems bottom panel is backed by persisted real diagnostics:
  the safe profile compiles Python syntax without importing workspace code and runs Ruff when
  available. Explicit build checks can run only project-local TypeScript with `--noEmit`, or
  `cargo check --locked --offline`; they never invoke npm/npx lifecycle scripts or download tools.
  A real local Python Language Server can be enabled with an absolute executable and literal argv
  in Diagnostics settings. It is disabled by default, never installed automatically, receives only
  bounded `didOpen` source, and all server requests (including edits and commands) are rejected.
  Without that explicit configuration LSP remains unavailable; syntax and Ruff findings are never
  mislabeled as LSP results. Runs are asynchronous, cancellable, and
  restored as stale/interrupted state after a Runtime restart.

Non-secret settings are validated and stored in the Tauri application-data directory as
`settings.json`. Model, Agent, RAG, and Python changes are passed to the next Python Sidecar and
can be applied immediately with `Save & Restart Runtime`; General and Appearance settings do
not restart an active task. Non-default access modes are deliberately excluded from persistent
defaults and reset to Normal after an app or project restart.

The composer Attach button accepts up to 10 local files. Files can also be dragged from the
desktop into the app window, and a copied image or screenshot can be pasted directly into
the focused chat box with `Ctrl+V`. Clipboard images are limited to 20 MB and appear as
removable attachment chips before sending. Selected attachments are shown with the persisted
user message. Image attachments use a compact 4:3 thumbnail with `contain` fitting, so the
whole image remains visible without expanding the conversation row excessively. Images use
the multimodal input pipeline; bounded
text and source files are inlined for the Agent, including files outside the workspace that
the user explicitly selected. Individual files are limited to 50 MB, inline text to 1 MB,
and total inlined attachment text to 300,000 characters.

When a separate vision endpoint is configured, Desktop uses the Python Runtime's automatic
vision router. Image-bearing turns use `VISION_MODEL_NAME`, while ordinary turns remain on
`LLM_MODEL_NAME`. The Runtime context displays a combined
provider label such as `deepseek+glm-vlm` when this route is active.

Each conversation has an independent `Trace On/Off` control in the composer. The setting is
persisted with that conversation, and redacted JSONL logs are written to
`<workspace>/.stellarcode/traces`. The Trace bottom panel shows the active log path.

The right sidebar includes a live Context budget panel. Provider-reported input/output usage
is displayed after every model call and accumulated for the active task and conversation. If
an API omits usage, the panel labels the local fallback as `Estimated`. When provider history
approaches the configured context limit, Python summarizes older complete turns, keeps recent
turns intact, preserves tool-call/result boundaries, and persists the summary and compaction
count with that conversation. While the summary request is running, the panel displays
`正在压缩上下文` and clears it when the compression lifecycle finishes.
The Services panel also reports ready/total MCP servers, the number of currently registered
MCP tools, and whether Code RAG is indexing, ready with indexed files, or not indexed. Problems
shows live error/warning counts, source/code, file/line/column, progress, cancellation, and the
last project-scoped result rather than a fixed placeholder.

Projects are registered in the Tauri application-data directory. Removing a project from
StellarCode never deletes its real directory. Each project can own multiple conversations;
their transcript and Agent message history are persisted outside the Git workspace and
restored after restarting the client.

Each loaded conversation keeps an independent short-term retrieval cache and Token budget.
The Python Runtime owns one synchronized long-term memory service per project, shared by all
of that project's conversation managers. Facts saved or extracted in one conversation are
available to the others immediately, and serialized atomic persistence prevents stale
conversation copies from losing concurrent updates.

Opening a conversation also performs a filtered replay from the project's durable Runtime
event journal. This restores task status and elapsed time, tool arguments/results/duration,
Plan progress, and approval decisions in their original positions between the matching user
message and final answer. Replay is de-duplicated by event ID, stops at the latest reset
boundary, and treats an unresolved historical approval as interrupted rather than actionable.
Conversation schema v2 also stores the journal sequence floor at reset time, preventing old
execution cards from reappearing across the snapshot/journal crash boundary.

The left sidebar renders projects and conversations as one tree instead of two independent
lists. Multiple project nodes can remain expanded simultaneously. Conversation metadata is
cached after a project is loaded, so switching Runtime workspace does not collapse or hide
the previous project's conversation list. Clicking a cached conversation under an inactive
project activates its already-loaded Runtime and then opens that exact conversation without
stopping background tasks. Running conversations have a live indicator and project rows show
their background task count. The `+` button on the active
project creates another conversation, and double-clicking an active conversation renames it.
Project rows keep their registration order when selected; the most recently opened project
is still restored on the next launch when that General setting is enabled.
The sidebar does not include a separate file explorer; the project/conversation tree uses
the available vertical space, while the Agent continues to inspect workspace files through
its Runtime tools.

On Windows, canonical project paths are stored and passed to shell tools without the
verbatim `\\?\` prefix. Existing project records are migrated on startup so CMD, PowerShell,
and Conda receive a normal drive-letter working directory such as `E:\project`.

Workspace commands run with stdin closed so they cannot consume the Sidecar JSONL protocol.
They also receive the user's original `PYTHONPATH`, not the temporary Runtime import path
that Tauri uses to launch `stellarcode.runtime.sidecar`.

The composer includes three Runtime access modes:

- `Normal` is the default and requests approval for risky operations.
- `Balanced` automatically approves medium-risk operations while still requesting approval
  for high-risk commands and file deletion. Restricted-mode hard policy checks remain active.
- `Full access` bypasses Runtime approvals and permits operations outside the active project,
  subject to Windows permissions. Enabling it requires confirmation and it resets to
  `Normal` after an app restart or project switch. `Balanced` resets the same way.

The development build expects the repository layout to remain intact. A future distributable
installer must bundle a frozen Python sidecar executable and its runtime assets.

## Validation

```powershell
npm run build
cd src-tauri
cargo test --no-default-features --locked --offline
cargo build --no-default-features --locked --offline
```

Runtime-only smoke test from the repository root:

```powershell
.\.venv\Scripts\python.exe -m stellarcode.runtime.sidecar --workspace .
```
