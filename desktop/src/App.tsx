/**
 * Desktop composition root.
 *
 * This component renders the shell and translates Runtime JSONL events into presentation
 * state. Durable execution truth remains in the Python Runtime; this file must not infer
 * task completion merely from a missing UI callback.
 */
import {
  ClipboardEvent,
  FormEvent,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  lazy,
  Suspense,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { confirm as confirmDialog, open } from "@tauri-apps/plugin-dialog";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import "./App.css";
import { Settings, X, Trash2 } from "lucide-react";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ToolActivityCard } from "./features/activity/ToolActivityCard";
import {
  SupervisionCenter,
  type SupervisionApproval as SupervisionApprovalView,
  type SupervisionTask as SupervisionTaskView,
} from "./features/supervision/SupervisionCenter";
import {
  reconcileChangedWorkspaceFile,
  type WorkspaceFileReference,
} from "./features/markdown/codeAnswer";
import type { WorkspaceFilePreview } from "./features/filePreview/FilePreviewPanel";
import type {
  AppSettings,
  SettingsSection,
  SettingsSnapshot,
} from "./SettingsPage";
import { DEFAULT_APP_SETTINGS } from "./settingsDefaults";
import { TranscriptWindow } from "./features/transcript/TranscriptWindow";
import { AssistantDeltaBuffer } from "./runtime/assistantDeltaBuffer";
import { translate, translateDiagnosticRuntimeText, translateRuntimeText, type Language, type TranslationValues, type Translator } from "./i18n";
import {
  INITIAL_FRONTEND_RUNTIME_STATE,
  selectActiveTaskCancelling,
  selectActiveTaskId,
  selectConversationReplaying,
  selectStaleProjectTaskRoutes,
  reconcileReplayedTask,
  shouldOpenActivatedWorkspace,
  transitionFrontendRuntime,
  type FrontendRuntimeAction,
} from "./runtime/frontendRuntimeMachine";
import {
  RuntimeClient,
  RuntimeClientError,
  RuntimeRequestError,
} from "./runtime/runtimeClient";
import { selectActiveTasks, selectTaskForSession, SupervisionStore } from "./runtime/supervisionStore";
import { sessionDraftStore } from "./runtime/sessionDraftStore";
import { useSmartTranscript } from "./hooks/useSmartTranscript";
import { useWorkspaceLayout } from "./hooks/useWorkspaceLayout";
import { WORKSPACE_LAYOUT_LIMITS, type WorkspaceColumn } from "./runtime/workspaceLayout";
import { orderTerminalTaskSummaries } from "./runtime/transcriptOrder";
import {
  mergeRuntimeReplayLanes,
  partitionRuntimeReplay,
  type ActiveTaskRegistration,
} from "./runtime/eventProjector";
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
  type RuntimeRequestDataMap,
  type RuntimeResponseDataMap,
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
  type BrowserSnapshot,
  type DiagnosticsSnapshot,
  type FileChangePreview,
  type TaskChangeSet,
  type TaskDiffResult,
  type UsageSnapshot,
} from "./protocol/runtimeEvents";

const ReviewChangesWorkbench = lazy(async () => {
  const module = await import("./features/review/ReviewChangesWorkbench");
  return { default: module.ReviewChangesWorkbench };
});
const SettingsPage = lazy(() => import("./SettingsPage").then((module) => ({ default: module.SettingsPage })));
const MarkdownContent = lazy(() => import("./features/markdown/MarkdownContent").then((module) => ({ default: module.MarkdownContent })));
const FilePreviewPanel = lazy(async () => {
  const module = await import("./features/filePreview/FilePreviewPanel");
  return { default: module.FilePreviewPanel };
});

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
  taskId?: string;
  name: string;
  detail: string;
  arguments?: Record<string, unknown>;
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
  streamText?: string;
  streaming?: boolean;
};
type PlanTranscriptEntry = {
  id: string;
  kind: "plan";
  taskId: string;
  goal: string;
  summary?: string;
  executionOrder: string[];
  steps: PlanStepView[];
  planning?: boolean;
  planningText?: string;
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
type TeamAgentStatus = "queued" | "working" | "completed" | "failed";
type TeamDialogueItem = {
  id: string;
  kind: "message" | "tool";
  timestamp?: string;
  direction?: "inbound" | "outbound";
  messageKind?: string;
  content?: string;
  toolName?: string;
  toolArguments?: Record<string, unknown>;
  toolStatus?: ToolStatus;
  elapsed?: number;
  streaming?: boolean;
};
type TeamAgentDialogue = {
  id: string;
  name: string;
  role: string;
  teamTaskId: string;
  status: TeamAgentStatus;
  items: TeamDialogueItem[];
};
type TeamTranscriptEntry = {
  id: string;
  kind: "team";
  taskId: string;
  runId: string;
  workerCount: number;
  phase: "running" | "completed" | "failed";
  message?: string;
  agents: TeamAgentDialogue[];
  timestamp?: string;
};
type TranscriptEntry = TextTranscriptEntry | ToolTranscriptEntry | ApprovalTranscriptEntry | PlanTranscriptEntry | TaskStatusTranscriptEntry | TeamTranscriptEntry;
type ActivityEntry = Extract<TranscriptEntry, { kind: "thinking" | "tool" | "approval" }>;
type SidebarActivityEntry = ActivityEntry | PlanTranscriptEntry | TeamTranscriptEntry;
type MessageEntry = Exclude<TranscriptEntry, ActivityEntry | PlanTranscriptEntry | TaskStatusTranscriptEntry | TeamTranscriptEntry>;
type TranscriptGroup =
  | { id: string; kind: "activity"; entries: ActivityEntry[] }
  | { id: string; kind: "plan"; entry: PlanTranscriptEntry }
  | { id: string; kind: "task-status"; entry: TaskStatusTranscriptEntry }
  | { id: string; kind: "team"; entry: TeamTranscriptEntry }
  | { id: string; kind: "message"; entry: MessageEntry };

type FilePreviewTab = {
  id: string;
  relativePath: string;
  fileName: string;
  preview: WorkspaceFilePreview | null;
  loading: boolean;
  error: string;
};

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

type PendingWorkspaceActivation = {
  targetProjectId: string;
  targetWorkspace: string;
  previous: {
    projectId: string;
    workspace: string;
    conversationId: string;
  } | null;
};

type WorkspaceActivationIntent = {
  project: ProjectRecord;
  forceRestart: boolean;
  conversationToOpen: string;
};

type PendingTaskSubmission = {
  sessionId: string;
  projectId: string;
  promptPreview: string;
  startedAt: string;
  draft: {
    prompt: string;
    attachments: RuntimeAttachment[];
  };
};

type ProjectRecoveryTask = RuntimeRecoveryTask & {
  project_id: string;
};

type RuntimeProjectRestoreTarget = {
  projectId: string;
  workspace: string;
};

type UsageView = UsageSnapshot & {
  task_input_tokens: number;
  task_output_tokens: number;
  task_cached_input_tokens: number;
  task_reasoning_tokens: number;
  task_llm_calls: number;
  task_estimated_cost: number;
  task_priced_llm_calls: number;
};

type MemoryExtractionView = {
  jobId: string;
  status: "starting" | "extracting" | "completed" | "failed";
  pendingMessageCount: number;
  processedMessageCount: number;
  factCount: number;
  savedCount: number;
  ignoredCount: number;
  userMemoryCount: number;
  conversationMemoryCount: number;
  error?: string;
};

type ManagementRequestType = Extract<
  RuntimeRequestType,
  | `mcp.${string}`
  | `rag.${string}`
  | `memory.${string}`
  | `skill.${string}`
  | `browser.${string}`
  | `diagnostics.${string}`
  | `prompt.${string}`
  | "task.diff"
  | "task.rollback"
>;

const UNCERTAIN_SUBMISSION_ERROR_CODES = new Set([
  "request_timeout",
  "runtime_transport_error",
  "client_disposed",
  "request_aborted",
  "request_superseded",
  "project_switched",
]);

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
  scope: "user",
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
  bundled_count: 0,
  updates_available: 0,
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
  "team.run.started",
  "team.run.completed",
  "team.run.failed",
  "team.agent.status",
  "team.agent.message",
  "team.agent.tool.started",
  "team.agent.tool.completed",
  "history.compaction.started",
  "history.compaction.finished",
  "memory.extraction.started",
  "memory.extraction.completed",
  "memory.extraction.failed",
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

function emptyMemoryExtractionView(): MemoryExtractionView {
  return {
    jobId: "",
    status: "starting",
    pendingMessageCount: 0,
    processedMessageCount: 0,
    factCount: 0,
    savedCount: 0,
    ignoredCount: 0,
    userMemoryCount: 0,
    conversationMemoryCount: 0,
  };
}

/** Coordinates project/session UI, Runtime RPC, replay barriers, and transient overlays. */
function App() {
  const runtimeClient = useMemo(() => new RuntimeClient(
    (message) => invoke("runtime_send", { message }),
    { defaultTimeoutMs: 60_000 },
  ), []);
  const supervisionStore = useMemo(() => new SupervisionStore(), []);
  const workspaceLayout = useWorkspaceLayout();
  const supervisionState = useSyncExternalStore(
    supervisionStore.subscribe,
    supervisionStore.getSnapshot,
    supervisionStore.getSnapshot,
  );
  const [mode, setMode] = useState<AgentMode>("react");
  const [runtimeControl, setRuntimeControl] = useState(INITIAL_FRONTEND_RUNTIME_STATE);
  const runtimeControlRef = useRef(INITIAL_FRONTEND_RUNTIME_STATE);
  const [sessionId, setSessionId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [model, setModel] = useState("not initialized");
  const [prompt, setPrompt] = useState("");
  const [referenceMatch, setReferenceMatch] = useState<ComposerReferenceMatch | null>(null);
  const [referenceSelection, setReferenceSelection] = useState(0);
  const [entries, setEntries] = useState<TranscriptEntry[]>([]);
  const [approvalQueue, setApprovalQueue] = useState<ApprovalEvent[]>([]);
  const [approvalDecisions, setApprovalDecisions] = useState<Record<string, "approve" | "reject" | "skip">>({});
  const [supervisionOpen, setSupervisionOpen] = useState(false);
  const [supervisionError, setSupervisionError] = useState("");
  const [taskActionPending, setTaskActionPending] = useState<Record<string, "open" | "stop" | "trace">>({});
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
  const [memoryExtractions, setMemoryExtractions] = useState<Record<string, MemoryExtractionView>>({});
  const [skillSnapshot, setSkillSnapshot] = useState<SkillSnapshot>(EMPTY_SKILL_SNAPSHOT);
  const [browserSnapshot, setBrowserSnapshot] = useState<BrowserSnapshot>(EMPTY_BROWSER_SNAPSHOT);
  const [diagnosticsSnapshot, setDiagnosticsSnapshot] = useState<DiagnosticsSnapshot>(EMPTY_DIAGNOSTICS_SNAPSHOT);
  const [timerNow, setTimerNow] = useState(() => Date.now());
  const [rollbackBusyTaskId, setRollbackBusyTaskId] = useState("");
  const [taskDiff, setTaskDiff] = useState<TaskDiffResult | null>(null);
  const [taskDiffSelectedPath, setTaskDiffSelectedPath] = useState("");
  const [taskDiffLoading, setTaskDiffLoading] = useState("");
  const [filePreviewTabs, setFilePreviewTabs] = useState<FilePreviewTab[]>([]);
  const [activeFilePreviewId, setActiveFilePreviewId] = useState("");
  const [rightSidebarView, setRightSidebarView] = useState<"context" | "activity" | "changes" | "file">("context");
  const appSettingsRef = useRef<AppSettings>(DEFAULT_APP_SETTINGS);
  const languageRef = useRef<Language>(DEFAULT_APP_SETTINGS.general.language);
  const promptRef = useRef("");
  const composerTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const composerHighlightRef = useRef<HTMLDivElement | null>(null);
  const filePreviewRequestsRef = useRef(new Map<string, string>());
  const projectActionBusyRef = useRef(false);
  const queuedWorkspaceActivation = useRef<WorkspaceActivationIntent | null>(null);

  function dispatchRuntime(action: FrontendRuntimeAction) {
    const next = transitionFrontendRuntime(runtimeControlRef.current, action);
    runtimeControlRef.current = next;
    setRuntimeControl(next);
  }

  const connection = runtimeControl.connection;
  const projectedRunningTasks = useMemo(() => Object.fromEntries(
    selectActiveTasks(supervisionState).map((task) => [task.sessionId, task.id]),
  ), [supervisionState]);
  const runningTasks = { ...projectedRunningTasks, ...runtimeControl.tasksBySession };
  const activeTaskId = selectActiveTaskId(runtimeControl)
    || projectedRunningTasks[runtimeControl.activeSessionId]
    || "";
  const busy = Boolean(
    activeTaskId
    || runtimeControl.activeSessionId
      && runtimeControl.submittingSessions[runtimeControl.activeSessionId],
  );
  const cancelling = selectActiveTaskCancelling(runtimeControl);
  const replayingConversation = selectConversationReplaying(runtimeControl);
  const activeMemoryExtraction = sessionId ? memoryExtractions[sessionId] : undefined;
  const memoryExtracting = activeMemoryExtraction?.status === "starting"
    || activeMemoryExtraction?.status === "extracting";

  function tx(message: string, values?: TranslationValues) {
    return translate(languageRef.current, message, values);
  }
  const t: Translator = useMemo(() => (message, values) => translate(appSettings.general.language, message, values), [appSettings.general.language]);

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
  const sessionDeleteTargetId = useRef("");
  const pendingConversationOpen = useRef<{ projectId: string; conversationId: string } | null>(null);
  const pendingWorkspaceActivation = useRef<PendingWorkspaceActivation | null>(null);
  const accessModeRequest = useRef("");
  const traceModeRequest = useRef("");
  const taskSubmitSessions = useRef(new Map<string, PendingTaskSubmission>());
  const taskRecoveryRequest = useRef("");
  const eventReplayRequest = useRef("");
  const approvalToolCalls = useRef(new Map<string, string>());
  const approvalQueueRef = useRef<ApprovalEvent[]>([]);
  const attachmentsRef = useRef<RuntimeAttachment[]>([]);
  const canAttachRef = useRef(false);
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
  const pendingRecovery = useRef<ProjectRecoveryTask | null>(null);
  const recoveryQueue = useRef<ProjectRecoveryTask[]>([]);
  const loadedProjectWorkspaces = useRef(new Map<string, string>());
  const restartProjectQueue = useRef<RuntimeProjectRestoreTarget[]>([]);
  const runtimeRestoreInProgress = useRef<number | null>(null);
  const runtimeRestoreEpoch = useRef(0);
  const projectsRef = useRef<ProjectRecord[]>([]);
  const lastEventSequence = useRef(new Map<string, number>());
  const seenEventIds = useRef(new Set<string>());
  const replayBarrier = useRef<ReplayBarrier | null>(null);
  const liveRuntimeEventHandler = useRef<(event: RuntimeEvent) => void>(() => undefined);
  const legacyRuntimeResponseHandler = useRef<(response: RuntimeResponse) => void>(() => undefined);
  // Last authoritative workspace.open active-task snapshot per project.  The
  // journal can be truncated or lag behind live events, so replay is always
  // reconciled against this control-plane truth before exposing global state.
  const authoritativeActiveTasks = useRef(new Map<string, ActiveTaskRegistration[]>());
  projectsRef.current = projects;

  const activeProject = useMemo(
    () => projects.find((project) => project.id === activeProjectId) ?? null,
    [activeProjectId, projects],
  );
  const activeConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === activeConversationId) ?? null,
    [activeConversationId, conversations],
  );
  const supervisionViews = useMemo(() => {
    const language = appSettings.general.language;
    const resolveContext = (session: string, projectedProjectId: string) => {
      const meta = supervisionState.sessions[session]?.meta;
      const projectId = projectedProjectId
        || meta?.projectId
        || sessionProjectIds.current.get(session)
        || "";
      const project = projects.find((candidate) => candidate.id === projectId);
      const projectConversations = projectId === activeProjectId
        ? conversations
        : conversationCache[projectId] ?? [];
      const conversation = projectConversations.find((candidate) => candidate.id === session);
      return {
        projectId,
        projectName: project?.name ?? (projectId || translate(language, "Unknown project")),
        conversationTitle: conversation?.title ?? meta?.title ?? translate(language, "Unknown conversation"),
        cwd: meta?.workspace ?? project?.path ?? "",
        tracePath: conversation?.trace_path ?? "",
      };
    };

    const sortedTasks = Object.values(supervisionState.tasks)
      .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt));
    const activeTaskIds = new Set(selectActiveTasks(supervisionState).map((task) => task.id));
    // Never let terminal history evict a long-running background task from the
    // global Center. Active work is unbounded; only completed history is capped.
    const tasksForCenter = [
      ...sortedTasks.filter((task) => activeTaskIds.has(task.id)),
      ...sortedTasks.filter((task) => !activeTaskIds.has(task.id)).slice(0, 100),
    ];
    const tasks = tasksForCenter
      .map((task): SupervisionTaskView => {
        const context = resolveContext(task.sessionId, task.projectId);
        const taskUsage = task.usage;
        return {
          id: task.id,
          projectId: context.projectId,
          projectName: context.projectName,
          sessionId: task.sessionId,
          conversationTitle: context.conversationTitle,
          status: task.status,
          activity: localizeSupervisionActivity(language, task.activity),
          startedAt: task.startedAt,
          elapsedMs: task.elapsedMs,
          mode: task.mode,
          usage: taskUsage ? {
            inputTokens: taskUsage.task_input_tokens,
            outputTokens: taskUsage.task_output_tokens,
            cachedInputTokens: taskUsage.task_cached_input_tokens,
            reasoningTokens: taskUsage.task_reasoning_tokens,
            estimatedCost: taskUsage.task_estimated_cost,
            currency: taskUsage.currency,
            costEstimated: taskUsage.cost_estimated,
          } : undefined,
          failureMessage: task.errorMessage
            ? translate(language, translateRuntimeText(language, task.errorMessage))
            : undefined,
          traceAvailable: Boolean(context.tracePath),
          stoppable: task.submissionUncertain !== true,
          actionPending: taskActionPending[task.id] ?? null,
        };
      });
    const projectedApprovals = Object.values(supervisionState.approvals)
      .filter((item) => item.status === "pending" || item.status === "resolving")
      .sort((left, right) => Date.parse(left.requestedAt) - Date.parse(right.requestedAt));
    const approvals = projectedApprovals.map((item, index): SupervisionApprovalView => {
      const context = resolveContext(item.sessionId, item.projectId);
      const pendingDecision = approvalDecisions[item.id];
      return {
        id: item.id,
        taskId: item.taskId,
        projectId: context.projectId,
        projectName: context.projectName,
        sessionId: item.sessionId,
        conversationTitle: context.conversationTitle,
        cwd: context.cwd,
        toolName: item.toolName,
        arguments: item.arguments,
        changePreview: item.changePreview,
        riskLevel: item.dangerLevel,
        riskDescription: translate(language, translateRuntimeText(language, item.riskDescription)),
        queuePosition: index + 1,
        queueTotal: projectedApprovals.length,
        requestedAt: item.requestedAt,
        resolving: pendingDecision === "approve" || pendingDecision === "reject"
          ? pendingDecision
          : null,
      };
    });
    return { tasks, approvals };
  }, [
    activeProjectId,
    appSettings.general.language,
    approvalDecisions,
    conversationCache,
    conversations,
    projects,
    supervisionState,
    taskActionPending,
  ]);
  const transcriptGroups = useMemo(() => groupTranscriptEntries(entries), [entries]);
  const sidebarActivityEntries = useMemo(() => entries.filter(
    (entry): entry is SidebarActivityEntry => isSidebarActivityEntry(entry),
  ), [entries]);
  const activeFilePreviewTab = useMemo(
    () => filePreviewTabs.find((tab) => tab.id === activeFilePreviewId) ?? null,
    [activeFilePreviewId, filePreviewTabs],
  );
  const activeFilePreviewTabIndex = activeFilePreviewTab
    ? filePreviewTabs.findIndex((tab) => tab.id === activeFilePreviewTab.id)
    : -1;
  const taskStatusById = useMemo(() => new Map(entries
    .filter((entry): entry is TaskStatusTranscriptEntry => entry.kind === "task-status")
    .map((entry) => [entry.taskId, entry])), [entries]);
  const reviewedTaskEntry = taskDiff
    ? entries.find((entry): entry is TaskStatusTranscriptEntry => entry.kind === "task-status" && entry.taskId === taskDiff.task_id) ?? null
    : null;
  useEffect(() => {
    if (!taskDiff) {
      setTaskDiffSelectedPath("");
      setRightSidebarView((current) => current === "changes" ? "context" : current);
    }
  }, [taskDiff]);
  useEffect(() => {
    filePreviewRequestsRef.current.clear();
    setFilePreviewTabs([]);
    setActiveFilePreviewId("");
    setRightSidebarView((current) => current === "file" ? "context" : current);
  }, [activeProjectId]);
  const {
    containerRef: transcriptRef,
    isFollowingBottom,
    unreadOutputCount,
    onScroll: handleTranscriptScroll,
    jumpToBottom,
  } = useSmartTranscript<HTMLDivElement>({ contentVersion: entries });

  useEffect(() => {
    // A newly selected conversation starts at its latest event. Subsequent
    // output only follows while the reader remains near the bottom.
    const frame = window.requestAnimationFrame(() => jumpToBottom("auto"));
    return () => window.cancelAnimationFrame(frame);
  }, [activeConversationId, jumpToBottom]);
  const activeProjectedTask = selectTaskForSession(supervisionState, sessionId);
  const activeTaskFinalizing = activeProjectedTask?.status === "finalizing" || entries.some((entry) => (
    entry.kind === "task-status"
    && entry.taskId === activeTaskId
    && entry.protection?.status === "finalize_pending"
  ));
  // Inline approvals belong only to the visible conversation. Background
  // approvals stay actionable in the global Approval Center with their full
  // project/session context, avoiding cross-project misattribution here.
  const approval = approvalQueue.find((item) => item.session_id === activeConversationId) ?? null;
  const resolvingApprovalId = approval && approvalDecisions[approval.data.approval_id]
    ? approval.data.approval_id
    : "";
  const ragIndexing = ragSnapshot.status === "indexing";
  const diagnosticsRunning = diagnosticsSnapshot.status === "running";
  const anyTaskRunning = Object.keys(runningTasks).length > 0;
  const activeProjectTaskRunning = Object.keys(runningTasks).some(
    (conversationId) => sessionProjectIds.current.get(conversationId) === activeProjectId,
  );
  const runtimeMutationBusy = busy || ragIndexing || diagnosticsRunning || replayingConversation || Boolean(rollbackBusyTaskId);
  canAttachRef.current = Boolean(sessionId && !runtimeMutationBusy);
  approvalQueueRef.current = approvalQueue;

  function registerRunningTask(taskId: string, taskSessionId: string) {
    if (!taskId || !taskSessionId) return;
    const projected = supervisionStore.getSnapshot().tasks[taskId];
    if (projected && (
      projected.status === "completed"
      || projected.status === "failed"
      || projected.status === "cancelled"
    )) return;
    if (!sessionProjectIds.current.has(taskSessionId) && projectIdRef.current) {
      sessionProjectIds.current.set(taskSessionId, projectIdRef.current);
    }
    const taskProjectId = sessionProjectIds.current.get(taskSessionId) ?? projectIdRef.current;
    if (taskProjectId) {
      const registrations = authoritativeActiveTasks.current.get(taskProjectId) ?? [];
      if (!registrations.some((item) => item.taskId === taskId)) {
        authoritativeActiveTasks.current.set(taskProjectId, [
          ...registrations,
          { taskId, sessionId: taskSessionId, projectId: taskProjectId },
        ]);
      }
    }
    dispatchRuntime({ type: "task.registered", taskId, sessionId: taskSessionId });
  }

  function releaseRunningTask(taskId: string | undefined, taskSessionId: string) {
    const state = runtimeControlRef.current;
    const resolvedSessionId = taskSessionId || (taskId ? state.sessionsByTask[taskId] ?? "" : "");
    if (taskId) {
      recoveryQueue.current = recoveryQueue.current.filter(
        (item) => item.task_id !== taskId,
      );
      if (pendingRecovery.current?.task_id === taskId) pendingRecovery.current = null;
    }
    if (taskId) clearApprovalsForTask(taskId);
    if (taskId) {
      const taskProjectId = sessionProjectIds.current.get(resolvedSessionId);
      if (taskProjectId) {
        authoritativeActiveTasks.current.set(
          taskProjectId,
          (authoritativeActiveTasks.current.get(taskProjectId) ?? [])
            .filter((item) => item.taskId !== taskId),
        );
      }
    }
    dispatchRuntime({ type: "task.released", taskId, sessionId: resolvedSessionId });
  }

  function clearApprovalsForTask(taskId: string) {
    const current = approvalQueueRef.current;
    const removedIds = new Set(current
      .filter((item) => item.task_id === taskId)
      .map((item) => item.data.approval_id));
    if (removedIds.size === 0) return;
    for (const approvalId of removedIds) approvalToolCalls.current.delete(approvalId);
    setApprovalDecisions((current) => Object.fromEntries(
      Object.entries(current).filter(([approvalId]) => !removedIds.has(approvalId)),
    ));
    const next = current.filter((item) => !removedIds.has(item.data.approval_id));
    approvalQueueRef.current = next;
    setApprovalQueue(next);
  }

  function detachReplayBarrier(barrier: ReplayBarrier | null) {
    if (!barrier) return [];
    if (replayBarrier.current === barrier) replayBarrier.current = null;
    const buffered = deduplicateRuntimeEvents(barrier.buffered)
      .sort((left, right) => left.sequence - right.sequence);
    barrier.buffered = [];
    return buffered;
  }

  function flushReplayBarrier(barrier: ReplayBarrier | null) {
    const buffered = detachReplayBarrier(barrier);
    // Re-enter the normal live path only after detaching the barrier; otherwise
    // these events would immediately buffer themselves again.
    for (const event of buffered) liveRuntimeEventHandler.current(event);
    return buffered;
  }

  function runtimeRequestScope<T extends RuntimeRequestType>(
    method: T,
    params: RuntimeRequestDataMap[T],
  ) {
    const values = params as Record<string, unknown>;
    const requestSessionId = String(values.session_id ?? "");
    const requestTaskId = String(values.task_id ?? "");
    const requestProjectId = String(values.project_id ?? "") || (requestSessionId
      ? sessionProjectIds.current.get(requestSessionId) ?? projectIdRef.current
      : projectIdRef.current);
    if (method === "workspace.open") return "workspace:open";
    if (method === "workspace.close") return "workspace:close";
    if (method === "runtime.ping" || method === "runtime.shutdown") return `runtime:${method}`;
    if (method.startsWith("task.")) {
      return `task:${requestTaskId || requestSessionId}:${method}`;
    }
    if (method === "approval.resolve") {
      return `approval:${String(values.approval_id ?? requestTaskId)}:${method}`;
    }
    if (requestSessionId) {
      return `project:${requestProjectId}:session:${requestSessionId}:${method}`;
    }
    return `project:${requestProjectId}:${method}`;
  }

  function runtimeRequestTimeout(method: RuntimeRequestType) {
    if (method === "workspace.open" || method === "task.recover") return 120_000;
    if (method === "event.replay") return 60_000;
    if (method === "task.submit") return 45_000;
    return 30_000;
  }

  /**
   * Compatibility projection for the remaining monolithic response reducer.
   * RuntimeClient exclusively owns correlation, timeout and scope cancellation;
   * the reducer receives an already-settled synthetic envelope exactly once.
   */
  async function sendRequest<T extends RuntimeRequestType>(
    method: T,
    params: RuntimeRequestDataMap[T],
    id = requestId(method.replace(/\./g, "-")),
  ): Promise<RuntimeResponse> {
    let response: RuntimeResponse;
    try {
      const values = params as Record<string, unknown>;
      const requestSessionId = String(values.session_id ?? "");
      const requestProjectId = String(values.project_id ?? "") || (requestSessionId
        ? sessionProjectIds.current.get(requestSessionId) ?? projectIdRef.current
        : projectIdRef.current);
      const routedParams = (
        method === "runtime.ping" || method === "runtime.shutdown"
          ? params
          : { ...params, project_id: requestProjectId }
      ) as RuntimeRequestDataMap[T];
      const result = await runtimeClient.request(method, routedParams, {
        requestId: id,
        scope: runtimeRequestScope(method, routedParams),
        supersede: method !== "task.submit" && method !== "task.recover",
        timeoutMs: runtimeRequestTimeout(method),
      });
      response = {
        kind: "response",
        protocol_version: RUNTIME_PROTOCOL_VERSION,
        request_id: id,
        ok: true,
        result: result as unknown as Record<string, unknown>,
      };
    } catch (error) {
      response = error instanceof RuntimeRequestError
        ? error.response
        : {
          kind: "response",
          protocol_version: RUNTIME_PROTOCOL_VERSION,
          request_id: id,
          ok: false,
          error: {
            code: error instanceof RuntimeClientError ? error.code : "runtime_transport_error",
            message: error instanceof Error ? error.message : String(error),
          },
        };
    }
    legacyRuntimeResponseHandler.current(response);
    return response;
  }

  function syncActiveConversationTask(conversationId: string) {
    dispatchRuntime({ type: "conversation.activated", sessionId: conversationId });
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
    const assistantDeltas = new AssistantDeltaBuffer((batch) => {
      const currentSession = activeConversationIdRef.current;
      const currentPid = activeRuntimePid.current;
      const accepted = batch.filter((event) => event.session_id === currentSession
        && (event.runtime_pid === undefined || event.runtime_pid === currentPid));
      if (disposed || !accepted.length) return;
      setEntries((current) => accepted.reduce((result, event) => applyAssistantDelta(
        result, event.task_id, event.event_id, event.data.text,
        event.data.reset === true, event.timestamp,
      ), current));
    });

    function captureRuntimeRestoreTargets() {
      const projectIds = new Set(
        selectActiveTasks(supervisionStore.getSnapshot())
          .map((task) => task.projectId || supervisionStore.getSnapshot().sessions[task.sessionId]?.meta.projectId || "")
          .filter(Boolean),
      );
      if (projectIdRef.current) projectIds.add(projectIdRef.current);
      const targets: RuntimeProjectRestoreTarget[] = [];
      for (const projectId of projectIds) {
        const workspace = loadedProjectWorkspaces.current.get(projectId)
          || projectsRef.current.find((project) => project.id === projectId)?.path
          || (projectId === projectIdRef.current ? workspaceRef.current : "");
        if (workspace) targets.push({ projectId, workspace });
      }
      return targets;
    }

    function reconcileProjectRuntimeState(
      projectId: string,
      result: RuntimeResponseDataMap["workspace.open"],
    ) {
      const resolvedProjectId = String(result.project_id || projectId);
      const resolvedWorkspace = String(result.workspace || loadedProjectWorkspaces.current.get(resolvedProjectId) || "");
      if (resolvedWorkspace) loadedProjectWorkspaces.current.set(resolvedProjectId, resolvedWorkspace);
      const recoveries = (
        result.recoveries as RuntimeRecoveryTask[] | undefined
      ) ?? (result.recovery ? [result.recovery] : []);
      const projectRecoveries: ProjectRecoveryTask[] = recoveries.map((recovery) => ({
        ...recovery,
        project_id: resolvedProjectId,
      }));
      const activeTasks = result.active_tasks ?? [];
      const registrationsById = new Map<string, ActiveTaskRegistration>();
      for (const task of activeTasks) {
        const taskId = String(task.task_id || "");
        const taskSessionId = String(task.session_id || "");
        if (!taskId || !taskSessionId) continue;
        registrationsById.set(taskId, {
          taskId,
          sessionId: taskSessionId,
          projectId: resolvedProjectId,
          phase: task.phase,
        });
      }
      for (const recovery of projectRecoveries) {
        registrationsById.set(recovery.task_id, {
          taskId: recovery.task_id,
          sessionId: recovery.session_id,
          projectId: resolvedProjectId,
          phase: recovery.status === "finalize_pending" ? "finalizing" : "recovering",
        });
      }
      const projectedTasks = supervisionStore.getSnapshot().tasks;
      const registrations = [...registrationsById.values()].filter((task) => {
        sessionProjectIds.current.set(task.sessionId, resolvedProjectId);
        const projected = projectedTasks[task.taskId];
        return !projected || !(
          projected.status === "completed"
          || projected.status === "failed"
          || projected.status === "cancelled"
        );
      });
      const authoritativeTaskIds = new Set(registrations.map((task) => task.taskId));
      for (const stale of selectStaleProjectTaskRoutes(
        runtimeControlRef.current,
        sessionProjectIds.current,
        resolvedProjectId,
        authoritativeTaskIds,
      )) {
        releaseRunningTask(stale.taskId, stale.sessionId);
      }
      authoritativeActiveTasks.current.set(resolvedProjectId, registrations);
      supervisionStore.reconcileProjectActiveTasks(resolvedProjectId, registrations);
      for (const task of registrations) registerRunningTask(task.taskId, task.sessionId);
      recoveryQueue.current = [
        ...recoveryQueue.current.filter((item) => item.project_id !== resolvedProjectId),
        ...projectRecoveries,
      ];
      if (
        pendingRecovery.current?.project_id === resolvedProjectId
        && !registrations.some((item) => item.taskId === pendingRecovery.current?.task_id)
      ) {
        pendingRecovery.current = null;
      }
      return projectRecoveries;
    }

    async function restoreProjectsAfterRuntimeReady() {
      const epoch = runtimeRestoreEpoch.current;
      if (runtimeRestoreInProgress.current === epoch || disposed) return;
      runtimeRestoreInProgress.current = epoch;
      const currentTarget = {
        projectId: projectIdRef.current,
        workspace: workspaceRef.current,
      };
      const queuedTargets = restartProjectQueue.current;
      const byProject = new Map<string, RuntimeProjectRestoreTarget>();
      for (const target of queuedTargets) byProject.set(target.projectId, target);
      if (currentTarget.projectId && currentTarget.workspace) {
        byProject.set(currentTarget.projectId, currentTarget);
      }
      const current = byProject.get(currentTarget.projectId);
      const background = [...byProject.values()].filter(
        (target) => target.projectId !== currentTarget.projectId,
      );
      let currentOpened = false;
      try {
        if (current && shouldOpenActivatedWorkspace({
          connection: runtimeControlRef.current.connection,
          activeProjectId: projectIdRef.current,
          targetProjectId: current.projectId,
          pendingActivationProjectId: pendingWorkspaceActivation.current?.targetProjectId,
          workspaceOpenRequestId: workspaceOpenRequest.current,
        })) {
          const id = requestId("workspace-open");
          workspaceOpenRequest.current = id;
          const response = await sendRequest(
            "workspace.open",
            { project_id: current.projectId, workspace: current.workspace },
            id,
          );
          currentOpened = response.ok;
        }
        for (const target of background) {
          if (disposed || epoch !== runtimeRestoreEpoch.current) return;
          try {
            const result = await runtimeClient.request(
              "workspace.open",
              { project_id: target.projectId, workspace: target.workspace },
              {
                scope: `runtime-restore:${epoch}:${target.projectId}`,
                supersede: false,
                timeoutMs: 120_000,
              },
            );
            reconcileProjectRuntimeState(target.projectId, result);
            requestPendingTaskRecovery();
          } catch (error) {
            authoritativeActiveTasks.current.set(target.projectId, []);
            supervisionStore.reconcileProjectActiveTasks(target.projectId, []);
            recoveryQueue.current = recoveryQueue.current.filter(
              (item) => item.project_id !== target.projectId,
            );
            console.warn(`Unable to restore background project ${target.projectId}: ${String(error)}`);
          }
        }
        // workspace.open maintains a legacy active-project alias for older
        // embedded integrations. Re-pin it to the visible project after all
        // explicit background restores so fallback-only code cannot drift.
        if (currentOpened && current && background.length > 0) {
          const result = await runtimeClient.request(
            "workspace.open",
            { project_id: current.projectId, workspace: current.workspace },
            {
              scope: `runtime-restore:${epoch}:active-project`,
              supersede: false,
              timeoutMs: 120_000,
            },
          );
          reconcileProjectRuntimeState(current.projectId, result);
        }
      } catch (error) {
        console.warn(`Runtime project restoration did not fully complete: ${String(error)}`);
      } finally {
        if (epoch === runtimeRestoreEpoch.current) restartProjectQueue.current = [];
        if (runtimeRestoreInProgress.current === epoch) {
          runtimeRestoreInProgress.current = null;
        }
        requestPendingTaskRecovery();
      }
    }

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
          retiredRuntimePids.current.delete(started.pid);
          runtimeStarted.current = true;
          setRuntimePython(started.python);
          console.info(tx("Runtime restarted after {count} attempt(s).", { count: attempt }));
        } catch (error) {
          console.warn(
            `${tx("Runtime restart attempt {count} failed.", { count: attempt })} ${String(error)}`,
          );
          if (attempt >= 6) {
            dispatchRuntime({ type: "connection.changed", connection: "error" });
            dispatchRuntime({ type: "tasks.cleared" });
            authoritativeActiveTasks.current.clear();
            supervisionStore.markRuntimeDisconnected(true);
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
        if (!disposed) console.info(payload);
      });
      const unlistenError = await listen<string>("runtime-transport-error", ({ payload }) => {
        if (!disposed) console.error(`Runtime transport: ${payload}`);
      });
      const unlistenExit = await listen<number>("runtime-exited", ({ payload }) => {
        if (!disposed && payload === activeRuntimePid.current) {
          const interruptedTaskId = selectActiveTaskId(runtimeControlRef.current);
          restartProjectQueue.current = captureRuntimeRestoreTargets();
          runtimeRestoreEpoch.current += 1;
          flushReplayBarrier(replayBarrier.current);
          retireRuntimePid(payload);
          activeRuntimePid.current = null;
          runtimeStarted.current = false;
          taskRecoveryRequest.current = "";
          recoveryQueue.current = [];
          eventReplayRequest.current = "";
          dispatchRuntime({ type: "replay.finished" });
          workspaceOpenRequest.current = "";
          sessionListRequest.current = "";
          sessionCreateRequest.current = "";
          sessionOpenRequest.current = "";
          sessionRenameRequest.current = "";
          sessionDeleteRequest.current = "";
          sessionDeleteTargetId.current = "";
          accessModeRequest.current = "";
          traceModeRequest.current = "";
          dispatchRuntime({ type: "tasks.cleared" });
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
          if (interruptedTaskId) {
            setEntries((current) => updateTaskStatus(current, interruptedTaskId, {
              summary: "Runtime was interrupted. Restarting and recovering the task.",
              summaryValues: undefined,
              compacting: false,
            }));
          }
          setRollbackBusyTaskId("");
          setTaskDiff(null);
          setTaskDiffLoading("");
          setApprovalQueue([]);
          setApprovalDecisions({});
          approvalToolCalls.current.clear();
          supervisionStore.markRuntimeDisconnected(false);
          rejectAllRuntimeRequests(tx("Python Runtime exited before the request completed."));
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
          dispatchRuntime({ type: "connection.changed", connection: "restarting" });
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
          dispatchRuntime({ type: "connection.changed", connection: "offline" });
        }
      } catch (error) {
        if (!disposed) {
          dispatchRuntime({ type: "connection.changed", connection: "error" });
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
        {
          project_id: recovery.project_id,
          session_id: recovery.session_id,
          task_id: recovery.task_id,
        },
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
        flushReplayBarrier(barrier);
        dispatchRuntime({ type: "replay.finished", sessionId: barrier.sessionId });
        reportError(error);
        requestPendingTaskRecovery();
      });
    }

    function handleRuntimeMessage(message: RuntimeMessage, replaying = false) {
      // Flush text before lifecycle/approval/response events so display order stays causal.
      if (message.kind !== "event" || message.type !== "assistant.delta") assistantDeltas.flush();
      // One reducer accepts both live Sidecar events and durable replay.  Replay
      // rebuilds presentation state only; live events also update the running
      // task registry used to keep background conversations independently busy.
      const messagePid = message.runtime_pid;
      if (messagePid !== undefined) {
        // runtime.ready can beat the runtime_start invoke response. Adopt that
        // process eagerly so a Windows-reused PID cannot lose its only ready
        // signal while it is still present in the retired tombstone set.
        if (
          activeRuntimePid.current === null
          && message.kind === "event"
          && message.type === "runtime.ready"
        ) {
          activeRuntimePid.current = messagePid;
          retiredRuntimePids.current.delete(messagePid);
          runtimeStarted.current = true;
        }
        // The active process always wins over the retired tombstone set. Windows
        // can reuse a PID, so checking retiredRuntimePids first would permanently
        // discard every event from a newly launched Sidecar with the same PID.
        if (activeRuntimePid.current !== null) {
          if (messagePid !== activeRuntimePid.current) return;
        } else if (retiredRuntimePids.current.has(messagePid)) {
          return;
        }
      }
      if (message.kind === "response") {
        // RuntimeClient exclusively owns request correlation. Successful or
        // failed calls are projected into handleResponse by sendRequest once
        // their Promise settles, so late envelopes cannot gain a second owner.
        if (runtimeClient.accept(message)) return;
        handleResponse(message);
        return;
      }
      if (message.kind !== "event") return;
      // Buffer live events while their older journal prefix is replayed.  Without
      // this barrier, a new tool card can be rendered before its task-started
      // event and be reordered when the historical entries arrive.
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
      if (!replaying) supervisionStore.ingest(message);
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
      // Memory extraction is a conversation-scoped background management job. Project
      // it before the inactive-conversation early return so switching chats never loses
      // its completion/failure state.
      if (message.type === "memory.extraction.started") {
        setMemoryExtractions((current) => ({
          ...current,
          [message.session_id]: {
            jobId: message.data.job_id,
            status: replaying ? "failed" : "extracting",
            pendingMessageCount: message.data.pending_message_count,
            processedMessageCount: 0,
            factCount: 0,
            savedCount: 0,
            ignoredCount: 0,
            userMemoryCount: 0,
            conversationMemoryCount: 0,
            error: replaying
              ? tx("Memory extraction was interrupted before completion. Retry it.")
              : undefined,
          },
        }));
      } else if (message.type === "memory.extraction.completed") {
        setMemoryExtractions((current) => ({
          ...current,
          [message.session_id]: {
            jobId: message.data.job_id,
            status: "completed",
            pendingMessageCount: current[message.session_id]?.pendingMessageCount
              ?? message.data.processed_message_count,
            processedMessageCount: message.data.processed_message_count,
            factCount: message.data.fact_count,
            savedCount: message.data.saved_count,
            ignoredCount: message.data.ignored_count,
            userMemoryCount: message.data.user_memory_count,
            conversationMemoryCount: message.data.conversation_memory_count,
          },
        }));
      } else if (message.type === "memory.extraction.failed") {
        setMemoryExtractions((current) => ({
          ...current,
          [message.session_id]: {
            ...(current[message.session_id] ?? emptyMemoryExtractionView()),
            jobId: message.data.job_id,
            status: "failed",
            error: message.data.message,
          },
        }));
      }
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
          setApprovalDecisions((current) => {
            const next = { ...current };
            delete next[message.data.approval_id];
            return next;
          });
        }
        return;
      }
      if (message.type === "runtime.ready") {
        restartAttempts.current = 0;
        void restoreProjectsAfterRuntimeReady();
      } else if (message.type === "task.started") {
        const taskId = message.task_id ?? message.event_id;
        const restoredStartedAt = message.data.started_at
          ? Date.parse(message.data.started_at)
          : Date.parse(message.timestamp);
        if (!replaying) {
          setTimerNow(Date.now());
          openRuntimeActivity();
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
      } else if (message.type === "plan.planning.started") {
        const taskId = message.task_id ?? message.event_id;
        setEntries((current) => upsertPlanEntry(current, {
          id: `plan-${taskId}`,
          kind: "plan",
          taskId,
          goal: message.data.goal,
          executionOrder: [],
          steps: [],
          planning: true,
          planningText: "",
          timestamp: message.timestamp,
        }));
      } else if (message.type === "plan.planning.delta") {
        setEntries((current) => appendPlanPlanningDelta(
          current,
          message.task_id,
          message.data.text,
        ));
      } else if (message.type === "team.run.started") {
        const taskId = message.task_id ?? `team-${message.data.run_id}`;
        setEntries((current) => upsertTeamEntry(current, {
          id: `team-${taskId}`,
          kind: "team",
          taskId,
          runId: message.data.run_id,
          workerCount: message.data.worker_count,
          phase: "running",
          agents: [],
          timestamp: message.timestamp,
        }));
      } else if (message.type === "team.run.completed" || message.type === "team.run.failed") {
        const taskId = message.task_id ?? `team-${message.data.run_id}`;
        setEntries((current) => finishTeamRun(
          current,
          taskId,
          message.data.run_id,
          message.type === "team.run.completed" ? "completed" : "failed",
          message.data.message,
          message.timestamp,
        ));
      } else if (message.type === "team.agent.status") {
        const taskId = message.task_id ?? `team-${message.data.run_id}`;
        setEntries((current) => updateTeamAgent(current, taskId, message.data.run_id, {
          id: teamAgentKey(message.data.agent_name, message.data.team_task_id),
          name: message.data.agent_name,
          role: message.data.agent_role,
          teamTaskId: message.data.team_task_id,
          status: message.data.status,
        }));
      } else if (message.type === "team.agent.message") {
        const taskId = message.task_id ?? `team-${message.data.run_id}`;
        setEntries((current) => updateTeamAgent(current, taskId, message.data.run_id, {
          id: teamAgentKey(message.data.agent_name, message.data.team_task_id),
          name: message.data.agent_name,
          role: message.data.agent_role,
          teamTaskId: message.data.team_task_id,
          status: "working",
        }, {
          id: message.event_id,
          kind: "message",
          direction: message.data.direction,
          messageKind: message.data.message_kind,
          content: message.data.content,
          timestamp: message.timestamp,
        }));
      } else if (message.type === "team.agent.delta") {
        const taskId = message.task_id ?? `team-${message.data.team_task_id}`;
        setEntries((current) => applyTeamAgentDelta(
          current,
          taskId,
          {
            id: teamAgentKey(message.data.agent_name, message.data.team_task_id),
            name: message.data.agent_name,
            role: message.data.agent_role,
            teamTaskId: message.data.team_task_id,
            status: "working",
          },
          message.data.text,
          message.data.reset === true,
          message.timestamp,
        ));
      } else if (message.type === "team.agent.tool.started") {
        const taskId = message.task_id ?? `team-${message.data.team_task_id}`;
        setEntries((current) => updateTeamAgent(current, taskId, "", {
          id: teamAgentKey(message.data.agent_name, message.data.team_task_id),
          name: message.data.agent_name,
          role: message.data.agent_role,
          teamTaskId: message.data.team_task_id,
          status: "working",
        }, {
          id: `team-tool-${message.data.tool_call_id}`,
          kind: "tool",
          toolName: message.data.name,
          toolStatus: "running",
          toolArguments: message.data.arguments,
          content: JSON.stringify(message.data.arguments),
          timestamp: message.timestamp,
        }));
      } else if (message.type === "team.agent.tool.completed") {
        const taskId = message.task_id ?? `team-${message.data.team_task_id}`;
        setEntries((current) => updateTeamAgent(current, taskId, "", {
          id: teamAgentKey(message.data.agent_name, message.data.team_task_id),
          name: message.data.agent_name,
          role: message.data.agent_role,
          teamTaskId: message.data.team_task_id,
          status: message.data.success ? "working" : "failed",
        }, {
          id: `team-tool-${message.data.tool_call_id}`,
          kind: "tool",
          toolName: message.data.name,
          toolStatus: message.data.success ? "completed" : "failed",
          content: message.data.result_preview,
          elapsed: message.data.elapsed_ms,
          timestamp: message.timestamp,
        }));
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
          { status: "running", resultPreview: undefined, error: undefined, streamText: "", streaming: true },
        ));
      } else if (message.type === "plan.step.delta") {
        setEntries((current) => updatePlanStep(
          current,
          message.task_id,
          message.data.step_id,
          message.data.reset
            ? { streamText: "", streaming: false }
            : { streamText: appendPlanStepDelta(current, message.task_id, message.data.step_id, message.data.text), streaming: true },
        ));
      } else if (message.type === "plan.step.completed") {
        setEntries((current) => updatePlanStep(
          current,
          message.task_id,
          message.data.step_id,
          { status: "completed", resultPreview: message.data.result_preview, error: undefined, streamText: undefined, streaming: false },
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
          taskId: message.task_id,
          name: message.data.name,
          detail: JSON.stringify(message.data.arguments),
          arguments: message.data.arguments,
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
          taskId: message.task_id,
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
          setApprovalQueue((current) => current.filter(
            (item) => item.data.approval_id !== message.data.approval_id,
          ));
          setApprovalDecisions((current) => {
            const next = { ...current };
            delete next[message.data.approval_id];
            return next;
          });
        }
      } else if (message.type === "assistant.delta") {
        if (replaying) return;
        assistantDeltas.push(message);
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
        if (!replaying && pendingRecovery.current?.task_id === message.task_id) pendingRecovery.current = null;
        setEntries((current) => updateTaskStatus(finishTaskStatus(
          current,
          message.task_id,
          "completed",
          undefined,
          message.data.elapsed_ms,
          Date.parse(message.timestamp),
        ), message.task_id, { changes: message.data.changes, rollbackState: "idle" }));
        if (!replaying) {
          dispatchRuntime({ type: "task.cancel.finished", taskId: message.task_id });
          if (message.task_id) clearApprovalsForTask(message.task_id);
          void requestConversationList();
        }
      } else if (message.type === "task.failed") {
        if (!replaying && pendingRecovery.current?.task_id === message.task_id) pendingRecovery.current = null;
        if (message.data.error_code === "worktree_merge_conflict") {
          setEntries((current) => removeUnappliedAssistantAnswer(current, message.task_id));
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
          dispatchRuntime({ type: "task.cancel.finished", taskId: message.task_id });
          if (message.task_id) clearApprovalsForTask(message.task_id);
        }
        setEntries((current) => finishPlanTask(current, message.task_id, "failed"));
        if (!replaying) reportError(message.data.message, message.event_id);
      } else if (message.type === "task.cancelled") {
        if (!replaying && pendingRecovery.current?.task_id === message.task_id) pendingRecovery.current = null;
        setEntries((current) => updateTaskStatus(finishTaskStatus(
          current,
          message.task_id,
          "cancelled",
          "Task cancelled by user.",
          message.data.elapsed_ms,
          Date.parse(message.timestamp),
        ), message.task_id, { changes: message.data.changes, rollbackState: "idle" }));
        if (!replaying) {
          dispatchRuntime({ type: "task.cancel.finished", taskId: message.task_id });
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
      assistantDeltas.flush();
      if (!message.ok) {
        const failedWorkspaceOpen = message.request_id === workspaceOpenRequest.current;
        const failedSessionList = message.request_id === sessionListRequest.current;
        const failedSessionCreate = message.request_id === sessionCreateRequest.current;
        const failedSessionOpen = message.request_id === sessionOpenRequest.current;
        const failedSessionRename = message.request_id === sessionRenameRequest.current;
        const failedSessionDelete = message.request_id === sessionDeleteRequest.current;
        const failedReplay = message.request_id === eventReplayRequest.current;
        const failedSubmission = taskSubmitSessions.current.get(message.request_id);
        const failedSubmitSessionId = failedSubmission?.sessionId ?? "";
        const responseCode = message.error?.code ?? "runtime_error";
        const submissionResultUncertain = Boolean(
          failedSubmitSessionId && UNCERTAIN_SUBMISSION_ERROR_CODES.has(responseCode),
        );
        if (message.request_id === accessModeRequest.current) {
          accessModeRequest.current = "";
        }
        if (message.request_id === traceModeRequest.current) {
          traceModeRequest.current = "";
        }
        if (failedSubmitSessionId) {
          taskSubmitSessions.current.delete(message.request_id);
          dispatchRuntime({
            type: "task.submit.finished",
            sessionId: failedSubmitSessionId,
            requestId: message.request_id,
          });
          supervisionStore.recordSubmissionFailure({
            requestId: message.request_id,
            sessionId: failedSubmitSessionId,
            projectId: failedSubmission?.projectId,
            promptPreview: failedSubmission?.promptPreview,
            errorCode: responseCode,
            errorMessage: message.error?.message ?? tx("Unknown error"),
            startedAt: failedSubmission?.startedAt,
            uncertain: submissionResultUncertain,
          });
          if (!submissionResultUncertain && failedSubmission?.draft) {
            restoreFailedSubmissionDraft(failedSubmitSessionId, failedSubmission.draft);
          }
        }
        if (message.request_id === taskRecoveryRequest.current) {
          taskRecoveryRequest.current = "";
          const failedRecovery = pendingRecovery.current;
          if (failedRecovery) {
            recoveryQueue.current = recoveryQueue.current.filter(
              (item) => item.task_id !== failedRecovery.task_id,
            );
            releaseRunningTask(failedRecovery.task_id, failedRecovery.session_id);
            supervisionStore.reconcileProjectActiveTasks(
              failedRecovery.project_id,
              authoritativeActiveTasks.current.get(failedRecovery.project_id) ?? [],
            );
          }
          pendingRecovery.current = null;
          requestPendingTaskRecovery();
        }
        if (failedReplay) {
          const failedBarrier = replayBarrier.current;
          const failedReplaySession = failedBarrier?.sessionId;
          eventReplayRequest.current = "";
          flushReplayBarrier(failedBarrier);
          dispatchRuntime({ type: "replay.finished", sessionId: failedReplaySession });
          requestPendingTaskRecovery();
        }
        if (failedSessionList) sessionListRequest.current = "";
        if (failedSessionCreate) sessionCreateRequest.current = "";
        if (failedSessionRename) sessionRenameRequest.current = "";
        if (failedSessionDelete) {
          sessionDeleteRequest.current = "";
          sessionDeleteTargetId.current = "";
        }
        if (failedSessionOpen) {
          const failedBarrier = replayBarrier.current;
          const failedSessionId = failedBarrier?.sessionId;
          sessionOpenRequest.current = "";
          flushReplayBarrier(failedBarrier);
          dispatchRuntime({ type: "replay.finished", sessionId: failedSessionId });
        }
        if (failedWorkspaceOpen) {
          workspaceOpenRequest.current = "";
          flushReplayBarrier(replayBarrier.current);
          dispatchRuntime({ type: "connection.changed", connection: "error" });
          const failedActivation = pendingWorkspaceActivation.current;
          pendingWorkspaceActivation.current = null;
          if (
            failedActivation?.previous
            && failedActivation.targetProjectId === projectIdRef.current
          ) {
            void restorePreviousWorkspace(failedActivation).catch(reportError);
          }
        }
        const backgroundSubmitFailure = Boolean(
          failedSubmitSessionId
          && failedSubmitSessionId !== activeConversationIdRef.current,
        );
        if (
          !backgroundSubmitFailure
          && responseCode !== "request_superseded"
          && responseCode !== "project_switched"
        ) {
          reportError(
            `${responseCode}: ${message.error?.message ?? tx("Unknown error")}`,
          );
        }
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
        const detachedBuffered = detachReplayBarrier(barrier);
        // Rebuild only from the durable replay prefix. Buffered envelopes are
        // live traffic and may contain assistant.delta, usage, access or trace
        // events that are intentionally absent from the restorable event list.
        const { replayEvents: ordered, liveEvents: buffered } = partitionRuntimeReplay(
          barrier?.replayed ?? replayed,
          detachedBuffered,
          RESTORABLE_EXECUTION_EVENT_TYPES,
        );
        const lastResetIndex = findLastRuntimeEventIndex(ordered, "session.reset");
        const restorable = lastResetIndex >= 0 ? ordered.slice(lastResetIndex + 1) : ordered;
        if (barrier?.sessionId) {
          const currentMeta = supervisionStore.getSnapshot().sessions[barrier.sessionId]?.meta;
          supervisionStore.replay(barrier.sessionId, restorable, currentMeta ?? {
            sessionId: barrier.sessionId,
            projectId: sessionProjectIds.current.get(barrier.sessionId),
            workspace: workspaceRef.current,
          }, buffered);
        }
        const previousActiveTaskId = selectActiveTaskId(runtimeControlRef.current);
        for (const lane of mergeRuntimeReplayLanes(restorable, buffered)) {
          handleRuntimeMessage(lane.event, lane.source === "replay");
        }
        if (barrier?.sessionId) {
          const replayProjectId = sessionProjectIds.current.get(barrier.sessionId)
            ?? supervisionStore.getSnapshot().sessions[barrier.sessionId]?.meta.projectId
            ?? "";
          const authoritative = authoritativeActiveTasks.current.get(replayProjectId);
          if (replayProjectId && authoritative) {
            supervisionStore.reconcileProjectActiveTasks(replayProjectId, authoritative);
          }
        }
        const recovery = barrier?.sessionId
          ? recoveryQueue.current.find((item) => item.session_id === barrier.sessionId) ?? null
          : null;
        const replayedTerminalTaskIds = new Set([...restorable, ...buffered]
          .filter((event) => (
            event.type === "task.completed"
            || event.type === "task.failed"
            || event.type === "task.cancelled"
          ))
          .map((event) => event.task_id)
          .filter((taskId): taskId is string => Boolean(taskId)));
        const recoveringTaskId = recovery && recovery.session_id === barrier?.sessionId
          ? recovery.task_id
          : undefined;
        const liveTaskId = barrier?.sessionId
          ? runtimeControlRef.current.tasksBySession[barrier.sessionId] ?? ""
          : "";
        const replayTask = reconcileReplayedTask(
          previousActiveTaskId,
          recoveringTaskId,
          liveTaskId,
          replayedTerminalTaskIds,
        );
        setEntries((current) => sortTranscriptEntries(
          settleInterruptedReplayEntries(
            current,
            replayTask.retainedTaskId || undefined,
            tx,
          ),
        ));
        if (replayTask.releaseTaskId) {
          releaseRunningTask(replayTask.releaseTaskId, barrier?.sessionId ?? "");
        } else if (recovery) {
          registerRunningTask(recovery.task_id, recovery.session_id);
        }
        dispatchRuntime({ type: "replay.finished", sessionId: barrier?.sessionId });
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
        pendingRecovery.current = null;
        requestPendingTaskRecovery();
        return;
      }
      const submittedSessionId = taskSubmitSessions.current.get(message.request_id)?.sessionId ?? "";
      if (submittedSessionId) {
        taskSubmitSessions.current.delete(message.request_id);
        dispatchRuntime({
          type: "task.submit.finished",
          sessionId: submittedSessionId,
          requestId: message.request_id,
        });
        registerRunningTask(String(message.result?.task_id ?? ""), submittedSessionId);
        return;
      }
      if (message.request_id === workspaceOpenRequest.current) {
        workspaceOpenRequest.current = "";
        const result = message.result ?? {};
        const completedActivation = pendingWorkspaceActivation.current;
        if (
          completedActivation
          && completedActivation.targetProjectId === projectIdRef.current
        ) {
          pendingWorkspaceActivation.current = null;
          void invoke("project_touch", { projectId: completedActivation.targetProjectId })
            .then(() => refreshProjects())
            .catch((error) => console.warn(`Unable to update recent projects: ${String(error)}`));
        }
        // A successful workspace.open is also a liveness proof when switching
        // projects inside an already-running multi-project Sidecar.
        dispatchRuntime({ type: "connection.changed", connection: "online" });
        setWorkspace(String(result.workspace ?? workspaceRef.current));
        setModel(`${String(result.provider ?? "provider")} / ${String(result.model ?? "model")}`);
        setAccessMode((result.access_mode as AccessMode | undefined) ?? "restricted");
        const openedProjectId = String(result.project_id ?? projectIdRef.current);
        const recoveries = reconcileProjectRuntimeState(
          openedProjectId,
          result as unknown as RuntimeResponseDataMap["workspace.open"],
        );
        const recovery = recoveries[0] ?? null;
        // Keep finalize_pending as the authoritative active-task state. It must
        // not be sent through task.recover (the Sidecar retries POST snapshot
        // only), but replay reconciliation still needs its task id so a buffered
        // terminal event can clear busy deterministically.
        if (recovery) {
          pendingConversationOpen.current = {
            projectId: openedProjectId,
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
          console.warn(`${tx("MCP refresh failed:")} ${String(error)}`);
        });
        void refreshRagSnapshot().catch((error) => {
          console.warn(`${tx("RAG refresh failed:")} ${String(error)}`);
        });
        void refreshManagementSnapshots();
        void refreshDiagnosticsSnapshot().catch((error) => {
          console.warn(`${tx("Diagnostics refresh failed:")} ${String(error)}`);
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
            supervisionStore.registerSession({
              sessionId: conversation.id,
              projectId: listedProjectId,
              title: conversation.title,
              workspace: workspaceRef.current,
            });
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
        if (identifier) {
          sessionProjectIds.current.set(identifier, projectIdRef.current);
          supervisionStore.registerSession({
            sessionId: identifier,
            projectId: projectIdRef.current,
            title: String(result.title ?? tx("New conversation")),
            workspace: String(result.workspace ?? workspaceRef.current),
          });
        }
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
        if (identifier) {
          sessionProjectIds.current.set(identifier, projectIdRef.current);
          supervisionStore.registerSession({
            sessionId: identifier,
            projectId: projectIdRef.current,
            title: String(result.title ?? identifier),
            workspace: String(result.workspace ?? workspaceRef.current),
          });
        }
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
          dispatchRuntime({ type: "replay.finished", sessionId: identifier });
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
        const deletedSessionId = sessionDeleteTargetId.current;
        sessionDeleteTargetId.current = "";
        if (deletedSessionId) sessionDraftStore.delete(deletedSessionId);
        if (deletedSessionId) setMemoryExtractions((current) => {
          const next = { ...current };
          delete next[deletedSessionId];
          return next;
        });
        const deletedActiveConversation = deletedSessionId === activeConversationIdRef.current;
        if (deletedActiveConversation) {
          setSessionId("");
          setActiveConversationId("");
          activeConversationIdRef.current = "";
          syncActiveConversationTask("");
          setEntries([]);
          setRollbackBusyTaskId("");
          setTaskDiff(null);
          setTaskDiffLoading("");
          replacePrompt("", false);
          replaceAttachments([], false);
          setTraceEnabled(false);
          setTracePath("");
          setUsage({ ...EMPTY_USAGE, context_window: appSettingsRef.current.agent.context_window });
          setHistoryState({ ...EMPTY_HISTORY, context_window: appSettingsRef.current.agent.context_window });
        }
        void requestConversationList(deletedActiveConversation);
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
      // A snapshot is deliberately compact (user/assistant/plan). Execution
      // cards, approvals and task state are reconstructed from event replay.
      setAccessMode((result.access_mode as AccessMode | undefined) ?? "restricted");
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
          taskId: item.task_id,
          attachments: item.attachments,
          timestamp: item.timestamp,
        }];
      }));
      restoreComposerDraft(activeConversationIdRef.current);
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

    liveRuntimeEventHandler.current = (event) => handleRuntimeMessage(event);
    legacyRuntimeResponseHandler.current = (response) => handleResponse(response);
    void start();
    return () => {
      disposed = true;
      assistantDeltas.dispose();
      liveRuntimeEventHandler.current = () => undefined;
      legacyRuntimeResponseHandler.current = () => undefined;
      rejectAllRuntimeRequests(tx("Desktop Runtime connection closed."));
      runtimeClient.dispose(tx("Desktop Runtime connection closed."));
      if (restartTimer.current !== null) {
        window.clearTimeout(restartTimer.current);
        restartTimer.current = null;
      }
      for (const unlisten of unlisteners) unlisten();
      if (runtimeStarted.current) void invoke("runtime_stop");
    };
  }, []);

  function reportError(error: unknown, id: string = crypto.randomUUID()) {
    if (
      error instanceof RuntimeClientError
      && (error.code === "project_switched" || error.code === "project_removed")
    ) return;
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

  function reportSupervisionError(error: unknown) {
    setSupervisionError(error instanceof Error ? error.message : String(error));
    setSupervisionOpen(true);
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

  function rejectAllRuntimeRequests(message: string) {
    runtimeClient.rejectAll(new Error(message));
  }

  function rejectProjectRequests(projectId: string, message: string, code = "project_switched") {
    if (!projectId) return;
    const reason = new RuntimeClientError(message, code);
    runtimeClient.rejectScopePrefix(`management:${projectId}:`, reason);
    runtimeClient.rejectScopePrefix(`project:${projectId}:`, reason);
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
      appSettings.general.worktree_directory,
      appSettings.models,
      appSettings.agent,
      appSettings.rag,
      appSettings.diagnostics,
    ]) !== JSON.stringify([
      snapshot.settings.general.worktree_directory,
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

  function requestManagement<M extends ManagementRequestType>(
    method: M,
    params: RuntimeRequestDataMap[M],
  ): Promise<RuntimeResponseDataMap[M]> {
    if (
      !runtimeStarted.current
      || !projectIdRef.current
      || runtimeControlRef.current.connection !== "online"
    ) {
      return Promise.reject(new Error(t("Open a project and wait for Python Runtime first.")));
    }
    return runtimeClient.request(method, {
      ...params,
      project_id: projectIdRef.current,
    } as RuntimeRequestDataMap[M], {
      scope: `management:${projectIdRef.current}:${method}`,
      timeoutMs: 60_000,
    });
  }

  async function refreshMcpSnapshot() {
    const next = await requestManagement("mcp.list", {});
    setMcpSnapshot(next);
    return next;
  }

  async function installMcpServer(
    name: string,
    config: McpInstallConfig,
    overwrite: boolean,
    confirmed: boolean,
  ) {
    const next = await requestManagement("mcp.install", {
      name,
      config,
      overwrite,
      confirmed,
    });
    setMcpSnapshot(next);
    return next;
  }

  async function setMcpServerEnabled(name: string, enabled: boolean) {
    const next = await requestManagement("mcp.set_enabled", {
      name,
      enabled,
    });
    setMcpSnapshot(next);
    return next;
  }

  async function restartMcpServer(name: string) {
    const next = await requestManagement("mcp.restart", { name });
    setMcpSnapshot(next);
    return next;
  }

  async function removeMcpServer(name: string) {
    const next = await requestManagement("mcp.remove", { name });
    setMcpSnapshot(next);
    return next;
  }

  async function readMcpServerLogs(name: string) {
    return requestManagement("mcp.logs", { name });
  }

  async function refreshRagSnapshot() {
    const next = await requestManagement("rag.snapshot", {});
    setRagSnapshot(next);
    return next;
  }

  async function addRagSources(paths: string[]) {
    const next = await requestManagement("rag.add_sources", { paths });
    setRagSnapshot(next);
    return next;
  }

  async function removeRagSource(path: string) {
    const next = await requestManagement("rag.remove_source", { path });
    setRagSnapshot(next);
    return next;
  }

  async function rebuildRagIndex() {
    const next = await requestManagement("rag.index", {});
    setRagSnapshot(next);
    return next;
  }

  async function clearRagIndex(confirmed: boolean) {
    const next = await requestManagement("rag.clear", { confirmed });
    setRagSnapshot(next);
    return next;
  }

  async function refreshMemorySnapshot(scope: MemorySnapshot["scope"] = "user", query = "") {
    if (scope === "conversation" && !sessionId) throw new Error(t("Create or open a conversation first."));
    const next = await requestManagement("memory.list", {
      session_id: sessionId || "default",
      scope,
      query,
      limit: 500,
    });
    setMemorySnapshot(next);
    return next;
  }

  async function refreshPromptSnapshot(includeMemory = false) {
    if (!sessionId) throw new Error(t("Create or open a conversation first."));
    return requestManagement("prompt.snapshot", {
      session_id: sessionId,
      include_memory: includeMemory,
    });
  }

  async function saveMemory(content: string, scope: MemorySnapshot["scope"]) {
    if (scope === "conversation" && !sessionId) throw new Error(t("Create or open a conversation first."));
    const next = await requestManagement("memory.save", {
      session_id: sessionId || "default",
      scope,
      content,
    });
    setMemorySnapshot(next);
    return next;
  }

  async function deleteMemory(id: string, scope: MemorySnapshot["scope"]) {
    if (scope === "conversation" && !sessionId) throw new Error(t("Create or open a conversation first."));
    const next = await requestManagement("memory.delete", {
      session_id: sessionId || "default",
      scope,
      id,
    });
    setMemorySnapshot(next);
    return next;
  }

  async function clearMemory(confirmed: boolean, scope: MemorySnapshot["scope"]) {
    if (scope === "conversation" && !sessionId) throw new Error(t("Create or open a conversation first."));
    const next = await requestManagement("memory.clear", {
      session_id: sessionId || "default",
      scope,
      confirmed,
    });
    setMemorySnapshot(next);
    return next;
  }

  async function extractConversationMemory() {
    const targetSessionId = sessionId;
    if (!targetSessionId || memoryExtracting || busy) return;
    setMemoryExtractions((current) => ({
      ...current,
      [targetSessionId]: emptyMemoryExtractionView(),
    }));
    try {
      const targetProjectId = sessionProjectIds.current.get(targetSessionId)
        ?? projectIdRef.current;
      const accepted = await runtimeClient.request("memory.extract", {
        session_id: targetSessionId,
        project_id: targetProjectId,
      }, {
        scope: `management:${targetProjectId}:session:${targetSessionId}:memory.extract`,
        timeoutMs: 30_000,
      });
      setMemoryExtractions((current) => ({
        ...current,
        [targetSessionId]: current[targetSessionId]?.status === "completed"
          || current[targetSessionId]?.status === "failed"
          ? current[targetSessionId]
          : {
              ...(current[targetSessionId] ?? emptyMemoryExtractionView()),
              jobId: accepted.job_id,
              status: "extracting",
              pendingMessageCount: accepted.pending_message_count,
            },
      }));
    } catch (error) {
      setMemoryExtractions((current) => ({
        ...current,
        [targetSessionId]: {
          ...(current[targetSessionId] ?? emptyMemoryExtractionView()),
          status: "failed",
          error: error instanceof Error ? error.message : String(error),
        },
      }));
    }
  }

  async function refreshSkillSnapshot() {
    const next = await requestManagement("skill.list", {});
    setSkillSnapshot(next);
    return next;
  }

  async function loadSkillDiff(name: string) {
    return requestManagement("skill.diff", {
      name,
      max_chars: 120_000,
    });
  }

  async function setSkillEnabled(name: string, enabled: boolean) {
    const next = await requestManagement("skill.set_enabled", { name, enabled });
    setSkillSnapshot(next);
    return next;
  }

  async function reloadSkills() {
    const next = await requestManagement("skill.reload", {});
    setSkillSnapshot(next);
    return next;
  }

  async function updateBundledSkill(name: string, currentHash: string, builtinHash: string) {
    const next = await requestManagement("skill.update", {
      name,
      current_hash: currentHash,
      builtin_hash: builtinHash,
      confirmed: true,
    });
    setSkillSnapshot(next);
    return next;
  }

  async function keepCustomSkill(name: string, currentHash: string, builtinHash: string) {
    const next = await requestManagement("skill.keep_custom", {
      name,
      current_hash: currentHash,
      builtin_hash: builtinHash,
    });
    setSkillSnapshot(next);
    return next;
  }

  async function restoreBundledSkill(name: string, currentHash: string, builtinHash: string) {
    const next = await requestManagement("skill.restore_default", {
      name,
      current_hash: currentHash,
      builtin_hash: builtinHash,
      confirmed: true,
    });
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
    const next = await requestManagement("browser.snapshot", {});
    setBrowserSnapshot(next);
    return next;
  }

  async function probeBrowser(port: number) {
    const probe = await requestManagement("browser.probe", { port });
    setBrowserSnapshot((current) => ({ ...current, legacy_probe: probe }));
    return probe;
  }

  async function connectBrowser(port?: number) {
    const result = await requestManagement("browser.connect", { port, confirmed: true });
    setBrowserSnapshot({ ...result.snapshot, message: result.message });
    return { ...result.snapshot, message: result.message };
  }

  async function disconnectBrowser() {
    const result = await requestManagement("browser.disconnect", { confirmed: true });
    setBrowserSnapshot({ ...result.snapshot, message: result.message });
    return { ...result.snapshot, message: result.message };
  }

  async function readBrowserTabs() {
    const result = await requestManagement("browser.tabs", {});
    setBrowserSnapshot((current) => ({ ...current, tabs_output: result.output }));
    return result;
  }

  function refreshManagementSnapshots() {
    for (const [label, operation] of [
      ["Memory", () => refreshMemorySnapshot(memorySnapshot.scope)],
      ["Skills", refreshSkillSnapshot],
      ["Browser", refreshBrowserSnapshot],
    ] as const) {
      void operation().catch((error) => {
        console.warn(`${tx("{name} refresh failed:", { name: label })} ${String(error)}`);
      });
    }
  }

  async function refreshDiagnosticsSnapshot() {
    const next = await requestManagement("diagnostics.snapshot", {});
    setDiagnosticsSnapshot(next);
    return next;
  }

  async function runDiagnostics(profile: "safe" | "build" = "safe") {
    if (profile === "build" && !await confirmDialog(
      t("Build checks may execute local project build scripts. Continue?"),
      { title: t("Run build checks"), kind: "warning" },
    )) return diagnosticsSnapshot;
    const next = await requestManagement("diagnostics.run", {
      profile,
      confirmed: profile === "build",
    });
    setDiagnosticsSnapshot(next);
    return next;
  }

  async function cancelDiagnostics() {
    const runId = diagnosticsSnapshot.run_id;
    if (!runId) return diagnosticsSnapshot;
    const next = await requestManagement("diagnostics.cancel", { run_id: runId });
    setDiagnosticsSnapshot(next);
    return next;
  }

  /** Opens the shared inspector without changing the durable transcript. */
  function openRuntimeActivity() {
    setRightSidebarView("activity");
    workspaceLayout.showColumn("right", 560);
  }

  async function previewWorkspaceFile(reference: WorkspaceFileReference, taskId?: string) {
    if (!activeProjectId) return;
    const taskEntry = taskId ? taskStatusById.get(taskId) : undefined;
    const resolvedReference = reconcileChangedWorkspaceFile(
      reference,
      taskEntry?.changes?.changed_files ?? [],
    );
    const tabId = filePreviewTabId(resolvedReference.relativePath);
    const request = requestId("file-preview");
    filePreviewRequestsRef.current.set(tabId, request);
    setFilePreviewTabs((current) => {
      const existing = current.find((tab) => tab.id === tabId);
      if (!existing) return [...current, {
        id: tabId,
        relativePath: resolvedReference.relativePath,
        fileName: fileNameFromPath(resolvedReference.relativePath) || t("File"),
        preview: null,
        loading: true,
        error: "",
      }];
      return current.map((tab) => tab.id === tabId ? {
        ...tab,
        preview: tab.preview ? { ...tab.preview, line: resolvedReference.line, column: resolvedReference.column } : null,
        loading: true,
        error: "",
      } : tab);
    });
    setActiveFilePreviewId(tabId);
    setRightSidebarView("file");
    workspaceLayout.showColumn("right", 680);
    try {
      const result = await invoke<WorkspaceFilePreview>("workspace_file_preview", {
        projectId: activeProjectId,
        relativePath: resolvedReference.relativePath,
      });
      if (filePreviewRequestsRef.current.get(tabId) !== request) return;
      setFilePreviewTabs((current) => current.map((tab) => tab.id === tabId ? {
        ...tab,
        fileName: result.file_name,
        preview: { ...result, line: resolvedReference.line, column: resolvedReference.column },
        loading: false,
        error: "",
      } : tab));
    } catch (error) {
      if (filePreviewRequestsRef.current.get(tabId) !== request) return;
      const taskDiffFallback = taskEntry?.changes?.diff_available
        && taskEntry.changes.changed_files.some(
          (file) => file.path === resolvedReference.relativePath,
        );
      setFilePreviewTabs((current) => current.map((tab) => tab.id === tabId ? {
        ...tab,
        preview: null,
        loading: false,
        error: taskDiffFallback
          ? t("The file is unavailable in the current workspace. Showing its recorded changes instead.")
          : t("Open file failed: {error}", { error: String(error) }),
      } : tab));
      if (taskDiffFallback) await viewTaskDiff(taskEntry, resolvedReference.relativePath);
    } finally {
      if (filePreviewRequestsRef.current.get(tabId) === request) {
        filePreviewRequestsRef.current.delete(tabId);
      }
    }
  }

  function closeFilePreview(tabId: string) {
    filePreviewRequestsRef.current.delete(tabId);
    const closingIndex = filePreviewTabs.findIndex((tab) => tab.id === tabId);
    const remaining = filePreviewTabs.filter((tab) => tab.id !== tabId);
    setFilePreviewTabs(remaining);
    if (activeFilePreviewId !== tabId) return;
    const nextTab = remaining[Math.min(Math.max(0, closingIndex), remaining.length - 1)] ?? null;
    setActiveFilePreviewId(nextTab?.id ?? "");
    if (rightSidebarView === "file") setRightSidebarView(nextTab ? "file" : "context");
  }

  async function viewTaskDiff(entry: TaskStatusTranscriptEntry, filePath?: string) {
    if (!sessionId || !entry.changes?.diff_available || taskDiffLoading) return;
    if (taskDiff?.task_id === entry.taskId) {
      const selectedPath = filePath && taskDiff.changed_files.some((file) => file.path === filePath)
        ? filePath
        : taskDiffSelectedPath || taskDiff.changed_files[0]?.path || "";
      setTaskDiffSelectedPath(selectedPath);
      setRightSidebarView("changes");
      workspaceLayout.showColumn("right", 640);
      return;
    }
    setTaskDiffSelectedPath(filePath || entry.changes.changed_files[0]?.path || "");
    setTaskDiffLoading(entry.taskId);
    try {
      const result = await requestManagement("task.diff", {
        session_id: sessionId,
        task_id: entry.taskId,
        max_chars: 200_000,
      });
      setTaskDiff(result);
      setTaskDiffSelectedPath(
        filePath && result.changed_files.some((file) => file.path === filePath)
          ? filePath
          : result.changed_files[0]?.path || "",
      );
      setRightSidebarView("changes");
      workspaceLayout.showColumn("right", 640);
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
    ) return false;
    const confirmed = await confirmDialog(
      t("Restore the {count} file(s) changed by this task? Later edits to those files will cause a safe conflict instead of being overwritten.", {
        count: changes.changed_files.length,
      }),
      { title: t("Undo task changes"), kind: "warning" },
    );
    if (!confirmed) return false;
    setRollbackBusyTaskId(entry.taskId);
    setEntries((current) => updateTaskStatus(current, entry.taskId, {
      rollbackState: "running",
      rollbackError: undefined,
    }));
    try {
      await requestManagement("task.rollback", {
        session_id: sessionId,
        task_id: entry.taskId,
        snapshot_id: changes.snapshot_id,
        confirmed: true,
      });
      void requestConversationList(false);
      return true;
    } catch (error) {
      setEntries((current) => updateTaskStatus(current, entry.taskId, {
        rollbackState: "failed",
        rollbackError: String(error),
      }));
      reportError(error);
      return false;
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
    // Bind a fresh replay barrier to this exact session before opening it. The
    // response handler rejects stale request ids, preventing two rapid clicks
    // from mixing events of different conversations.
    const id = requestId("session-open");
    sessionOpenRequest.current = id;
    dispatchRuntime({ type: "replay.started", sessionId: conversationId });
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
      flushReplayBarrier(replayBarrier.current);
      dispatchRuntime({ type: "replay.finished", sessionId: conversationId });
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
    sessionDeleteTargetId.current = conversation.id;
    try {
      await sendRequest("session.delete", { session_id: conversation.id }, id);
    } catch (error) {
      sessionDeleteRequest.current = "";
      sessionDeleteTargetId.current = "";
      reportError(error);
    }
  }

  async function changeAccessMode(nextMode: AccessMode) {
    if (runtimeMutationBusy || !sessionId || nextMode === accessMode || accessModeRequest.current) return;
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
      await sendRequest("runtime.set_access_mode", { session_id: sessionId, mode: nextMode }, id);
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

  /**
   * Re-establishes the last confirmed workspace after an optimistic project
   * switch fails. The Sidecar commits its active workspace only after a
   * successful workspace.open response, so the UI must follow the same rule.
   */
  async function restorePreviousWorkspace(activation: PendingWorkspaceActivation) {
    const previous = activation.previous;
    if (!previous) return;
    rejectProjectRequests(
      activation.targetProjectId,
      tx("A management request was interrupted by a project or Runtime switch."),
    );
    pendingConversationOpen.current = previous.conversationId
      ? { projectId: previous.projectId, conversationId: previous.conversationId }
      : null;
    setActiveProjectId(previous.projectId);
    setExpandedProjectIds((current) => new Set(current).add(previous.projectId));
    setWorkspace(previous.workspace);
    workspaceRef.current = previous.workspace;
    projectIdRef.current = previous.projectId;
    setSessionId("");
    setAccessMode("restricted");
    setTraceEnabled(false);
    setTracePath("");
    setMcpSnapshot(EMPTY_MCP_SNAPSHOT);
    setRagSnapshot(EMPTY_RAG_SNAPSHOT);
    setMemorySnapshot(EMPTY_MEMORY_SNAPSHOT);
    setSkillSnapshot(EMPTY_SKILL_SNAPSHOT);
    setBrowserSnapshot(EMPTY_BROWSER_SNAPSHOT);
    setDiagnosticsSnapshot({ ...EMPTY_DIAGNOSTICS_SNAPSHOT, workspace: previous.workspace });
    setConversations([]);
    setActiveConversationId("");
    activeConversationIdRef.current = "";
    dispatchRuntime({ type: "conversation.activated", sessionId: "" });
    dispatchRuntime({ type: "replay.finished" });
    dispatchRuntime({ type: "connection.changed", connection: "starting" });
    sessionListRequest.current = "";
    sessionCreateRequest.current = "";
    sessionOpenRequest.current = "";
    sessionRenameRequest.current = "";
    sessionDeleteRequest.current = "";
    sessionDeleteTargetId.current = "";
    accessModeRequest.current = "";
    traceModeRequest.current = "";
    setEntries([]);
    replacePrompt("", false);
    replaceAttachments([], false);
    setEventCount(0);

    const rollbackRequestId = requestId("workspace-restore");
    workspaceOpenRequest.current = rollbackRequestId;
    await sendRequest(
      "workspace.open",
      { project_id: previous.projectId, workspace: previous.workspace },
      rollbackRequestId,
    );
  }

  async function activateProject(project: ProjectRecord, forceRestart = false, conversationToOpen = "") {
    // Never silently drop a second project click. React state and the legacy
    // workspace request id can both lag by one turn, so a ref owns the actual
    // critical section and the newest intent runs as soon as it is released.
    if (projectActionBusyRef.current) {
      queuedWorkspaceActivation.current = { project, forceRestart, conversationToOpen };
      return;
    }
    projectActionBusyRef.current = true;
    const previousProjectId = projectIdRef.current;
    const previousWorkspace = workspaceRef.current;
    const previousConversationId = activeConversationIdRef.current;
    const runtimeAlreadyRunning = runtimeStarted.current;
    setProjectActionBusy(true);
    if (restartTimer.current !== null) {
      window.clearTimeout(restartTimer.current);
      restartTimer.current = null;
    }
    restartAttempts.current = 0;
    pendingRecovery.current = null;
    flushReplayBarrier(replayBarrier.current);
    eventReplayRequest.current = "";
    taskRecoveryRequest.current = "";
    if (forceRestart) {
      lastEventSequence.current.clear();
      seenEventIds.current.clear();
    }
    pendingWorkspaceActivation.current = {
      targetProjectId: project.id,
      targetWorkspace: project.path,
      previous: previousProjectId && previousProjectId !== project.id
        ? {
          projectId: previousProjectId,
          workspace: previousWorkspace,
          conversationId: previousConversationId,
        }
        : null,
    };
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
    dispatchRuntime({ type: "conversation.activated", sessionId: "" });
    dispatchRuntime({ type: "replay.finished" });
    dispatchRuntime({
      type: "workspace.activation.started",
      runtimeAlreadyRunning,
      forceRestart,
    });
    workspaceOpenRequest.current = "";
    sessionListRequest.current = "";
    sessionCreateRequest.current = "";
    sessionOpenRequest.current = "";
    sessionRenameRequest.current = "";
    sessionDeleteRequest.current = "";
    sessionDeleteTargetId.current = "";
    accessModeRequest.current = "";
    traceModeRequest.current = "";
    setEntries([]);
    replacePrompt("", false);
    replaceAttachments([], false);
    setEventCount(0);
    try {
      rejectProjectRequests(
        previousProjectId,
        tx("A management request was interrupted by a project or Runtime switch."),
      );
      if (forceRestart && runtimeStarted.current) {
        rejectAllRuntimeRequests(tx("Python Runtime exited before the request completed."));
        retireRuntimePid(activeRuntimePid.current);
        activeRuntimePid.current = null;
        await invoke("runtime_stop");
        runtimeStarted.current = false;
        dispatchRuntime({ type: "tasks.cleared" });
        setApprovalQueue([]);
        setApprovalDecisions({});
        approvalToolCalls.current.clear();
      }
      if (runtimeStarted.current) {
        const id = requestId("workspace-open");
        workspaceOpenRequest.current = id;
        const openResponse = await sendRequest(
          "workspace.open",
          { project_id: project.id, workspace: project.path },
          id,
        );
        if (!openResponse.ok) return;
      } else {
        const started = await invoke<RuntimeStartResult>("runtime_start", {
          workspace: project.path,
        });
        activeRuntimePid.current = started.pid;
        retiredRuntimePids.current.delete(started.pid);
        setRuntimePython(started.python);
        setRuntimeSettingsDirty(false);
        runtimeStarted.current = true;
        // The first project has no already-running Sidecar, so activation used
        // to depend entirely on the timing of runtime.ready. Ensure the
        // workspace is opened after the process is registered as a fallback;
        // the guard coalesces this with an early runtime.ready handler.
        if (shouldOpenActivatedWorkspace({
          connection: runtimeControlRef.current.connection,
          activeProjectId: projectIdRef.current,
          targetProjectId: project.id,
          pendingActivationProjectId: pendingWorkspaceActivation.current?.targetProjectId,
          workspaceOpenRequestId: workspaceOpenRequest.current,
        })) {
          const id = requestId("workspace-open");
          workspaceOpenRequest.current = id;
          const openResponse = await sendRequest(
            "workspace.open",
            { project_id: project.id, workspace: project.path },
            id,
          );
          if (!openResponse.ok) return;
        }
      }
    } catch (error) {
      const failedActivation = pendingWorkspaceActivation.current;
      pendingWorkspaceActivation.current = null;
      pendingConversationOpen.current = null;
      dispatchRuntime({ type: "connection.changed", connection: "error" });
      reportError(error);
      if (failedActivation?.previous && runtimeStarted.current) {
        void restorePreviousWorkspace(failedActivation).catch(reportError);
      }
    } finally {
      projectActionBusyRef.current = false;
      setProjectActionBusy(false);
      const queued = queuedWorkspaceActivation.current;
      queuedWorkspaceActivation.current = null;
      if (queued) {
        window.queueMicrotask(() => void activateProject(
          queued.project,
          queued.forceRestart,
          queued.conversationToOpen,
        ));
      }
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
      authoritativeActiveTasks.current.delete(removedProjectId);
      loadedProjectWorkspaces.current.delete(removedProjectId);
      restartProjectQueue.current = restartProjectQueue.current.filter(
        (target) => target.projectId !== removedProjectId,
      );
      recoveryQueue.current = recoveryQueue.current.filter(
        (recovery) => recovery.project_id !== removedProjectId,
      );
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
      rejectProjectRequests(
        removedProjectId,
        t("A management request was interrupted because the project was removed."),
        "project_removed",
      );
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
        activeConversationIdRef.current = "";
        dispatchRuntime({ type: "workspace.reset", connection: "offline" });
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
    replacePrompt(nextPrompt);
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
    if ((!value && attachments.length === 0) || !sessionId || runtimeMutationBusy || memoryExtracting) return;
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
    sessionDraftStore.clear(sessionId);
    replacePrompt("", false);
    setReferenceMatch(null);
    replaceAttachments([], false);
    const id = requestId("task-submit");
    dispatchRuntime({ type: "task.submit.started", sessionId, requestId: id });
    taskSubmitSessions.current.set(id, {
      sessionId,
      projectId: sessionProjectIds.current.get(sessionId) ?? projectIdRef.current,
      promptPreview: displayText.slice(0, 500),
      startedAt: new Date().toISOString(),
      draft: {
        prompt: value,
        attachments: submittedAttachments.map((attachment) => ({ ...attachment })),
      },
    });
    try {
      await sendRequest(
        "task.submit",
        { session_id: sessionId, prompt: value, attachments: submittedAttachments },
        id,
      );
    } catch (error) {
      if (taskSubmitSessions.current.get(id)?.sessionId === sessionId) {
        taskSubmitSessions.current.delete(id);
      }
      dispatchRuntime({ type: "task.submit.finished", sessionId, requestId: id });
      reportError(error);
    }
  }

  async function chooseAttachments() {
    if (runtimeMutationBusy || !sessionId) return;
    const targetSessionId = sessionId;
    const selected = await open({
      directory: false,
      multiple: true,
      title: t("Attach files to this message"),
    });
    if (!selected) return;
    await addAttachmentPaths(typeof selected === "string" ? [selected] : selected, targetSessionId);
  }

  async function pasteImages(event: ClipboardEvent<HTMLTextAreaElement>) {
    if (runtimeMutationBusy || !sessionId) return;
    const targetSessionId = sessionId;
    const files = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null);
    if (!files.length) return;
    event.preventDefault();
    try {
      const pasted = await Promise.all(files.map((file) => clipboardImageAttachment(file, t)));
      const current = composerDraftForSession(targetSessionId).attachments;
      const merged = [...current, ...pasted];
      if (merged.length > 10) {
        reportAttachmentError(targetSessionId, t("A message can contain at most 10 attachments."));
      }
      replaceSessionAttachments(targetSessionId, merged.slice(0, 10));
    } catch (error) {
      reportAttachmentError(targetSessionId, error);
    }
  }

  async function addAttachmentPaths(paths: string[], targetSessionId = activeConversationIdRef.current) {
    if (!paths.length || !targetSessionId) return;
    try {
      const inspected = await invoke<RuntimeAttachment[]>("attachment_inspect", { paths });
      const current = composerDraftForSession(targetSessionId).attachments;
      const known = new Set(current.map((item) => item.local_path?.toLowerCase()));
      const additions = inspected.filter(
        (item) => !known.has(item.local_path?.toLowerCase()),
      );
      const merged = [...current, ...additions];
      if (merged.length > 10) {
        reportAttachmentError(targetSessionId, t("A message can contain at most 10 attachments."));
      }
      replaceSessionAttachments(targetSessionId, merged.slice(0, 10));
    } catch (error) {
      reportAttachmentError(targetSessionId, error);
    }
  }

  function composerDraftForSession(targetSessionId: string) {
    if (targetSessionId === activeConversationIdRef.current) {
      return { prompt: promptRef.current, attachments: attachmentsRef.current };
    }
    return sessionDraftStore.get(targetSessionId) ?? { prompt: "", attachments: [] };
  }

  function replaceSessionAttachments(targetSessionId: string, nextAttachments: RuntimeAttachment[]) {
    const draft = composerDraftForSession(targetSessionId);
    sessionDraftStore.save(targetSessionId, { prompt: draft.prompt, attachments: nextAttachments });
    if (targetSessionId === activeConversationIdRef.current) {
      attachmentsRef.current = nextAttachments;
      setAttachments(nextAttachments);
    }
  }

  function reportAttachmentError(targetSessionId: string, error: unknown) {
    if (targetSessionId === activeConversationIdRef.current) {
      reportError(error);
    } else {
      console.warn(`Attachment processing failed for background session ${targetSessionId}: ${String(error)}`);
    }
  }

  function removeAttachment(id: string) {
    if (runtimeMutationBusy) return;
    replaceAttachments((current) => current.filter((item) => item.id !== id));
  }

  function replacePrompt(value: string, saveDraft = true) {
    promptRef.current = value;
    setPrompt(value);
    if (saveDraft) saveComposerDraft();
  }

  function replaceAttachments(
    value: RuntimeAttachment[] | ((current: RuntimeAttachment[]) => RuntimeAttachment[]),
    saveDraft = true,
  ) {
    const next = typeof value === "function" ? value(attachmentsRef.current) : value;
    attachmentsRef.current = next;
    setAttachments(next);
    if (saveDraft) saveComposerDraft();
  }

  function saveComposerDraft(targetSessionId = activeConversationIdRef.current) {
    if (!targetSessionId) return;
    const draft = {
      prompt: promptRef.current,
      attachments: attachmentsRef.current,
    };
    if (draft.prompt.trim() || draft.attachments.length > 0) {
      sessionDraftStore.save(targetSessionId, draft);
    } else {
      sessionDraftStore.clear(targetSessionId);
    }
  }

  function restoreComposerDraft(targetSessionId: string) {
    const draft = targetSessionId ? sessionDraftStore.get(targetSessionId) : undefined;
    promptRef.current = draft?.prompt ?? "";
    attachmentsRef.current = draft?.attachments ?? [];
    setPrompt(promptRef.current);
    setAttachments(attachmentsRef.current);
    setReferenceMatch(null);
    setReferenceSelection(0);
  }

  function restoreFailedSubmissionDraft(
    targetSessionId: string,
    submittedDraft: { prompt: string; attachments: RuntimeAttachment[] },
  ) {
    const existing = sessionDraftStore.get(targetSessionId);
    if (existing && (existing.prompt.trim() || existing.attachments.length > 0)) return;
    sessionDraftStore.save(targetSessionId, submittedDraft);
    if (activeConversationIdRef.current === targetSessionId) {
      restoreComposerDraft(targetSessionId);
    }
  }

  async function cancelTask() {
    const taskId = selectActiveTaskId(runtimeControlRef.current)
      || selectTaskForSession(supervisionStore.getSnapshot(), sessionId)?.id
      || "";
    if (!busy || !taskId || cancelling) return;
    await cancelSupervisedTask(taskId, sessionId);
  }

  async function cancelSupervisedTask(
    taskId: string,
    taskSessionId: string,
    errorTarget: "transcript" | "supervision" = "transcript",
  ) {
    if (!taskId || !taskSessionId || runtimeControlRef.current.cancellingTasks[taskId]) return;
    if (errorTarget === "supervision") setSupervisionError("");
    dispatchRuntime({ type: "task.cancel.started", taskId });
    setTaskActionPending((current) => ({ ...current, [taskId]: "stop" }));
    try {
      const result = await runtimeClient.request(
        "task.cancel",
        {
          session_id: taskSessionId,
          task_id: taskId,
          project_id: sessionProjectIds.current.get(taskSessionId) ?? projectIdRef.current,
        },
        { scope: `task:${taskId}:cancel`, supersede: false, timeoutMs: 30_000 },
      );
      if (result.accepted) {
        supervisionStore.markTaskStopping(taskId);
        clearApprovalsForTask(taskId);
      } else {
        dispatchRuntime({ type: "task.cancel.finished", taskId });
        const message = t("The task is no longer running.");
        if (errorTarget === "supervision") reportSupervisionError(message);
        else reportError(message);
      }
    } catch (error) {
      dispatchRuntime({ type: "task.cancel.finished", taskId });
      if (errorTarget === "supervision") reportSupervisionError(error);
      else reportError(error);
    } finally {
      setTaskActionPending((current) => {
        const next = { ...current };
        delete next[taskId];
        return next;
      });
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
    await resolveSupervisedApproval({
      id: approval.data.approval_id,
      sessionId: approval.session_id,
      taskId: approval.task_id ?? "",
    }, decision);
  }

  async function resolveSupervisedApproval(
    target: { id: string; sessionId: string; taskId: string },
    decision: "approve" | "reject" | "skip",
    errorTarget: "transcript" | "supervision" = "transcript",
  ) {
    const projected = supervisionStore.getSnapshot().approvals[target.id];
    if (projected && projected.status !== "pending") return;
    if (errorTarget === "supervision") setSupervisionError("");
    supervisionStore.markApprovalResolving(target.id, true);
    setApprovalDecisions((current) => ({ ...current, [target.id]: decision }));
    try {
      await runtimeClient.request("approval.resolve", {
        session_id: target.sessionId,
        task_id: target.taskId,
        approval_id: target.id,
        decision,
        project_id: sessionProjectIds.current.get(target.sessionId) ?? projectIdRef.current,
      }, {
        scope: `approval:${target.id}`,
        supersede: false,
        timeoutMs: 30_000,
      });
    } catch (error) {
      supervisionStore.markApprovalResolving(target.id, false);
      if (errorTarget === "supervision") reportSupervisionError(error);
      else reportError(error);
    } finally {
      setApprovalDecisions((current) => {
        const next = { ...current };
        delete next[target.id];
        return next;
      });
    }
  }

  async function openSupervisedSession(target: { projectId: string; sessionId: string }) {
    const projectId = target.projectId || sessionProjectIds.current.get(target.sessionId) || "";
    const project = projects.find((candidate) => candidate.id === projectId);
    if (!project) {
      reportSupervisionError(t("The project for this task is no longer available."));
      return;
    }
    setSupervisionOpen(false);
    if (project.id === activeProjectId) {
      if (target.sessionId !== activeConversationIdRef.current) {
        await requestOpenConversation(target.sessionId);
      }
      return;
    }
    await activateProject(project, false, target.sessionId);
  }

  async function openSupervisedTrace(task: SupervisionTaskView) {
    const projectConversations = task.projectId === activeProjectId
      ? conversations
      : conversationCache[task.projectId] ?? [];
    const conversation = projectConversations.find((candidate) => candidate.id === task.sessionId);
    const path = conversation?.trace_path ?? "";
    if (!path) {
      reportSupervisionError(t("No trace log is available for this conversation."));
      return;
    }
    setTaskActionPending((current) => ({ ...current, [task.id]: "trace" }));
    try {
      await revealItemInDir(path);
    } catch (error) {
      reportSupervisionError(error);
    } finally {
      setTaskActionPending((current) => {
        const next = { ...current };
        delete next[task.id];
        return next;
      });
    }
  }

  async function restartRuntimeFromSettings() {
    if (!activeProject || anyTaskRunning || runtimeMutationBusy) return;
    await activateProject(activeProject, true);
  }

  function renderAgentMarkdown(content: string, taskId?: string) {
    const taskEntry = taskId ? taskStatusById.get(taskId) : undefined;
    const canReviewChanges = Boolean(taskEntry?.changes?.diff_available);
    return <ErrorBoundary t={t} resetKey={activeConversationId}><Suspense fallback={<div className="markdown-loading">{content}</div>}><MarkdownContent
      content={content}
      workspace={workspace}
      projectId={activeProjectId}
      t={t}
      reviewChangesBusy={Boolean(taskId && taskDiffLoading === taskId)}
      onReviewChanges={canReviewChanges && taskEntry ? () => void viewTaskDiff(taskEntry) : undefined}
      onOpenWorkspaceFile={(reference) => previewWorkspaceFile(reference, taskId)}
    /></Suspense></ErrorBoundary>;
  }

  return (
    <div
      className={`app-shell theme-${appSettings.appearance.theme} ${appSettings.general.compact_tools ? "compact-tools" : ""} ${appSettings.general.compact_plans ? "compact-plans" : ""} ${workspaceLayout.resizingColumn ? "layout-resizing" : ""}`}
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
        <button
          type="button"
          className={`layout-toggle ${workspaceLayout.layout.leftCollapsed ? "" : "active"}`}
          onClick={() => workspaceLayout.toggleColumn("left")}
          aria-controls="project-sidebar"
          aria-expanded={!workspaceLayout.layout.leftCollapsed}
          aria-label={t(workspaceLayout.layout.leftCollapsed ? "Show project sidebar" : "Hide project sidebar")}
          title={t(workspaceLayout.layout.leftCollapsed ? "Show project sidebar" : "Hide project sidebar")}
        ><span className="layout-toggle-icon left" aria-hidden="true"><i /></span></button>
        <span className="topbar-divider" />
        <span className="workspace-path" title={workspace}>{workspace || t("No workspace")}</span>
        <span className="branch-chip">desktop-client</span>
        <div className="topbar-spacer" />
        <button
          type="button"
          className={`layout-toggle ${workspaceLayout.layout.rightCollapsed ? "" : "active"}`}
          onClick={() => workspaceLayout.toggleColumn("right")}
          aria-controls="context-sidebar"
          aria-expanded={!workspaceLayout.layout.rightCollapsed}
          aria-label={t(workspaceLayout.layout.rightCollapsed ? "Show context sidebar" : "Hide context sidebar")}
          title={t(workspaceLayout.layout.rightCollapsed ? "Show context sidebar" : "Hide context sidebar")}
        ><span className="layout-toggle-icon right" aria-hidden="true"><i /></span></button>
        <SupervisionCenter
          tasks={supervisionViews.tasks}
          approvals={supervisionViews.approvals}
          t={t}
          locale={appSettings.general.language}
          open={supervisionOpen}
          onOpenChange={setSupervisionOpen}
          error={supervisionError}
          onDismissError={() => setSupervisionError("")}
          onOpenTask={(task) => openSupervisedSession(task)}
          onStopTask={(task) => cancelSupervisedTask(task.id, task.sessionId, "supervision")}
          onOpenTrace={openSupervisedTrace}
          onOpenApproval={(item) => openSupervisedSession(item)}
          onResolveApproval={(item, decision) => resolveSupervisedApproval({
            id: item.id,
            sessionId: item.sessionId,
            taskId: item.taskId,
          }, decision, "supervision")}
        />
        <span className={`connection-state ${connection}`}><i /> {connectionLabel}</span>
        <button className="icon-button ui-icon-button" title={t("Open settings")} aria-label={t("Open settings")} onClick={() => setSettingsTarget("general")}><Settings size={18} /></button>
      </header>

      <div
        className={`workspace-grid ${workspaceLayout.layout.leftCollapsed ? "left-collapsed" : ""} ${workspaceLayout.layout.rightCollapsed ? "right-collapsed" : ""}`}
        style={{
          "--left-sidebar-width": `${workspaceLayout.layout.leftCollapsed ? 0 : workspaceLayout.layout.leftWidth}px`,
          "--right-sidebar-width": `${workspaceLayout.layout.rightCollapsed ? 0 : workspaceLayout.layout.rightWidth}px`,
          "--left-divider-width": workspaceLayout.layout.leftCollapsed ? "0px" : `${WORKSPACE_LAYOUT_LIMITS.dividerWidth}px`,
          "--right-divider-width": workspaceLayout.layout.rightCollapsed ? "0px" : `${WORKSPACE_LAYOUT_LIMITS.dividerWidth}px`,
        } as CSSProperties}
      >
        <aside id="project-sidebar" className="left-sidebar" aria-hidden={workspaceLayout.layout.leftCollapsed}>
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
                          <span className="session-row-status">
                            {hasSessionDraft(conversation.id) && <span className="session-draft-label">{t("Draft")}</span>}
                            {runningTasks[conversation.id] && <span className="session-running-label">{t("Running")}</span>}
                          </span>
                        </button>
                        {isActiveProject && <button className="row-delete" title={t("Delete {name}", { name: conversation.title })} onClick={() => void deleteConversation(conversation)} disabled={Boolean(runningTasks[conversation.id]) || projectActionBusy} aria-label={t("Delete {name}", { name: conversation.title })}><Trash2 size={14} /></button>}
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

        <WorkspaceResizeHandle
          column="left"
          width={workspaceLayout.layout.leftWidth}
          maximum={workspaceLayout.maximumWidth("left")}
          collapsed={workspaceLayout.layout.leftCollapsed}
          active={workspaceLayout.resizingColumn === "left"}
          t={t}
          onPointerDown={(event) => workspaceLayout.beginResize("left", event)}
          onKeyDown={(event) => workspaceLayout.handleSeparatorKeyDown("left", event)}
          onDoubleClick={() => workspaceLayout.resetColumn("left")}
        />

        <main className="conversation-panel">
          <div className="conversation-header">
            <div><h1>{activeConversation?.title ?? t("New conversation")}</h1><p>{activeProject?.name ?? t("No project")}</p></div>
            <ModeSwitch mode={mode} onChange={changeMode} disabled={runtimeMutationBusy || memoryExtracting || !sessionId} t={t} />
          </div>
          <div className="transcript-shell">
          <div
            className="transcript"
            ref={transcriptRef}
            onScroll={handleTranscriptScroll}
            aria-label={t("Conversation transcript")}
            aria-busy={busy}
            tabIndex={0}
          >
            <div className="transcript-content">
            {entries.length === 0 && <Message role="StellarCode" timestampFallback={t("runtime")} language={appSettings.general.language} accent>
              {sessionId ? t("Runtime connected to {name}. Submit a task to start.", { name: activeProject?.name ?? t("No project") }) : connectionLabel}
            </Message>}
            <TranscriptWindow key={activeConversationId} items={transcriptGroups.filter((group) => group.kind !== "activity")} containerRef={transcriptRef} t={t} renderItem={(group) => group.kind === "task-status" ? (
              <AgentRunStatus
                entry={group.entry}
                now={timerNow}
                t={t}
                diffLoading={taskDiffLoading === group.entry.taskId}
                rollbackBusy={rollbackBusyTaskId === group.entry.taskId}
                selectedFilePath={taskDiff?.task_id === group.entry.taskId || taskDiffLoading === group.entry.taskId ? taskDiffSelectedPath : ""}
                onSelectFile={(path) => void viewTaskDiff(group.entry, path)}
                onRollback={() => void rollbackTaskChanges(group.entry)}
                key={group.id}
              />
            ) : group.kind === "team" ? (
              <TeamConversationCard
                entry={group.entry}
                t={t}
                compact
                onOpenDetails={openRuntimeActivity}
                renderMarkdown={(content) => renderAgentMarkdown(content, group.entry.taskId)}
                key={group.id}
              />
            ) : group.kind === "plan" ? (
              <PlanCard plan={group.entry} t={t} compact onOpenDetails={openRuntimeActivity} key={group.id} />
            ) : (
              <Message role={group.entry.kind === "user" ? t("You") : group.entry.kind === "error" ? t("Runtime error") : "StellarCode"} timestamp={group.entry.timestamp} timestampFallback={t("now")} language={appSettings.general.language} accent={group.entry.kind !== "user"} key={group.id}>
                {group.entry.kind === "user"
                  ? <UserMessageContent text={group.entry.text} attachments={group.entry.attachments} t={t} />
                  : group.entry.kind === "assistant"
                  ? <>{renderAgentMarkdown(group.entry.text, group.entry.taskId)}{group.entry.streaming && <span className="streaming-caret" aria-label={t("Streaming response")} />}</>
                  : group.entry.text}
              </Message>
            )} />
            {approval && <section className="approval-card pending">
              <div className="approval-icon">!</div><div className="approval-content">
                <div className="approval-heading"><strong>{t("Approval required")}</strong><span>{t(`${approval.data.danger_level} risk`)}</span></div>
                <p>{translateRuntimeText(appSettings.general.language, approval.data.risk_description)}</p><code>{approval.data.name} {JSON.stringify(approval.data.arguments)}</code>
                {approval.data.change_preview && <ChangePreviewPanel preview={approval.data.change_preview} t={t} expanded />}
                <small>{t("Task: {task} - Approval {position}", { task: approval.task_id ?? "", position: approvalQueue.length > 1 ? t("1 of {count}", { count: approvalQueue.length }) : t("pending") })}</small>
                <div className="approval-actions">
                  <button className="secondary-button" onClick={() => void resolveApproval("reject")} disabled={Boolean(resolvingApprovalId)}>{t("Reject")}</button>
                  <button className={`approval-allow-button risk-${approval.data.danger_level}`} onClick={() => void resolveApproval("approve")} disabled={Boolean(resolvingApprovalId)}>{t(resolvingApprovalId ? "Resolving..." : "Allow once")}</button>
                </div>
              </div>
            </section>}
            </div>
          </div>
          <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">
            {approval ? t("Approval required") : activeTaskFinalizing ? t("Finalizing workspace protection") : busy ? t("Agent is running") : ""}
          </span>
          {(!isFollowingBottom || unreadOutputCount > 0) && <button
            className="transcript-jump"
            type="button"
            onClick={() => jumpToBottom("auto")}
          >
            <span aria-hidden="true">↓</span>
            {unreadOutputCount > 0
              ? t("{count} new updates", { count: Math.min(unreadOutputCount, 99) })
              : t("Jump to latest")}
          </button>}
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
                <button type="button" title={t("Remove {name}", { name: attachment.display_name })} onClick={() => removeAttachment(attachment.id)} aria-label={t("Remove {name}", { name: attachment.display_name })}><X size={14} /></button>
              </div>)}
            </div>}
            {activeMemoryExtraction && <div className={`memory-extraction-status ${activeMemoryExtraction.status}`} role="status">
              <span className="memory-extraction-dot" aria-hidden="true" />
              <div>
                <strong>{t(activeMemoryExtraction.status === "starting" || activeMemoryExtraction.status === "extracting"
                  ? "Extracting long-term memory..."
                  : activeMemoryExtraction.status === "failed"
                  ? "Memory extraction failed"
                  : activeMemoryExtraction.processedMessageCount === 0
                  ? "No new user messages to extract."
                  : activeMemoryExtraction.factCount === 0
                  ? "No durable memory was found."
                  : "Long-term memory extracted")}</strong>
                <small>{activeMemoryExtraction.status === "failed"
                  ? activeMemoryExtraction.error
                  : activeMemoryExtraction.status === "starting" || activeMemoryExtraction.status === "extracting"
                  ? t("Analyzing {count} pending user message(s) with the LLM.", { count: activeMemoryExtraction.pendingMessageCount })
                  : t("Processed {messages} message(s): {saved} saved, {ignored} unchanged · {user} user / {conversation} conversation.", {
                    messages: activeMemoryExtraction.processedMessageCount,
                    saved: activeMemoryExtraction.savedCount,
                    ignored: activeMemoryExtraction.ignoredCount,
                    user: activeMemoryExtraction.userMemoryCount,
                    conversation: activeMemoryExtraction.conversationMemoryCount,
                  })}</small>
              </div>
              {!memoryExtracting && <button type="button" onClick={() => setMemoryExtractions((current) => {
                const next = { ...current };
                delete next[sessionId];
                return next;
              })} aria-label={t("Dismiss memory extraction status")}>×</button>}
            </div>}
            <div className="composer-input-layer">
              <div className="composer-input-highlight" ref={composerHighlightRef} aria-hidden="true">
                <HighlightedComposerPrompt value={prompt} />
              </div>
              <textarea ref={composerTextareaRef} value={prompt} onChange={(event) => {
                replacePrompt(event.currentTarget.value);
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
              <button className={`text-button memory-extract-button ${memoryExtracting ? "active" : ""}`} type="button" onClick={() => void extractConversationMemory()} disabled={runtimeMutationBusy || !sessionId || busy || memoryExtracting} title={t("Let the LLM extract durable facts from unprocessed user messages in this conversation")}>{t(memoryExtracting ? "Extracting memory..." : "Extract memory")}</button>
              <button className={`trace-toggle ${traceEnabled ? "active" : ""}`} type="button" onClick={() => void toggleTrace()} disabled={runtimeMutationBusy || memoryExtracting || !sessionId || Boolean(traceModeRequest.current)} title={tracePath || t("Record complete LLM, tool, approval, and Runtime event logs for this conversation")}>{t(traceEnabled ? "Trace On" : "Trace Off")}</button>
              <div className="access-switch" aria-label={t("Access mode")}>
                <button className={accessMode === "restricted" ? "active" : ""} type="button" onClick={() => void changeAccessMode("restricted")} disabled={runtimeMutationBusy || !sessionId} title={t("Medium- and high-risk operations require approval")}>{t("Normal")}</button>
                <button className={accessMode === "balanced" ? "active balanced" : ""} type="button" onClick={() => void changeAccessMode("balanced")} disabled={runtimeMutationBusy || !sessionId} title={t("Medium-risk operations are approved automatically; high-risk operations still require approval")}>{t("Balanced")}</button>
                <button className={accessMode === "full-access" ? "active dangerous" : ""} type="button" onClick={() => void changeAccessMode("full-access")} disabled={runtimeMutationBusy || !sessionId} title={t("All approval prompts are bypassed")}>{t("Full access")}</button>
              </div>
              <span className="composer-hint">{activeTaskFinalizing ? t("Finalizing workspace protection") : busy ? t("Agent is running") : ragIndexing ? t("RAG index is building") : sessionId ? t(appSettings.general.send_shortcut === "enter" ? "Enter to send - Shift+Enter for newline" : "Ctrl+Enter to send") : connectionLabel}</span>
              <button className="secondary-button" type="button" onClick={() => void cancelTask()} disabled={!busy || !activeTaskId || cancelling || activeTaskFinalizing}>{t(activeTaskFinalizing ? "Finalizing..." : cancelling ? "Stopping..." : "Stop")}</button>
              <button className="primary-button" type="submit" disabled={!sessionId || runtimeMutationBusy || memoryExtracting || (!prompt.trim() && attachments.length === 0)}>{t("Send")}</button>
            </div>
          </form>
        </main>

        <WorkspaceResizeHandle
          column="right"
          width={workspaceLayout.layout.rightWidth}
          maximum={workspaceLayout.maximumWidth("right")}
          collapsed={workspaceLayout.layout.rightCollapsed}
          active={workspaceLayout.resizingColumn === "right"}
          t={t}
          onPointerDown={(event) => workspaceLayout.beginResize("right", event)}
          onKeyDown={(event) => workspaceLayout.handleSeparatorKeyDown("right", event)}
          onDoubleClick={() => workspaceLayout.resetColumn("right")}
        />

        <aside id="context-sidebar" className="context-sidebar" aria-hidden={workspaceLayout.layout.rightCollapsed}>
          <div className="right-sidebar-tabs" role="tablist" aria-label={t("Right sidebar") }>
            <button
              type="button"
              id="right-context-tab"
              role="tab"
              aria-selected={rightSidebarView === "context"}
              aria-controls="right-context-panel"
              className={rightSidebarView === "context" ? "active" : ""}
              onClick={() => setRightSidebarView("context")}
            >{t("Context")}</button>
            <button
              type="button"
              id="right-activity-tab"
              role="tab"
              aria-selected={rightSidebarView === "activity"}
              aria-controls="right-activity-panel"
              className={rightSidebarView === "activity" ? "active" : ""}
              onClick={() => setRightSidebarView("activity")}
            >{t("Activity")}{sidebarActivityEntries.length > 0 && <span>{sidebarActivityEntries.length}</span>}</button>
            <button
              type="button"
              id="right-changes-tab"
              role="tab"
              aria-selected={rightSidebarView === "changes"}
              aria-controls="right-changes-panel"
              className={rightSidebarView === "changes" ? "active" : ""}
              onClick={() => setRightSidebarView("changes")}
              disabled={!taskDiff}
            >{t("Changes")}{taskDiff && <span>{taskDiff.changed_files.length}</span>}</button>
            {filePreviewTabs.map((tab, index) => <div className="right-file-tab-group" key={tab.id}>
              <button
                type="button"
                id={`right-file-tab-${index}`}
                role="tab"
                aria-selected={rightSidebarView === "file" && activeFilePreviewId === tab.id}
                aria-busy={tab.loading}
                aria-controls="right-file-panel"
                className={`right-file-tab ${rightSidebarView === "file" && activeFilePreviewId === tab.id ? "active" : ""}`}
                onClick={() => {
                  setActiveFilePreviewId(tab.id);
                  setRightSidebarView("file");
                }}
                title={tab.preview?.absolute_path || tab.relativePath}
              ><strong>{tab.fileName}</strong>{tab.loading && <i aria-hidden="true" />}</button>
              <button
                type="button"
                className="right-file-tab-close"
                onClick={() => closeFilePreview(tab.id)}
                aria-label={t("Close {name}", { name: tab.fileName })}
                title={t("Close {name}", { name: tab.fileName })}
              >×</button>
            </div>)}
          </div>
          <div
            id="right-context-panel"
            className="context-sidebar-content"
            role="tabpanel"
            aria-labelledby="right-context-tab"
            hidden={rightSidebarView !== "context"}
          >
          <PanelSection title={t("Run context")}>
            <DefinitionRow label={t("Model")} value={t(model)} /><DefinitionRow label={t("Mode")} value={mode === "react" ? "ReAct" : t(mode === "plan" ? "Plan" : "Team")} />
            <DefinitionRow label={t("Access")} value={t(accessMode === "restricted" ? "normal" : accessMode === "balanced" ? "balanced" : "full access")} emphasis={accessMode === "full-access"} /><DefinitionRow label={t("Workspace")} value={activeProject?.name ?? t("none")} />
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
          </div>
          <div
            id="right-activity-panel"
            className="activity-sidebar-content"
            role="tabpanel"
            aria-labelledby="right-activity-tab"
            hidden={rightSidebarView !== "activity"}
          >
            <RuntimeActivityPanel
              entries={sidebarActivityEntries}
              language={appSettings.general.language}
              t={t}
              renderMarkdown={(content, taskId) => renderAgentMarkdown(content, taskId)}
            />
          </div>
          <div
            id="right-changes-panel"
            className="changes-sidebar-content"
            role="tabpanel"
            aria-labelledby="right-changes-tab"
            hidden={rightSidebarView !== "changes"}
          >
            {taskDiff ? <ErrorBoundary t={t} resetKey={taskDiff.task_id}><Suspense fallback={<div className="right-sidebar-empty">{t("Loading changes...")}</div>}>
              <ReviewChangesWorkbench
                embedded
                result={taskDiff}
                projectId={activeProjectId}
                workspace={workspace}
                rollbackBusy={rollbackBusyTaskId === taskDiff.task_id}
                canRollback={Boolean(
                  reviewedTaskEntry
                  && reviewedTaskEntry.changes?.rollback_available
                  && !busy
                  && !activeProjectTaskRunning
                  && !taskDiff.rolled_back
                )}
                t={t}
                selectedPath={taskDiffSelectedPath}
                showFileSidebar={false}
                onClose={() => {
                  setTaskDiff(null);
                  setRightSidebarView("context");
                }}
                onRollback={async () => {
                  if (!reviewedTaskEntry) return false;
                  return rollbackTaskChanges(reviewedTaskEntry);
                }}
              />
            </Suspense></ErrorBoundary> : <div className="right-sidebar-empty">{t("No task changes loaded.")}</div>}
          </div>
          <div
            id="right-file-panel"
            className="file-sidebar-content"
            role="tabpanel"
            aria-labelledby={activeFilePreviewTabIndex >= 0 ? `right-file-tab-${activeFilePreviewTabIndex}` : undefined}
            hidden={rightSidebarView !== "file"}
          >
            {activeFilePreviewTab?.preview ? <Suspense fallback={<div className="right-sidebar-empty">{t("Loading file...")}</div>}>
              <ErrorBoundary t={t} resetKey={activeFilePreviewTab.id}><FilePreviewPanel preview={activeFilePreviewTab.preview} t={t} /></ErrorBoundary>
            </Suspense> : activeFilePreviewTab?.error
              ? <div className="right-sidebar-empty file-preview-error"><div><strong>{t("File preview failed")}</strong><p>{activeFilePreviewTab.error}</p><button type="button" className="secondary-button" onClick={() => closeFilePreview(activeFilePreviewTab.id)}>{t("Close")}</button></div></div>
              : <div className="right-sidebar-empty">{t("Loading file...")}</div>}
          </div>
        </aside>
      </div>

      {settingsTarget && settingsSnapshot && <ErrorBoundary overlay t={t} onDismiss={() => setSettingsTarget(null)}><Suspense fallback={<div className="settings-overlay" role="status">{t("Loading settings...")}</div>}><SettingsPage
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
        onMemoryRefresh={(scope) => refreshMemorySnapshot(scope)}
        onMemorySave={saveMemory}
        onMemoryDelete={deleteMemory}
        onMemoryClear={clearMemory}
        onPromptRefresh={refreshPromptSnapshot}
        onSkillRefresh={refreshSkillSnapshot}
        onSkillDiff={loadSkillDiff}
        onSkillSetEnabled={setSkillEnabled}
        onSkillReload={reloadSkills}
        onSkillUpdate={updateBundledSkill}
        onSkillKeepCustom={keepCustomSkill}
        onSkillRestoreDefault={restoreBundledSkill}
        onSkillPrepareDirectory={prepareSkillDirectory}
        onBrowserRefresh={refreshBrowserSnapshot}
        onBrowserProbe={probeBrowser}
        onBrowserConnect={connectBrowser}
        onBrowserDisconnect={disconnectBrowser}
        onBrowserTabs={readBrowserTabs}
        onDiagnosticsRefresh={refreshDiagnosticsSnapshot}
        onDiagnosticsRun={runDiagnostics}
        onDiagnosticsCancel={cancelDiagnostics}
      /></Suspense></ErrorBoundary>}
    </div>
  );
}

function WorkspaceResizeHandle({
  column,
  width,
  maximum,
  collapsed,
  active,
  t,
  onPointerDown,
  onKeyDown,
  onDoubleClick,
}: {
  column: WorkspaceColumn;
  width: number;
  maximum: number;
  collapsed: boolean;
  active: boolean;
  t: Translator;
  onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => void;
  onDoubleClick: () => void;
}) {
  const minimum = column === "left" ? WORKSPACE_LAYOUT_LIMITS.leftMin : WORKSPACE_LAYOUT_LIMITS.rightMin;
  const label = t(column === "left" ? "Resize project sidebar" : "Resize context sidebar");
  return <div
    className={`workspace-resizer ${column} ${active ? "active" : ""} ${collapsed ? "collapsed" : ""}`}
    role="separator"
    aria-orientation="vertical"
    aria-label={label}
    aria-controls={column === "left" ? "project-sidebar" : "context-sidebar"}
    aria-valuemin={minimum}
    aria-valuemax={maximum}
    aria-valuenow={width}
    aria-valuetext={`${width} px`}
    aria-hidden={collapsed}
    tabIndex={collapsed ? -1 : 0}
    title={`${label} · ${t("Double-click to reset width")}`}
    onPointerDown={onPointerDown}
    onKeyDown={onKeyDown}
    onDoubleClick={onDoubleClick}
  ><span className="workspace-resizer-grip" aria-hidden="true" /></div>;
}

function SidebarSection({ title, action, grow = false, onAction, children }: { title: string; action: string; grow?: boolean; onAction?: () => void; children: React.ReactNode }) {
  return <section className={`sidebar-section ${grow ? "grow" : ""}`}><div className="section-heading"><span>{title}</span><button aria-label={`${action} ${title}`} onClick={onAction} disabled={!onAction}>{action}</button></div>{children}</section>;
}

function localizeSupervisionActivity(language: Language, activity: string) {
  if (language === "en" || !activity) return activity;
  const exact = translate(language, activity);
  if (exact !== activity) return exact;
  const patterns: Array<{
    pattern: RegExp;
    key: string;
    values: (match: RegExpMatchArray) => TranslationValues;
  }> = [
    { pattern: /^Waiting for approval: (.+)$/, key: "Waiting for approval: {tool}", values: (match) => ({ tool: match[1] }) },
    { pattern: /^Running plan step (.+)$/, key: "Running plan step {step}", values: (match) => ({ step: match[1] }) },
    { pattern: /^Plan step (.+) completed$/, key: "Plan step {step} completed", values: (match) => ({ step: match[1] }) },
    { pattern: /^Plan step (.+) failed$/, key: "Plan step {step} failed", values: (match) => ({ step: match[1] }) },
    { pattern: /^Plan step (.+) skipped$/, key: "Plan step {step} skipped", values: (match) => ({ step: match[1] }) },
    { pattern: /^Team started with (\d+) workers$/, key: "Team started with {count} workers", values: (match) => ({ count: match[1] }) },
    { pattern: /^Running (.+)$/, key: "Running {name}", values: (match) => ({ name: match[1] }) },
    { pattern: /^(.+) completed$/, key: "{name} completed", values: (match) => ({ name: match[1] }) },
    { pattern: /^(.+) failed$/, key: "{name} failed", values: (match) => ({ name: match[1] }) },
    { pattern: /^Approval (.+)$/, key: "Approval {decision}", values: (match) => ({ decision: translate(language, match[1]) }) },
  ];
  for (const item of patterns) {
    const match = activity.match(item.pattern);
    if (match) return translate(language, item.key, item.values(match));
  }
  const agentStatus = activity.match(/^(.+): (queued|working|completed|failed)$/);
  if (agentStatus) return `${agentStatus[1]}: ${translate(language, agentStatus[2])}`;
  return activity;
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

function Message({ role, timestamp, timestampFallback, language, accent = false, children }: {
  role: string;
  timestamp?: string;
  timestampFallback?: string;
  language: Language;
  accent?: boolean;
  children: React.ReactNode;
}) {
  return <article className={`message ${accent ? "assistant" : "user"}`}><div className="message-body"><div className="message-meta"><strong>{role}</strong><TranscriptTime timestamp={timestamp} language={language} fallback={timestampFallback} /></div><div className="message-content">{children}</div></div></article>;
}

function TranscriptTime({ timestamp, language, fallback = "" }: { timestamp?: string; language: Language; fallback?: string }) {
  const parsed = timestamp ? new Date(timestamp) : null;
  if (!parsed || Number.isNaN(parsed.getTime())) return <time>{fallback}</time>;
  const locale = language === "zh-CN" ? "zh-CN" : "en-US";
  const now = new Date();
  const sameDay = parsed.getFullYear() === now.getFullYear()
    && parsed.getMonth() === now.getMonth()
    && parsed.getDate() === now.getDate();
  const label = new Intl.DateTimeFormat(locale, sameDay
    ? { hour: "2-digit", minute: "2-digit" }
    : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(parsed);
  const title = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "medium" }).format(parsed);
  return <time dateTime={timestamp} title={title}>{label}</time>;
}

function RuntimeActivityPanel({ entries, language, t, renderMarkdown }: {
  entries: SidebarActivityEntry[];
  language: Language;
  t: Translator;
  renderMarkdown: (content: string, taskId?: string) => React.ReactNode;
}) {
  const tools = entries.reduce((count, entry) => count + (
    entry.kind === "tool"
      ? 1
      : entry.kind === "team"
        ? entry.agents.reduce((agentCount, agent) => agentCount + agent.items.filter((item) => item.kind === "tool").length, 0)
        : 0
  ), 0);
  const approvals = entries.filter((entry) => entry.kind === "approval").length;
  if (entries.length === 0) {
    return <div className="right-sidebar-empty activity-empty">
      <div><strong>{t("No runtime activity yet.")}</strong><p>{t("Tool calls, resolved approvals, execution details, and team conversations appear here.")}</p></div>
    </div>;
  }
  return <div className="runtime-activity-panel">
    <header className="runtime-activity-header">
      <div><span className="plan-eyebrow">{t("Runtime activity")}</span><strong>{t("{count} activity item(s)", { count: entries.length })}</strong></div>
      <div><span>{t("{count} tool(s)", { count: tools })}</span><span>{t("{count} approval result(s)", { count: approvals })}</span></div>
    </header>
    <div className="runtime-activity-list">
      {entries.map((entry) => entry.kind === "tool" ? <section className="runtime-activity-item kind-tool" key={entry.id}>
        <ToolCard
          name={entry.name}
          status={entry.status}
          arguments={entry.arguments}
          detail={entry.detail}
          elapsed={entry.elapsed === undefined ? "" : `${entry.elapsed} ms`}
          changePreview={entry.changePreview}
          t={t}
        />
      </section> : entry.kind === "approval" ? <section className="runtime-activity-item kind-approval" key={entry.id}>
        <ApprovalHistoryCard entry={entry} t={t} language={language} />
      </section> : entry.kind === "thinking" ? <section className="runtime-activity-item kind-thinking" key={entry.id}>
        <div className="runtime-activity-item-heading"><strong>{t("Thinking")}</strong><TranscriptTime timestamp={entry.timestamp} language={language} fallback={t("now")} /></div>
        <p>{entry.text}</p>
      </section> : entry.kind === "plan" ? <section className="runtime-activity-item kind-plan" key={entry.id}>
        <PlanCard plan={entry} t={t} />
      </section> : <section className="runtime-activity-item kind-team" key={entry.id}>
        <TeamConversationCard entry={entry} t={t} renderMarkdown={(content) => renderMarkdown(content, entry.taskId)} />
      </section>)}
    </div>
  </div>;
}

function TeamConversationCard({ entry, t, renderMarkdown, compact = false, onOpenDetails }: {
  entry: TeamTranscriptEntry;
  t: Translator;
  renderMarkdown: (content: string) => React.ReactNode;
  compact?: boolean;
  onOpenDetails?: () => void;
}) {
  const finished = entry.agents.filter((agent) => agent.status === "completed").length;
  const failed = entry.agents.filter((agent) => agent.status === "failed").length;
  const status = entry.phase === "running"
    ? "Working"
    : entry.phase === "completed" ? "Completed" : "Failed";
  if (compact) {
    const roleSummaries = ["planner", "worker", "reviewer"].flatMap((role) => {
      const agents = entry.agents.filter((agent) => agent.role === role);
      if (agents.length === 0) return [];
      const roleFinished = agents.filter((agent) => agent.status === "completed").length;
      const roleStatus = agents.some((agent) => agent.status === "failed")
        ? "Failed"
        : agents.some((agent) => agent.status === "working")
          ? "Working"
          : agents.every((agent) => agent.status === "completed") ? "Completed" : "Queued";
      return [{ role, count: agents.length, finished: roleFinished, status: roleStatus }];
    });
    return <article className={`team-conversation-card team-summary-card phase-${entry.phase}`}>
      <header className="team-summary-header">
        <div className="team-card-summary-copy">
          <span className="plan-eyebrow">{t("Team collaboration")}</span>
          <strong>{t("{count} worker(s) · {finished} finished", { count: entry.workerCount, finished })}</strong>
          <small>{entry.phase === "running" ? t("Sub-agents are working through the MessageBus.") : entry.message || t("Team run finished.")}</small>
        </div>
        <div className="summary-card-actions"><span className={`team-run-status ${entry.phase}`}>{t(status)}</span><button type="button" onClick={onOpenDetails}>{t("View details")}</button></div>
      </header>
      {roleSummaries.length > 0 && <div className="team-role-summary">{roleSummaries.map((role) => <div key={role.role}>
        <strong>{t(teamRoleLabel(role.role))}</strong>
        <span className={`status-${role.status.toLowerCase()}`}>{role.count > 1 ? `${role.finished}/${role.count} ${t("Completed")}` : t(role.status)}</span>
      </div>)}</div>}
    </article>;
  }
  return <article className={`team-conversation-card phase-${entry.phase}`}>
    <details>
      <summary>
        <div className="team-card-summary-copy">
          <span className="plan-eyebrow">{t("Team collaboration")}</span>
          <strong>{t("{count} worker(s) · {finished} finished", { count: entry.workerCount, finished })}</strong>
          <small>{entry.phase === "running" ? t("Sub-agents are working through the MessageBus.") : entry.message || t("Team run finished.")}</small>
        </div>
        <span className={`team-run-status ${entry.phase}`}>{t(status)}</span>
      </summary>
      <div className="team-dialogues">
        {entry.agents.length === 0 ? <p className="team-dialogue-empty">{t("No sub-agent messages yet.")}</p> : entry.agents.map((agent) => (
          <section className={`team-agent-dialogue status-${agent.status}`} key={agent.id}>
            <details>
              <summary aria-label={t("Toggle {name} dialogue", { name: agent.name })}>
                <div>
                  <span className="team-agent-role">{t(teamRoleLabel(agent.role))}</span>
                  <strong>{agent.name}</strong>
                  {agent.teamTaskId !== "planning" && <small>{agent.teamTaskId}</small>}
                </div>
                <span>{t(teamStatusLabel(agent.status))}</span>
              </summary>
              <div className="team-dialogue-messages">
                {agent.items.map((item) => item.kind === "tool" ? (
                  <ToolCard
                    key={item.id}
                    name={item.toolName || t("Tool")}
                    arguments={item.toolArguments}
                    status={item.toolStatus || "running"}
                    detail={item.content || ""}
                    elapsed={item.elapsed === undefined ? "" : `${item.elapsed} ms`}
                    t={t}
                  />
                ) : (
                  <div className={`team-dialogue-bubble ${item.direction === "inbound" ? "from-lead" : "from-agent"}`} key={item.id}>
                    <small>{item.direction === "inbound" ? t("Lead → {name}", { name: agent.name }) : `${agent.name} → ${t("Lead")}`} · {t(teamMessageKindLabel(item.messageKind))}</small>
                    <div>{item.direction === "outbound" ? renderMarkdown(item.content || "") : item.content}</div>
                  </div>
                ))}
              </div>
            </details>
          </section>
        ))}
      </div>
      {failed > 0 && <p className="team-card-warning">{t("{count} sub-agent(s) reported a failure.", { count: failed })}</p>}
    </details>
  </article>;
}

function teamRoleLabel(role: string) {
  return { planner: "Planner", worker: "Worker", reviewer: "Reviewer" }[role] ?? role;
}

function teamStatusLabel(status: TeamAgentStatus) {
  return { queued: "Queued", working: "Working", completed: "Completed", failed: "Failed" }[status];
}

function teamMessageKindLabel(kind?: string) {
  if (kind === "review_request") return "Review request";
  if (kind === "result") return "Reply";
  if (kind === "error") return "Error";
  return "Request";
}

function AgentRunStatus({ entry, now, t, diffLoading, rollbackBusy, selectedFilePath, onSelectFile, onRollback }: {
  entry: TaskStatusTranscriptEntry;
  now: number;
  t: Translator;
  diffLoading: boolean;
  rollbackBusy: boolean;
  selectedFilePath: string;
  onSelectFile: (path: string) => void;
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
      {entry.phase === "running" && protection?.protected && <div className="snapshot-ready">{t(protection.worktree_isolated ? "Isolated in a protected task Git worktree" : "Protected by an automatic task Git snapshot")}</div>}
      {entry.phase === "running" && protection && !protection.protected && <div className="snapshot-warning">{t("Workspace snapshot unavailable: {error}", { error: protection.error || t("unknown error") })}</div>}
      {entry.changes?.has_changes && <div className={`task-change-set ${entry.changes.rolled_back ? "rolled-back" : ""}`}>
        <div className="task-change-heading"><strong>{entry.changes.rolled_back ? t("Task changes rolled back") : t("{count} protected file(s) changed", { count: entry.changes.changed_files.length })}</strong><span>+{entry.changes.additions} -{entry.changes.deletions}</span></div>
        <div className="task-change-files" aria-label={t("Changed files")}>{entry.changes.changed_files.map((file) => <button
          type="button"
          className={selectedFilePath === file.path ? "active" : ""}
          onClick={() => onSelectFile(file.path)}
          disabled={diffLoading || rollbackBusy || !entry.changes?.diff_available}
          title={t("View changes for {path}", { path: file.path })}
          key={file.path}
        ><span>{changeStatusMarker(file.status)}</span><code>{file.path}</code><small><i>+{file.additions}</i><b>-{file.deletions}</b></small></button>)}</div>
        {entry.changes.rollback_block_reason && <p className="snapshot-warning">{t(entry.changes.rollback_block_reason)}</p>}
        {entry.rollbackError && <p className="rollback-error">{entry.rollbackError}</p>}
        <div className="task-change-actions">
          <button className="secondary-button danger-button" onClick={onRollback} disabled={rollbackBusy || !entry.changes.rollback_available}>{t(rollbackBusy || entry.rollbackState === "running" ? "Rolling back..." : entry.phase === "completed" ? "Undo task changes" : "Rollback task changes")}</button>
        </div>
      </div>}
    </div>
  </article>;
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

function fileNameFromPath(path: string) {
  return path.replace(/\\/g, "/").split("/").filter(Boolean).pop() ?? "";
}

function filePreviewTabId(path: string) {
  return `file:${path.replace(/\\/g, "/").toLocaleLowerCase()}`;
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

function ToolCard({ name, status, detail, elapsed, arguments: args, changePreview, t }: { name: string; status: ToolStatus; detail: string; elapsed: string; arguments?: Record<string, unknown>; changePreview?: FileChangePreview; t: Translator }) {
  return <ToolActivityCard name={name} status={status} detail={detail} elapsed={elapsed} arguments={args} t={t}>
    {changePreview && <ChangePreviewPanel preview={changePreview} t={t} expanded />}
  </ToolActivityCard>;
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

function PlanCard({ plan, t, compact = false, onOpenDetails }: {
  plan: PlanTranscriptEntry;
  t: Translator;
  compact?: boolean;
  onOpenDetails?: () => void;
}) {
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

  if (compact) {
    const visibleSteps = steps.slice(0, 5);
    return <article className={`plan-card plan-summary-card plan-${overall.toLowerCase().replace(" ", "-")}`}>
      <header className="plan-header">
        <div><span className="plan-eyebrow">{t("Execution plan")}</span><strong>{plan.planning ? t("Creating execution plan...") : plan.summary || plan.goal}</strong>{(plan.planning || plan.summary) && <small>{plan.goal}</small>}</div>
        <div className="summary-card-actions"><span className="plan-overall">{t(plan.planning ? "Working" : overall)}</span><button type="button" onClick={onOpenDetails}>{t("View details")}</button></div>
      </header>
      <div className="plan-progress"><span style={{ width: `${progress}%` }} /></div>
      {!plan.planning && <>
        <div className="plan-progress-label"><span>{t("{settled} of {total} steps finished", { settled, total: steps.length })}</span><strong>{t("{count} completed", { count: completed })}</strong></div>
        <ol className="plan-summary-steps">
          {visibleSteps.map((step, index) => <li className={`step-${step.status}`} key={step.id}>
            <span className="plan-step-marker">{planStepMarker(step.status, index + 1)}</span><strong>{step.description}</strong><span>{t(step.status)}</span>
          </li>)}
        </ol>
        {steps.length > visibleSteps.length && <p className="plan-summary-overflow">{t("{count} more step(s) in Activity", { count: steps.length - visibleSteps.length })}</p>}
      </>}
    </article>;
  }

  if (plan.planning) {
    return <article className="plan-card plan-planning">
      <header className="plan-header"><div><span className="plan-eyebrow">{t("Execution plan")}</span><strong>{t("Creating execution plan...")}</strong><small>{plan.goal}</small></div><span className="plan-overall">{t("Working")}</span></header>
      <pre className="plan-planning-stream">{plan.planningText || t("Waiting for the planner stream...")}<span className="streaming-caret" aria-label={t("Streaming response")} /></pre>
    </article>;
  }

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
          {step.streaming && step.streamText && <p className="plan-step-stream">{step.streamText}<span className="streaming-caret" aria-label={t("Streaming response")} /></p>}
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

function removeUnappliedAssistantAnswer(
  entries: TranscriptEntry[],
  taskId: string | undefined,
): TranscriptEntry[] {
  if (!taskId) return entries;
  return entries.filter((entry) => !(
    entry.kind === "assistant" && entry.taskId === taskId
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
  retainedTaskId: string | undefined,
  t: Translator,
): TranscriptEntry[] {
  return entries.map((entry) => {
    if (entry.kind === "approval" && entry.status === "pending") {
      if (retainedTaskId && entry.taskId === retainedTaskId) return entry;
      return { ...entry, status: "interrupted" };
    }
    if (
      entry.kind === "tool"
      && (entry.status === "running" || entry.status === "waiting_approval")
    ) {
      if (retainedTaskId && entry.taskId === retainedTaskId) return entry;
      return {
        ...entry,
        status: "failed",
        detail: t("Runtime interrupted; the tool outcome is unconfirmed."),
      };
    }
    if (
      entry.kind === "task-status"
      && entry.phase === "running"
      && entry.taskId !== retainedTaskId
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

function teamAgentKey(name: string, teamTaskId: string) {
  return `${name}::${teamTaskId}`;
}

function upsertTeamEntry(
  entries: TranscriptEntry[],
  next: TeamTranscriptEntry,
): TranscriptEntry[] {
  const index = entries.findIndex((entry) => entry.kind === "team" && entry.taskId === next.taskId);
  if (index < 0) return [...entries, next];
  return entries.map((entry, entryIndex) => entryIndex === index && entry.kind === "team"
    ? {
        ...entry,
        ...next,
        id: entry.id,
        runId: next.runId || entry.runId,
        workerCount: next.workerCount || entry.workerCount,
        agents: next.agents.length ? next.agents : entry.agents,
        timestamp: entry.timestamp ?? next.timestamp,
      }
    : entry);
}

function updateTeamAgent(
  entries: TranscriptEntry[],
  taskId: string,
  runId: string,
  agent: Omit<TeamAgentDialogue, "items">,
  item?: TeamDialogueItem,
): TranscriptEntry[] {
  const existing = entries.find(
    (entry): entry is TeamTranscriptEntry => entry.kind === "team" && entry.taskId === taskId,
  );
  const base: TeamTranscriptEntry = existing ?? {
    id: `team-${taskId}`,
    kind: "team",
    taskId,
    runId,
    workerCount: 0,
    phase: "running",
    agents: [],
    timestamp: item?.timestamp,
  };
  const agentIndex = base.agents.findIndex((candidate) => candidate.id === agent.id);
  const previous = agentIndex < 0 ? undefined : base.agents[agentIndex];
  const previousItems = previous?.items ?? [];
  const activeStreamIndex = item?.kind === "message"
    && item.direction === "outbound"
    && !item.streaming
    ? previousItems.findIndex((candidate) => (
        candidate.kind === "message"
        && candidate.direction === "outbound"
        && candidate.streaming
      ))
    : -1;
  const nextAgent: TeamAgentDialogue = {
    ...previous,
    ...agent,
    items: !item
      ? previousItems
      : activeStreamIndex >= 0
      ? previousItems.map((candidate, index) => index === activeStreamIndex
          ? { ...item, id: candidate.id, timestamp: candidate.timestamp ?? item.timestamp, streaming: false }
          : candidate)
      : upsertTeamDialogueItem(previousItems, item),
  };
  const agents = agentIndex < 0
    ? [...base.agents, nextAgent]
    : base.agents.map((candidate, index) => index === agentIndex ? nextAgent : candidate);
  return upsertTeamEntry(entries, { ...base, runId: runId || base.runId, agents });
}

function finishTeamRun(
  entries: TranscriptEntry[],
  taskId: string,
  runId: string,
  phase: TeamTranscriptEntry["phase"],
  message: string,
  timestamp: string,
): TranscriptEntry[] {
  const existing = entries.find(
    (entry): entry is TeamTranscriptEntry => entry.kind === "team" && entry.taskId === taskId,
  );
  const pendingStatus: TeamAgentStatus = phase === "completed" ? "completed" : "failed";
  const agents = (existing?.agents ?? []).map((agent) => (
    agent.status === "working" || agent.status === "queued"
      ? { ...agent, status: pendingStatus }
      : agent
  ));
  return upsertTeamEntry(entries, {
    id: existing?.id ?? `team-${taskId}`,
    kind: "team",
    taskId,
    runId: runId || existing?.runId || "team",
    workerCount: existing?.workerCount ?? 0,
    phase,
    message,
    agents,
    timestamp: existing?.timestamp ?? timestamp,
  });
}

function upsertTeamDialogueItem(items: TeamDialogueItem[], next: TeamDialogueItem) {
  const index = items.findIndex((item) => item.id === next.id);
  if (index < 0) return [...items, next];
  return items.map((item, itemIndex) => itemIndex === index
    ? next.streaming && item.streaming
      ? { ...item, ...next, content: `${item.content ?? ""}${next.content ?? ""}`, timestamp: item.timestamp ?? next.timestamp }
      : { ...item, ...next, timestamp: item.timestamp ?? next.timestamp }
    : item);
}

function applyTeamAgentDelta(
  entries: TranscriptEntry[],
  taskId: string,
  agent: Omit<TeamAgentDialogue, "items">,
  text: string,
  reset: boolean,
  timestamp: string,
): TranscriptEntry[] {
  const streamId = `team-stream-${agent.id}`;
  if (reset) {
    return entries.map((entry) => entry.kind === "team" && entry.taskId === taskId
      ? {
          ...entry,
          agents: entry.agents.map((candidate) => candidate.id === agent.id
            ? { ...candidate, items: candidate.items.filter((item) => item.id !== streamId) }
            : candidate),
        }
      : entry);
  }
  if (!text) return entries;
  return updateTeamAgent(entries, taskId, "", agent, {
    id: streamId,
    kind: "message",
    direction: "outbound",
    messageKind: "Reply",
    content: text,
    streaming: true,
    timestamp,
  });
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

function appendPlanPlanningDelta(
  entries: TranscriptEntry[],
  taskId: string | undefined,
  text: string,
): TranscriptEntry[] {
  if (!taskId || !text) return entries;
  return entries.map((entry) => entry.kind === "plan" && entry.taskId === taskId
    ? { ...entry, planningText: `${entry.planningText ?? ""}${text}` }
    : entry);
}

function appendPlanStepDelta(
  entries: TranscriptEntry[],
  taskId: string | undefined,
  stepId: string,
  text: string,
) {
  if (!text) return "";
  const plan = entries.find(
    (entry): entry is PlanTranscriptEntry => entry.kind === "plan" && (!taskId || entry.taskId === taskId),
  );
  const step = plan?.steps.find((candidate) => candidate.id === stepId);
  return `${step?.streamText ?? ""}${text}`;
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
  if (entry.kind === "thinking" || entry.kind === "tool" || entry.kind === "approval" || entry.kind === "plan" || entry.kind === "team") {
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

function hasSessionDraft(sessionId: string) {
  const draft = sessionDraftStore.get(sessionId);
  return Boolean(draft && (draft.prompt.trim() || draft.attachments.length > 0));
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
  for (const entry of orderTerminalTaskSummaries(entries)) {
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
    if (entry.kind === "team") {
      groups.push({ id: entry.id, kind: "team", entry });
      continue;
    }
    groups.push({ id: entry.id, kind: "message", entry });
  }
  return groups;
}

/** Pending approvals stay actionable in the transcript; resolved records move to Activity. */
function isSidebarActivityEntry(entry: TranscriptEntry): entry is SidebarActivityEntry {
  if (entry.kind === "approval") return entry.status !== "pending";
  return entry.kind === "thinking"
    || entry.kind === "tool"
    || entry.kind === "plan"
    || entry.kind === "team";
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
