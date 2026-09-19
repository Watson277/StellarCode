// Browser fixture only: never imported by the production entry point.
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { mockIPC, mockWindows } from "@tauri-apps/api/mocks";
import { emit } from "@tauri-apps/api/event";
import App from "../src/App";
import { DEFAULT_APP_SETTINGS } from "../src/settingsDefaults";
import { ErrorBoundary } from "../src/components/ErrorBoundary";
import { ToolActivityCard } from "../src/features/activity/ToolActivityCard";
import { translator } from "../src/i18n";
import "../src/ui-foundations.css";

const now = new Date().toISOString();
const project = { id: "project-1", name: "Example project", path: "C:/fixture", canonical_path: "C:/fixture", created_at: now, last_opened_at: now };
const conversations = ["conversation-1", "conversation-2"].map((id, index) => ({
  id, title: `Conversation ${index + 1}`, mode: "react", created_at: now, updated_at: now,
  message_count: 0, trace_enabled: false, transcript: [] as any[], workspace: project.path,
}));
const changes = { snapshot_id: "snapshot-1", task_id: "task-1", backend: "side-git", protected: true,
  status: "completed", has_changes: true, changed_files: [{ path: "example.py", status: "modified", additions: 1, deletions: 1 }],
  additions: 1, deletions: 1, diff_available: true, rollback_available: true, rolled_back: false };
const parameters = new URLSearchParams(location.search);
const historyCount = Math.min(5000, Number(parameters.get("history")) || 0);
if (historyCount) {
  conversations[0].transcript = Array.from({ length: historyCount }, (_, index) => ({
    id: `history-${index}`,
    role: index % 2 ? "assistant" : "user", content: `History message ${index}\n\n${"Example text for a long conversation. ".repeat(4)}`,
    timestamp: new Date(Date.parse(now) + index).toISOString(),
  }));
}
const settings = structuredClone(DEFAULT_APP_SETTINGS);
settings.general.language = parameters.get("lang") === "zh" ? "zh-CN" : "en";
const snapshot = { settings, settings_path: "C:/fixture/settings.json", env_path: "C:/fixture/.env", app_data_path: "C:/fixture", default_worktree_path: "C:/fixture/worktrees", image_cache_path: "C:/fixture/cache", api_keys: { llm: true } };
let sequence = 0;
let task = 0;
let activeSession = "conversation-1";
let pid = 123;
let taskStarted = Promise.resolve();
const journal: any[] = [];
const fixture = {
  calls: [] as { command: string; args: any }[], confirm: true, failSave: false,
  async event(type: string, data: unknown, session = activeSession) {
    if (type === "assistant.completed") {
      conversations.find((item) => item.id === session)?.transcript.push({
        id: `answer-${sequence}`, role: "assistant", content: (data as { content: string }).content,
        task_id: `task-${task}`, timestamp: new Date().toISOString(),
      });
    }
    const envelope = { kind: "event", protocol_version: 1, runtime_pid: pid,
      event_id: `event-${++sequence}`, sequence, session_id: session, task_id: `task-${task}`,
      timestamp: new Date().toISOString(), type, data };
    journal.push(envelope);
    await emit("runtime-message", envelope);
  },
  async disconnect() { await emit("runtime-exited", pid); },
  async completeWithChanges() { await taskStarted; await this.event("task.completed", { status: "completed", elapsed_ms: 200, changes }); },
};
(window as any).fixture = fixture;
mockWindows("main");
mockIPC(async (command, args: any) => {
  fixture.calls.push({ command, args });
  if (command === "settings_get") return structuredClone(snapshot);
  if (command === "settings_update") {
    if (fixture.failSave) throw new Error("Settings file is read-only");
    snapshot.settings = structuredClone(args.settings);
    return structuredClone(snapshot);
  }
  if (command === "settings_reset") return structuredClone(snapshot);
  if (command === "plugin:dialog|message") return fixture.confirm ? "Ok" : "Cancel";
  if (command === "project_list") return [project];
  if (command === "project_touch" || command === "runtime_stop" || command.startsWith("plugin:webview|")) return;
  if (command === "runtime_start") {
    const startedPid = ++pid;
    setTimeout(() => void fixture.event("runtime.ready", { runtime_version: "test", capabilities: [] }, "runtime"), 30);
    return { workspace: project.path, python: "fixture-python", pid: startedPid };
  }
  if (command === "runtime_send") {
    const request = args.message;
    const { method, params } = request;
    let result: any = {};
    if (method === "workspace.open") result = { project_id: project.id, workspace: project.path, provider: "fixture", model: "test-model", active_tasks: [], recoveries: [] };
    else if (method === "session.list") result = { conversations };
    else if (method === "session.open") {
      activeSession = params.session_id;
      result = conversations.find((item) => item.id === activeSession);
    } else if (method === "event.replay") result = {
      events: journal.filter((event) => event.session_id === params.session_id && event.sequence > params.after_sequence
        && (!params.event_types || params.event_types.includes(event.type))),
      has_more: false, last_sequence: sequence,
    };
    else if (method === "mcp.list") result = { servers: [], total_servers: 0, ready_servers: 0, total_tools: 0 };
    else if (method === "skill.list") result = { skills: [], total_count: 0, enabled_count: 0, warnings: [] };
    else if (method === "rag.snapshot") result = { sources: [], status: "idle", source_count: 0, chunk_count: 0, relation_count: 0, indexed_file_count: 0 };
    else if (method === "memory.list") result = { entries: [], count: 0, token_count: 0, warnings: [], scope: params.scope };
    else if (method === "browser.snapshot") result = { mode: "isolated", connected: false };
    else if (method === "diagnostics.snapshot") result = { status: "not_run", diagnostics: [], languages: [], counts: { error: 0, warning: 0, information: 0, hint: 0 }, toolchains: [] };
    else if (method === "task.submit") {
      task++;
      result = { task_id: `task-${task}`, session_id: params.session_id, accepted: true };
      taskStarted = new Promise((resolve) => setTimeout(() => void fixture.event("task.started", { prompt: params.prompt, mode: "react" }).then(resolve), 40));
    } else if (method === "task.cancel") {
      result = { task_id: params.task_id, cancelled: true };
      setTimeout(() => void fixture.event("task.cancelled", { status: "cancelled", reason: "user", elapsed_ms: 200 }), 40);
    } else if (method === "approval.resolve") {
      result = { resolved: true };
      setTimeout(() => void fixture.event("approval.resolved", { approval_id: params.approval_id, decision: params.decision }), 40);
    } else if (method === "task.diff") result = { ...changes, diff_truncated: false,
      diff: "diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n" };
    else if (method === "task.rollback") {
      result = { ...changes, rolled_back: true, rollback_available: false };
      setTimeout(() => void fixture.event("task.rollback.completed", result), 40);
    } else throw new Error(`Unhandled fixture Runtime method: ${method}`);
    setTimeout(() => void emit("runtime-message", { kind: "response", protocol_version: 1, runtime_pid: pid,
      request_id: request.request_id, ok: true, result }), 5);
    return;
  }
  throw new Error(`Unhandled fixture IPC: ${command}`);
}, { shouldMockEvents: true });

let shouldCrash = true;
function Unstable() { if (shouldCrash) throw new Error("Deliberate fixture render error"); return <p>Recovered view</p>; }
function Components() {
  const [status, setStatus] = useState<"running" | "failed">("running");
  const t = translator(snapshot.settings.general.language);
  return <div className="app-shell" style={{ display: "block", padding: 24, overflow: "auto" }}>
    <button onClick={() => setStatus("failed")}>Fail operation</button>
    <ToolActivityCard name="execute_command" arguments={{ command: "git rev-parse --is-inside-work-tree --show-toplevel" }} status={status} detail={status === "failed" ? "exit_code: 1\nstderr: 位置 行:1 字符:2\nParserError: MissingTypename" : "git status"} elapsed="1422 ms" t={t} />
    <button onClick={() => { shouldCrash = false; }}>Repair fixture</button>
    <ErrorBoundary t={t}><Unstable /></ErrorBoundary>
  </div>;
}
function ToolCards() {
  const t = translator(snapshot.settings.general.language);
  return <div className="app-shell" style={{ display: "block", overflow: "auto", padding: 16 }}>
    <div style={{ width: 300, maxWidth: "100%" }}>
      <ToolActivityCard name="execute_command" arguments={{ command: "git rev-parse --is-inside-work-tree --show-toplevel" }} status="failed" detail={"exit_code: 1\nstderr: ParserError: MissingTypename"} elapsed="1422 ms" t={t} />
      <ToolActivityCard name="read_file" arguments={{ path: "E:/project/docs/architecture-and-implementation-notes.md" }} status="completed" detail={"Project architecture overview\nMore details in the file."} elapsed="82 ms" t={t} />
      <ToolActivityCard name="web_search" arguments={{ query: "Python async documentation" }} status="completed" detail='{"results":[{},{}]}' elapsed="2030 ms" t={t} />
    </div>
  </div>;
}
createRoot(document.getElementById("root")!).render(<ErrorBoundary fullPage>
  {parameters.has("cards") ? <ToolCards /> : parameters.has("components") ? <Components /> : <App />}
</ErrorBoundary>);
