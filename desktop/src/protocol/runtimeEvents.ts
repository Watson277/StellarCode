/**
 * TypeScript mirror of the Python RuntimeEvent protocol.
 *
 * Keep additions backward-compatible: the desktop may reconnect to an existing Python
 * Sidecar and reconstruct UI state from journaled events created before a frontend update.
 */
export const RUNTIME_PROTOCOL_VERSION = 1 as const;

export type AgentMode = "react" | "plan" | "team";
export type AccessMode = "restricted" | "balanced" | "full-access";
export type TaskTerminalStatus = "completed" | "failed" | "cancelled";
export type ApprovalDecision = "approve" | "reject" | "skip" | "modify";
export type McpServerState = "disabled" | "starting" | "ready" | "error";
export type RagStatus = "idle" | "indexing" | "error";
export type DiagnosticsStatus = "not_run" | "running" | "completed" | "failed" | "cancelled";
export type DiagnosticSeverity = "error" | "warning" | "information" | "hint";

export interface MemoryEntryInfo {
  id: string;
  content: string;
  embedding: number[];
  status: "active" | "superseded";
  created_at: string;
  updated_at: string;
}

export interface MemorySnapshot {
  scope: "user" | "conversation";
  entries: MemoryEntryInfo[];
  count: number;
  token_count: number;
  storage_path: string;
  warnings: string[];
  query?: string;
}

export interface MemoryExtractionResult {
  processed_message_count: number;
  fact_count: number;
  saved_count: number;
  ignored_count: number;
  user_memory_count: number;
  conversation_memory_count: number;
}

export interface MemoryExtractionAccepted {
  job_id: string;
  status: "extracting";
  pending_message_count: number;
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
  builtin?: boolean;
  builtin_version?: string;
  builtin_hash?: string;
  installed_version?: string;
  installed_hash?: string;
  current_version?: string | null;
  current_hash?: string;
  customized?: boolean;
  update_available?: boolean;
  update_acknowledged?: boolean;
  upgrade_state?:
    | "not_bundled"
    | "current"
    | "customized"
    | "update_available"
    | "custom_kept"
    | "error";
  error?: string;
}

export interface SkillDetail extends SkillInfo {
  body: string;
}

export interface SkillSnapshot {
  skills: SkillInfo[];
  total_count: number;
  enabled_count: number;
  bundled_count?: number;
  updates_available?: number;
  warnings: string[];
  state_path: string;
  user_dir: string;
  project_dir: string;
}

export interface SkillDiff {
  name: string;
  diff: string;
  truncated: boolean;
  additions: number;
  deletions: number;
  current_hash: string;
  builtin_hash: string;
  current_version?: string | null;
  builtin_version: string;
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
  worktree_isolated?: boolean;
  worktree_path?: string;
  merge_state?: "active" | "applying" | "merged" | "conflict" | "not_isolated" | string;
  merge_conflict?: boolean;
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

export interface PromptMetricSnapshot {
  char_count: number;
  estimated_tokens: number;
  sha256: string;
}

export interface PromptLayerSnapshot extends PromptMetricSnapshot {
  name: string;
  role: "system" | "user" | string;
  sensitive: boolean;
  content_hidden: boolean;
  content: string;
}

export interface PromptSnapshot {
  available: boolean;
  version: string;
  mode: string;
  requested_mode?: AgentMode;
  generated_at: string;
  session_id?: string;
  conversation_title?: string;
  total: PromptMetricSnapshot;
  system: PromptMetricSnapshot;
  layers: PromptLayerSnapshot[];
  memory_hidden: boolean;
  assembled_preview: string;
}

export interface ConversationSummary {
  id: string;
  title: string;
  mode: AgentMode;
  access_mode?: AccessMode;
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
  task_id?: string;
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
  "plan.planning.started": {
    goal: string;
  };
  "plan.planning.delta": {
    text: string;
  };
  "team.run.started": {
    run_id: string;
    worker_count: number;
  };
  "team.run.completed": {
    run_id: string;
    message: string;
  };
  "team.run.failed": {
    run_id: string;
    message: string;
  };
  "team.agent.status": {
    run_id: string;
    agent_name: string;
    agent_role: "planner" | "worker" | "reviewer" | string;
    team_task_id: string;
    status: "queued" | "working" | "completed" | "failed";
  };
  "team.agent.message": {
    run_id: string;
    agent_name: string;
    agent_role: "planner" | "worker" | "reviewer" | string;
    team_task_id: string;
    direction: "inbound" | "outbound";
    message_kind: string;
    content: string;
  };
  "team.agent.delta": {
    agent_name: string;
    agent_role: "planner" | "worker" | "reviewer" | string;
    team_task_id: string;
    text: string;
    reset?: boolean;
  };
  "team.agent.tool.started": {
    agent_name: string;
    agent_role: "planner" | "worker" | "reviewer" | string;
    team_task_id: string;
    tool_call_id: string;
    name: string;
    arguments: Record<string, unknown>;
  };
  "team.agent.tool.completed": {
    agent_name: string;
    agent_role: "planner" | "worker" | "reviewer" | string;
    team_task_id: string;
    tool_call_id: string;
    name: string;
    result_preview: string;
    elapsed_ms: number;
    success: boolean;
    timed_out: boolean;
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
  "plan.step.delta": {
    step_id: string;
    text: string;
    reset?: boolean;
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
  "memory.extraction.started": {
    job_id: string;
    pending_message_count: number;
  };
  "memory.extraction.completed": MemoryExtractionResult & {
    job_id: string;
  };
  "memory.extraction.failed": {
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

export interface ProjectRequestContext {
  project_id?: string;
}

type ProjectScoped<T extends object> = T & ProjectRequestContext;

export interface RuntimeRequestDataMap {
  "runtime.ping": Record<string, never>;
  "workspace.open": {
    project_id: string;
    workspace: string;
  };
  "workspace.close": ProjectRequestContext;
  "runtime.set_access_mode": ProjectScoped<{ session_id: string; mode: AccessMode }>;
  "session.list": ProjectRequestContext;
  "session.create": ProjectScoped<{
    mode: AgentMode;
    title?: string;
  }>;
  "session.open": ProjectScoped<{ session_id: string }>;
  "session.rename": ProjectScoped<{ session_id: string; title: string }>;
  "session.delete": ProjectScoped<{ session_id: string }>;
  "session.reset": ProjectScoped<{
    session_id: string;
  }>;
  "session.set_mode": ProjectScoped<{
    session_id: string;
    mode: AgentMode;
  }>;
  "session.set_trace": ProjectScoped<{
    session_id: string;
    enabled: boolean;
  }>;
  "prompt.snapshot": ProjectScoped<{
    session_id: string;
    include_memory?: boolean;
  }>;
  "mcp.list": ProjectRequestContext;
  "mcp.install": ProjectScoped<{
    name: string;
    config: McpInstallConfig;
    overwrite?: boolean;
    confirmed?: boolean;
  }>;
  "mcp.set_enabled": ProjectScoped<{ name: string; enabled: boolean }>;
  "mcp.restart": ProjectScoped<{ name: string }>;
  "mcp.remove": ProjectScoped<{ name: string }>;
  "mcp.logs": ProjectScoped<{ name: string }>;
  "rag.snapshot": ProjectRequestContext;
  "rag.add_sources": ProjectScoped<{ paths: string[] }>;
  "rag.remove_source": ProjectScoped<{ path: string }>;
  "rag.index": ProjectRequestContext;
  "rag.clear": ProjectScoped<{ confirmed: boolean }>;
  "memory.list": ProjectScoped<{
    session_id: string;
    scope: MemorySnapshot["scope"];
    query?: string;
    limit?: number;
  }>;
  "memory.save": ProjectScoped<{
    session_id: string;
    scope: MemorySnapshot["scope"];
    content: string;
  }>;
  "memory.delete": ProjectScoped<{
    session_id: string;
    scope: MemorySnapshot["scope"];
    id: string;
  }>;
  "memory.clear": ProjectScoped<{
    session_id: string;
    scope: MemorySnapshot["scope"];
    confirmed: boolean;
  }>;
  "memory.extract": ProjectScoped<{
    session_id: string;
  }>;
  "skill.list": ProjectRequestContext;
  "skill.get": ProjectScoped<{ name: string }>;
  "skill.diff": ProjectScoped<{ name: string; max_chars?: number }>;
  "skill.set_enabled": ProjectScoped<{ name: string; enabled: boolean }>;
  "skill.reload": ProjectRequestContext;
  "skill.update": ProjectScoped<{
    name: string;
    current_hash: string;
    builtin_hash: string;
    confirmed: boolean;
  }>;
  "skill.keep_custom": ProjectScoped<{
    name: string;
    current_hash: string;
    builtin_hash: string;
  }>;
  "skill.restore_default": ProjectScoped<{
    name: string;
    current_hash: string;
    builtin_hash: string;
    confirmed: boolean;
  }>;
  "browser.snapshot": ProjectRequestContext;
  "browser.probe": ProjectScoped<{ port: number }>;
  "browser.connect": ProjectScoped<{ port?: number; confirmed: boolean }>;
  "browser.disconnect": ProjectScoped<{ confirmed: boolean }>;
  "browser.tabs": ProjectRequestContext;
  "diagnostics.snapshot": ProjectRequestContext;
  "diagnostics.run": ProjectScoped<{ profile: "safe" | "build"; confirmed?: boolean }>;
  "diagnostics.cancel": ProjectScoped<{ run_id: string }>;
  "event.replay": ProjectScoped<{
    session_id: string;
    after_sequence: number;
    limit?: number;
    event_types?: RuntimeEventType[];
  }>;
  "task.submit": ProjectScoped<{
    session_id: string;
    prompt: string;
    attachments?: RuntimeAttachment[];
  }>;
  "task.recover": ProjectScoped<{
    session_id: string;
    task_id: string;
  }>;
  "task.cancel": ProjectScoped<{
    session_id: string;
    task_id: string;
  }>;
  "task.diff": ProjectScoped<{
    session_id: string;
    task_id: string;
    max_chars?: number;
  }>;
  "task.rollback": ProjectScoped<{
    session_id: string;
    task_id: string;
    snapshot_id: string;
    confirmed: boolean;
  }>;
  "approval.resolve": ProjectScoped<{
    session_id: string;
    task_id: string;
    approval_id: string;
    decision: ApprovalDecision;
    effective_arguments?: Record<string, unknown>;
  }>;
  "runtime.shutdown": Record<string, never>;
}

export interface EmptyRuntimeResult extends Record<string, never> {}

export interface WorkspaceOpenResult {
  project_id: string;
  workspace: string;
  provider: string;
  model: string;
  conversation_count: number;
  access_mode: AccessMode;
  recovery?: RuntimeRecoveryTask | null;
  recoveries?: RuntimeRecoveryTask[];
  active_tasks?: Array<{
    task_id: string;
    session_id: string;
    phase: string;
  }>;
}

export interface SessionSnapshotResult extends ConversationSummary {
  session_id?: string;
  provider?: string;
  model?: string;
  project_id?: string;
  workspace?: string;
  transcript: ConversationTranscriptEntry[];
}

export interface EventReplayResult {
  events: RuntimeEvent[];
  last_sequence: number;
  has_more: boolean;
}

export interface TaskAcceptedResult {
  task_id: string;
  accepted?: boolean;
  recovery_attempt?: number;
}

export interface TaskCancelResult {
  task_id: string;
  accepted: boolean;
}

export interface McpLogsResult {
  name: string;
  logs: string;
}

export interface BrowserMutationResult {
  message: string;
  snapshot: BrowserSnapshot;
}

/**
 * Result counterpart to RuntimeRequestDataMap.
 *
 * Keeping this map next to the wire protocol makes RuntimeClient.request() infer both
 * request parameters and response data from the method literal.  New protocol methods
 * must be added to both maps, so an untyped management response cannot silently leak
 * back into the UI.
 */
export interface RuntimeResponseDataMap {
  "runtime.ping": { runtime_version: string };
  "workspace.open": WorkspaceOpenResult;
  "workspace.close": EmptyRuntimeResult;
  "runtime.set_access_mode": { session_id: string; mode: AccessMode };
  "session.list": { conversations: ConversationSummary[] };
  "session.create": SessionSnapshotResult;
  "session.open": SessionSnapshotResult;
  "session.rename": ConversationSummary;
  "session.delete": ConversationSummary;
  "session.reset": { cleared_message_count: number };
  "session.set_mode": { mode: AgentMode };
  "session.set_trace": { enabled: boolean; path?: string | null };
  "prompt.snapshot": PromptSnapshot;
  "mcp.list": McpSnapshot;
  "mcp.install": McpSnapshot;
  "mcp.set_enabled": McpSnapshot;
  "mcp.restart": McpSnapshot;
  "mcp.remove": McpSnapshot;
  "mcp.logs": McpLogsResult;
  "rag.snapshot": RagSnapshot;
  "rag.add_sources": RagSnapshot;
  "rag.remove_source": RagSnapshot;
  "rag.index": RagSnapshot;
  "rag.clear": RagSnapshot;
  "memory.list": MemorySnapshot;
  "memory.save": MemorySnapshot;
  "memory.delete": MemorySnapshot;
  "memory.clear": MemorySnapshot;
  "memory.extract": MemoryExtractionAccepted;
  "skill.list": SkillSnapshot;
  "skill.get": SkillDetail;
  "skill.diff": SkillDiff;
  "skill.set_enabled": SkillSnapshot;
  "skill.reload": SkillSnapshot;
  "skill.update": SkillSnapshot;
  "skill.keep_custom": SkillSnapshot;
  "skill.restore_default": SkillSnapshot;
  "browser.snapshot": BrowserSnapshot;
  "browser.probe": BrowserProbeSnapshot;
  "browser.connect": BrowserMutationResult;
  "browser.disconnect": BrowserMutationResult;
  "browser.tabs": { output: string };
  "diagnostics.snapshot": DiagnosticsSnapshot;
  "diagnostics.run": DiagnosticsSnapshot;
  "diagnostics.cancel": DiagnosticsSnapshot;
  "event.replay": EventReplayResult;
  "task.submit": TaskAcceptedResult;
  "task.recover": TaskAcceptedResult;
  "task.cancel": TaskCancelResult;
  "task.diff": TaskDiffResult;
  "task.rollback": TaskChangeSet;
  "approval.resolve": EmptyRuntimeResult;
  "runtime.shutdown": EmptyRuntimeResult;
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
