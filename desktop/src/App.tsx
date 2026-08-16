import { ClipboardEvent, FormEvent, type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { confirm as confirmDialog, open } from "@tauri-apps/plugin-dialog";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "./App.css";
import {
  DEFAULT_APP_SETTINGS,
  SettingsPage,
  type AppSettings,
  type SettingsSection,
  type SettingsSnapshot,
} from "./SettingsPage";
import { translate, translateDiagnosticRuntimeText, translateRuntimeText, type Language, type TranslationValues, type Translator } from "./i18n";
import {
  RUNTIME_PROTOCOL_VERSION,
  type AccessMode,
  type AgentMode,
  type ConversationSummary,
  type ConversationTranscriptEntry,
  type PlanTaskDescriptor,
  type RuntimeEvent,
  type RuntimeAttachment,
  type RuntimeMessage,
  type RuntimeRequest,
  type RuntimeRequestDataMap,
  type RuntimeRequestType,
  type RuntimeResponse,
  type RuntimeRecoveryTask,
  type HistorySnapshot,
  type McpInstallConfig,
  type McpServerInfo,
  type McpSnapshot,
  type RagSnapshot,
  type MemorySnapshot,
  type SkillSnapshot,
  type SkillDirectoryResult,
  type SkillInstallScope,
  type BrowserProbeSnapshot,
  type BrowserSnapshot,
  type DiagnosticsSnapshot,
  type WorkspaceProblem,
  type FileChangePreview,
  type TaskChangeSet,
  type TaskDiffResult,
  type UsageSnapshot,
} from "./protocol/runtimeEvents";

type BottomPanel = "terminal" | "problems" | "trace";
type ConnectionState = "starting" | "restarting" | "online" | "offline" | "error";
type ToolStatus = "waiting_approval" | "running" | "completed" | "failed";
type TextEntryKind = "user" | "assistant" | "thinking" | "error";
type TextTranscriptEntry = {
  [K in TextEntryKind]: {
    id: string;
    kind: K;
    text: string;
    timestamp?: string;
    attachments?: RuntimeAttachment[];
    taskId?: string;
    streaming?: boolean;
  };
}[TextEntryKind];
type ToolTranscriptEntry = {
  id: string;
  kind: "tool";
  toolCallId: string;
  name: string;
  detail: string;
  status: ToolStatus;
  elapsed?: number;
  timestamp?: string;
  changePreview?: FileChangePreview;
};
type ApprovalRecordStatus = "pending" | "approve" | "reject" | "skip" | "modify" | "interrupted";
type ApprovalTranscriptEntry = {
  id: string;
  kind: "approval";
  approvalId: string;
  toolCallId: string;
  taskId?: string;
  name: string;
  arguments: Record<string, unknown>;
  dangerLevel: "low" | "medium" | "high";
  riskDescription: string;
  status: ApprovalRecordStatus;
  effectiveArguments?: Record<string, unknown>;
  changePreview?: FileChangePreview;
  timestamp?: string;
};
type PlanStepStatus = "pending" | "running" | "completed" | "failed" | "skipped" | "cancelled";
type PlanStepView = PlanTaskDescriptor & {
  status: PlanStepStatus;
  resultPreview?: string;
  error?: string;
};
type PlanTranscriptEntry = {
  id: string;
  kind: "plan";
  taskId: string;
  goal: string;
  summary?: string;
  executionOrder: string[];
  steps: PlanStepView[];
  timestamp?: string;
};
type TaskRunPhase = "running" | "completed" | "failed" | "cancelled";
type TaskStatusTranscriptEntry = {
  id: string;
  kind: "task-status";
  taskId: string;
  phase: TaskRunPhase;
  startedAt: number;
  elapsedMs?: number;
  summary: string;
  summaryValues?: TranslationValues;
  compacting: boolean;
  timestamp?: string;
  recovered?: boolean;
  protection?: TaskChangeSet;
  changes?: TaskChangeSet;
  rollbackState?: "idle" | "running" | "completed" | "failed";
  rollbackError?: string;
};
type TranscriptEntry = TextTranscriptEntry | ToolTranscriptEntry | ApprovalTranscriptEntry | PlanTranscriptEntry | TaskStatusTranscriptEntry;
type ActivityEntry = Extract<TranscriptEntry, { kind: "thinking" | "tool" | "approval" }>;
type MessageEntry = Exclude<TranscriptEntry, ActivityEntry | PlanTranscriptEntry | TaskStatusTranscriptEntry>;
type TranscriptGroup =
  | { id: string; kind: "activity"; entries: ActivityEntry[] }
  | { id: string; kind: "plan"; entry: PlanTranscriptEntry }
  | { id: string; kind: "task-status"; entry: TaskStatusTranscriptEntry }
  | { id: string; kind: "message"; entry: MessageEntry };

type ComposerReference = {
  id: string;
  kind: "skill" | "mcp";
  token: string;
  label: string;
  description: string;
  meta: string;
  available: boolean;
};
type ComposerReferenceMatch = {
  start: number;
  end: number;
  query: string;
};

type ApprovalEvent = Extract<RuntimeEvent, { type: "approval.requested" }>;
type ReplayBarrier = {
  sessionId: string;
  afterSequence: number;
  buffered: RuntimeEvent[];
  replayed: RuntimeEvent[];
};

interface ProjectRecord {
  id: string;
  name: string;
  path: string;
  canonical_path: string;
  created_at: string;
  last_opened_at: string;
}

interface RuntimeStartResult {
  workspace: string;
  python: string;
  pid: number;
}

type UsageView = UsageSnapshot & {
  task_input_tokens: number;
  task_output_tokens: number;
  task_cached_input_tokens: number;
  task_reasoning_tokens: number;
  task_llm_calls: number;
  task_estimated_cost: number;
  task_priced_llm_calls: number;
};

type ManagementRequestType = Extract<
  RuntimeRequestType,
  | `mcp.${string}`
  | `rag.${string}`
  | `memory.${string}`
  | `skill.${string}`
  | `browser.${string}`
  | `diagnostics.${string}`
  | "task.diff"
  | "task.rollback"
>;
type PendingManagementRequest = {
  resolve: (result: unknown) => void;
  reject: (error: Error) => void;
};

const EMPTY_USAGE: UsageView = {
  input_tokens: 0,
  output_tokens: 0,
  cached_input_tokens: 0,
  reasoning_tokens: 0,
  llm_calls: 0,
  last_context_tokens: 0,
  last_input_tokens: 0,
  last_output_tokens: 0,
  last_cached_input_tokens: 0,
  last_reasoning_tokens: 0,
  last_exact: false,
  estimated_cost: 0,
  priced_llm_calls: 0,
  last_estimated_cost: null,
  last_cost_estimated: true,
  cost_currency: "",
  cost_source: "",
  provider: "",
  model: "",
  operation: "",
  context_window: 200000,
  task_input_tokens: 0,
  task_output_tokens: 0,
  task_cached_input_tokens: 0,
  task_reasoning_tokens: 0,
  task_llm_calls: 0,
  task_estimated_cost: 0,
  task_priced_llm_calls: 0,
};

const EMPTY_HISTORY: HistorySnapshot = {
  summary: "",
  compaction_count: 0,
  last_compacted_at: null,
  context_window: 200000,
};

const EMPTY_MCP_SNAPSHOT: McpSnapshot = {
  servers: [],
  ready_servers: 0,
  total_servers: 0,
  total_tools: 0,
  project_config_path: "",
  user_config_path: "",
};

const EMPTY_RAG_SNAPSHOT: RagSnapshot = {
  workspace: "",
  sources: [],
  source_count: 0,
  indexed_file_count: 0,
  chunk_count: 0,
  relation_count: 0,
  last_indexed_at: null,
  last_result: null,
  embedding_provider: "not initialized",
  embedding_model: "not initialized",
  embedding_base_url: "",
  embedding_api_key_configured: false,
  needs_rebuild: false,
  storage_path: "",
  status: "idle",
};

const EMPTY_MEMORY_SNAPSHOT: MemorySnapshot = {
  scope: "project",
  entries: [],
  count: 0,
  token_count: 0,
  storage_path: "",
  warnings: [],
};

const EMPTY_SKILL_SNAPSHOT: SkillSnapshot = {
  skills: [],
  total_count: 0,
  enabled_count: 0,
  warnings: [],
  state_path: "",
  user_dir: "",
  project_dir: "",
};

const EMPTY_BROWSER_SNAPSHOT: BrowserSnapshot = {
  mode: "isolated",
  browser_url: "",
  last_navigated_url: "",
  agent_opened_pages: [],
  chrome_server: { status: "not_configured", error: "", tool_count: 0 },
};

const EMPTY_DIAGNOSTICS_SNAPSHOT: DiagnosticsSnapshot = {
  workspace: "",
  status: "not_run",
  run_id: null,
  problems: [],
  error_count: 0,
  warning_count: 0,
  information_count: 0,
  providers: [],
  detected_projects: [],
  stale: false,
};

const RESTORABLE_EXECUTION_EVENT_TYPES: RuntimeEvent["type"][] = [
  "session.reset",
  "task.started",
  "assistant.thinking",
  "history.compaction.started",
  "history.compaction.finished",
  "plan.created",
  "plan.step.started",
  "plan.step.completed",
  "plan.step.failed",
  "plan.step.skipped",
  "tool.started",
  "tool.completed",
  "tool.failed",
  "approval.requested",
  "approval.resolved",
  "task.completed",
  "task.failed",
  "task.cancelled",
  "task.finalization.pending",
  "workspace.snapshot.created",
  "task.rollback.started",
  "task.rollback.completed",
  "task.rollback.failed",
];

function requestId(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`;
}

async function sendRequest<T extends RuntimeRequestType>(
  method: T,
  params: RuntimeRequestDataMap[T],
  id = requestId(method.replace(/\./g, "-")),
) {
  const message = {
    kind: "request",
    protocol_version: RUNTIME_PROTOCOL_VERSION,
    request_id: id,
    method,
    params,
  } as RuntimeRequest;
  await invoke("runtime_send", { message });
  return id;
}

function App() {
  const [mode, setMode] = useState<AgentMode>("react");
  const [bottomPanel, setBottomPanel] = useState<BottomPanel>("terminal");
  const [connection, setConnection] = useState<ConnectionState>("starting");
  const [sessionId, setSessionId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [model, setModel] = useState("not initialized");
  const [prompt, setPrompt] = useState("");
  const [referenceMatch, setReferenceMatch] = useState<ComposerReferenceMatch | null>(null);
  const [referenceSelection, setReferenceSelection] = useState(0);
  const [busy, setBusy] = useState(false);
  const [runningTasks, setRunningTasks] = useState<Record<string, string>>({});
  const [replayingConversation, setReplayingConversation] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [entries, setEntries] = useState<TranscriptEntry[]>([]);
  const [approvalQueue, setApprovalQueue] = useState<ApprovalEvent[]>([]);
  const [resolvingApprovalId, setResolvingApprovalId] = useState("");
  const [logs, setLogs] = useState<string[]>([]);
  const [eventCount, setEventCount] = useState(0);
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [activeProjectId, setActiveProjectId] = useState("");
  const [expandedProjectIds, setExpandedProjectIds] = useState<Set<string>>(() => new Set());
  const [projectActionBusy, setProjectActionBusy] = useState(false);
  const [accessMode, setAccessMode] = useState<AccessMode>("restricted");
  const [traceEnabled, setTraceEnabled] = useState(false);
  const [tracePath, setTracePath] = useState("");
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationCache, setConversationCache] = useState<Record<string, ConversationSummary[]>>({});
  const [activeConversationId, setActiveConversationId] = useState("");
  const [attachments, setAttachments] = useState<RuntimeAttachment[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [appSettings, setAppSettings] = useState<AppSettings>(DEFAULT_APP_SETTINGS);
  const [settingsSnapshot, setSettingsSnapshot] = useState<SettingsSnapshot | null>(null);
  const [settingsTarget, setSettingsTarget] = useState<SettingsSection | null>(null);
  const [runtimePython, setRuntimePython] = useState("");
  const [runtimeSettingsDirty, setRuntimeSettingsDirty] = useState(false);
  const [usage, setUsage] = useState<UsageView>(EMPTY_USAGE);
  const [historyState, setHistoryState] = useState<HistorySnapshot>(EMPTY_HISTORY);
  const [mcpSnapshot, setMcpSnapshot] = useState<McpSnapshot>(EMPTY_MCP_SNAPSHOT);
  const [ragSnapshot, setRagSnapshot] = useState<RagSnapshot>(EMPTY_RAG_SNAPSHOT);
  const [memorySnapshot, setMemorySnapshot] = useState<MemorySnapshot>(EMPTY_MEMORY_SNAPSHOT);
  const [skillSnapshot, setSkillSnapshot] = useState<SkillSnapshot>(EMPTY_SKILL_SNAPSHOT);
  const [browserSnapshot, setBrowserSnapshot] = useState<BrowserSnapshot>(EMPTY_BROWSER_SNAPSHOT);
  const [diagnosticsSnapshot, setDiagnosticsSnapshot] = useState<DiagnosticsSnapshot>(EMPTY_DIAGNOSTICS_SNAPSHOT);
  const [timerNow, setTimerNow] = useState(() => Date.now());
  const [rollbackBusyTaskId, setRollbackBusyTaskId] = useState("");
  const [taskDiff, setTaskDiff] = useState<TaskDiffResult | null>(null);
  const [taskDiffLoading, setTaskDiffLoading] = useState("");
  const appSettingsRef = useRef<AppSettings>(DEFAULT_APP_SETTINGS);
  const languageRef = useRef<Language>(DEFAULT_APP_SETTINGS.general.language);
  const composerTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const composerHighlightRef = useRef<HTMLDivElement | null>(null);

  function tx(message: string, values?: TranslationValues) {
    return translate(languageRef.current, message, values);
  }
  const t: Translator = tx;

  const composerReferences = useMemo<ComposerReference[]>(() => {
    const skills = skillSnapshot.skills
      .filter((skill) => skill.enabled)
      .map((skill) => ({
        id: `skill:${skill.name}`,
        kind: "skill" as const,
        token: `@skill:${skill.name}`,
        label: skill.name,
        description: skill.description,
        meta: skill.source,
        available: true,
      }));
    const tools = mcpSnapshot.servers
      .filter((server) => !server.disabled && server.status === "ready")
      .flatMap((server) => server.tools.map((tool) => ({
        id: `mcp:${tool.namespaced_name}`,
        kind: "mcp" as const,
        token: `@mcp:${tool.namespaced_name}`,
        label: tool.name,
        description: tool.description,
        meta: server.name,
        available: true,
      })));
    return [...skills, ...tools];
  }, [mcpSnapshot.servers, skillSnapshot.skills]);

  const filteredComposerReferences = useMemo(() => {
    if (!referenceMatch) return [];
    const query = referenceMatch.query.trim().toLocaleLowerCase();
    return composerReferences
      .filter((item) => {
        if (!query) return true;
        const haystack = `${item.token.slice(1)} ${item.label} ${item.description} ${item.meta}`.toLocaleLowerCase();
        return haystack.includes(query);
      })
      .slice(0, 12);
  }, [composerReferences, referenceMatch]);

  useEffect(() => {
    setReferenceSelection((current) => Math.min(current, Math.max(0, filteredComposerReferences.length - 1)));
  }, [filteredComposerReferences.length]);

  const workspaceOpenRequest = useRef("");
  const sessionListRequest = useRef("");
  const sessionListAutoOpen = useRef(true);
  const sessionCreateRequest = useRef("");
  const sessionOpenRequest = useRef("");
  const sessionRenameRequest = useRef("");
  const sessionDeleteRequest = useRef("");
  const pendingConversationOpen = useRef<{ projectId: string; conversationId: string } | null>(null);
  const accessModeRequest = useRef("");
  const traceModeRequest = useRef("");
  const taskSubmitRequest = useRef("");
  const taskSubmitSession = useRef("");
  const taskCancelRequest = useRef("");
  const taskRecoveryRequest = useRef("");
  const eventReplayRequest = useRef("");
  const approvalResolveRequests = useRef(new Map<string, string>());
  const approvalToolCalls = useRef(new Map<string, string>());
  const approvalQueueRef = useRef<ApprovalEvent[]>([]);
  const pendingManagementRequests = useRef(new Map<string, PendingManagementRequest>());
  const attachmentsRef = useRef<RuntimeAttachment[]>([]);
  const canAttachRef = useRef(false);
  const activeTaskId = useRef("");
  const runningTasksBySession = useRef(new Map<string, string>());
  const taskSessions = useRef(new Map<string, string>());
  const sessionProjectIds = useRef(new Map<string, string>());
  const activeConversationIdRef = useRef("");
  const workspaceRef = useRef("");
  const projectIdRef = useRef("");
  const modeRef = useRef<AgentMode>("react");
  const defaultModeRef = useRef<AgentMode>("react");
  const activeRuntimePid = useRef<number | null>(null);
  const retiredRuntimePids = useRef(new Set<number>());
  const runtimeStarted = useRef(false);
  const restartTimer = useRef<number | null>(null);
  const restartAttempts = useRef(0);
  const pendingRecovery = useRef<RuntimeRecoveryTask | null>(null);
  const recoveryQueue = useRef<RuntimeRecoveryTask[]>([]);
  const lastEventSequence = useRef(new Map<string, number>());
  const seenEventIds = useRef(new Set<string>());
  const replayBarrier = useRef<ReplayBarrier | null>(null);

  const activeProject = useMemo(
    () => projects.find((project) => project.id === activeProjectId) ?? null,
    [activeProjectId, projects],
  );
  const activeConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === activeConversationId) ?? null,
    [activeConversationId, conversations],
  );
  const transcriptGroups = useMemo(() => groupTranscriptEntries(entries), [entries]);
  const activeTaskFinalizing = entries.some((entry) => (
    entry.kind === "task-status"
    && entry.taskId === activeTaskId.current
    && entry.protection?.status === "finalize_pending"
  ));
  const approval = approvalQueue[0] ?? null;
  const ragIndexing = ragSnapshot.status === "indexing";
  const diagnosticsRunning = diagnosticsSnapshot.status === "running";
  const anyTaskRunning = Object.keys(runningTasks).length > 0;
  const activeProjectTaskRunning = Object.keys(runningTasks).some(
    (conversationId) => sessionProjectIds.current.get(conversationId) === activeProjectId,
  );
  const runtimeMutationBusy = busy || ragIndexing || diagnosticsRunning || replayingConversation || Boolean(rollbackBusyTaskId);
  canAttachRef.current = Boolean(sessionId && !runtimeMutationBusy);
  approvalQueueRef.current = approvalQueue;

  function publishRunningTasks() {
    setRunningTasks(Object.fromEntries(runningTasksBySession.current));
  }

  function registerRunningTask(taskId: string, taskSessionId: string) {
    if (!taskId || !taskSessionId) return;
    if (!sessionProjectIds.current.has(taskSessionId) && projectIdRef.current) {
      sessionProjectIds.current.set(taskSessionId, projectIdRef.current);
    }
    runningTasksBySession.current.set(taskSessionId, taskId);
    taskSessions.current.set(taskId, taskSessionId);
    publishRunningTasks();
    if (taskSessionId === activeConversationIdRef.current) {
      activeTaskId.current = taskId;
      setBusy(true);
    }
  }

  function releaseRunningTask(taskId: string | undefined, taskSessionId: string) {
    const resolvedSessionId = taskSessionId || (taskId ? taskSessions.current.get(taskId) ?? "" : "");
    if (resolvedSessionId && (!taskId || runningTasksBySession.current.get(resolvedSessionId) === taskId)) {
      runningTasksBySession.current.delete(resolvedSessionId);
    }
    if (taskId) taskSessions.current.delete(taskId);
    if (taskId) {
      recoveryQueue.current = recoveryQueue.current.filter(
        (item) => item.task_id !== taskId,
      );
      if (pendingRecovery.current?.task_id === taskId) pendingRecovery.current = null;
    }
    if (taskId) clearApprovalsForTask(taskId);
    publishRunningTasks();
    if (resolvedSessionId === activeConversationIdRef.current) {
      activeTaskId.current = "";
      setBusy(false);
      setCancelling(false);
    }
  }

  function clearApprovalsForTask(taskId: string) {
    const current = approvalQueueRef.current;
    const removedIds = new Set(current
      .filter((item) => item.task_id === taskId)
      .map((item) => item.data.approval_id));
    if (removedIds.size === 0) return;
    for (const approvalId of removedIds) approvalToolCalls.current.delete(approvalId);
    for (const [requestId, approvalId] of approvalResolveRequests.current) {
      if (removedIds.has(approvalId)) approvalResolveRequests.current.delete(requestId);
    }
    setResolvingApprovalId((currentId) => removedIds.has(currentId) ? "" : currentId);
    const next = current.filter((item) => !removedIds.has(item.data.approval_id));
    approvalQueueRef.current = next;
    setApprovalQueue(next);
  }

  function syncActiveConversationTask(conversationId: string) {
    const taskId = runningTasksBySession.current.get(conversationId) ?? "";
    activeTaskId.current = taskId;
    setBusy(Boolean(taskId));
    setCancelling(false);
  }

  useEffect(() => {
    if (!busy) return;
    setTimerNow(Date.now());
    const interval = window.setInterval(() => setTimerNow(Date.now()), 500);
    return () => window.clearInterval(interval);
  }, [busy]);

  useEffect(() => {
    let disposed = false;
    const unlisteners: Array<() => void> = [];

    function scheduleRuntimeRestart() {
      if (disposed || restartTimer.current !== null || !workspaceRef.current) return;
      const attempt = restartAttempts.current + 1;
      restartAttempts.current = attempt;
      const delay = Math.min(15_000, 500 * (2 ** Math.min(attempt - 1, 5)));
      restartTimer.current = window.setTimeout(async () => {
        restartTimer.current = null;
        if (disposed) return;
        try {
          const started = await invoke<RuntimeStartResult>("runtime_start", {
            workspace: workspaceRef.current,
          });
          if (disposed) {
            await invoke("runtime_stop");
            return;
          }
          activeRuntimePid.current = started.pid;
          runtimeStarted.current = true;
          setRuntimePython(started.python);
          setLogs((current) => [
            ...current.slice(-199),
            tx("Runtime restarted after {count} attempt(s).", { count: attempt }),
          ]);
        } catch (error) {
          setLogs((current) => [
            ...current.slice(-199),
            `${tx("Runtime restart attempt {count} failed.", { count: attempt })} ${String(error)}`,
          ]);
          if (attempt >= 6) {
            setConnection("error");
            activeTaskId.current = "";
            setBusy(false);
            setEntries((current) => finishTaskStatus(
              current,
              undefined,
              "failed",
              "Runtime automatic restart failed. Restart it manually from Settings.",
            ));
            return;
          }
          scheduleRuntimeRestart();
        }
      }, delay);
    }

    async function start() {
      const unlistenMessage = await listen<RuntimeMessage>("runtime-message", ({ payload }) => {
        if (!disposed) handleRuntimeMessage(payload);
      });
      const unlistenLog = await listen<string>("runtime-log", ({ payload }) => {
        if (!disposed) setLogs((current) => [...current.slice(-199), payload]);
      });
      const unlistenError = await listen<string>("runtime-transport-error", ({ payload }) => {
        if (!disposed) {
          setLogs((current) => [...current.slice(-199), `Runtime transport: ${payload}`]);
        }
      });
      const unlistenExit = await listen<number>("runtime-exited", ({ payload }) => {
        if (!disposed && payload === activeRuntimePid.current) {
          retireRuntimePid(payload);
          activeRuntimePid.current = null;
          runtimeStarted.current = false;
          taskSubmitRequest.current = "";
          taskCancelRequest.current = "";
          taskRecoveryRequest.current = "";
          recoveryQueue.current = [];
          eventReplayRequest.current = "";
          setReplayingConversation(false);
          workspaceOpenRequest.current = "";
          sessionListRequest.current = "";
          sessionCreateRequest.current = "";
          sessionOpenRequest.current = "";
          accessModeRequest.current = "";
          traceModeRequest.current = "";
          runningTasksBySession.current.clear();
          taskSessions.current.clear();
          publishRunningTasks();
          lastEventSequence.current.delete("runtime");
          const interruptedSession = activeConversationIdRef.current;
          if (interruptedSession) {
            lastEventSequence.current.delete(interruptedSession);
            seenEventIds.current.clear();
            replayBarrier.current = {
              sessionId: interruptedSession,
              afterSequence: 0,
              buffered: [],
              replayed: [],
            };
            pendingConversationOpen.current = {
              projectId: projectIdRef.current,
              conversationId: interruptedSession,
            };
          }
          if (activeTaskId.current) {
            setEntries((current) => updateTaskStatus(current, activeTaskId.current, {
              summary: "Runtime was interrupted. Restarting and recovering the task.",
              summaryValues: undefined,
              compacting: false,
            }));
          } else {
            setBusy(false);
          }
          setCancelling(false);
          setRollbackBusyTaskId("");
          setTaskDiff(null);
          setTaskDiffLoading("");
          setApprovalQueue([]);
          setResolvingApprovalId("");
          approvalResolveRequests.current.clear();
          approvalToolCalls.current.clear();
          rejectPendingManagementRequests(tx("Python Runtime exited before the management request completed."));
          setMcpSnapshot((current) => markMcpServersStarting(current));
          setRagSnapshot((current) => current.status === "indexing"
            ? {
                ...current,
                status: "error",
                progress: undefined,
                error: tx("Python Runtime exited during indexing. Rebuild the index after recovery."),
              }
            : current);
          setDiagnosticsSnapshot((current) => current.status === "running"
            ? {
                ...current,
                status: "failed",
                progress: undefined,
                stale: true,
                error: tx("Python Runtime exited during diagnostics. Run the checks again after recovery."),
              }
            : { ...current, stale: true });
          setConnection("restarting");
          scheduleRuntimeRestart();
        }
      });
      const unlistenDragDrop = await getCurrentWebview().onDragDropEvent((event) => {
        if (disposed) return;
        if (event.payload.type === "drop") {
          setDragActive(false);
          if (canAttachRef.current) void addAttachmentPaths(event.payload.paths);
        } else if (event.payload.type === "enter" || event.payload.type === "over") {
          setDragActive(canAttachRef.current);
        } else {
          setDragActive(false);
        }
      });
      unlisteners.push(unlistenMessage, unlistenLog, unlistenError, unlistenExit, unlistenDragDrop);

      try {
        const loadedSettings = await invoke<SettingsSnapshot>("settings_get");
        if (disposed) return;
        applySettings(loadedSettings);
        const storedProjects = await invoke<ProjectRecord[]>("project_list");
        if (disposed) return;
        setProjects(storedProjects);
        if (storedProjects.length > 0 && loadedSettings.settings.general.reopen_last_project) {
          const mostRecentlyOpened = storedProjects.reduce((latest, project) => (
            project.last_opened_at > latest.last_opened_at ? project : latest
          ));
          await activateProject(mostRecentlyOpened, false);
        } else {
          setConnection("offline");
        }
      } catch (error) {
        if (!disposed) {
          setConnection("error");
          reportError(error);
        }
      }
    }

    function requestPendingTaskRecovery() {
      const recovery = recoveryQueue.current.find(
        (item) => item.status !== "finalize_pending",
      );
      if (
        !recovery
        || taskRecoveryRequest.current
        || replayBarrier.current?.sessionId === recovery.session_id
        || eventReplayRequest.current && recovery.session_id === activeConversationIdRef.current
      ) return;
      pendingRecovery.current = recovery;
      const id = requestId("task-recover");
      taskRecoveryRequest.current = id;
      void sendRequest(
        "task.recover",
        { session_id: recovery.session_id, task_id: recovery.task_id },
        id,
      );
    }

    function requestEventReplay(barrier: ReplayBarrier, afterSequence: number) {
      const id = requestId("event-replay");
      eventReplayRequest.current = id;
      void sendRequest(
        "event.replay",
        {
          session_id: barrier.sessionId,
          after_sequence: afterSequence,
          limit: 2000,
          event_types: RESTORABLE_EXECUTION_EVENT_TYPES,
        },
        id,
      ).catch((error) => {
        if (eventReplayRequest.current !== id) return;
        eventReplayRequest.current = "";
        if (replayBarrier.current === barrier) replayBarrier.current = null;
        setReplayingConversation(false);
        reportError(error);
        requestPendingTaskRecovery();
      });
    }

    function handleRuntimeMessage(message: RuntimeMessage, replaying = false) {
      const messagePid = message.runtime_pid;
      if (
        messagePid !== undefined
        && (
          retiredRuntimePids.current.has(messagePid)
          || activeRuntimePid.current !== null
          && messagePid !== activeRuntimePid.current
        )
      ) return;
      if (message.kind === "response") {
        handleResponse(message);
        return;
      }
      if (message.kind !== "event") return;
      const barrier = replayBarrier.current;
      if (!replaying && barrier && message.session_id === barrier.sessionId) {
        barrier.buffered.push(message);
        return;
      }
      if (!replaying && seenEventIds.current.has(message.event_id)) return;
      const previousSequence = lastEventSequence.current.get(message.session_id) ?? 0;
      if (!replaying && message.sequence <= previousSequence) return;
      seenEventIds.current.add(message.event_id);
      if (seenEventIds.current.size > 10_000) {
        seenEventIds.current = new Set(Array.from(seenEventIds.current).slice(-5_000));
      }
      lastEventSequence.current.set(message.session_id, message.sequence);
      if (!replaying) setEventCount((count) => count + 1);
      const isTaskTerminal = message.type === "task.completed"
        || message.type === "task.failed"
        || message.type === "task.cancelled";
      if (!replaying && message.type === "task.started") {
        registerRunningTask(message.task_id ?? message.event_id, message.session_id);
      } else if (!replaying && isTaskTerminal) {
        releaseRunningTask(message.task_id, message.session_id);
      }
      const workspaceEventProject = message.session_id.startsWith("workspace-")
        ? message.session_id.slice("workspace-".length)
        : "";
      if (workspaceEventProject && workspaceEventProject !== projectIdRef.current) return;
      const isConversationEvent = message.session_id !== "runtime" && !workspaceEventProject;
      const isActiveConversationEvent = message.session_id === activeConversationIdRef.current;
      if (isConversationEvent && !isActiveConversationEvent && !replaying) {
        // Background conversations keep running, but their transcript is
        // reconstructed from the durable snapshot+journal only when opened.
        // Approval requests are the exception: they must remain actionable
        // regardless of which project/conversation is currently visible.
        if (message.type === "approval.requested") {
          approvalToolCalls.current.set(message.data.approval_id, message.data.tool_call_id);
          setApprovalQueue((current) => current.some(
            (item) => item.data.approval_id === message.data.approval_id,
          ) ? current : [...current, message]);
        } else if (message.type === "approval.resolved") {
          approvalToolCalls.current.delete(message.data.approval_id);
          setApprovalQueue((current) => current.filter(
            (item) => item.data.approval_id !== message.data.approval_id,
          ));
          setResolvingApprovalId((current) => (
            current === message.data.approval_id ? "" : current
          ));
        }
        return;
      }
      if (message.type === "runtime.ready") {
        restartAttempts.current = 0;
        setConnection("online");
        if (!workspaceOpenRequest.current) {
          const id = requestId("workspace-open");
          workspaceOpenRequest.current = id;
          void sendRequest(
            "workspace.open",
            { project_id: projectIdRef.current, workspace: workspaceRef.current },
            id,
          );
        }
      } else if (message.type === "task.started") {
        const taskId = message.task_id ?? message.event_id;
        const restoredStartedAt = message.data.started_at
          ? Date.parse(message.data.started_at)
          : Date.parse(message.timestamp);
        if (!replaying) {
          activeTaskId.current = taskId;
          setBusy(true);
          setTimerNow(Date.now());
        }
        if (message.data.recovered) {
          setEntries((current) => interruptOpenToolEntries(current, tx));
        }
        setEntries((current) => upsertTaskStatus(current, {
          id: `task-status-${taskId}`,
          kind: "task-status",
          taskId,
          phase: "running",
          startedAt: Number.isFinite(restoredStartedAt) ? restoredStartedAt : Date.now(),
          summary: message.data.recovered
            ? "Runtime recovered. Continuing from the last safe checkpoint."
            : "Analyzing the task and preparing the next action.",
          summaryValues: undefined,
          compacting: false,
          timestamp: message.timestamp,
          recovered: message.data.recovered === true,
          protection: message.data.protection,
          rollbackState: "idle",
        }));
      } else if (message.type === "access.mode_changed") {
        setAccessMode(message.data.mode);
      } else if (message.type === "trace.status_changed") {
        setTraceEnabled(message.data.enabled);
        setTracePath(message.data.path ?? "");
      } else if (message.type === "mcp.status_changed") {
        setMcpSnapshot((current) => upsertMcpServer(current, message.data));
      } else if (message.type === "rag.index.started") {
        setRagSnapshot((current) => ({
          ...current,
          status: "indexing",
          job_id: message.data.job_id,
          progress: tx("Preparing {count} source(s)...", { count: message.data.source_count }),
          error: undefined,
        }));
      } else if (message.type === "rag.index.progress") {
        setRagSnapshot((current) => current.job_id && current.job_id !== message.data.job_id
          ? current
          : { ...current, status: "indexing", job_id: message.data.job_id, progress: message.data.message });
      } else if (message.type === "rag.index.completed") {
        setRagSnapshot({ ...message.data.snapshot, status: "idle", progress: undefined, error: undefined });
      } else if (message.type === "rag.index.failed") {
        setRagSnapshot((current) => ({
          ...current,
          status: "error",
          job_id: message.data.job_id,
          progress: undefined,
          error: message.data.message,
        }));
      } else if (message.type === "diagnostics.started") {
        setDiagnosticsSnapshot((current) => ({
          ...current,
          status: "running",
          run_id: message.data.run_id,
          profile: message.data.profile,
          progress: tx("Starting workspace diagnostics..."),
          error: undefined,
        }));
      } else if (message.type === "diagnostics.progress") {
        setDiagnosticsSnapshot((current) => current.run_id && current.run_id !== message.data.run_id
          ? current
          : {
              ...current,
              status: "running",
              run_id: message.data.run_id,
              progress: translateDiagnosticRuntimeText(languageRef.current, message.data.message),
            });
      } else if (message.type === "diagnostics.completed") {
        void refreshDiagnosticsSnapshot().catch((error) => {
          setDiagnosticsSnapshot((current) => ({
            ...current,
            status: "failed",
            progress: undefined,
            error: String(error),
          }));
        });
      } else if (message.type === "diagnostics.failed") {
        setDiagnosticsSnapshot((current) => ({
          ...current,
          status: "failed",
          run_id: message.data.run_id,
          progress: undefined,
          error: message.data.message,
        }));
      } else if (message.type === "diagnostics.cancelled") {
        void refreshDiagnosticsSnapshot().catch((error) => {
          setDiagnosticsSnapshot((current) => ({
            ...current,
            status: "cancelled",
            progress: undefined,
            error: String(error),
          }));
        });
      } else if (message.type === "usage.updated") {
        setUsage({
          input_tokens: message.data.conversation_input_tokens,
          output_tokens: message.data.conversation_output_tokens,
          cached_input_tokens: message.data.conversation_cached_input_tokens,
          reasoning_tokens: message.data.conversation_reasoning_tokens,
          llm_calls: message.data.conversation_llm_calls,
          last_context_tokens: message.data.context_tokens,
          last_input_tokens: message.data.input_tokens,
          last_output_tokens: message.data.output_tokens,
          last_cached_input_tokens: message.data.cached_input_tokens,
          last_reasoning_tokens: message.data.reasoning_tokens,
          last_exact: message.data.exact,
          estimated_cost: message.data.conversation_estimated_cost,
          priced_llm_calls: message.data.conversation_priced_llm_calls,
          last_estimated_cost: message.data.estimated_cost,
          last_cost_estimated: message.data.cost_estimated,
          cost_currency: message.data.currency,
          cost_source: message.data.cost_source,
          provider: message.data.provider,
          model: message.data.model,
          operation: message.data.operation,
          context_window: message.data.context_window,
          task_input_tokens: message.data.task_input_tokens,
          task_output_tokens: message.data.task_output_tokens,
          task_cached_input_tokens: message.data.task_cached_input_tokens,
          task_reasoning_tokens: message.data.task_reasoning_tokens,
          task_llm_calls: message.data.task_llm_calls,
          task_estimated_cost: message.data.task_estimated_cost,
          task_priced_llm_calls: message.data.task_priced_llm_calls,
        });
      } else if (message.type === "history.compaction.started") {
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          compacting: true,
          summary: "Organizing older conversation history while retaining recent messages.",
          summaryValues: undefined,
        }));
      } else if (message.type === "history.compaction.finished") {
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          compacting: false,
          summary: message.data.compacted
            ? "Context compression completed. Continuing the task."
            : "Continuing the task.",
          summaryValues: undefined,
        }));
      } else if (message.type === "history.compacted") {
        setHistoryState((current) => ({
          ...current,
          compaction_count: message.data.compaction_count,
        }));
      } else if (message.type === "assistant.thinking") {
        if (message.data.status === "active" && message.data.summary) {
          setEntries((current) => updateTaskStatus(current, message.task_id, {
            summary: briefThinkingSummary(message.data.summary!, tx),
            summaryValues: undefined,
          }));
        }
      } else if (message.type === "plan.created") {
        const taskId = message.task_id ?? message.event_id;
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          summary: "Execution plan created with {count} steps.",
          summaryValues: { count: message.data.tasks.length },
        }));
        setEntries((current) => upsertPlanEntry(current, {
          id: message.event_id,
          kind: "plan",
          taskId,
          goal: message.data.goal,
          summary: message.data.summary,
          executionOrder: message.data.execution_order,
          steps: message.data.tasks.map((step) => ({ ...step, status: "pending" })),
          timestamp: message.timestamp,
        }));
      } else if (message.type === "plan.step.started") {
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          summary: "Running plan step {step}",
          summaryValues: { step: message.data.step_id },
        }));
        setEntries((current) => updatePlanStep(
          current,
          message.task_id,
          message.data.step_id,
          { status: "running", resultPreview: undefined, error: undefined },
        ));
      } else if (message.type === "plan.step.completed") {
        setEntries((current) => updatePlanStep(
          current,
          message.task_id,
          message.data.step_id,
          { status: "completed", resultPreview: message.data.result_preview, error: undefined },
        ));
      } else if (message.type === "plan.step.failed") {
        setEntries((current) => updatePlanStep(
          current,
          message.task_id,
          message.data.step_id,
          { status: "failed", error: message.data.error },
        ));
      } else if (message.type === "plan.step.skipped") {
        setEntries((current) => updatePlanStep(
          current,
          message.task_id,
          message.data.step_id,
          { status: "skipped", error: message.data.reason },
        ));
      } else if (message.type === "tool.started") {
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          summary: "Calling {tool}",
          summaryValues: { tool: message.data.name },
        }));
        setEntries((current) => upsertToolEntry(current, message.data.tool_call_id, {
          id: message.event_id,
          kind: "tool",
          toolCallId: message.data.tool_call_id,
          name: message.data.name,
          detail: JSON.stringify(message.data.arguments),
          status: "running",
          timestamp: message.timestamp,
          changePreview: message.data.change_preview,
        }));
      } else if (message.type === "tool.completed" || message.type === "tool.failed") {
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          summary: message.type === "tool.completed"
            ? "{tool} completed. Analyzing the result."
            : "{tool} failed. Handling the error.",
          summaryValues: { tool: message.data.name },
        }));
        setEntries((current) => upsertToolEntry(current, message.data.tool_call_id, {
          id: message.event_id,
          kind: "tool",
          toolCallId: message.data.tool_call_id,
          name: message.data.name,
          detail: message.type === "tool.completed" ? message.data.result_preview : message.data.error,
          elapsed: message.data.elapsed_ms,
          status: message.type === "tool.completed" ? "completed" : "failed",
        }));
      } else if (message.type === "approval.requested") {
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          summary: "Waiting for approval: {tool}",
          summaryValues: { tool: message.data.name },
        }));
        if (!replaying) {
          approvalToolCalls.current.set(
            message.data.approval_id,
            message.data.tool_call_id,
          );
        }
        setEntries((current) => upsertApprovalEntry(current, {
          id: message.event_id,
          kind: "approval",
          approvalId: message.data.approval_id,
          toolCallId: message.data.tool_call_id,
          taskId: message.task_id,
          name: message.data.name,
          arguments: message.data.arguments,
          dangerLevel: message.data.danger_level,
          riskDescription: message.data.risk_description,
          changePreview: message.data.change_preview,
          status: "pending",
          timestamp: message.timestamp,
        }));
        if (!replaying) {
          setApprovalQueue((current) => current.some(
            (item) => item.data.approval_id === message.data.approval_id,
          ) ? current : [...current, message]);
        }
        setEntries((current) => updateToolStatus(
          current,
          message.data.tool_call_id,
          "waiting_approval",
        ));
      } else if (message.type === "approval.resolved") {
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          summary: message.data.decision === "approve" || message.data.decision === "modify"
            ? "Operation approved. Continuing execution."
            : "Operation rejected. Adjusting the approach.",
          summaryValues: undefined,
        }));
        const mappedToolCallId = approvalToolCalls.current.get(message.data.approval_id);
        setEntries((current) => {
          const approvalEntry = current.find(
            (entry): entry is ApprovalTranscriptEntry => entry.kind === "approval"
              && entry.approvalId === message.data.approval_id,
          );
          const updated = updateApprovalEntry(current, message.data.approval_id, {
            status: message.data.decision,
            effectiveArguments: message.data.effective_arguments,
          });
          const toolCallId = approvalEntry?.toolCallId ?? mappedToolCallId;
          return toolCallId ? updateToolStatus(
            updated,
            toolCallId,
            message.data.decision === "approve" || message.data.decision === "modify"
              ? "running"
              : "failed",
            "waiting_approval",
          ) : updated;
        });
        if (!replaying) {
          approvalToolCalls.current.delete(message.data.approval_id);
          for (const [requestId, approvalId] of approvalResolveRequests.current) {
            if (approvalId === message.data.approval_id) {
              approvalResolveRequests.current.delete(requestId);
            }
          }
          setApprovalQueue((current) => current.filter(
            (item) => item.data.approval_id !== message.data.approval_id,
          ));
          setResolvingApprovalId((current) => (
            current === message.data.approval_id ? "" : current
          ));
        }
      } else if (message.type === "assistant.delta") {
        if (replaying) return;
        setEntries((current) => applyAssistantDelta(
          current,
          message.task_id,
          message.event_id,
          message.data.text,
          message.data.reset === true,
          message.timestamp,
        ));
      } else if (message.type === "assistant.completed") {
        if (replaying) return;
        setEntries((current) => completeAssistantStream(
          current,
          message.task_id,
          message.event_id,
          message.data.content,
          message.timestamp,
        ));
      } else if (message.type === "task.completed") {
        if (!replaying) {
          if (pendingRecovery.current?.task_id === message.task_id) pendingRecovery.current = null;
          activeTaskId.current = "";
          setBusy(false);
        }
        setEntries((current) => updateTaskStatus(finishTaskStatus(
          current,
          message.task_id,
          "completed",
          undefined,
          message.data.elapsed_ms,
          Date.parse(message.timestamp),
        ), message.task_id, { changes: message.data.changes, rollbackState: "idle" }));
        if (!replaying) {
          setCancelling(false);
          if (message.task_id) clearApprovalsForTask(message.task_id);
          void requestConversationList();
        }
      } else if (message.type === "task.failed") {
        if (!replaying) {
          if (pendingRecovery.current?.task_id === message.task_id) pendingRecovery.current = null;
          activeTaskId.current = "";
          setBusy(false);
        }
        setEntries((current) => updateTaskStatus(finishTaskStatus(
          current,
          message.task_id,
          "failed",
          message.data.message,
          message.data.elapsed_ms,
          Date.parse(message.timestamp),
        ), message.task_id, { changes: message.data.changes, rollbackState: "idle" }));
        if (!replaying) {
          setCancelling(false);
          if (message.task_id) clearApprovalsForTask(message.task_id);
        }
        setEntries((current) => finishPlanTask(current, message.task_id, "failed"));
        if (!replaying) reportError(message.data.message, message.event_id);
      } else if (message.type === "task.cancelled") {
        if (!replaying) {
          if (pendingRecovery.current?.task_id === message.task_id) pendingRecovery.current = null;
          activeTaskId.current = "";
          setBusy(false);
        }
        setEntries((current) => updateTaskStatus(finishTaskStatus(
          current,
          message.task_id,
          "cancelled",
          "Task cancelled by user.",
          message.data.elapsed_ms,
          Date.parse(message.timestamp),
        ), message.task_id, { changes: message.data.changes, rollbackState: "idle" }));
        if (!replaying) {
          setCancelling(false);
          if (message.task_id) clearApprovalsForTask(message.task_id);
        }
        setEntries((current) => replaying
          ? finishPlanTask(current, message.task_id, "cancelled")
          : [
              ...finishPlanTask(current, message.task_id, "cancelled"),
              {
                id: message.event_id,
                kind: "assistant",
                text: tx("Task cancelled by user."),
                timestamp: message.timestamp,
              },
            ]);
        if (!replaying) void requestConversationList(false);
      } else if (message.type === "workspace.snapshot.created") {
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          protection: message.data,
        }));
      } else if (message.type === "task.finalization.pending") {
        const pendingTaskId = message.task_id;
        if (!pendingTaskId) return;
        setEntries((current) => updateTaskStatus(current, pendingTaskId, {
          summary: "Securing the final workspace snapshot before completing this task.",
          summaryValues: undefined,
          protection: {
            ...(current.find((entry): entry is TaskStatusTranscriptEntry => (
              entry.kind === "task-status" && entry.taskId === pendingTaskId
            ))?.protection ?? {
              snapshot_id: "",
              task_id: pendingTaskId,
              session_id: message.session_id,
              backend: "side-git",
              protected: false,
              status: "finalize_pending",
              has_changes: false,
              changed_files: [],
              additions: 0,
              deletions: 0,
              diff_available: false,
              rollback_available: false,
              rolled_back: false,
            }),
            error: message.data.message,
          },
        }));
      } else if (message.type === "task.rollback.started") {
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          rollbackState: "running",
          rollbackError: undefined,
        }));
      } else if (message.type === "task.rollback.completed") {
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          changes: message.data,
          rollbackState: "completed",
          rollbackError: undefined,
        }));
        if (!replaying) {
          setRollbackBusyTaskId("");
          setTaskDiff(null);
        }
      } else if (message.type === "task.rollback.failed") {
        setEntries((current) => updateTaskStatus(current, message.task_id, {
          rollbackState: "failed",
          rollbackError: message.data.message,
        }));
        if (!replaying) setRollbackBusyTaskId("");
      }
    }

    function handleResponse(message: RuntimeResponse) {
      const pendingManagement = pendingManagementRequests.current.get(message.request_id);
      if (pendingManagement) {
        pendingManagementRequests.current.delete(message.request_id);
        if (message.ok) {
          pendingManagement.resolve(message.result ?? {});
        } else {
          pendingManagement.reject(new Error(
            `${message.error?.code ?? "runtime_error"}: ${message.error?.message ?? tx("Unknown management error")}`,
          ));
        }
        return;
      }
      if (!message.ok) {
        const failedWorkspaceOpen = message.request_id === workspaceOpenRequest.current;
        const failedSessionCreate = message.request_id === sessionCreateRequest.current;
        const failedSessionOpen = message.request_id === sessionOpenRequest.current;
        const failedReplay = message.request_id === eventReplayRequest.current;
        const approvalId = approvalResolveRequests.current.get(message.request_id);
        if (approvalId) {
          approvalResolveRequests.current.delete(message.request_id);
          setResolvingApprovalId((current) => current === approvalId ? "" : current);
        }
        if (message.request_id === accessModeRequest.current) {
          accessModeRequest.current = "";
        }
        if (message.request_id === traceModeRequest.current) {
          traceModeRequest.current = "";
        }
        if (message.request_id === taskSubmitRequest.current) {
          taskSubmitRequest.current = "";
          taskSubmitSession.current = "";
          activeTaskId.current = "";
          setBusy(false);
        }
        if (message.request_id === taskCancelRequest.current) {
          taskCancelRequest.current = "";
          setCancelling(false);
        }
        if (message.request_id === taskRecoveryRequest.current) {
          taskRecoveryRequest.current = "";
          const failedRecovery = pendingRecovery.current;
          if (failedRecovery) {
            recoveryQueue.current = recoveryQueue.current.filter(
              (item) => item.task_id !== failedRecovery.task_id,
            );
            releaseRunningTask(failedRecovery.task_id, failedRecovery.session_id);
          }
          pendingRecovery.current = null;
          requestPendingTaskRecovery();
        }
        if (failedReplay) {
          eventReplayRequest.current = "";
          replayBarrier.current = null;
          setReplayingConversation(false);
          requestPendingTaskRecovery();
        }
        if (failedSessionCreate) sessionCreateRequest.current = "";
        if (failedSessionOpen) {
          sessionOpenRequest.current = "";
          replayBarrier.current = null;
          setReplayingConversation(false);
        }
        if (failedWorkspaceOpen) {
          workspaceOpenRequest.current = "";
          setConnection("error");
        }
        reportError(
          `${message.error?.code ?? "runtime_error"}: ${message.error?.message ?? tx("Unknown error")}`,
        );
        return;
      }
      if (message.request_id === eventReplayRequest.current) {
        eventReplayRequest.current = "";
        const barrier = replayBarrier.current;
        const replayed = ((message.result?.events as RuntimeEvent[] | undefined) ?? []);
        if (barrier) barrier.replayed.push(...replayed);
        if (barrier && message.result?.has_more === true) {
          requestEventReplay(
            barrier,
            Number(message.result?.last_sequence ?? barrier.afterSequence),
          );
          return;
        }
        replayBarrier.current = null;
        const buffered = barrier?.buffered ?? [];
        const ordered = deduplicateRuntimeEvents([
          ...(barrier?.replayed ?? replayed),
          ...buffered,
        ])
          .filter((event) => RESTORABLE_EXECUTION_EVENT_TYPES.includes(event.type))
          .sort((left, right) => (
            left.session_id === right.session_id
              ? left.sequence - right.sequence
              : left.timestamp.localeCompare(right.timestamp)
        ));
        const lastResetIndex = findLastRuntimeEventIndex(ordered, "session.reset");
        const restorable = lastResetIndex >= 0 ? ordered.slice(lastResetIndex + 1) : ordered;
        for (const event of restorable) handleRuntimeMessage(event, true);
        const recovery = pendingRecovery.current;
        const previousActiveTaskId = activeTaskId.current;
        const replayedTerminalTaskIds = new Set(restorable
          .filter((event) => (
            event.type === "task.completed"
            || event.type === "task.failed"
            || event.type === "task.cancelled"
          ))
          .map((event) => event.task_id)
          .filter((taskId): taskId is string => Boolean(taskId)));
        const activeTaskReachedTerminal = Boolean(
          previousActiveTaskId && replayedTerminalTaskIds.has(previousActiveTaskId),
        );
        const recoveringTaskId = recovery && recovery.session_id === barrier?.sessionId
          ? recovery.task_id
          : undefined;
        setEntries((current) => sortTranscriptEntries(
          settleInterruptedReplayEntries(
            current,
            activeTaskReachedTerminal ? undefined : recoveringTaskId,
            tx,
          ),
        ));
        if (
          activeTaskReachedTerminal
          || previousActiveTaskId && !recovery
        ) {
          if (previousActiveTaskId) {
            releaseRunningTask(previousActiveTaskId, barrier?.sessionId ?? "");
          }
          pendingRecovery.current = null;
        } else if (recovery) {
          registerRunningTask(recovery.task_id, recovery.session_id);
        }
        setReplayingConversation(false);
        requestPendingTaskRecovery();
        return;
      }
      if (message.request_id === taskRecoveryRequest.current) {
        taskRecoveryRequest.current = "";
        const recoverySessionId = pendingRecovery.current?.session_id ?? activeConversationIdRef.current;
        const taskId = String(message.result?.task_id ?? pendingRecovery.current?.task_id ?? "");
        registerRunningTask(taskId, recoverySessionId);
        recoveryQueue.current = recoveryQueue.current.filter(
          (item) => item.task_id !== taskId,
        );
        pendingRecovery.current = recoveryQueue.current.find(
          (item) => item.session_id === activeConversationIdRef.current,
        ) ?? recoveryQueue.current[0] ?? null;
        requestPendingTaskRecovery();
        return;
      }
      if (message.request_id === taskSubmitRequest.current) {
        taskSubmitRequest.current = "";
        const submittedSessionId = taskSubmitSession.current;
        taskSubmitSession.current = "";
        registerRunningTask(String(message.result?.task_id ?? ""), submittedSessionId);
        return;
      }
      if (message.request_id === taskCancelRequest.current) {
        taskCancelRequest.current = "";
        if (message.result?.accepted !== true) {
          setCancelling(false);
        }
        return;
      }
      if (message.request_id === workspaceOpenRequest.current) {
        workspaceOpenRequest.current = "";
        const result = message.result ?? {};
        setWorkspace(String(result.workspace ?? workspaceRef.current));
        setModel(`${String(result.provider ?? "provider")} / ${String(result.model ?? "model")}`);
        setAccessMode((result.access_mode as AccessMode | undefined) ?? "restricted");
        const activeTasks = (result.active_tasks as Array<{ task_id?: string; session_id?: string }> | undefined) ?? [];
        for (const task of activeTasks) {
          registerRunningTask(String(task.task_id ?? ""), String(task.session_id ?? ""));
        }
        const recoveries = (
          result.recoveries as RuntimeRecoveryTask[] | undefined
        ) ?? ((result.recovery as RuntimeRecoveryTask | null | undefined)
          ? [result.recovery as RuntimeRecoveryTask]
          : []);
        recoveryQueue.current = recoveries;
        const recovery = recoveries[0] ?? null;
        // Keep finalize_pending as the authoritative active-task state. It must
        // not be sent through task.recover (the Sidecar retries POST snapshot
        // only), but replay reconciliation still needs its task id so a buffered
        // terminal event can clear busy deterministically.
        pendingRecovery.current = recovery;
        if (recovery) {
          pendingConversationOpen.current = {
            projectId: projectIdRef.current,
            conversationId: recovery.session_id,
          };
          registerRunningTask(recovery.task_id, recovery.session_id);
        }
        let barrier = replayBarrier.current;
        if (!barrier && recovery) {
          barrier = {
            sessionId: recovery.session_id,
            afterSequence: 0,
            buffered: [],
            replayed: [],
          };
          replayBarrier.current = barrier;
        }
        void requestConversationList();
        void refreshMcpSnapshot().catch((error) => {
          setLogs((current) => [...current.slice(-199), `${tx("MCP refresh failed:")} ${String(error)}`]);
        });
        void refreshRagSnapshot().catch((error) => {
          setLogs((current) => [...current.slice(-199), `${tx("RAG refresh failed:")} ${String(error)}`]);
        });
        void refreshManagementSnapshots();
        void refreshDiagnosticsSnapshot().catch((error) => {
          setLogs((current) => [...current.slice(-199), `${tx("Diagnostics refresh failed:")} ${String(error)}`]);
        });
        return;
      }
      if (message.request_id === sessionListRequest.current) {
        sessionListRequest.current = "";
        const listed = ((message.result?.conversations as ConversationSummary[] | undefined) ?? []);
        setConversations(listed);
        const listedProjectId = projectIdRef.current;
        if (listedProjectId) {
          for (const conversation of listed) {
            sessionProjectIds.current.set(conversation.id, listedProjectId);
          }
          setConversationCache((current) => ({ ...current, [listedProjectId]: listed }));
        }
        const pending = pendingConversationOpen.current;
        if (pending?.projectId === listedProjectId && listed.some((item) => item.id === pending.conversationId)) {
          pendingConversationOpen.current = null;
          void requestOpenConversation(pending.conversationId);
        } else if (sessionListAutoOpen.current && listed.length > 0) {
          if (pending?.projectId === listedProjectId) pendingConversationOpen.current = null;
          void requestOpenConversation(listed[0].id);
        } else if (sessionListAutoOpen.current) {
          if (pending?.projectId === listedProjectId) pendingConversationOpen.current = null;
          void requestCreateConversation();
        }
        return;
      }
      if (message.request_id === sessionCreateRequest.current) {
        const result = message.result ?? {};
        sessionCreateRequest.current = "";
        const identifier = String(result.session_id ?? "");
        setSessionId(identifier);
        setActiveConversationId(identifier);
        activeConversationIdRef.current = identifier;
        syncActiveConversationTask(identifier);
        setWorkspace(String(result.workspace ?? workspaceRef.current));
        setModel(`${String(result.provider ?? "provider")} / ${String(result.model ?? "model")}`);
        if (result.mode) {
          const createdMode = String(result.mode) as AgentMode;
          setMode(createdMode);
          modeRef.current = createdMode;
        }
        applySnapshot(result);
        void requestConversationList(false);
        return;
      }
      if (message.request_id === sessionOpenRequest.current) {
        sessionOpenRequest.current = "";
        const result = message.result ?? {};
        const identifier = String(result.id ?? "");
        setSessionId(identifier);
        setActiveConversationId(identifier);
        activeConversationIdRef.current = identifier;
        syncActiveConversationTask(identifier);
        if (result.mode) {
          const restoredMode = String(result.mode) as AgentMode;
          setMode(restoredMode);
          modeRef.current = restoredMode;
        }
        applySnapshot(result);
        const barrier = replayBarrier.current;
        if (barrier && barrier.sessionId === identifier && !eventReplayRequest.current) {
          const eventFloor = Number(result.event_floor_sequence ?? 0);
          if (Number.isFinite(eventFloor)) {
            barrier.afterSequence = Math.max(barrier.afterSequence, eventFloor);
          }
          requestEventReplay(barrier, barrier.afterSequence);
        } else {
          setReplayingConversation(false);
          requestPendingTaskRecovery();
        }
        return;
      }
      if (message.request_id === sessionRenameRequest.current) {
        sessionRenameRequest.current = "";
        void requestConversationList(false);
        return;
      }
      if (message.request_id === sessionDeleteRequest.current) {
        sessionDeleteRequest.current = "";
        setSessionId("");
        setActiveConversationId("");
        activeConversationIdRef.current = "";
        setEntries([]);
        setRollbackBusyTaskId("");
        setTaskDiff(null);
        setTaskDiffLoading("");
        replaceAttachments([]);
        setTraceEnabled(false);
        setTracePath("");
        setUsage({ ...EMPTY_USAGE, context_window: appSettingsRef.current.agent.context_window });
        setHistoryState({ ...EMPTY_HISTORY, context_window: appSettingsRef.current.agent.context_window });
        void requestConversationList(true);
        return;
      }
      if (message.request_id === accessModeRequest.current) {
        accessModeRequest.current = "";
        setAccessMode((message.result?.mode as AccessMode | undefined) ?? "restricted");
        return;
      }
      if (message.request_id === traceModeRequest.current) {
        traceModeRequest.current = "";
        setTraceEnabled(message.result?.enabled === true);
        setTracePath(String(message.result?.path ?? ""));
      }
    }

    function applySnapshot(result: Record<string, unknown>) {
      const transcript = (result.transcript as ConversationTranscriptEntry[] | undefined) ?? [];
      setEntries(transcript.flatMap((item): TranscriptEntry[] => {
        if (item.role === "plan" && item.plan) {
          return [{
            id: item.id,
            kind: "plan",
            taskId: item.plan.task_id,
            goal: item.plan.goal,
            summary: item.plan.summary,
            executionOrder: item.plan.execution_order,
            steps: item.plan.steps.map((step) => ({
              id: step.id,
              description: step.description,
              task_type: step.task_type,
              dependencies: step.dependencies,
              status: step.status,
              resultPreview: step.result_preview,
              error: step.error,
            })),
            timestamp: item.timestamp,
          }];
        }
        if (item.role === "plan") return [];
        return [{
          id: item.id,
          kind: item.role,
          text: item.content,
          attachments: item.attachments,
          timestamp: item.timestamp,
        }];
      }));
      replaceAttachments([]);
      setRollbackBusyTaskId("");
      setTaskDiff(null);
      setTaskDiffLoading("");
      setTraceEnabled(result.trace_enabled === true);
      setTracePath(String(result.trace_path ?? ""));
      const restoredUsage = result.usage as UsageSnapshot | undefined;
      setUsage(restoredUsage ? {
        ...EMPTY_USAGE,
        ...restoredUsage,
        task_input_tokens: 0,
        task_output_tokens: 0,
        task_cached_input_tokens: 0,
        task_reasoning_tokens: 0,
        task_llm_calls: 0,
        task_estimated_cost: 0,
        task_priced_llm_calls: 0,
      } : { ...EMPTY_USAGE, context_window: appSettingsRef.current.agent.context_window });
      setHistoryState((result.history as HistorySnapshot | undefined) ?? {
        ...EMPTY_HISTORY,
        context_window: appSettingsRef.current.agent.context_window,
      });
    }

    void start();
    return () => {
      disposed = true;
      rejectPendingManagementRequests(tx("Desktop Runtime connection closed."));
      if (restartTimer.current !== null) {
        window.clearTimeout(restartTimer.current);
        restartTimer.current = null;
      }
      for (const unlisten of unlisteners) unlisten();
      if (runtimeStarted.current) void invoke("runtime_stop");
    };
  }, []);

  function reportError(error: unknown, id: string = crypto.randomUUID()) {
    setEntries((current) => [
      ...current,
      {
        id,
        kind: "error",
        text: error instanceof Error ? error.message : String(error),
        timestamp: new Date().toISOString(),
      },
    ]);
  }

  function retireRuntimePid(pid: number | null) {
    if (pid === null) return;
    retiredRuntimePids.current.add(pid);
    if (retiredRuntimePids.current.size > 32) {
      retiredRuntimePids.current = new Set(
        Array.from(retiredRuntimePids.current).slice(-16),
      );
    }
  }

  function rejectPendingManagementRequests(message: string) {
    for (const pending of pendingManagementRequests.current.values()) {
      pending.reject(new Error(message));
    }
    pendingManagementRequests.current.clear();
  }

  function applySettings(snapshot: SettingsSnapshot) {
    setSettingsSnapshot(snapshot);
    setAppSettings(snapshot.settings);
    appSettingsRef.current = snapshot.settings;
    languageRef.current = snapshot.settings.general.language;
    document.documentElement.lang = snapshot.settings.general.language;
    void getCurrentWebview()
      .setZoom(snapshot.settings.appearance.client_font_size / DEFAULT_APP_SETTINGS.appearance.client_font_size)
      .catch((error) => console.warn("Unable to apply client font scale", error));
    defaultModeRef.current = snapshot.settings.agent.default_mode;
    if (!activeConversationId) {
      setMode(snapshot.settings.agent.default_mode);
      modeRef.current = snapshot.settings.agent.default_mode;
    }
  }

  function handleSettingsSaved(snapshot: SettingsSnapshot) {
    const runtimeChanged = JSON.stringify([
      appSettings.models,
      appSettings.agent,
      appSettings.rag,
      appSettings.diagnostics,
    ]) !== JSON.stringify([
      snapshot.settings.models,
      snapshot.settings.agent,
      snapshot.settings.rag,
      snapshot.settings.diagnostics,
    ]);
    applySettings(snapshot);
    if (runtimeStarted.current && runtimeChanged) setRuntimeSettingsDirty(true);
  }

  async function requestConversationList(autoOpen = true) {
    if (sessionListRequest.current) return;
    const id = requestId("session-list");
    sessionListRequest.current = id;
    sessionListAutoOpen.current = autoOpen;
    try {
      await sendRequest("session.list", {}, id);
    } catch (error) {
      sessionListRequest.current = "";
      reportError(error);
    }
  }

  function requestManagement<T, M extends ManagementRequestType>(
    method: M,
    params: RuntimeRequestDataMap[M],
  ): Promise<T> {
    if (!runtimeStarted.current || !projectIdRef.current) {
      return Promise.reject(new Error(t("Open a project and wait for Python Runtime first.")));
    }
    const id = requestId(method.replace(/\./g, "-"));
    return new Promise<T>((resolve, reject) => {
      pendingManagementRequests.current.set(id, {
        resolve: (result) => resolve(result as T),
        reject,
      });
      void sendRequest(method, params, id).catch((error) => {
        pendingManagementRequests.current.delete(id);
        reject(error instanceof Error ? error : new Error(String(error)));
      });
    });
  }

  async function refreshMcpSnapshot() {
    const next = await requestManagement<McpSnapshot, "mcp.list">("mcp.list", {});
    setMcpSnapshot(next);
    return next;
  }

  async function installMcpServer(
    name: string,
    config: McpInstallConfig,
    overwrite: boolean,
    confirmed: boolean,
  ) {
    const next = await requestManagement<McpSnapshot, "mcp.install">("mcp.install", {
      name,
      config,
      overwrite,
      confirmed,
    });
    setMcpSnapshot(next);
    return next;
  }

  async function setMcpServerEnabled(name: string, enabled: boolean) {
    const next = await requestManagement<McpSnapshot, "mcp.set_enabled">("mcp.set_enabled", {
      name,
      enabled,
    });
    setMcpSnapshot(next);
    return next;
  }

  async function restartMcpServer(name: string) {
    const next = await requestManagement<McpSnapshot, "mcp.restart">("mcp.restart", { name });
    setMcpSnapshot(next);
    return next;
  }

  async function removeMcpServer(name: string) {
    const next = await requestManagement<McpSnapshot, "mcp.remove">("mcp.remove", { name });
    setMcpSnapshot(next);
    return next;
  }

  async function readMcpServerLogs(name: string) {
    return requestManagement<{ name: string; logs: string }, "mcp.logs">("mcp.logs", { name });
  }

  async function refreshRagSnapshot() {
    const next = await requestManagement<RagSnapshot, "rag.snapshot">("rag.snapshot", {});
    setRagSnapshot(next);
    return next;
  }

  async function addRagSources(paths: string[]) {
    const next = await requestManagement<RagSnapshot, "rag.add_sources">("rag.add_sources", { paths });
    setRagSnapshot(next);
    return next;
  }

  async function removeRagSource(path: string) {
    const next = await requestManagement<RagSnapshot, "rag.remove_source">("rag.remove_source", { path });
    setRagSnapshot(next);
    return next;
  }

  async function rebuildRagIndex() {
    const next = await requestManagement<RagSnapshot, "rag.index">("rag.index", {});
    setRagSnapshot(next);
    return next;
  }

  async function clearRagIndex(confirmed: boolean) {
    const next = await requestManagement<RagSnapshot, "rag.clear">("rag.clear", { confirmed });
    setRagSnapshot(next);
    return next;
  }

  async function refreshMemorySnapshot(query = "") {
    const next = await requestManagement<MemorySnapshot, "memory.list">("memory.list", { query, limit: 500 });
    setMemorySnapshot(next);
    return next;
  }

  async function saveMemory(content: string) {
    const next = await requestManagement<MemorySnapshot, "memory.save">("memory.save", { content });
    setMemorySnapshot(next);
    return next;
  }

  async function deleteMemory(id: string) {
    const next = await requestManagement<MemorySnapshot, "memory.delete">("memory.delete", { id });
    setMemorySnapshot(next);
    return next;
  }

  async function clearMemory(confirmed: boolean) {
    const next = await requestManagement<MemorySnapshot, "memory.clear">("memory.clear", { confirmed });
    setMemorySnapshot(next);
    return next;
  }

  async function refreshSkillSnapshot() {
    const next = await requestManagement<SkillSnapshot, "skill.list">("skill.list", {});
    setSkillSnapshot(next);
    return next;
  }

  async function setSkillEnabled(name: string, enabled: boolean) {
    const next = await requestManagement<SkillSnapshot, "skill.set_enabled">("skill.set_enabled", { name, enabled });
    setSkillSnapshot(next);
    return next;
  }

  async function reloadSkills() {
    const next = await requestManagement<SkillSnapshot, "skill.reload">("skill.reload", {});
    setSkillSnapshot(next);
    return next;
  }

  async function prepareSkillDirectory(scope: SkillInstallScope) {
    return invoke<SkillDirectoryResult>("skill_directory_open", {
      scope,
      projectId: scope === "project" ? activeProjectId : null,
    });
  }

  async function refreshBrowserSnapshot() {
    const next = await requestManagement<BrowserSnapshot, "browser.snapshot">("browser.snapshot", {});
    setBrowserSnapshot(next);
    return next;
  }

  async function probeBrowser(port: number) {
    const probe = await requestManagement<BrowserProbeSnapshot, "browser.probe">("browser.probe", { port });
    setBrowserSnapshot((current) => ({ ...current, legacy_probe: probe }));
    return probe;
  }

  async function connectBrowser(port?: number) {
    const result = await requestManagement<{ message: string; snapshot: BrowserSnapshot }, "browser.connect">("browser.connect", { port, confirmed: true });
    setBrowserSnapshot({ ...result.snapshot, message: result.message });
    return { ...result.snapshot, message: result.message };
  }

  async function disconnectBrowser() {
    const result = await requestManagement<{ message: string; snapshot: BrowserSnapshot }, "browser.disconnect">("browser.disconnect", { confirmed: true });
    setBrowserSnapshot({ ...result.snapshot, message: result.message });
    return { ...result.snapshot, message: result.message };
  }

  async function readBrowserTabs() {
    const result = await requestManagement<{ output: string }, "browser.tabs">("browser.tabs", {});
    setBrowserSnapshot((current) => ({ ...current, tabs_output: result.output }));
    return result;
  }

  function refreshManagementSnapshots() {
    for (const [label, operation] of [
      ["Memory", refreshMemorySnapshot],
      ["Skills", refreshSkillSnapshot],
      ["Browser", refreshBrowserSnapshot],
    ] as const) {
      void operation().catch((error) => {
        setLogs((current) => [...current.slice(-199), `${tx("{name} refresh failed:", { name: label })} ${String(error)}`]);
      });
    }
  }

  async function refreshDiagnosticsSnapshot() {
    const next = await requestManagement<DiagnosticsSnapshot, "diagnostics.snapshot">("diagnostics.snapshot", {});
    setDiagnosticsSnapshot(next);
    return next;
  }

  async function runDiagnostics(profile: "safe" | "build" = "safe") {
    if (profile === "build" && !await confirmDialog(
      t("Build checks may execute local project build scripts. Continue?"),
      { title: t("Run build checks"), kind: "warning" },
    )) return diagnosticsSnapshot;
    const next = await requestManagement<DiagnosticsSnapshot, "diagnostics.run">("diagnostics.run", {
      profile,
      confirmed: profile === "build",
    });
    setDiagnosticsSnapshot(next);
    setBottomPanel("problems");
    return next;
  }

  async function cancelDiagnostics() {
    const runId = diagnosticsSnapshot.run_id;
    if (!runId) return diagnosticsSnapshot;
    const next = await requestManagement<DiagnosticsSnapshot, "diagnostics.cancel">("diagnostics.cancel", { run_id: runId });
    setDiagnosticsSnapshot(next);
    return next;
  }

  async function viewTaskDiff(entry: TaskStatusTranscriptEntry) {
    if (!sessionId || !entry.changes?.diff_available || taskDiffLoading) return;
    setTaskDiffLoading(entry.taskId);
    try {
      const result = await requestManagement<TaskDiffResult, "task.diff">("task.diff", {
        session_id: sessionId,
        task_id: entry.taskId,
        max_chars: 120_000,
      });
      setTaskDiff(result);
    } catch (error) {
      reportError(error);
    } finally {
      setTaskDiffLoading("");
    }
  }

  async function rollbackTaskChanges(entry: TaskStatusTranscriptEntry) {
    const changes = entry.changes;
    if (
      !sessionId
      || !changes?.rollback_available
      || !changes.snapshot_id
      || rollbackBusyTaskId
      || busy
      || activeProjectTaskRunning
    ) return;
    const confirmed = await confirmDialog(
      t("Restore the {count} file(s) changed by this task? Later edits to those files will cause a safe conflict instead of being overwritten.", {
        count: changes.changed_files.length,
      }),
      { title: t("Undo task changes"), kind: "warning" },
    );
    if (!confirmed) return;
    setRollbackBusyTaskId(entry.taskId);
    setEntries((current) => updateTaskStatus(current, entry.taskId, {
      rollbackState: "running",
      rollbackError: undefined,
    }));
    try {
      await requestManagement<TaskChangeSet, "task.rollback">("task.rollback", {
        session_id: sessionId,
        task_id: entry.taskId,
        snapshot_id: changes.snapshot_id,
        confirmed: true,
      });
      void requestConversationList(false);
    } catch (error) {
      setEntries((current) => updateTaskStatus(current, entry.taskId, {
        rollbackState: "failed",
        rollbackError: String(error),
      }));
      reportError(error);
    } finally {
      setRollbackBusyTaskId("");
    }
  }

  async function requestCreateConversation() {
    if (sessionCreateRequest.current || replayingConversation || projectActionBusy) return;
    const id = requestId("session-create");
    sessionCreateRequest.current = id;
    try {
      await sendRequest(
        "session.create",
        { mode: defaultModeRef.current, title: t("New conversation") },
        id,
      );
    } catch (error) {
      sessionCreateRequest.current = "";
      reportError(error);
    }
  }

  async function requestOpenConversation(conversationId: string) {
    if (
      sessionOpenRequest.current
      || eventReplayRequest.current
      || replayingConversation
      || projectActionBusy
      || conversationId === activeConversationId
    ) return;
    const id = requestId("session-open");
    sessionOpenRequest.current = id;
    setReplayingConversation(true);
    replayBarrier.current = {
      sessionId: conversationId,
      afterSequence: 0,
      buffered: [],
      replayed: [],
    };
    try {
      await sendRequest("session.open", { session_id: conversationId }, id);
    } catch (error) {
      sessionOpenRequest.current = "";
      replayBarrier.current = null;
      setReplayingConversation(false);
      reportError(error);
    }
  }

  async function renameConversation(conversation: ConversationSummary) {
    if (runningTasks[conversation.id] || sessionRenameRequest.current) return;
    const title = window.prompt(t("Conversation title:"), conversation.title)?.trim();
    if (!title || title === conversation.title) return;
    const id = requestId("session-rename");
    sessionRenameRequest.current = id;
    try {
      await sendRequest("session.rename", { session_id: conversation.id, title }, id);
    } catch (error) {
      sessionRenameRequest.current = "";
      reportError(error);
    }
  }

  async function deleteConversation(conversation: ConversationSummary) {
    if (runningTasks[conversation.id] || sessionDeleteRequest.current) return;
    if (!await confirmDialog(
      t('Delete conversation "{name}"?\n\nProject files will not be changed.', { name: conversation.title }),
      { title: t("Delete conversation"), kind: "warning" },
    )) return;
    const id = requestId("session-delete");
    sessionDeleteRequest.current = id;
    try {
      await sendRequest("session.delete", { session_id: conversation.id }, id);
    } catch (error) {
      sessionDeleteRequest.current = "";
      reportError(error);
    }
  }

  async function changeAccessMode(nextMode: AccessMode) {
    if (runtimeMutationBusy || activeProjectTaskRunning || !sessionId || nextMode === accessMode || accessModeRequest.current) return;
    if (nextMode === "full-access") {
      const confirmed = await confirmDialog(
        `${t("Enable Full access?")}\n\n${t("StellarCode will execute file writes, deletes, commands, MCP tools, and full-disk scans without approval. It may access files outside the active project, subject only to Windows permissions. This setting resets to Normal when the app or project is restarted.")}`,
        { title: t("Enable Full access"), kind: "warning" },
      );
      if (!confirmed) return;
    }
    const id = requestId("access-mode");
    accessModeRequest.current = id;
    try {
      await sendRequest("runtime.set_access_mode", { mode: nextMode }, id);
    } catch (error) {
      accessModeRequest.current = "";
      reportError(error);
    }
  }

  async function toggleTrace() {
    if (runtimeMutationBusy || !sessionId || traceModeRequest.current) return;
    const id = requestId("trace-mode");
    traceModeRequest.current = id;
    try {
      await sendRequest(
        "session.set_trace",
        { session_id: sessionId, enabled: !traceEnabled },
        id,
      );
    } catch (error) {
      traceModeRequest.current = "";
      reportError(error);
    }
  }

  async function refreshProjects() {
    const storedProjects = await invoke<ProjectRecord[]>("project_list");
    setProjects(storedProjects);
    return storedProjects;
  }

  async function activateProject(project: ProjectRecord, forceRestart = false, conversationToOpen = "") {
    if (projectActionBusy || workspaceOpenRequest.current) return;
    setProjectActionBusy(true);
    if (restartTimer.current !== null) {
      window.clearTimeout(restartTimer.current);
      restartTimer.current = null;
    }
    restartAttempts.current = 0;
    pendingRecovery.current = null;
    replayBarrier.current = null;
    eventReplayRequest.current = "";
    setReplayingConversation(false);
    taskRecoveryRequest.current = "";
    if (forceRestart) {
      lastEventSequence.current.clear();
      seenEventIds.current.clear();
    }
    setActiveProjectId(project.id);
    setExpandedProjectIds((current) => new Set(current).add(project.id));
    pendingConversationOpen.current = conversationToOpen
      ? { projectId: project.id, conversationId: conversationToOpen }
      : null;
    setWorkspace(project.path);
    workspaceRef.current = project.path;
    projectIdRef.current = project.id;
    setSessionId("");
    setAccessMode("restricted");
    setTraceEnabled(false);
    setTracePath("");
    setMcpSnapshot(EMPTY_MCP_SNAPSHOT);
    setRagSnapshot(EMPTY_RAG_SNAPSHOT);
    setMemorySnapshot(EMPTY_MEMORY_SNAPSHOT);
    setSkillSnapshot(EMPTY_SKILL_SNAPSHOT);
    setBrowserSnapshot(EMPTY_BROWSER_SNAPSHOT);
    setDiagnosticsSnapshot({ ...EMPTY_DIAGNOSTICS_SNAPSHOT, workspace: project.path });
    setConversations([]);
    setActiveConversationId("");
    activeConversationIdRef.current = "";
    activeTaskId.current = "";
    setBusy(false);
    setCancelling(false);
    workspaceOpenRequest.current = "";
    sessionListRequest.current = "";
    sessionCreateRequest.current = "";
    sessionOpenRequest.current = "";
    accessModeRequest.current = "";
    traceModeRequest.current = "";
    setEntries([]);
    replaceAttachments([]);
    setEventCount(0);
    setConnection("starting");
    try {
      rejectPendingManagementRequests(tx("A management request was interrupted by a project or Runtime switch."));
      if (forceRestart && runtimeStarted.current) {
        retireRuntimePid(activeRuntimePid.current);
        activeRuntimePid.current = null;
        await invoke("runtime_stop");
        runtimeStarted.current = false;
        runningTasksBySession.current.clear();
        taskSessions.current.clear();
        publishRunningTasks();
        setApprovalQueue([]);
        setResolvingApprovalId("");
        approvalResolveRequests.current.clear();
        approvalToolCalls.current.clear();
      }
      if (runtimeStarted.current) {
        const id = requestId("workspace-open");
        workspaceOpenRequest.current = id;
        await sendRequest(
          "workspace.open",
          { project_id: project.id, workspace: project.path },
          id,
        );
      } else {
        const started = await invoke<RuntimeStartResult>("runtime_start", {
          workspace: project.path,
        });
        activeRuntimePid.current = started.pid;
        setRuntimePython(started.python);
        setRuntimeSettingsDirty(false);
        runtimeStarted.current = true;
      }
      await invoke("project_touch", { projectId: project.id });
      await refreshProjects();
    } catch (error) {
      pendingConversationOpen.current = null;
      setConnection("error");
      reportError(error);
    } finally {
      setProjectActionBusy(false);
    }
  }

  async function openWorkspace() {
    if (projectActionBusy) return;
    const selected = await open({ directory: true, multiple: false, title: t("Select a workspace") });
    if (typeof selected !== "string") return;
    setProjectActionBusy(true);
    try {
      const project = await invoke<ProjectRecord>("project_register", { path: selected });
      await refreshProjects();
      await activateProject(project);
    } catch (error) {
      reportError(error);
      setProjectActionBusy(false);
    }
  }

  async function createProject() {
    if (projectActionBusy) return;
    const parent = await open({ directory: true, multiple: false, title: t("Choose a parent folder") });
    if (typeof parent !== "string") return;
    const name = window.prompt(t("Project folder name:"), "new-project")?.trim();
    if (!name) return;
    setProjectActionBusy(true);
    try {
      const project = await invoke<ProjectRecord>("project_create", { parent, name });
      await refreshProjects();
      await activateProject(project);
    } catch (error) {
      reportError(error);
      setProjectActionBusy(false);
    }
  }

  async function removeActiveProject() {
    if (!activeProject || anyTaskRunning || runtimeMutationBusy || projectActionBusy) return;
    if (!await confirmDialog(
      t("Remove {name} from StellarCode?\n\nThe project directory will not be deleted.", { name: activeProject.name }),
      { title: t("Remove project from StellarCode"), kind: "warning" },
    )) return;
    setProjectActionBusy(true);
    try {
      await invoke("project_remove", { projectId: activeProject.id });
      const removedProjectId = activeProject.id;
      setExpandedProjectIds((current) => {
        const updated = new Set(current);
        updated.delete(removedProjectId);
        return updated;
      });
      setConversationCache((current) => {
        const updated = { ...current };
        delete updated[removedProjectId];
        return updated;
      });
      rejectPendingManagementRequests(t("A management request was interrupted because the project was removed."));
      retireRuntimePid(activeRuntimePid.current);
      activeRuntimePid.current = null;
      if (runtimeStarted.current) await invoke("runtime_stop");
      runtimeStarted.current = false;
      const remaining = await refreshProjects();
      if (remaining.length > 0) {
        await activateProject(remaining[0], false);
      } else {
        setActiveProjectId("");
        setExpandedProjectIds(new Set());
        setWorkspace("");
        setSessionId("");
        setAccessMode("restricted");
        setTraceEnabled(false);
        setTracePath("");
        setMcpSnapshot(EMPTY_MCP_SNAPSHOT);
        setRagSnapshot(EMPTY_RAG_SNAPSHOT);
        setMemorySnapshot(EMPTY_MEMORY_SNAPSHOT);
        setSkillSnapshot(EMPTY_SKILL_SNAPSHOT);
        setBrowserSnapshot(EMPTY_BROWSER_SNAPSHOT);
        setDiagnosticsSnapshot(EMPTY_DIAGNOSTICS_SNAPSHOT);
        setConversations([]);
        setActiveConversationId("");
        setConnection("offline");
      }
    } catch (error) {
      reportError(error);
    } finally {
      setProjectActionBusy(false);
    }
  }

  function toggleProjectExpansion(project: ProjectRecord) {
    const isExpanded = expandedProjectIds.has(project.id);
    setExpandedProjectIds((current) => {
      const updated = new Set(current);
      if (isExpanded) updated.delete(project.id);
      else updated.add(project.id);
      return updated;
    });
    const hasCachedConversations = Object.prototype.hasOwnProperty.call(conversationCache, project.id);
    if (!isExpanded && project.id !== activeProjectId && !hasCachedConversations && !projectActionBusy) {
      void activateProject(project);
    }
  }

  function selectProject(project: ProjectRecord) {
    setExpandedProjectIds((current) => new Set(current).add(project.id));
    if (project.id !== activeProjectId && !projectActionBusy) {
      void activateProject(project);
    }
  }

  function openProjectConversation(project: ProjectRecord, conversation: ConversationSummary) {
    if (projectActionBusy) return;
    if (project.id === activeProjectId) {
      void requestOpenConversation(conversation.id);
      return;
    }
    void activateProject(project, false, conversation.id);
  }

  function createConversationInActiveProject() {
    if (!activeProject || projectActionBusy || replayingConversation) return;
    setExpandedProjectIds((current) => new Set(current).add(activeProject.id));
    void requestCreateConversation();
  }

  const connectionLabel = useMemo(() => {
    if (connection === "online") return translate(appSettings.general.language, sessionId ? "Runtime ready" : "Runtime online");
    if (connection === "starting") return translate(appSettings.general.language, "Runtime starting");
    if (connection === "restarting") return translate(appSettings.general.language, "Runtime recovering");
    if (connection === "error") return translate(appSettings.general.language, "Runtime error");
    return translate(appSettings.general.language, "Runtime offline");
  }, [appSettings.general.language, connection, sessionId]);

  function updateReferenceMatch(value: string, caret: number | null) {
    if (caret === null) {
      setReferenceMatch(null);
      return;
    }
    const beforeCaret = value.slice(0, caret);
    const match = beforeCaret.match(/(?:^|\s)@([^\s@]*)$/);
    if (!match) {
      setReferenceMatch(null);
      return;
    }
    setReferenceMatch({
      start: caret - match[1].length - 1,
      end: caret,
      query: match[1],
    });
    setReferenceSelection(0);
  }

  function insertComposerReference(reference: ComposerReference) {
    if (!referenceMatch) return;
    const nextPrompt = `${prompt.slice(0, referenceMatch.start)}${reference.token} ${prompt.slice(referenceMatch.end)}`;
    const nextCaret = referenceMatch.start + reference.token.length + 1;
    setPrompt(nextPrompt);
    setReferenceMatch(null);
    setReferenceSelection(0);
    window.requestAnimationFrame(() => {
      composerTextareaRef.current?.focus();
      composerTextareaRef.current?.setSelectionRange(nextCaret, nextCaret);
    });
  }

  function syncComposerHighlightScroll(textarea: HTMLTextAreaElement) {
    if (!composerHighlightRef.current) return;
    composerHighlightRef.current.scrollTop = textarea.scrollTop;
    composerHighlightRef.current.scrollLeft = textarea.scrollLeft;
  }

  async function submitPrompt(event: FormEvent) {
    event.preventDefault();
    const value = prompt.trim();
    if ((!value && attachments.length === 0) || !sessionId || runtimeMutationBusy) return;
    const submittedAttachments = attachments;
    const displayText = value || t("Please analyze the attached files.");
    setEntries((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        kind: "user",
        text: displayText,
        attachments: submittedAttachments,
        timestamp: new Date().toISOString(),
      },
    ]);
    setPrompt("");
    setReferenceMatch(null);
    replaceAttachments([]);
    setBusy(true);
    const id = requestId("task-submit");
    taskSubmitRequest.current = id;
    taskSubmitSession.current = sessionId;
    try {
      await sendRequest(
        "task.submit",
        { session_id: sessionId, prompt: value, attachments: submittedAttachments },
        id,
      );
    } catch (error) {
      taskSubmitRequest.current = "";
      taskSubmitSession.current = "";
      activeTaskId.current = "";
      setBusy(false);
      reportError(error);
    }
  }

  async function chooseAttachments() {
    if (runtimeMutationBusy || !sessionId) return;
    const selected = await open({
      directory: false,
      multiple: true,
      title: t("Attach files to this message"),
    });
    if (!selected) return;
    await addAttachmentPaths(typeof selected === "string" ? [selected] : selected);
  }

  async function pasteImages(event: ClipboardEvent<HTMLTextAreaElement>) {
    if (runtimeMutationBusy || !sessionId) return;
    const files = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null);
    if (!files.length) return;
    event.preventDefault();
    try {
      const pasted = await Promise.all(files.map((file) => clipboardImageAttachment(file, t)));
      const merged = [...attachmentsRef.current, ...pasted];
      if (merged.length > 10) {
        reportError(t("A message can contain at most 10 attachments."));
      }
      replaceAttachments(merged.slice(0, 10));
    } catch (error) {
      reportError(error);
    }
  }

  async function addAttachmentPaths(paths: string[]) {
    if (!paths.length) return;
    try {
      const inspected = await invoke<RuntimeAttachment[]>("attachment_inspect", { paths });
      const current = attachmentsRef.current;
      const known = new Set(current.map((item) => item.local_path?.toLowerCase()));
      const additions = inspected.filter(
        (item) => !known.has(item.local_path?.toLowerCase()),
      );
      const merged = [...current, ...additions];
      if (merged.length > 10) {
        reportError(t("A message can contain at most 10 attachments."));
      }
      replaceAttachments(merged.slice(0, 10));
    } catch (error) {
      reportError(error);
    }
  }

  function removeAttachment(id: string) {
    if (runtimeMutationBusy) return;
    replaceAttachments((current) => current.filter((item) => item.id !== id));
  }

  function replaceAttachments(
    value: RuntimeAttachment[] | ((current: RuntimeAttachment[]) => RuntimeAttachment[]),
  ) {
    const next = typeof value === "function" ? value(attachmentsRef.current) : value;
    attachmentsRef.current = next;
    setAttachments(next);
  }

  async function cancelTask() {
    const taskId = activeTaskId.current;
    if (!busy || !taskId || cancelling || taskCancelRequest.current) return;
    const id = requestId("task-cancel");
    taskCancelRequest.current = id;
    setCancelling(true);
    try {
      await sendRequest(
        "task.cancel",
        { session_id: sessionId, task_id: taskId },
        id,
      );
    } catch (error) {
      taskCancelRequest.current = "";
      setCancelling(false);
      reportError(error);
    }
  }

  async function changeMode(nextMode: AgentMode) {
    setMode(nextMode);
    modeRef.current = nextMode;
    if (sessionId) {
      await sendRequest("session.set_mode", { session_id: sessionId, mode: nextMode });
    }
  }

  async function resolveApproval(decision: "approve" | "reject" | "skip") {
    if (!approval || resolvingApprovalId) return;
    const approvalId = approval.data.approval_id;
    const id = requestId("approval-resolve");
    setResolvingApprovalId(approvalId);
    approvalResolveRequests.current.set(id, approvalId);
    try {
      await sendRequest("approval.resolve", {
        session_id: approval.session_id,
        task_id: approval.task_id ?? "",
        approval_id: approvalId,
        decision,
      }, id);
    } catch (error) {
      approvalResolveRequests.current.delete(id);
      setResolvingApprovalId((current) => current === approvalId ? "" : current);
      reportError(error);
    }
  }

  async function restartRuntimeFromSettings() {
    if (!activeProject || anyTaskRunning || runtimeMutationBusy) return;
    await activateProject(activeProject, true);
  }

  return (
    <div
      className={`app-shell theme-${appSettings.appearance.theme} ${appSettings.general.compact_tools ? "compact-tools" : ""} ${appSettings.general.compact_plans ? "compact-plans" : ""}`}
      style={{
        "--conversation-font-size": `${appSettings.general.conversation_font_size}px`,
        "--accent": appSettings.appearance.accent_color,
        "--bg": appSettings.appearance.background_color,
        "--panel": appSettings.appearance.panel_color,
        "--text": appSettings.appearance.text_color,
        colorScheme: appSettings.appearance.theme,
      } as CSSProperties}
    >
      {dragActive && <div className="attachment-drop-overlay"><div><strong>{t("Drop files to attach")}</strong><span>{t("Images, code, and text files - up to 10 files")}</span></div></div>}
      <header className="topbar">
        <div className="brand-mark" aria-hidden="true">*</div>
        <strong className="brand-name">StellarCode</strong>
        <span className="topbar-divider" />
        <span className="workspace-path" title={workspace}>{workspace || t("No workspace")}</span>
        <span className="branch-chip">desktop-client</span>
        <div className="topbar-spacer" />
        <span className={`connection-state ${connection}`}><i /> {connectionLabel}</span>
        <button className="icon-button" aria-label={t("Open management center")} onClick={() => setSettingsTarget("memory")}>{t("Manage")}</button>
        <button className="icon-button" aria-label={t("Open settings")} onClick={() => setSettingsTarget("general")}>{t("Settings")}</button>
      </header>

      <div className="workspace-grid">
        <aside className="left-sidebar">
          <div className="sidebar-actions">
            <button className="primary-button full-width" onClick={() => void openWorkspace()} disabled={projectActionBusy}>+ {t("Open workspace")}</button>
          </div>
          <SidebarSection title={t("Projects")} action="+" onAction={() => void createProject()} grow>
            <div className="project-tree">
              {projects.map((project) => {
                const isActiveProject = project.id === activeProjectId;
                const isExpanded = expandedProjectIds.has(project.id);
                const hasCachedConversations = Object.prototype.hasOwnProperty.call(conversationCache, project.id);
                const projectConversations = isActiveProject
                  ? conversations
                  : conversationCache[project.id] ?? [];
                const projectRunningCount = Object.keys(runningTasks).filter(
                  (conversationId) => sessionProjectIds.current.get(conversationId) === project.id,
                ).length;
                return <div className="project-node" key={project.id}>
                  <div className={`project-row ${isActiveProject ? "active" : ""}`}>
                    <button
                      className="project-expand-toggle"
                      onClick={() => toggleProjectExpansion(project)}
                      aria-expanded={isExpanded}
                      aria-label={t(isExpanded ? "Collapse {name}" : "Expand {name}", { name: project.name })}
                    >
                      <span className={`project-chevron ${isExpanded ? "expanded" : ""}`}>&gt;</span>
                    </button>
                    <button
                      className="project-select"
                      onClick={() => selectProject(project)}
                      disabled={projectActionBusy}
                      title={project.path}
                    >
                      <span className="project-icon">▣</span>
                      <span className="project-label">{project.name}</span>
                      {projectRunningCount > 0 && <span className="running-count" title={t("{count} background task(s) running", { count: projectRunningCount })}>{projectRunningCount}</span>}
                    </button>
                    {isActiveProject && <button
                      className="project-new-conversation"
                      onClick={createConversationInActiveProject}
                      disabled={replayingConversation || projectActionBusy}
                      aria-label={t("New conversation in {name}", { name: project.name })}
                      title={t("New conversation")}
                    >+</button>}
                  </div>
                  {isExpanded && <div className="project-conversations">
                    {projectConversations.map((conversation) => (
                      <div className="conversation-row" key={conversation.id}>
                        <button
                          className={`session-item ${isActiveProject && conversation.id === activeConversationId ? "active" : ""}`}
                          onClick={() => openProjectConversation(project, conversation)}
                          onDoubleClick={() => isActiveProject && void renameConversation(conversation)}
                          disabled={projectActionBusy || replayingConversation}
                          title={`${conversation.title}\n${t(isActiveProject ? "Double-click to rename" : "Click to switch project and open")}`}
                        >
                          <span className={`session-dot ${runningTasks[conversation.id] ? "running" : ""}`} /><span className="project-label">{conversation.title}</span>
                          {runningTasks[conversation.id] && <span className="session-running-label">{t("Running")}</span>}
                        </button>
                        {isActiveProject && <button className="row-delete" onClick={() => void deleteConversation(conversation)} disabled={Boolean(runningTasks[conversation.id]) || projectActionBusy} aria-label={t("Delete {name}", { name: conversation.title })}>x</button>}
                      </div>
                    ))}
                    {projectConversations.length === 0 && <span className="empty-conversations">{
                      isActiveProject
                        ? connection === "online" ? t("No conversations") : t("Loading conversations...")
                        : hasCachedConversations ? t("No conversations") : t("Select project to load conversations")
                    }</span>}
                  </div>}
                </div>;
              })}
              {projects.length === 0 && <span className="empty-conversations">{t("Open a workspace to begin")}</span>}
            </div>
          </SidebarSection>
          <div className="sidebar-footer">
            <button className="footer-action" onClick={() => void removeActiveProject()} disabled={!activeProject || anyTaskRunning || runtimeMutationBusy || projectActionBusy}>{t("Remove project")}</button>
            <span className="sidebar-footer-spacer" /><span>{t("{count} projects", { count: projects.length })}</span>
          </div>
        </aside>

        <main className="conversation-panel">
          <div className="conversation-header">
            <div><h1>{activeConversation?.title ?? t("New conversation")}</h1><p>{activeProject?.name ?? t("No project")} - {t("protocol v{version}", { version: RUNTIME_PROTOCOL_VERSION })}</p></div>
            <ModeSwitch mode={mode} onChange={changeMode} disabled={runtimeMutationBusy || !sessionId} t={t} />
          </div>
          <div className="transcript" aria-live="polite">
            {entries.length === 0 && <Message role="StellarCode" timestamp={t("runtime")} accent>
              {sessionId ? t("Runtime connected to {name}. Submit a task to start.", { name: activeProject?.name ?? t("No project") }) : connectionLabel}
            </Message>}
            {transcriptGroups.map((group) => group.kind === "activity" ? (
              <article className="message assistant activity-message" key={group.id}>
                <div className="message-body">
                  <div className="message-meta"><strong>StellarCode</strong><time>{t("now")}</time></div>
                  <div className="activity-stack">
                    {group.entries.map((entry) => entry.kind === "tool" ? (
                      <ToolCard
                        name={entry.name}
                        status={entry.status}
                        detail={entry.detail}
                        elapsed={entry.elapsed === undefined ? "" : `${entry.elapsed} ms`}
                        changePreview={entry.changePreview}
                        t={t}
                        key={entry.id}
                      />
                    ) : entry.kind === "approval" ? (
                      approvalQueue.some((item) => item.data.approval_id === entry.approvalId)
                        ? null
                        : <ApprovalHistoryCard entry={entry} t={t} language={appSettings.general.language} key={entry.id} />
                    ) : (
                      <div className="activity-progress" key={entry.id}>{entry.text}</div>
                    ))}
                  </div>
                </div>
              </article>
            ) : group.kind === "task-status" ? (
              <AgentRunStatus
                entry={group.entry}
                now={timerNow}
                t={t}
                diffLoading={taskDiffLoading === group.entry.taskId}
                rollbackBusy={rollbackBusyTaskId === group.entry.taskId}
                onViewDiff={() => void viewTaskDiff(group.entry)}
                onRollback={() => void rollbackTaskChanges(group.entry)}
                key={group.id}
              />
            ) : group.kind === "plan" ? (
              <PlanCard plan={group.entry} t={t} key={group.id} />
            ) : (
              <Message role={group.entry.kind === "user" ? t("You") : group.entry.kind === "error" ? t("Runtime error") : "StellarCode"} timestamp={t("now")} accent={group.entry.kind !== "user"} key={group.id}>
                {group.entry.kind === "user"
                  ? <UserMessageContent text={group.entry.text} attachments={group.entry.attachments} t={t} />
                  : group.entry.kind === "assistant"
                  ? <><MarkdownContent content={group.entry.text} />{group.entry.streaming && <span className="streaming-caret" aria-label={t("Streaming response")} />}</>
                  : group.entry.text}
              </Message>
            ))}
            {approval && <section className="approval-card pending">
              <div className="approval-icon">!</div><div className="approval-content">
                <div className="approval-heading"><strong>{t("Approval required")}</strong><span>{t(`${approval.data.danger_level} risk`)}</span></div>
                <p>{translateRuntimeText(appSettings.general.language, approval.data.risk_description)}</p><code>{approval.data.name} {JSON.stringify(approval.data.arguments)}</code>
                {approval.data.change_preview && <ChangePreviewPanel preview={approval.data.change_preview} t={t} expanded />}
                <small>{t("Task: {task} - Approval {position}", { task: approval.task_id ?? "", position: approvalQueue.length > 1 ? t("1 of {count}", { count: approvalQueue.length }) : t("pending") })}</small>
                <div className="approval-actions">
                  <button className="secondary-button" onClick={() => void resolveApproval("reject")} disabled={Boolean(resolvingApprovalId)}>{t("Reject")}</button>
                  <button className="primary-button" onClick={() => void resolveApproval("approve")} disabled={Boolean(resolvingApprovalId)}>{t(resolvingApprovalId ? "Resolving..." : "Allow once")}</button>
                </div>
              </div>
            </section>}
          </div>
          <form className="composer" onSubmit={submitPrompt}>
            {referenceMatch && <div className="reference-menu" role="listbox" aria-label={t("References")}>
              <div className="reference-menu-heading">
                <strong>{t("References")}</strong>
                <span>{t("Skills and MCP tools")}</span>
              </div>
              <div className="reference-menu-list">
                {filteredComposerReferences.length > 0 ? filteredComposerReferences.map((reference, index) => (
                  <button
                    className={`reference-option ${index === referenceSelection ? "selected" : ""}`}
                    type="button"
                    role="option"
                    aria-selected={index === referenceSelection}
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseEnter={() => setReferenceSelection(index)}
                    onClick={() => insertComposerReference(reference)}
                    key={reference.id}
                  >
                    <span className={`reference-kind ${reference.kind}`}>{t(reference.kind === "skill" ? "Skill" : "MCP")}</span>
                    <span className="reference-copy">
                      <strong>{reference.label}</strong>
                      <small>{reference.description || reference.token}</small>
                    </span>
                    <span className={`reference-meta ${reference.available ? "" : "unavailable"}`}>{t(reference.meta)}{!reference.available ? ` · ${t("unavailable")}` : ""}</span>
                  </button>
                )) : <div className="reference-empty">{t("No matching references")}</div>}
              </div>
              <div className="reference-menu-hint">{t("Use Up and Down to navigate, Enter or Tab to insert, and Escape to close")}</div>
            </div>}
            {attachments.length > 0 && <div className="composer-attachments">
              {attachments.map((attachment) => <div className={`attachment-chip ${attachment.kind}`} key={attachment.id} title={attachment.local_path}>
                <AttachmentPreview attachment={attachment} t={t} />
                <span className="attachment-details"><span className="attachment-name">{attachment.display_name}</span><span className="attachment-size">{formatFileSize(attachment.size_bytes)}</span></span>
                <button type="button" onClick={() => removeAttachment(attachment.id)} aria-label={t("Remove {name}", { name: attachment.display_name })}>x</button>
              </div>)}
            </div>}
            <div className="composer-input-layer">
              <div className="composer-input-highlight" ref={composerHighlightRef} aria-hidden="true">
                <HighlightedComposerPrompt value={prompt} />
              </div>
              <textarea ref={composerTextareaRef} value={prompt} onChange={(event) => {
                setPrompt(event.currentTarget.value);
                updateReferenceMatch(event.currentTarget.value, event.currentTarget.selectionStart);
              }} onScroll={(event) => syncComposerHighlightScroll(event.currentTarget)} onSelect={(event) => updateReferenceMatch(event.currentTarget.value, event.currentTarget.selectionStart)} onPaste={(event) => void pasteImages(event)} onKeyDown={(event) => {
              if (referenceMatch) {
                if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                  event.preventDefault();
                  if (filteredComposerReferences.length > 0) {
                    const direction = event.key === "ArrowDown" ? 1 : -1;
                    setReferenceSelection((current) => (current + direction + filteredComposerReferences.length) % filteredComposerReferences.length);
                  }
                  return;
                }
                if (event.key === "Escape") {
                  event.preventDefault();
                  setReferenceMatch(null);
                  return;
                }
                if ((event.key === "Enter" || event.key === "Tab") && filteredComposerReferences.length > 0) {
                  event.preventDefault();
                  insertComposerReference(filteredComposerReferences[referenceSelection] ?? filteredComposerReferences[0]);
                  return;
                }
              }
              const shouldSend = appSettings.general.send_shortcut === "enter"
                ? event.key === "Enter" && !event.shiftKey
                : event.key === "Enter" && event.ctrlKey;
                if (shouldSend) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
              }} placeholder={t("Ask StellarCode to inspect, explain, or change the workspace...")} rows={2} disabled={!sessionId || runtimeMutationBusy} />
            </div>
            <div className="composer-footer">
              <button className="text-button" type="button" onClick={() => void chooseAttachments()} disabled={runtimeMutationBusy || !sessionId}>+ {t("Attach")}{attachments.length ? ` (${attachments.length})` : ""}</button>
              <button className={`trace-toggle ${traceEnabled ? "active" : ""}`} type="button" onClick={() => void toggleTrace()} disabled={runtimeMutationBusy || !sessionId || Boolean(traceModeRequest.current)} title={tracePath || t("Record complete LLM, tool, approval, and Runtime event logs for this conversation")}>{t(traceEnabled ? "Trace On" : "Trace Off")}</button>
              <div className="access-switch" aria-label={t("Access mode")}>
                <button className={accessMode === "restricted" ? "active" : ""} type="button" onClick={() => void changeAccessMode("restricted")} disabled={runtimeMutationBusy || activeProjectTaskRunning || !sessionId}>{t("Normal")}</button>
                <button className={accessMode === "full-access" ? "active dangerous" : ""} type="button" onClick={() => void changeAccessMode("full-access")} disabled={runtimeMutationBusy || activeProjectTaskRunning || !sessionId}>{t("Full access")}</button>
              </div>
              <span className="composer-hint">{activeTaskFinalizing ? t("Finalizing workspace protection") : busy ? t("Agent is running") : ragIndexing ? t("RAG index is building") : sessionId ? t(appSettings.general.send_shortcut === "enter" ? "Enter to send - Shift+Enter for newline" : "Ctrl+Enter to send") : connectionLabel}</span>
              <button className="secondary-button" type="button" onClick={() => void cancelTask()} disabled={!busy || !activeTaskId.current || cancelling || activeTaskFinalizing}>{t(activeTaskFinalizing ? "Finalizing..." : cancelling ? "Stopping..." : "Stop")}</button>
              <button className="primary-button" type="submit" disabled={!sessionId || runtimeMutationBusy || (!prompt.trim() && attachments.length === 0)}>{t("Send")}</button>
            </div>
          </form>
        </main>

        <aside className="context-sidebar">
          <PanelSection title={t("Run context")}>
            <DefinitionRow label={t("Model")} value={t(model)} /><DefinitionRow label={t("Mode")} value={mode === "react" ? "ReAct" : t(mode === "plan" ? "Plan" : "Team")} />
            <DefinitionRow label={t("Access")} value={t(accessMode === "restricted" ? "normal" : "full access")} emphasis={accessMode === "full-access"} /><DefinitionRow label={t("Workspace")} value={activeProject?.name ?? t("none")} />
          </PanelSection>
          <PanelSection title={t("Project")}>
            <DefinitionRow label={t("Projects")} value={`${projects.length}`} /><DefinitionRow label={t("Conversations")} value={`${conversations.length}`} />
          </PanelSection>
          <PanelSection title={t("Context budget")}>
            <div className="meter-label"><span>{t(usage.last_exact ? "Provider usage" : "Estimated")}</span><strong>{contextPercent(usage)}%</strong></div>
            <div className="meter"><span style={{ width: `${contextPercent(usage)}%` }} /></div>
            <DefinitionRow label={t("Current")} value={`${formatTokens(usage.last_context_tokens)} / ${formatTokens(usage.context_window)}`} />
            <DefinitionRow label={t("Last call")} value={t("{input} in / {output} out", { input: formatTokens(usage.last_input_tokens), output: formatTokens(usage.last_output_tokens) })} />
            <DefinitionRow label={t("Last details")} value={t("{cached} cached / {reasoning} reasoning", { cached: formatTokens(usage.last_cached_input_tokens), reasoning: formatTokens(usage.last_reasoning_tokens) })} />
            <DefinitionRow label={t("Task")} value={t("{input} in / {output} out", { input: formatTokens(usage.task_input_tokens), output: formatTokens(usage.task_output_tokens) })} />
            <DefinitionRow label={t("Conversation")} value={t("{input} in / {output} out", { input: formatTokens(usage.input_tokens), output: formatTokens(usage.output_tokens) })} />
            <DefinitionRow label={t("Task cost")} value={usage.task_priced_llm_calls > 0 ? formatCost(usage.task_estimated_cost, usage.cost_currency) : t("pricing unavailable")} />
            <DefinitionRow label={t("Conversation cost")} value={usage.priced_llm_calls > 0 ? formatCost(usage.estimated_cost, usage.cost_currency) : t("pricing unavailable")} />
            <DefinitionRow label={t("Compactions")} value={`${historyState.compaction_count}`} />
            {usage.cost_source && <p className="panel-note">{t("Cost {method} · {source}", { method: t(usage.last_cost_estimated ? "estimated from token pricing" : "reported by provider"), source: usage.cost_source })}</p>}
            <p className="panel-note">{t("{count} LLM calls", { count: usage.llm_calls })}{usage.operation ? ` · ${t("latest {operation}", { operation: usage.operation })}` : ""}</p>
          </PanelSection>
          <PanelSection title={t("Services")}>
            <StatusRow label={t("Python runtime")} status={t(connection)} good={connection === "online"} />
            <StatusRow label={t("Session")} status={t(sessionId ? "ready" : "not created")} good={Boolean(sessionId)} />
            <StatusRow
              label={t("MCP servers")}
              status={`${mcpSnapshot.ready_servers} / ${mcpSnapshot.total_servers}`}
              good={mcpSnapshot.total_servers === 0 || mcpSnapshot.ready_servers === mcpSnapshot.total_servers}
            />
            <StatusRow label={t("MCP tools")} status={`${mcpSnapshot.total_tools}`} good={mcpSnapshot.total_servers === 0 || mcpSnapshot.total_tools > 0} />
            <StatusRow
              label={t("Code RAG")}
              status={ragSnapshot.status === "indexing"
                ? t("indexing")
                : ragSnapshot.chunk_count > 0
                  ? t("{count} files", { count: ragSnapshot.indexed_file_count })
                  : t("not indexed")}
              good={ragSnapshot.status !== "error" && ragSnapshot.chunk_count > 0}
            />
          </PanelSection>
          <PanelSection title={t("Protocol")}>
            <DefinitionRow label={t("Version")} value={`v${RUNTIME_PROTOCOL_VERSION}`} /><DefinitionRow label={t("Events")} value={`${eventCount}`} />
            <p className="panel-note">{t("JSONL over stdio. Sidecar logs are routed to stderr.")}</p>
          </PanelSection>
        </aside>
      </div>

      <section className="bottom-panel">
        <div className="bottom-tabs">{(["terminal", "problems", "trace"] as BottomPanel[]).map((panel) => (
          <button className={bottomPanel === panel ? "active" : ""} onClick={() => setBottomPanel(panel)} key={panel}>{t(panel[0].toUpperCase() + panel.slice(1))}{panel === "problems" && <span className="count-badge">{diagnosticsSnapshot.error_count + diagnosticsSnapshot.warning_count}</span>}</button>
        ))}<span className="bottom-tabs-spacer" /></div>
        <div className={`bottom-content ${bottomPanel === "problems" ? "problems-content" : ""}`}>
          {bottomPanel === "terminal" && <><span className="prompt-symbol">&gt;</span><span>{logs[logs.length - 1] || connectionLabel}</span></>}
          {bottomPanel === "problems" && <ProblemsPanel
            snapshot={diagnosticsSnapshot}
            language={appSettings.general.language}
            runtimeOnline={connection === "online"}
            mutationBusy={busy || ragIndexing || replayingConversation || Boolean(rollbackBusyTaskId)}
            onRun={runDiagnostics}
            onCancel={cancelDiagnostics}
            onOpenManagement={() => setSettingsTarget("diagnostics")}
            t={t}
          />}
          {bottomPanel === "trace" && <span>{traceEnabled ? t("Recording trace: {path}", { path: tracePath || t("initializing log file") }) : t("Trace recording is off for this conversation.")}</span>}
        </div>
      </section>
      {settingsTarget && settingsSnapshot && <SettingsPage
        initialSection={settingsTarget}
        snapshot={settingsSnapshot}
        runtimeOnline={connection === "online"}
        runtimePython={runtimePython}
        runtimeRestartPending={runtimeSettingsDirty}
        mcpSnapshot={mcpSnapshot}
        ragSnapshot={ragSnapshot}
        memorySnapshot={memorySnapshot}
        skillSnapshot={skillSnapshot}
        browserSnapshot={browserSnapshot}
        diagnosticsSnapshot={diagnosticsSnapshot}
        busy={runtimeMutationBusy || activeProjectTaskRunning || projectActionBusy}
        onClose={() => setSettingsTarget(null)}
        onSaved={handleSettingsSaved}
        onRestartRuntime={restartRuntimeFromSettings}
        onMcpRefresh={refreshMcpSnapshot}
        onMcpInstall={installMcpServer}
        onMcpSetEnabled={setMcpServerEnabled}
        onMcpRestart={restartMcpServer}
        onMcpRemove={removeMcpServer}
        onMcpLogs={readMcpServerLogs}
        onRagRefresh={refreshRagSnapshot}
        onRagAddSources={addRagSources}
        onRagRemoveSource={removeRagSource}
        onRagIndex={rebuildRagIndex}
        onRagClear={clearRagIndex}
        onMemoryRefresh={() => refreshMemorySnapshot()}
        onMemorySave={saveMemory}
        onMemoryDelete={deleteMemory}
        onMemoryClear={clearMemory}
        onSkillRefresh={refreshSkillSnapshot}
        onSkillSetEnabled={setSkillEnabled}
        onSkillReload={reloadSkills}
        onSkillPrepareDirectory={prepareSkillDirectory}
        onBrowserRefresh={refreshBrowserSnapshot}
        onBrowserProbe={probeBrowser}
        onBrowserConnect={connectBrowser}
        onBrowserDisconnect={disconnectBrowser}
        onBrowserTabs={readBrowserTabs}
        onDiagnosticsRefresh={refreshDiagnosticsSnapshot}
        onDiagnosticsRun={runDiagnostics}
        onDiagnosticsCancel={cancelDiagnostics}
      />}
      {taskDiff && <section className="diff-overlay" role="dialog" aria-modal="true" aria-label={t("Task diff")}>
        <div className="diff-dialog">
          <header><div><strong>{t("Task diff")}</strong><span>{t("{count} files · +{additions} -{deletions}", { count: taskDiff.changed_files.length, additions: taskDiff.additions, deletions: taskDiff.deletions })}</span></div><button className="secondary-button" onClick={() => setTaskDiff(null)}>{t("Close")}</button></header>
          <pre className="task-diff-content">{taskDiff.diff || t("No textual diff is available.")}</pre>
          {taskDiff.diff_truncated && <p>{t("The displayed diff was truncated. The Git snapshot still contains the complete task state.")}</p>}
        </div>
      </section>}
    </div>
  );
}

function SidebarSection({ title, action, grow = false, onAction, children }: { title: string; action: string; grow?: boolean; onAction?: () => void; children: React.ReactNode }) {
  return <section className={`sidebar-section ${grow ? "grow" : ""}`}><div className="section-heading"><span>{title}</span><button aria-label={`${action} ${title}`} onClick={onAction} disabled={!onAction}>{action}</button></div>{children}</section>;
}

function ProblemsPanel({
  snapshot,
  language,
  runtimeOnline,
  mutationBusy,
  onRun,
  onCancel,
  onOpenManagement,
  t,
}: {
  snapshot: DiagnosticsSnapshot;
  language: Language;
  runtimeOnline: boolean;
  mutationBusy: boolean;
  onRun: (profile?: "safe" | "build") => Promise<DiagnosticsSnapshot>;
  onCancel: () => Promise<DiagnosticsSnapshot>;
  onOpenManagement: () => void;
  t: Translator;
}) {
  const running = snapshot.status === "running";
  const problems = snapshot.problems;
  const statusText = running
    ? translateDiagnosticRuntimeText(language, snapshot.progress || t("Running workspace diagnostics..."))
    : snapshot.status === "not_run"
      ? t("Diagnostics have not been run for this workspace.")
      : snapshot.status === "failed"
        ? snapshot.error || t("Diagnostics failed.")
        : problems.length === 0
          ? t("No problems were reported by the completed checks.")
          : t("{count} problem(s) reported.", { count: problems.length });
  return <div className="problems-panel">
    <div className="problems-toolbar">
      <span className={`diagnostics-run-state ${snapshot.status}`}><i />{statusText}</span>
      {snapshot.stale && !running && <span className="problems-stale" title={t("Results may be stale.")}>{t("Results may be stale.")}</span>}
      <span className="problems-count errors">{t("{count} errors", { count: snapshot.error_count })}</span>
      <span className="problems-count warnings">{t("{count} warnings", { count: snapshot.warning_count })}</span>
      <span className="bottom-tabs-spacer" />
      <button className="secondary-button" onClick={onOpenManagement}>{t("Providers")}</button>
      {running
        ? <button className="secondary-button danger-button" onClick={() => void onCancel()}>{t("Cancel")}</button>
        : <>
            <button className="secondary-button" onClick={() => void onRun("safe")} disabled={!runtimeOnline || mutationBusy}>{t("Run checks")}</button>
            <button className="secondary-button" onClick={() => void onRun("build")} disabled={!runtimeOnline || mutationBusy}>{t("Run build checks")}</button>
          </>}
    </div>
    {problems.length > 0 && <div className="problems-list" role="list" aria-label={t("Double-click a problem to reveal its file.")}>
      {problems.map((problem) => <ProblemRow problem={problem} t={t} key={problem.id} />)}
    </div>}
  </div>;
}

function ProblemRow({ problem, t }: { problem: WorkspaceProblem; t: Translator }) {
  const location = `${problem.relative_path || problem.path}:${problem.line}:${problem.column}`;
  const reveal = () => void revealItemInDir(problem.path);
  return <div role="listitem">
    <button
      type="button"
      className={`problem-row severity-${problem.severity}`}
      title={t("Double-click to reveal {path}", { path: problem.path })}
      aria-label={`${problem.severity}: ${location}: ${problem.message}. ${t("Double-click a problem to reveal its file.")}`}
      onDoubleClick={reveal}
      onKeyDown={(event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        reveal();
      }}
    >
      <span className="problem-severity" aria-label={t(problem.severity)}>{problem.severity === "error" ? "E" : problem.severity === "warning" ? "W" : "I"}</span>
      <code>{location}</code>
      <span className="problem-message">{problem.message}</span>
      <span className="problem-source">{problem.source}{problem.code ? ` (${problem.code})` : ""}</span>
    </button>
  </div>;
}

function upsertMcpServer(snapshot: McpSnapshot, server: McpServerInfo): McpSnapshot {
  const servers = [
    ...snapshot.servers.filter((item) => item.name !== server.name),
    server,
  ].sort((left, right) => left.name.localeCompare(right.name));
  return {
    ...snapshot,
    servers,
    ready_servers: servers.filter((item) => item.status === "ready").length,
    total_servers: servers.length,
    total_tools: servers.reduce((total, item) => total + item.tool_count, 0),
  };
}

function markMcpServersStarting(snapshot: McpSnapshot): McpSnapshot {
  const servers = snapshot.servers.map((server) => server.disabled
    ? server
    : { ...server, status: "starting" as const, tools: [], tool_count: 0 });
  return {
    ...snapshot,
    servers,
    ready_servers: 0,
    total_tools: 0,
  };
}

function ModeSwitch({ mode, onChange, disabled, t }: { mode: AgentMode; onChange: (mode: AgentMode) => void; disabled: boolean; t: Translator }) {
  return <div className="mode-switch" aria-label={t("Agent mode")}>{(["react", "plan", "team"] as AgentMode[]).map((item) => (
    <button className={mode === item ? "active" : ""} onClick={() => onChange(item)} key={item} disabled={disabled}>{item === "react" ? "ReAct" : t(item[0].toUpperCase() + item.slice(1))}</button>
  ))}</div>;
}

function HighlightedComposerPrompt({ value }: { value: string }) {
  const pattern = /@(?:skill:[A-Za-z0-9][A-Za-z0-9._-]*|mcp:mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+)/g;
  const content: React.ReactNode[] = [];
  let cursor = 0;
  for (const match of value.matchAll(pattern)) {
    const start = match.index;
    if (start > cursor) content.push(value.slice(cursor, start));
    content.push(<strong className="composer-reference-token" key={`${start}-${match[0]}`}>{match[0]}</strong>);
    cursor = start + match[0].length;
  }
  if (cursor < value.length) content.push(value.slice(cursor));
  if (value.endsWith("\n")) content.push(<span key="trailing-line">&#8203;</span>);
  return <>{content}</>;
}

function Message({ role, timestamp, accent = false, children }: { role: string; timestamp: string; accent?: boolean; children: React.ReactNode }) {
  return <article className={`message ${accent ? "assistant" : "user"}`}><div className="message-body"><div className="message-meta"><strong>{role}</strong><time>{timestamp}</time></div><div className="message-content">{children}</div></div></article>;
}

function AgentRunStatus({ entry, now, t, diffLoading, rollbackBusy, onViewDiff, onRollback }: {
  entry: TaskStatusTranscriptEntry;
  now: number;
  t: Translator;
  diffLoading: boolean;
  rollbackBusy: boolean;
  onViewDiff: () => void;
  onRollback: () => void;
}) {
  const elapsed = entry.phase === "running"
    ? Math.max(0, now - entry.startedAt)
    : entry.elapsedMs ?? Math.max(0, now - entry.startedAt);
  const label = entry.phase === "running"
    ? entry.compacting ? t("Compressing context") : t("Working")
    : entry.phase === "completed"
    ? t("Completed")
    : entry.phase === "cancelled"
    ? t("Cancelled")
    : t("Failed");
  const protection = entry.changes ?? entry.protection;
  return <article className={`agent-run-status phase-${entry.phase} ${entry.compacting ? "compacting" : ""}`}>
    <span className="agent-run-dot" />
    <div className="agent-run-copy">
      <div className="agent-run-heading"><strong>{label}</strong><time>{formatElapsed(elapsed)}</time></div>
      <p>{t(entry.summary, entry.summaryValues)}</p>
      {entry.phase === "running" && protection?.protected && <div className="snapshot-ready">{t("Protected by an automatic task Git snapshot")}</div>}
      {entry.phase === "running" && protection && !protection.protected && <div className="snapshot-warning">{t("Workspace snapshot unavailable: {error}", { error: protection.error || t("unknown error") })}</div>}
      {entry.changes?.has_changes && <div className={`task-change-set ${entry.changes.rolled_back ? "rolled-back" : ""}`}>
        <div className="task-change-heading"><strong>{entry.changes.rolled_back ? t("Task changes rolled back") : t("{count} protected file(s) changed", { count: entry.changes.changed_files.length })}</strong><span>+{entry.changes.additions} -{entry.changes.deletions}</span></div>
        <div className="task-change-files">{entry.changes.changed_files.slice(0, 6).map((file) => <div key={file.path}><span>{changeStatusMarker(file.status)}</span><code title={file.path}>{file.path}</code><small>+{file.additions} -{file.deletions}</small></div>)}</div>
        {entry.changes.changed_files.length > 6 && <small>{t("and {count} more file(s)", { count: entry.changes.changed_files.length - 6 })}</small>}
        {entry.changes.rollback_block_reason && <p className="snapshot-warning">{t(entry.changes.rollback_block_reason)}</p>}
        {entry.rollbackError && <p className="rollback-error">{entry.rollbackError}</p>}
        <div className="task-change-actions">
          <button className="secondary-button" onClick={onViewDiff} disabled={diffLoading || rollbackBusy || !entry.changes.diff_available}>{t(diffLoading ? "Loading diff..." : "View diff")}</button>
          <button className="secondary-button danger-button" onClick={onRollback} disabled={rollbackBusy || !entry.changes.rollback_available}>{t(rollbackBusy || entry.rollbackState === "running" ? "Rolling back..." : entry.phase === "completed" ? "Undo task changes" : "Rollback task changes")}</button>
        </div>
      </div>}
    </div>
  </article>;
}

function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="markdown-content">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function UserMessageContent({ text, attachments = [], t }: { text: string; attachments?: RuntimeAttachment[]; t: Translator }) {
  return <div className="user-message-content">
    {text && <div className="user-message-text">{text}</div>}
    {attachments.length > 0 && <div className="sent-attachments">
      {attachments.map((attachment) => <div className={`sent-attachment ${attachment.kind}`} key={attachment.id} title={attachment.local_path}>
        <AttachmentPreview attachment={attachment} t={t} />
        <div><strong>{attachment.display_name}</strong><small>{formatFileSize(attachment.size_bytes)}</small></div>
      </div>)}
    </div>}
  </div>;
}

function formatFileSize(bytes?: number) {
  if (bytes === undefined) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const PREVIEWABLE_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/bmp",
  "image/tiff",
]);

function AttachmentPreview({ attachment, t }: { attachment: RuntimeAttachment; t: Translator }) {
  const inlineSource = attachment.data_base64 && PREVIEWABLE_IMAGE_TYPES.has(attachment.mime_type)
    ? `data:${attachment.mime_type};base64,${attachment.data_base64}`
    : "";
  const [source, setSource] = useState(inlineSource);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
    if (inlineSource) {
      setSource(inlineSource);
      return;
    }
    if (attachment.kind !== "image" || !attachment.local_path) {
      setSource("");
      return;
    }
    let active = true;
    void invoke<string>("attachment_preview_data", { path: attachment.local_path })
      .then((previewSource) => {
        if (active) setSource(previewSource);
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    return () => {
      active = false;
    };
  }, [attachment.kind, attachment.local_path, inlineSource]);

  if (attachment.kind === "image" && source && !failed) {
    return <img className="attachment-preview" src={source} alt={attachment.display_name} onError={() => setFailed(true)} />;
  }
  return <span className={`attachment-kind ${attachment.kind}`}>{t(attachment.kind === "image" ? "Image" : "File")}</span>;
}

const MAX_PASTED_IMAGE_BYTES = 20 * 1024 * 1024;

async function clipboardImageAttachment(file: File, t: Translator): Promise<RuntimeAttachment> {
  if (file.size === 0) throw new Error(t("The pasted image is empty."));
  if (file.size > MAX_PASTED_IMAGE_BYTES) {
    throw new Error(t("The pasted image exceeds the 20 MB clipboard limit."));
  }
  const dataUrl = await readFileAsDataUrl(file, t);
  const comma = dataUrl.indexOf(",");
  if (comma < 0) throw new Error(t("The clipboard image could not be encoded."));
  const mimeType = file.type || "image/png";
  const extension = mimeType.split("/")[1]?.replace("jpeg", "jpg") || "png";
  return {
    id: `attachment-${crypto.randomUUID()}`,
    kind: "image",
    mime_type: mimeType,
    display_name: `pasted-image-${new Date().toISOString().replace(/[:.]/g, "-")}.${extension}`,
    size_bytes: file.size,
    data_base64: dataUrl.slice(comma + 1),
  };
}

function readFileAsDataUrl(file: File, t: Translator): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => typeof reader.result === "string"
      ? resolve(reader.result)
      : reject(new Error(t("The clipboard image could not be read.")));
    reader.onerror = () => reject(reader.error ?? new Error(t("The clipboard image could not be read.")));
    reader.readAsDataURL(file);
  });
}

function ToolCard({ name, status, detail, elapsed, changePreview, t }: { name: string; status: ToolStatus; detail: string; elapsed: string; changePreview?: FileChangePreview; t: Translator }) {
  const display = {
    waiting_approval: { marker: "WAIT", label: "Waiting for approval" },
    running: { marker: "RUN", label: "Running" },
    completed: { marker: "OK", label: "Completed" },
    failed: { marker: "FAIL", label: "Failed" },
  }[status];
  return <div className={`tool-card tool-${status}`}><div className="tool-status">{t(display.marker)}</div><div><strong>{name}</strong><code>{detail}</code>{changePreview && <ChangePreviewPanel preview={changePreview} t={t} />}</div><span className="tool-result">{t(display.label)}{elapsed && ` - ${elapsed}`}</span></div>;
}

function ChangePreviewPanel({ preview, t, expanded = false }: { preview: FileChangePreview; t: Translator; expanded?: boolean }) {
  return <div className={`change-preview ${expanded ? "expanded" : "compact"}`}>
    <div><strong>{t(changeOperationLabel(preview.operation))}</strong><code>{preview.path || t("Unknown path")}</code><span>+{preview.additions} -{preview.deletions}</span></div>
    {!preview.rollback_protected && <small className="snapshot-warning">{t(changeProtectionReason(preview.protection_reason))}</small>}
    {preview.sensitive && <small>{t("Diff hidden for a potentially sensitive file")}</small>}
    {preview.error && <small className="rollback-error">{preview.error}</small>}
    {expanded && preview.diff && <pre>{preview.diff}</pre>}
  </div>;
}

function ApprovalHistoryCard({ entry, t, language }: { entry: ApprovalTranscriptEntry; t: Translator; language: Language }) {
  const display = {
    pending: { marker: "…", label: "Waiting for approval" },
    approve: { marker: "✓", label: "Approved" },
    reject: { marker: "×", label: "Rejected" },
    skip: { marker: "–", label: "Skipped" },
    modify: { marker: "±", label: "Modified" },
    interrupted: { marker: "!", label: "Interrupted" },
  }[entry.status];
  const argumentsToShow = entry.effectiveArguments ?? entry.arguments;
  return <div className={`tool-card approval-history-card approval-${entry.status}`}>
    <div className="tool-status">{display.marker}</div>
    <div>
      <strong>{t("Approval: {name}", { name: entry.name })}</strong>
      <code>{JSON.stringify(argumentsToShow)}</code>
      <small>{translateRuntimeText(language, entry.riskDescription)}</small>
      {entry.changePreview && <ChangePreviewPanel preview={entry.changePreview} t={t} />}
    </div>
    <span className="tool-result">{t(display.label)}</span>
  </div>;
}

function changeOperationLabel(operation: FileChangePreview["operation"]) {
  return {
    create: "Create file",
    modify: "Modify file",
    delete: "Delete file",
    no_change: "No file changes",
    unknown: "File change preview unavailable",
  }[operation];
}

function changeProtectionReason(reason?: string | null) {
  return {
    outside_workspace: "Outside the workspace · task Git rollback does not cover this path",
    generated_or_internal_path: "Generated or internal path · task Git rollback intentionally excludes it",
    sensitive_path: "Sensitive path · its contents are not copied into task Git snapshots",
    preview_error: "The change could not be included in task Git rollback",
  }[reason ?? ""] ?? "The change could not be included in task Git rollback";
}

function changeStatusMarker(status: TaskChangeSet["changed_files"][number]["status"]) {
  return {
    created: "A",
    modified: "M",
    deleted: "D",
    type_changed: "T",
    binary: "B",
  }[status];
}

function PlanCard({ plan, t }: { plan: PlanTranscriptEntry; t: Translator }) {
  const order = new Map(plan.executionOrder.map((id, index) => [id, index]));
  const steps = [...plan.steps].sort((left, right) => (
    (order.get(left.id) ?? Number.MAX_SAFE_INTEGER) - (order.get(right.id) ?? Number.MAX_SAFE_INTEGER)
  ));
  const settled = steps.filter((step) => ["completed", "failed", "skipped", "cancelled"].includes(step.status)).length;
  const completed = steps.filter((step) => step.status === "completed").length;
  const failed = steps.some((step) => step.status === "failed");
  const cancelled = steps.some((step) => step.status === "cancelled");
  const running = steps.some((step) => step.status === "running");
  const overall = cancelled ? "Cancelled" : failed ? "Needs attention" : settled === steps.length ? "Completed" : running ? "Running" : "Ready";
  const progress = steps.length ? Math.round((settled / steps.length) * 100) : 0;

  return <article className={`plan-card plan-${overall.toLowerCase().replace(" ", "-")}`}>
    <header className="plan-header">
      <div><span className="plan-eyebrow">{t("Execution plan")}</span><strong>{plan.summary || plan.goal}</strong>{plan.summary && <small>{plan.goal}</small>}</div>
      <span className="plan-overall">{t(overall)}</span>
    </header>
    <div className="plan-progress"><span style={{ width: `${progress}%` }} /></div>
    <div className="plan-progress-label"><span>{t("{settled} of {total} steps finished", { settled, total: steps.length })}</span><strong>{t("{count} completed", { count: completed })}</strong></div>
    <ol className="plan-steps">
      {steps.map((step, index) => <li className={`plan-step step-${step.status}`} key={step.id}>
        <span className="plan-step-marker">{planStepMarker(step.status, index + 1)}</span>
        <div className="plan-step-body">
          <div className="plan-step-heading"><span className="plan-step-type">{t(step.task_type.replace("_", " "))}</span><strong>{step.description}</strong></div>
          {step.dependencies.length > 0 && <small>{t("Depends on {dependencies}", { dependencies: step.dependencies.join(", ") })}</small>}
          {(step.resultPreview || step.error) && <p>{step.resultPreview || step.error}</p>}
        </div>
        <span className="plan-step-status">{t(step.status)}</span>
      </li>)}
    </ol>
  </article>;
}

function planStepMarker(status: PlanStepStatus, index: number) {
  if (status === "completed") return "OK";
  if (status === "failed") return "!";
  if (status === "skipped") return "-";
  if (status === "cancelled") return "x";
  if (status === "running") return ">";
  return String(index);
}

function applyAssistantDelta(
  entries: TranscriptEntry[],
  taskId: string | undefined,
  eventId: string,
  text: string,
  reset: boolean,
  timestamp: string,
): TranscriptEntry[] {
  const index = findStreamingAssistantIndex(entries, taskId);
  if (reset) {
    return index < 0 ? entries : entries.filter((_, entryIndex) => entryIndex !== index);
  }
  if (!text) return entries;
  if (index < 0) {
    return [
      ...entries,
      {
        id: `assistant-stream-${taskId || eventId}`,
        kind: "assistant",
        text,
        taskId,
        streaming: true,
        timestamp,
      },
    ];
  }
  return entries.map((entry, entryIndex) => (
    entryIndex === index && entry.kind === "assistant"
      ? { ...entry, text: entry.text + text }
      : entry
  ));
}

function completeAssistantStream(
  entries: TranscriptEntry[],
  taskId: string | undefined,
  eventId: string,
  content: string,
  timestamp: string,
): TranscriptEntry[] {
  const index = findStreamingAssistantIndex(entries, taskId);
  if (index < 0) {
    return [...entries, { id: eventId, kind: "assistant", text: content, taskId, timestamp }];
  }
  return entries.map((entry, entryIndex) => (
    entryIndex === index && entry.kind === "assistant"
      ? { ...entry, text: content, streaming: false }
      : entry
  ));
}

function findStreamingAssistantIndex(
  entries: TranscriptEntry[],
  taskId: string | undefined,
) {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (
      entry.kind === "assistant"
      && entry.streaming
      && (!taskId || entry.taskId === taskId)
    ) return index;
  }
  return -1;
}

function upsertToolEntry(
  entries: TranscriptEntry[],
  toolCallId: string,
  next: Extract<TranscriptEntry, { kind: "tool" }>,
): TranscriptEntry[] {
  const index = entries.findIndex(
    (entry) => entry.kind === "tool" && entry.toolCallId === toolCallId,
  );
  if (index < 0) return [...entries, next];
  return entries.map((entry, entryIndex) => (
    entryIndex === index ? {
      ...entry,
      ...next,
      id: entry.id,
      timestamp: entry.timestamp ?? next.timestamp,
    } : entry
  ));
}

function upsertApprovalEntry(
  entries: TranscriptEntry[],
  next: ApprovalTranscriptEntry,
): TranscriptEntry[] {
  const index = entries.findIndex(
    (entry) => entry.kind === "approval" && entry.approvalId === next.approvalId,
  );
  if (index < 0) return [...entries, next];
  return entries.map((entry, entryIndex) => (
    entryIndex === index && entry.kind === "approval"
      ? {
          ...entry,
          ...next,
          id: entry.id,
          timestamp: entry.timestamp ?? next.timestamp,
        }
      : entry
  ));
}

function updateApprovalEntry(
  entries: TranscriptEntry[],
  approvalId: string,
  patch: Partial<ApprovalTranscriptEntry>,
): TranscriptEntry[] {
  return entries.map((entry) => entry.kind === "approval" && entry.approvalId === approvalId
    ? { ...entry, ...patch }
    : entry);
}

function settleInterruptedReplayEntries(
  entries: TranscriptEntry[],
  recoveringTaskId: string | undefined,
  t: Translator,
): TranscriptEntry[] {
  return entries.map((entry) => {
    if (entry.kind === "approval" && entry.status === "pending") {
      return { ...entry, status: "interrupted" };
    }
    if (
      entry.kind === "tool"
      && (entry.status === "running" || entry.status === "waiting_approval")
    ) {
      return {
        ...entry,
        status: "failed",
        detail: t("Runtime interrupted; the tool outcome is unconfirmed."),
      };
    }
    if (
      entry.kind === "task-status"
      && entry.phase === "running"
      && entry.taskId !== recoveringTaskId
    ) {
      return {
        ...entry,
        phase: "failed",
        compacting: false,
        summary: "Runtime interrupted; the task has no resumable checkpoint.",
        summaryValues: undefined,
      };
    }
    return entry;
  });
}

function updateToolStatus(
  entries: TranscriptEntry[],
  toolCallId: string,
  status: ToolStatus,
  onlyFrom?: ToolStatus,
): TranscriptEntry[] {
  return entries.map((entry) => (
    entry.kind === "tool"
      && entry.toolCallId === toolCallId
      && (!onlyFrom || entry.status === onlyFrom)
      ? { ...entry, status }
      : entry
  ));
}

function interruptOpenToolEntries(entries: TranscriptEntry[], t: Translator): TranscriptEntry[] {
  return entries.map((entry) => entry.kind === "tool"
    && (entry.status === "running" || entry.status === "waiting_approval")
    ? {
        ...entry,
        status: "failed",
        detail: t("Runtime interrupted; the tool outcome is unconfirmed."),
      }
    : entry);
}

function upsertPlanEntry(entries: TranscriptEntry[], next: PlanTranscriptEntry): TranscriptEntry[] {
  const index = entries.findIndex((entry) => entry.kind === "plan" && entry.taskId === next.taskId);
  if (index < 0) return [...entries, next];
  return entries.map((entry, entryIndex) => entryIndex === index
    ? { ...next, id: entry.id, timestamp: entry.timestamp ?? next.timestamp }
    : entry);
}

function updatePlanStep(
  entries: TranscriptEntry[],
  taskId: string | undefined,
  stepId: string,
  patch: Partial<PlanStepView>,
): TranscriptEntry[] {
  return entries.map((entry) => entry.kind === "plan" && (!taskId || entry.taskId === taskId)
    ? { ...entry, steps: entry.steps.map((step) => step.id === stepId ? { ...step, ...patch } : step) }
    : entry);
}

function finishPlanTask(
  entries: TranscriptEntry[],
  taskId: string | undefined,
  status: Extract<PlanStepStatus, "failed" | "cancelled">,
): TranscriptEntry[] {
  return entries.map((entry) => entry.kind === "plan" && (!taskId || entry.taskId === taskId)
    ? {
        ...entry,
        steps: entry.steps.map((step) => step.status === "running" || step.status === "pending"
          ? { ...step, status }
          : step),
      }
    : entry);
}

function upsertTaskStatus(
  entries: TranscriptEntry[],
  next: TaskStatusTranscriptEntry,
): TranscriptEntry[] {
  const index = entries.findIndex(
    (entry) => entry.kind === "task-status" && entry.taskId === next.taskId,
  );
  if (index < 0) return [...entries, next];
  return entries.map((entry, entryIndex) => entryIndex === index && entry.kind === "task-status"
    ? {
        ...entry,
        ...next,
        id: entry.id,
        startedAt: Math.min(entry.startedAt, next.startedAt),
        timestamp: entry.timestamp ?? next.timestamp,
      }
    : entry);
}

function updateTaskStatus(
  entries: TranscriptEntry[],
  taskId: string | undefined,
  patch: Partial<TaskStatusTranscriptEntry>,
): TranscriptEntry[] {
  const index = findTaskStatusIndex(entries, taskId);
  if (index < 0) return entries;
  return entries.map((entry, entryIndex) => entryIndex === index && entry.kind === "task-status"
    ? { ...entry, ...patch }
    : entry);
}

function finishTaskStatus(
  entries: TranscriptEntry[],
  taskId: string | undefined,
  phase: Exclude<TaskRunPhase, "running">,
  summary?: string,
  elapsedMs?: number,
  finishedAt = Date.now(),
): TranscriptEntry[] {
  const index = findTaskStatusIndex(entries, taskId);
  if (index < 0) return entries;
  return entries.map((entry, entryIndex) => entryIndex === index && entry.kind === "task-status"
    ? {
        ...entry,
        phase,
        compacting: false,
        elapsedMs: entry.recovered
          ? Math.max(0, finishedAt - entry.startedAt)
          : elapsedMs ?? Math.max(0, finishedAt - entry.startedAt),
        summary: summary ? briefThinkingSummary(summary) : entry.summary,
        summaryValues: summary ? undefined : entry.summaryValues,
      }
    : entry);
}

function deduplicateRuntimeEvents(events: RuntimeEvent[]): RuntimeEvent[] {
  const byId = new Map<string, RuntimeEvent>();
  for (const event of events) byId.set(event.event_id, event);
  return Array.from(byId.values());
}

function sortTranscriptEntries(entries: TranscriptEntry[]): TranscriptEntry[] {
  return entries
    .map((entry, index) => ({ entry, index }))
    .sort((left, right) => {
      const leftTime = left.entry.timestamp ? Date.parse(left.entry.timestamp) : Number.MAX_SAFE_INTEGER;
      const rightTime = right.entry.timestamp ? Date.parse(right.entry.timestamp) : Number.MAX_SAFE_INTEGER;
      const safeLeft = Number.isFinite(leftTime) ? leftTime : Number.MAX_SAFE_INTEGER;
      const safeRight = Number.isFinite(rightTime) ? rightTime : Number.MAX_SAFE_INTEGER;
      if (safeLeft !== safeRight) return safeLeft - safeRight;
      const priorityDifference = transcriptEntrySortPriority(left.entry)
        - transcriptEntrySortPriority(right.entry);
      return priorityDifference || left.index - right.index;
    })
    .map(({ entry }) => entry);
}

function transcriptEntrySortPriority(entry: TranscriptEntry): number {
  if (entry.kind === "user") return 0;
  if (entry.kind === "task-status") return 1;
  if (entry.kind === "thinking" || entry.kind === "tool" || entry.kind === "approval" || entry.kind === "plan") {
    return 2;
  }
  return 3;
}

function findLastRuntimeEventIndex(events: RuntimeEvent[], type: RuntimeEvent["type"]): number {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index].type === type) return index;
  }
  return -1;
}

function findTaskStatusIndex(entries: TranscriptEntry[], taskId: string | undefined) {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.kind === "task-status" && (!taskId || entry.taskId === taskId)) return index;
  }
  return -1;
}

function briefThinkingSummary(value: string, t: Translator = (message) => message) {
  const normalized = value.replace(/\s+/g, " ").trim();
  const agentCall = normalized.match(/^\[Agent\s+\d+\/\d+\]\s+calling\s+([^\s{]+)/i);
  if (agentCall) return t("Calling {tool}", { tool: agentCall[1] });
  const toolCompleted = normalized.match(/^\[Tool\s+([^\]]+)\]\s+completed/i);
  if (toolCompleted) return t("{tool} completed. Analyzing the result.", { tool: toolCompleted[1] });
  const bounded = normalized || t("Analyzing the task and preparing the next action.");
  return bounded.length > 140 ? `${bounded.slice(0, 137)}...` : bounded;
}

function formatElapsed(milliseconds: number) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function groupTranscriptEntries(entries: TranscriptEntry[]): TranscriptGroup[] {
  const groups: TranscriptGroup[] = [];
  for (const entry of entries) {
    if (entry.kind === "task-status") {
      groups.push({ id: entry.id, kind: "task-status", entry });
      continue;
    }
    if (entry.kind === "thinking" || entry.kind === "tool" || entry.kind === "approval") {
      const previous = groups[groups.length - 1];
      if (previous?.kind === "activity") {
        previous.entries.push(entry);
      } else {
        groups.push({ id: `activity-${entry.id}`, kind: "activity", entries: [entry] });
      }
      continue;
    }
    if (entry.kind === "plan") {
      groups.push({ id: entry.id, kind: "plan", entry });
      continue;
    }
    groups.push({ id: entry.id, kind: "message", entry });
  }
  return groups;
}

function contextPercent(usage: UsageView) {
  if (usage.context_window <= 0) return 0;
  return Math.min(100, Math.round((usage.last_context_tokens / usage.context_window) * 100));
}

function formatTokens(value: number) {
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  }
  if (value >= 1_000) {
    return `${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}K`;
  }
  return String(value);
}

function formatCost(value: number, currency: string) {
  const digits = value < 0.01 ? 6 : value < 1 ? 4 : 2;
  const amount = value.toFixed(digits);
  if (currency === "USD") return `$${amount}`;
  if (currency === "CNY") return `¥${amount}`;
  return `${amount} ${currency || "USD"}`;
}


function PanelSection({ title, children }: { title: string; children: React.ReactNode }) { return <section className="panel-section"><h2>{title}</h2>{children}</section>; }
function DefinitionRow({ label, value, emphasis = false }: { label: string; value: string; emphasis?: boolean }) { return <div className="definition-row"><span>{label}</span><strong className={emphasis ? "warning-text" : ""}>{value}</strong></div>; }
function StatusRow({ label, status, good = false }: { label: string; status: string; good?: boolean }) { return <div className="status-row"><span><i className={good ? "good" : ""} />{label}</span><strong>{status}</strong></div>; }

export default App;
