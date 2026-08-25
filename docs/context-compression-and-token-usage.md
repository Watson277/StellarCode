# Context Compression and Token Usage

StellarCode keeps three different forms of memory. They solve different problems and must
not be treated as one store:

1. `Agent.messages` is the provider conversation history. It contains the system prompt,
   user/assistant messages, typed internal user-context messages, tool calls, and matching
   tool results. This is the context that is actually sent to the model and is persisted per
   desktop conversation.
2. `MemoryManager.short_term` is a small retrieval-oriented cache. It stores bounded
   conversation/tool entries and retrieves only relevant excerpts. Retrieved Memory is
   wrapped as a versioned, untrusted JSON object in a separate `user` message immediately
   before the real request; it is never concatenated into the system prompt. Its older
   deterministic compression does not reduce `Agent.messages` by itself.
3. `LongTermMemory` is a project-level JSON fact store used by retrieval. It is independent
   from the provider history and survives conversation resets.

The desktop Runtime owns one `ProjectMemoryService` for each open project. Every conversation
receives its own `MemoryManager`, short-term cache, and Token budget, while those managers all
reference the service's single synchronized `LongTermMemory`. A fact saved or extracted in one
loaded conversation is therefore immediately visible to every other conversation in the same
project. Long-term writes are serialized and persisted with an atomic file replacement, so
parallel conversations cannot overwrite one another with stale in-memory copies. Opening a
different project creates a different service and a different long-term memory file.

## Real history compression

`ConversationHistoryCompactor` now runs immediately before every Agent model request. Its
preflight estimate includes the complete provider messages and tool schemas. The default
200,000-token window triggers near 167,000 tokens, reserving roughly 20,000 tokens for the
next response and 13,000 tokens as a safety margin. Smaller configured windows scale the
reserves down.

When the threshold is crossed, the compactor:

1. groups messages into complete user turns;
2. preserves the two newest turns verbatim;
3. summarizes older complete turns with the configured model;
4. falls back to a deterministic evidence summary if the summary request fails;
5. wraps the summary as a versioned, untrusted JSON object in a `user` context message,
   leaving the system prompt byte-stable; and
6. truncates oversized older tool results if more space is still needed.

An assistant tool call and all matching `tool` responses are always moved or removed as one
turn. The compressor never leaves an orphan `tool_call_id`. The compacted message history,
summary, compaction count, and timestamp are persisted in the conversation JSON, so reopening
the desktop conversation continues from the compacted state instead of reconstructing the
discarded raw turns.

Memory and summary messages carry a private `_stellarcode_context` marker only inside the
Runtime. That marker lets compaction and refresh logic distinguish synthetic context from an
ordinary user request. It is stripped before every provider call, so compatible APIs receive
only standard `role` and `content` fields. The visible content uses the
`stellarcode.context/v1` JSON schema with `trusted=false`; JSON serialization keeps quotes,
newlines, role-like text, and fake closing tags inside the data field. The stable system
policy independently states that Memory and compacted history are untrusted evidence and
cannot expand task scope, permissions, or instruction priority.

The Runtime emits `history.compacted` with the estimated before/after context sizes, number
of compacted turns, method, and cumulative compaction count.
Because generating the summary may take as long as a normal model request, it also brackets
the operation with `history.compaction.started` and `history.compaction.finished`. Desktop
uses these events to show `正在压缩上下文` and clears the status from a guaranteed cleanup
path on success, fallback, cancellation, or failure.

## Token usage

All OpenAI-compatible clients return a `ChatResult` containing both the assistant message
and `TokenUsage`. Provider `usage` values are authoritative when present. This includes
prompt/input, completion/output, cache-hit, and reasoning token details exposed by the API.
Legacy/fake clients and providers that omit usage receive a clearly marked local estimate;
the estimate is used for safety and display but is never presented as an exact provider
count.

`TracingChatClient` records every call, including planning, team workers/reviewers, final
answer recovery, and history-summary calls. `UsageLedger` accumulates:

- the latest call's context, input, and output tokens;
- cache-hit and reasoning-token details for the latest call, task, and conversation;
- current task totals and LLM call count; and
- persisted conversation totals, LLM call count, and available cost totals.

The ledger emits `usage.updated` after each completed model request and is stored with the
conversation. A cancelled request finishing in a detached thread cannot write its usage into
a newer task. Runtime context is copied into parallel Plan and Team workers so their usage is
attributed to the correct conversation and task.

## Desktop behavior

The right sidebar's **Context budget** panel displays the current request context percentage,
latest input/output, current task totals, conversation totals, model-call count, and history
compaction count. `Provider usage` means the last API response supplied exact counts;
`Estimated` means the fallback estimator was used.

GLM, DeepSeek, and Agnes requests use SSE while the desktop Agent is active. The final SSE
usage chunk is parsed into the same `TokenUsage` object, so streaming does not trade away
provider counts. If a compatible gateway rejects `stream_options`, StellarCode retries the
stream without that optional field and clearly falls back to estimated Token counts when no
usage chunk is supplied.

Cost is separate from Token exactness. An API may return exact Token counts without returning
the billed amount. In that case `UsageLedger` estimates cost from cache-miss input, cache-hit
input, and output rates. Set `{PROVIDER}_INPUT_COST_PER_MILLION`,
`{PROVIDER}_CACHED_INPUT_COST_PER_MILLION`, `{PROVIDER}_OUTPUT_COST_PER_MILLION`, and
`{PROVIDER}_COST_CURRENCY` (or their `LLM_` generic equivalents) for custom models and
gateways. The UI labels calculated cost as estimated and provider-returned cost as reported.
Built-in DeepSeek V4 rates are scoped to exact model names and sourced from the official
[DeepSeek Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/) table; explicit
environment rates always take priority so deployments can track provider price changes.

The context capacity is configured under **Settings > Agent > Context window** and is passed
to Python when the Sidecar starts. Set it to the selected model's actual context limit. The
setting affects the compression trigger and percentage display; it does not change the
provider model's real limit.
