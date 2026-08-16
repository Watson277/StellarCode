export const RUNTIME_PROTOCOL_VERSION = 1 as const;

export type AgentMode = "react" | "plan" | "team";
export type AccessMode = "restricted" | "full-access";
export type TaskTerminalStatus = "completed" | "failed" | "cancelled";
export type ApprovalDecision = "approve" | "reject" | "skip" | "modify";
export type McpServerState = "disabled" | "starting" | "ready" | "error";
export type RagStatus = "idle" | "indexing" | "error";
export type DiagnosticsStatus = "not_run" | "running" | "completed" | "failed" | "cancelled";
export type DiagnosticSeverity = "error" | "warning" | "information" | "hint";

export interface MemoryEntryInfo {
  id: string;
  content: string;
  type: string;
  timestamp: number;
  metadata: Record<string, string>;
  token_count: number;
}

export interface MemorySnapshot {
  scope: "project";
  entries: MemoryEntryInfo[];
  count: number;
  token_count: number;
  storage_path: string;
  warnings: string[];
  query?: string;
}

export interface SkillInfo {
  name: string;
  description: string;
  version?: string | null;
  author?: string | null;
  tags: string[];
  source: "user" | "project";
  enabled: boolean;
  skill_md_path: string;
  references_path?: string | null;
}

export interface SkillDetail extends SkillInfo {
  body: string;
}

export interface SkillSnapshot {
  skills: SkillInfo[];
  total_count: number;
  enabled_count: number;
  warnings: string[];
  state_path: string;
  user_dir: string;
  project_dir: string;
}

export type SkillInstallScope = "user" | "project";

export interface SkillDirectoryResult {
  scope: SkillInstallScope;
  path: string;
}

export interface BrowserProbeSnapshot {
  port: number;
  connected: boolean;
  browser_url: string;
  browser_version: string;
  error: string;
}

export interface BrowserSnapshot {
  mode: "isolated" | "shared";
  browser_url: string;
  last_navigated_url: string;
  agent_opened_pages: string[];
  chrome_server: {
    status: string;
    error: string;
    tool_count: number;
  };
  legacy_probe?: BrowserProbeSnapshot | null;
  message?: string;
  tabs_output?: string;
}

export interface DiagnosticProviderInfo {
  id: string;
  label: string;
  kind: "syntax" | "lint" | "build" | "lsp";
  available: boolean;
  detail: string;
}

export interface DetectedProjectInfo {
  kind: string;
  root: string;
  markers: string[];
}

export interface WorkspaceProblem {
  id: string;
  source: string;
  severity: DiagnosticSeverity;
  code?: string | null;
  message: string;
  path: string;
  relative_path: string;
  line: number;
  column: number;
  end_line?: number | null;
  end_column?: number | null;
}

export interface DiagnosticsSnapshot {
  workspace: string;
  status: DiagnosticsStatus;
  run_id?: string | null;
  profile?: "safe" | "build";
  problems: WorkspaceProblem[];
  error_count: number;
  warning_count: number;
  information_count: number;
  providers: DiagnosticProviderInfo[];
  detected_projects: DetectedProjectInfo[];
  started_at?: string | null;
  finished_at?: string | null;
  stale: boolean;
  progress?: string;
  error?: string;
}

export interface RagSourceInfo {
  path: string;
  kind: "file" | "directory";
  added_at: string;
}

export interface RagIndexResult {
  chunk_count: number;
  relation_count: number;
  file_count: number;
  error_count: number;
  message: string;
}

export interface RagSnapshot {
  workspace: string;
  sources: RagSourceInfo[];
  source_count: number;
  indexed_file_count: number;
  chunk_count: number;
  relation_count: number;
  last_indexed_at?: string | null;
  last_result?: RagIndexResult | null;
  embedding_provider: string;
  embedding_model: string;
  embedding_base_url: string;
  embedding_api_key_configured: boolean;
  needs_rebuild: boolean;
  storage_path: string;
  status: RagStatus;
  job_id?: string;
  progress?: string;
  error?: string;
}

export interface McpToolInfo {
  name: string;
  namespaced_name: string;
  description: string;
  input_schema: Record<string, unknown>;
}

export interface McpServerInfo {
  name: string;
  status: McpServerState;
  transport: "stdio" | "http";
  source: "user" | "project" | "unknown";
  disabled: boolean;
  command: string;
  args: string[];
  url: string;
  env_keys: string[];
  header_keys: string[];
  tool_count: number;
  tools: McpToolInfo[];
  error: string;
  uptime_seconds: number;
  process_id?: number | null;
  capabilities: string[];
}

export interface McpSnapshot {
  servers: McpServerInfo[];
  ready_servers: number;
  total_servers: number;
  total_tools: number;
  project_config_path: string;
  user_config_path: string;
}

export interface McpInstallConfig {
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  disabled?: boolean;
}

export interface RuntimeRecoveryTask {
  task_id: string;
  session_id: string;
  mode: AgentMode;
  status: string;
  checkpoint_stage: string;
  recovery_attempts: number;
  started_at: string;
  prompt_preview: string;
  terminal_outcome?: "completed" | "failed" | "cancelled" | string;
}

export interface RuntimeAttachment {
  id: string;
  kind: "image" | "file";
  mime_type: string;
  display_name: string;
  local_path?: string;
  size_bytes?: number;
  data_base64?: string;
}

export interface ToolDescriptor {
  tool_call_id: string;
  name: string;
  arguments: Record<string, unknown>;
  change_preview?: FileChangePreview;
}

export interface FileChangePreview {
  operation: "create" | "modify" | "delete" | "no_change" | "unknown";
  path: string;
  workspace_scoped: boolean;
  rollback_protected: boolean;
  protection_reason?: "outside_workspace" | "generated_or_internal_path" | "sensitive_path" | "preview_error" | string | null;
  sensitive: boolean;
  binary: boolean;
  before_sha256?: string | null;
  after_sha256?: string | null;
  additions: number;
  deletions: number;
  diff: string;
  truncated: boolean;
  error?: string;
}

export interface ChangedFileSummary {
  path: string;
  status: "created" | "modified" | "deleted" | "type_changed" | "binary";
  additions: number;
  deletions: number;
}

export interface TaskChangeSet {
  snapshot_id: string;
  task_id: string;
  session_id?: string;
  backend: "side-git" | string;
  protected: boolean;
  status: string;
  has_changes: boolean;
  changed_files: ChangedFileSummary[];
  additions: number;
  deletions: number;
  diff_available: boolean;
  rollback_available: boolean;
  rollback_block_reason?: string;
  change_attribution?: "task" | "shared_workspace_overlap" | string;
  concurrent_task_ids?: string[];
  rolled_back: boolean;
  rollback_state?: "idle" | "in_progress" | "completed" | "failed" | "recovery_failed" | string;
  rollback_recovery_event_pending?: boolean;
  error?: string;
  created_at?: string | null;
  completed_at?: string | null;
  rolled_back_at?: string | null;
}

export interface TaskDiffResult extends TaskChangeSet {
  diff: string;
  diff_truncated: boolean;
}

export interface PlanTaskDescriptor {
  id: string;
  description: string;
  task_type: string;
  dependencies: string[];
}

export interface ConversationPlanStep extends PlanTaskDescriptor {
  status: "pending" | "running" | "completed" | "failed" | "skipped" | "cancelled";
  result_preview?: string;
  error?: string;
}

export interface ConversationPlanSnapshot {
  task_id: string;
  goal: string;
  summary?: string;
  execution_order: string[];
  steps: ConversationPlanStep[];
}

export interface UsageSnapshot {
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  reasoning_tokens: number;
  llm_calls: number;
  last_context_tokens: number;
  last_input_tokens: number;
  last_output_tokens: number;
  last_cached_input_tokens: number;
  last_reasoning_tokens: number;
  last_exact: boolean;
  estimated_cost: number;
  priced_llm_calls: number;
  last_estimated_cost?: number | null;
  last_cost_estimated: boolean;
  cost_currency: string;
  cost_source: string;
  provider: string;
  model: string;
  operation: string;
  context_window: number;
}

export interface HistorySnapshot {
  summary: string;
  compaction_count: number;
  last_compacted_at?: string | null;
  context_window: number;
}

export interface ConversationSummary {
  id: string;
  title: string;
  mode: AgentMode;
  created_at: string;
  updated_at: string;
  message_count: number;
  event_floor_sequence?: number;
  trace_enabled: boolean;
  trace_path?: string | null;
  usage: UsageSnapshot;
  history: HistorySnapshot;
}

export interface ConversationTranscriptEntry {
  id: string;
  role: "user" | "assistant" | "plan";
  content: string;
  timestamp: string;
  attachments?: RuntimeAttachment[];
  plan?: ConversationPlanSnapshot;
}

export interface RuntimeEventDataMap {
  "runtime.ready": {
    runtime_version: string;
    capabilities: string[];
  };
  "runtime.shutdown": {
    reason: string;
  };
  "workspace.opened": {
    project_id: string;
    workspace: string;
    provider: string;
    model: string;
    conversation_count: number;
    access_mode: AccessMode;
  };
  "workspace.closed": {
    project_id: string;
  };
  "access.mode_changed": {
    mode: AccessMode;
  };
  "session.created": {
    workspace: string;
    mode: AgentMode;
    title?: string;
  };
  "session.opened": { title: string };
  "session.renamed": { title: string };
  "session.deleted": { title: string };
  "session.snapshot": ConversationSummary & {
    project_id: string;
    workspace: string;
    transcript: ConversationTranscriptEntry[];
  };
  "session.reset": {
    cleared_message_count: number;
  };
  "trace.status_changed": {
    enabled: boolean;
    path?: string | null;
  };
  "task.started": {
    mode: AgentMode;
    prompt_preview: string;
    recovered?: boolean;
    recovery_attempt?: number;
    started_at?: string | null;
    protection?: TaskChangeSet;
  };
  "assistant.delta": {
    text: string;
    reset?: boolean;
  };
  "assistant.completed": {
    content: string;
    finish_reason: "stop" | "iteration_limit" | "tool_failure";
  };
  "assistant.thinking": {
    status: "started" | "active" | "finished";
    summary?: string;
  };
  "tool.started": ToolDescriptor & {
    iteration: number;
  };
  "tool.completed": {
    tool_call_id: string;
    name: string;
    result_preview: string;
    elapsed_ms: number;
    success: true;
    has_attachments: boolean;
  };
  "tool.failed": {
    tool_call_id: string;
    name: string;
    error: string;
    elapsed_ms: number;
    timed_out: boolean;
  };
  "attachment.available": {
    tool_call_id?: string;
    attachment: RuntimeAttachment;
  };
  "approval.requested": ToolDescriptor & {
    approval_id: string;
    danger_level: "low" | "medium" | "high";
    risk_description: string;
    expires_at?: string;
  };
  "approval.resolved": {
    approval_id: string;
    decision: ApprovalDecision;
    effective_arguments?: Record<string, unknown>;
  };
  "plan.created": {
    goal: string;
    summary?: string;
    tasks: PlanTaskDescriptor[];
    execution_order: string[];
  };
  "plan.step.started": {
    step_id: string;
    worker?: string;
  };
  "plan.step.completed": {
    step_id: string;
    worker?: string;
    result_preview: string;
    review_approved?: boolean;
    retry_count: number;
  };
  "plan.step.failed": {
    step_id: string;
    worker?: string;
    error: string;
  };
  "plan.step.skipped": {
    step_id: string;
    reason: string;
  };
  "mcp.status_changed": McpServerInfo;
  "rag.index.started": {
    job_id: string;
    source_count: number;
  };
  "rag.index.progress": {
    job_id: string;
    message: string;
  };
  "rag.index.completed": {
    job_id: string;
    snapshot: RagSnapshot;
  };
  "rag.index.failed": {
    job_id: string;
    message: string;
  };
  "diagnostics.started": {
    run_id: string;
    profile: "safe" | "build";
  };
  "diagnostics.progress": {
    run_id: string;
    message: string;
  };
  "diagnostics.completed": {
    run_id: string;
    error_count: number;
    warning_count: number;
    information_count: number;
  };
  "diagnostics.failed": {
    run_id: string;
    message: string;
  };
  "diagnostics.cancelled": {
    run_id: string;
  };
  "usage.updated": {
    input_tokens: number;
    output_tokens: number;
    cached_input_tokens: number;
    reasoning_tokens: number;
    context_tokens: number;
    context_window: number;
    task_input_tokens: number;
    task_output_tokens: number;
    task_cached_input_tokens: number;
    task_reasoning_tokens: number;
    task_llm_calls: number;
    conversation_input_tokens: number;
    conversation_output_tokens: number;
    conversation_cached_input_tokens: number;
    conversation_reasoning_tokens: number;
    conversation_llm_calls: number;
    provider: string;
    model: string;
    operation: string;
    exact: boolean;
    estimated_cost?: number | null;
    task_estimated_cost: number;
    conversation_estimated_cost: number;
    currency: string;
    cost_estimated: boolean;
    cost_source: string;
    task_priced_llm_calls: number;
    conversation_priced_llm_calls: number;
  };
  "history.compaction.started": {
    estimated_tokens: number;
    trigger_tokens: number;
  };
  "history.compaction.finished": {
    compacted: boolean;
  };
  "history.compacted": {
    before_tokens: number;
    after_tokens: number;
    compacted_turns: number;
    method: "llm" | "fallback" | "tool-result-truncation";
    compaction_count: number;
  };
  "task.completed": {
    status: Extract<TaskTerminalStatus, "completed">;
    elapsed_ms: number;
    changes?: TaskChangeSet;
  };
  "task.failed": {
    status: Extract<TaskTerminalStatus, "failed">;
    error_code: string;
    message: string;
    recoverable: boolean;
    elapsed_ms?: number;
    changes?: TaskChangeSet;
  };
  "task.cancelled": {
    status: Extract<TaskTerminalStatus, "cancelled">;
    reason: "user" | "shutdown" | "timeout";
    elapsed_ms?: number;
    changes?: TaskChangeSet;
  };
  "task.finalization.pending": {
    outcome: "completed" | "failed" | "cancelled" | string;
    message: string;
    recoverable: boolean;
  };
  "workspace.snapshot.created": TaskChangeSet;
  "task.rollback.started": {
    snapshot_id: string;
  };
  "task.rollback.completed": TaskChangeSet & {
    restored_files: string[];
  };
  "task.rollback.failed": {
    snapshot_id: string;
    code: "workspace_changed" | "rollback_unavailable" | string;
    message: string;
    conflicted_paths: string[];
  };
}

export type RuntimeEventType = keyof RuntimeEventDataMap;

export interface RuntimeEventEnvelope<T extends RuntimeEventType> {
  kind: "event";
  protocol_version: typeof RUNTIME_PROTOCOL_VERSION;
  event_id: string;
  session_id: string;
  task_id?: string;
  sequence: number;
  timestamp: string;
  type: T;
  data: RuntimeEventDataMap[T];
}

export type RuntimeEvent = {
  [T in RuntimeEventType]: RuntimeEventEnvelope<T>;
}[RuntimeEventType];

export interface RuntimeRequestDataMap {
  "runtime.ping": Record<string, never>;
  "workspace.open": {
    project_id: string;
    workspace: string;
  };
  "workspace.close": Record<string, never>;
  "runtime.set_access_mode": { mode: AccessMode };
  "session.list": Record<string, never>;
  "session.create": {
    mode: AgentMode;
    title?: string;
  };
  "session.open": { session_id: string };
  "session.rename": { session_id: string; title: string };
  "session.delete": { session_id: string };
  "session.reset": {
    session_id: string;
  };
  "session.set_mode": {
    session_id: string;
    mode: AgentMode;
  };
  "session.set_trace": {
    session_id: string;
    enabled: boolean;
  };
  "mcp.list": Record<string, never>;
  "mcp.install": {
    name: string;
    config: McpInstallConfig;
    overwrite?: boolean;
    confirmed?: boolean;
  };
  "mcp.set_enabled": { name: string; enabled: boolean };
  "mcp.restart": { name: string };
  "mcp.remove": { name: string };
  "mcp.logs": { name: string };
  "rag.snapshot": Record<string, never>;
  "rag.add_sources": { paths: string[] };
  "rag.remove_source": { path: string };
  "rag.index": Record<string, never>;
  "rag.clear": { confirmed: boolean };
  "memory.list": { query?: string; limit?: number };
  "memory.save": { content: string };
  "memory.delete": { id: string };
  "memory.clear": { confirmed: boolean };
  "skill.list": Record<string, never>;
  "skill.get": { name: string };
  "skill.set_enabled": { name: string; enabled: boolean };
  "skill.reload": Record<string, never>;
  "browser.snapshot": Record<string, never>;
  "browser.probe": { port: number };
  "browser.connect": { port?: number; confirmed: boolean };
  "browser.disconnect": { confirmed: boolean };
  "browser.tabs": Record<string, never>;
  "diagnostics.snapshot": Record<string, never>;
  "diagnostics.run": { profile: "safe" | "build"; confirmed?: boolean };
  "diagnostics.cancel": { run_id: string };
  "event.replay": {
    session_id: string;
    after_sequence: number;
    limit?: number;
    event_types?: RuntimeEventType[];
  };
  "task.submit": {
    session_id: string;
    prompt: string;
    attachments?: RuntimeAttachment[];
  };
  "task.recover": {
    session_id: string;
    task_id: string;
  };
  "task.cancel": {
    session_id: string;
    task_id: string;
  };
  "task.diff": {
    session_id: string;
    task_id: string;
    max_chars?: number;
  };
  "task.rollback": {
    session_id: string;
    task_id: string;
    snapshot_id: string;
    confirmed: boolean;
  };
  "approval.resolve": {
    session_id: string;
    task_id: string;
    approval_id: string;
    decision: ApprovalDecision;
    effective_arguments?: Record<string, unknown>;
  };
  "runtime.shutdown": Record<string, never>;
}

export type RuntimeRequestType = keyof RuntimeRequestDataMap;

export type RuntimeRequest = {
  [T in RuntimeRequestType]: {
    kind: "request";
    protocol_version: typeof RUNTIME_PROTOCOL_VERSION;
    request_id: string;
    method: T;
    params: RuntimeRequestDataMap[T];
  };
}[RuntimeRequestType];

export interface RuntimeResponse {
  kind: "response";
  protocol_version: typeof RUNTIME_PROTOCOL_VERSION;
  request_id: string;
  ok: boolean;
  result?: Record<string, unknown>;
  error?: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}

export type RuntimeMessage = (RuntimeRequest | RuntimeResponse | RuntimeEvent) & {
  /** Added by the Tauri transport; not part of Python's persisted protocol envelope. */
  runtime_pid?: number;
};
