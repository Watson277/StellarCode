import type {
  AgentMode,
  ApprovalDecision,
  FileChangePreview,
  RuntimeEvent,
  RuntimeEventDataMap,
  TaskChangeSet,
} from "../protocol/runtimeEvents";

export type RuntimeEventSource = "live" | "replay" | "local";
export type SupervisionTaskStatus =
  | "accepted"
  | "running"
  | "waiting_approval"
  | "stopping"
  | "recovering"
  | "finalizing"
  | "completed"
  | "failed"
  | "cancelled";
export type SupervisionApprovalStatus =
  | "pending"
  | "resolving"
  | "approved"
  | "rejected"
  | "skipped"
  | "modified"
  | "interrupted";

export interface SessionMeta {
  sessionId: string;
  projectId?: string;
  title?: string;
  workspace?: string;
}

export interface ActiveTaskRegistration {
  taskId: string;
  sessionId: string;
  projectId?: string;
  phase?: string;
  registeredAt?: string;
}

export interface FailedSubmissionRegistration {
  requestId: string;
  sessionId: string;
  projectId?: string;
  promptPreview?: string;
  errorCode: string;
  errorMessage: string;
  startedAt?: string;
  failedAt?: string;
  /** The transport failed before the client learned whether Runtime accepted the task. */
  uncertain?: boolean;
}

export interface SupervisionTask {
  id: string;
  sessionId: string;
  projectId: string;
  mode?: AgentMode;
  status: SupervisionTaskStatus;
  activity: string;
  promptPreview: string;
  currentToolName?: string;
  currentToolCallId?: string;
  currentPlanStepId?: string;
  pendingApprovalIds: string[];
  recoverable: boolean;
  submissionUncertain?: boolean;
  errorCode?: string;
  errorMessage?: string;
  startedAt: string;
  updatedAt: string;
  completedAt?: string;
  elapsedMs?: number;
  changes?: TaskChangeSet;
  usage?: RuntimeEventDataMap["usage.updated"];
  lastEventId: string;
  lastEventSource: RuntimeEventSource;
}

export interface SupervisionApproval {
  id: string;
  sessionId: string;
  projectId: string;
  taskId: string;
  toolCallId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  changePreview?: FileChangePreview;
  dangerLevel: "low" | "medium" | "high";
  riskDescription: string;
  status: SupervisionApprovalStatus;
  decision?: ApprovalDecision;
  effectiveArguments?: Record<string, unknown>;
  requestedAt: string;
  updatedAt: string;
  resolvedAt?: string;
  expiresAt?: string;
  lastEventId: string;
  lastEventSource: RuntimeEventSource;
}

export interface SessionSupervision {
  meta: SessionMeta;
  taskIds: string[];
  approvalIds: string[];
  eventIds: string[];
  lastSequence: number;
}

export interface SupervisionState {
  sessions: Record<string, SessionSupervision>;
  tasks: Record<string, SupervisionTask>;
  approvals: Record<string, SupervisionApproval>;
  seenEventIds: Record<string, true>;
}

export interface ProjectRuntimeEventOptions {
  source?: RuntimeEventSource;
  /** SupervisionStore can own dedupe outside the immutable projection snapshot. */
  externalDedupe?: boolean;
}

const MAX_SESSION_EVENT_IDS = 4_096;
const MAX_SEEN_EVENT_IDS = 20_000;
const RETAINED_SEEN_EVENT_IDS = 10_000;

export function createInitialSupervisionState(): SupervisionState {
  return {
    sessions: {},
    tasks: {},
    approvals: {},
    seenEventIds: {},
  };
}

/** Projects one live or journal-replayed event into normalized supervision state. */
export function projectRuntimeEvent(
  state: SupervisionState,
  event: RuntimeEvent,
  options: ProjectRuntimeEventOptions = {},
): SupervisionState {
  if (!options.externalDedupe) {
    if (state.seenEventIds[event.event_id]) return state;
    const existingSession = state.sessions[event.session_id];
    if (existingSession && event.sequence <= existingSession.lastSequence) return state;
  }

  const source = options.source ?? "live";
  let next = ensureSession(state, event.session_id);
  next = recordAcceptedEvent(next, event, !options.externalDedupe);

  if (event.type === "session.snapshot") {
    return registerSupervisionSession(next, {
      sessionId: event.session_id,
      projectId: event.data.project_id,
      title: event.data.title,
      workspace: event.data.workspace,
    });
  }
  if (event.type === "session.created" || event.type === "session.opened" || event.type === "session.renamed") {
    return registerSupervisionSession(next, {
      sessionId: event.session_id,
      title: event.data.title,
      ...(event.type === "session.created" ? { workspace: event.data.workspace } : {}),
    });
  }
  if (event.type === "session.deleted") {
    const removed = removeSupervisionSession(next, event.session_id);
    // Keep the deletion envelope as a tombstone after its session index is removed.
    // Otherwise a duplicated live/replay envelope would recreate and delete the
    // session again instead of returning the same state reference.
    return options.externalDedupe
      ? removed
      : {
        ...removed,
        seenEventIds: { ...removed.seenEventIds, [event.event_id]: true },
      };
  }
  if (event.type === "session.reset") {
    return clearSessionExecution(next, event.session_id);
  }

  const taskId = event.task_id ?? "";
  if (event.type === "approval.requested") {
    return projectApprovalRequested(next, event, source);
  }
  if (event.type === "approval.resolved") {
    return projectApprovalResolved(next, event, source);
  }
  if (!taskId) return next;

  const task = next.tasks[taskId] ?? createTask(next, event, source);
  const patch = taskPatchForEvent(task, event);
  const updatedTask: SupervisionTask = {
    ...task,
    ...patch,
    projectId: task.projectId || sessionProjectId(next, event.session_id),
    updatedAt: event.timestamp,
    lastEventId: event.event_id,
    lastEventSource: source,
  };
  let projected = upsertTask(next, updatedTask);
  if (event.type === "task.started" || isTerminalTaskEvent(event)) {
    projected = removeUncertainSubmissionsForSession(projected, event.session_id);
  }
  return isTerminalTaskEvent(event) || isRecoveredTaskStart(event)
    ? interruptPendingTaskApprovals(projected, taskId, event.timestamp, source, event.event_id)
    : projected;
}

export function projectRuntimeEvents(
  state: SupervisionState,
  events: readonly RuntimeEvent[],
  options: ProjectRuntimeEventOptions = {},
) {
  return [...events]
    .sort((left, right) => left.sequence - right.sequence)
    .reduce(
      (current, event) => projectRuntimeEvent(current, event, options),
      state,
    );
}

/**
 * Keeps durable history and live traffic as separate lanes during journal replay.
 * Only the historical lane is filtered; filtering the buffered lane would drop
 * streaming answers, usage and mode changes that occurred while replay paged.
 */
export function partitionRuntimeReplay(
  replayed: readonly RuntimeEvent[],
  bufferedLive: readonly RuntimeEvent[],
  restorableTypes: readonly RuntimeEvent["type"][],
) {
  const restorable = new Set(restorableTypes);
  return {
    replayEvents: uniqueEvents(replayed)
      .filter((event) => restorable.has(event.type))
      .sort(compareEvents),
    liveEvents: uniqueEvents(bufferedLive).sort(compareEvents),
  };
}

export type RuntimeReplayLane = {
  event: RuntimeEvent;
  source: RuntimeEventSource;
};

/** Merges the two lanes chronologically; a duplicate live envelope wins. */
export function mergeRuntimeReplayLanes(
  replayEvents: readonly RuntimeEvent[],
  liveEvents: readonly RuntimeEvent[],
): RuntimeReplayLane[] {
  const byId = new Map<string, RuntimeReplayLane>();
  for (const event of replayEvents) byId.set(event.event_id, { event, source: "replay" });
  for (const event of liveEvents) byId.set(event.event_id, { event, source: "live" });
  return [...byId.values()].sort((left, right) => compareEvents(left.event, right.event));
}

export function registerSupervisionSession(
  state: SupervisionState,
  meta: SessionMeta,
): SupervisionState {
  if (!meta.sessionId) return state;
  const existing = state.sessions[meta.sessionId];
  const session = existing ?? emptySession(meta.sessionId);
  const nextMeta = {
    ...session.meta,
    ...definedMeta(meta),
    sessionId: meta.sessionId,
  };
  if (existing && shallowEqualMeta(existing.meta, nextMeta)) return state;
  const nextSession = { ...session, meta: nextMeta };
  let tasks = state.tasks;
  let approvals = state.approvals;
  if (meta.projectId && meta.projectId !== session.meta.projectId) {
    tasks = { ...tasks };
    approvals = { ...approvals };
    for (const taskId of session.taskIds) {
      const task = tasks[taskId];
      if (task) tasks[taskId] = { ...task, projectId: meta.projectId };
    }
    for (const approvalId of session.approvalIds) {
      const approval = approvals[approvalId];
      if (approval) approvals[approvalId] = { ...approval, projectId: meta.projectId };
    }
  }
  return {
    ...state,
    sessions: { ...state.sessions, [meta.sessionId]: nextSession },
    tasks,
    approvals,
  };
}

/** Hydrates workspace.open.active_tasks before their journal replay is available. */
export function registerActiveSupervisionTask(
  state: SupervisionState,
  registration: ActiveTaskRegistration,
): SupervisionState {
  if (!registration.taskId || !registration.sessionId) return state;
  const timestamp = registration.registeredAt ?? new Date().toISOString();
  let next = registerSupervisionSession(state, {
    sessionId: registration.sessionId,
    projectId: registration.projectId,
  });
  next = removeUncertainSubmissionsForSession(next, registration.sessionId);
  const existing = next.tasks[registration.taskId];
  // A task terminal event is stronger evidence than workspace.open's earlier
  // active-task snapshot. Never let a delayed workspace response resurrect a
  // completed task as a permanently busy "zombie".
  if (existing && isTerminalTaskStatus(existing.status)) return next;
  const status = hydratedTaskStatus(registration.phase);
  next = upsertTask(next, {
    id: registration.taskId,
    sessionId: registration.sessionId,
    projectId: registration.projectId ?? existing?.projectId ?? "",
    mode: existing?.mode,
    status,
    activity: hydratedTaskActivity(status),
    promptPreview: existing?.promptPreview ?? "",
    currentToolName: existing?.currentToolName,
    currentToolCallId: existing?.currentToolCallId,
    currentPlanStepId: existing?.currentPlanStepId,
    pendingApprovalIds: existing?.pendingApprovalIds ?? [],
    recoverable: status === "recovering" || existing?.recoverable === true,
    errorCode: existing?.errorCode,
    errorMessage: existing?.errorMessage,
    startedAt: existing?.startedAt ?? timestamp,
    updatedAt: timestamp,
    completedAt: existing?.completedAt,
    elapsedMs: existing?.elapsedMs,
    changes: existing?.changes,
    usage: existing?.usage,
    lastEventId: existing?.lastEventId ?? `workspace-active:${registration.taskId}`,
    lastEventSource: existing?.lastEventSource ?? "replay",
  });
  return next;
}

/** Records a task.submit failure that happened before the Runtime created a task id. */
export function registerFailedSupervisionSubmission(
  state: SupervisionState,
  registration: FailedSubmissionRegistration,
): SupervisionState {
  if (!registration.requestId || !registration.sessionId) return state;
  const failedAt = registration.failedAt ?? new Date().toISOString();
  const startedAt = registration.startedAt ?? failedAt;
  const taskId = `submission:${registration.requestId}`;
  const uncertain = registration.uncertain === true;
  const next = registerSupervisionSession(state, {
    sessionId: registration.sessionId,
    projectId: registration.projectId,
  });
  return upsertTask(next, {
    id: taskId,
    sessionId: registration.sessionId,
    projectId: registration.projectId ?? next.sessions[registration.sessionId]?.meta.projectId ?? "",
    status: uncertain ? "recovering" : "failed",
    activity: uncertain
      ? "Task submission result is unknown; checking Runtime state"
      : "Task submission failed",
    promptPreview: registration.promptPreview ?? "",
    pendingApprovalIds: [],
    recoverable: uncertain,
    submissionUncertain: uncertain,
    errorCode: registration.errorCode,
    errorMessage: registration.errorMessage,
    startedAt,
    updatedAt: failedAt,
    completedAt: uncertain ? undefined : failedAt,
    elapsedMs: uncertain ? undefined : elapsedBetween(startedAt, failedAt),
    lastEventId: `local:${registration.requestId}`,
    lastEventSource: "local",
  });
}

export function removeSupervisionSession(state: SupervisionState, sessionId: string) {
  const session = state.sessions[sessionId];
  if (!session) return state;
  const sessions = { ...state.sessions };
  const tasks = { ...state.tasks };
  const approvals = { ...state.approvals };
  const seenEventIds = { ...state.seenEventIds };
  delete sessions[sessionId];
  for (const taskId of session.taskIds) delete tasks[taskId];
  for (const approvalId of session.approvalIds) delete approvals[approvalId];
  for (const eventId of session.eventIds) delete seenEventIds[eventId];
  return { sessions, tasks, approvals, seenEventIds };
}

function ensureSession(state: SupervisionState, sessionId: string) {
  if (state.sessions[sessionId]) return state;
  return {
    ...state,
    sessions: { ...state.sessions, [sessionId]: emptySession(sessionId) },
  };
}

function recordAcceptedEvent(state: SupervisionState, event: RuntimeEvent, trackEventIds: boolean) {
  const session = state.sessions[event.session_id];
  const eventIds = trackEventIds
    ? [...session.eventIds, event.event_id].slice(-MAX_SESSION_EVENT_IDS)
    : session.eventIds;
  if (!trackEventIds) {
    return {
      ...state,
      sessions: {
        ...state.sessions,
        [event.session_id]: {
          ...session,
          lastSequence: event.sequence,
        },
      },
    };
  }
  let seenEventIds = { ...state.seenEventIds, [event.event_id]: true as const };
  if (Object.keys(seenEventIds).length > MAX_SEEN_EVENT_IDS) {
    seenEventIds = Object.fromEntries(
      Object.keys(seenEventIds).slice(-RETAINED_SEEN_EVENT_IDS).map((eventId) => [eventId, true]),
    );
  }
  return {
    ...state,
    sessions: {
      ...state.sessions,
      [event.session_id]: {
        ...session,
        lastSequence: event.sequence,
        eventIds,
      },
    },
    seenEventIds,
  };
}

function emptySession(sessionId: string): SessionSupervision {
  return {
    meta: { sessionId },
    taskIds: [],
    approvalIds: [],
    eventIds: [],
    lastSequence: 0,
  };
}

function clearSessionExecution(state: SupervisionState, sessionId: string) {
  const session = state.sessions[sessionId];
  if (!session) return state;
  const tasks = { ...state.tasks };
  const approvals = { ...state.approvals };
  for (const taskId of session.taskIds) delete tasks[taskId];
  for (const approvalId of session.approvalIds) delete approvals[approvalId];
  return {
    ...state,
    tasks,
    approvals,
    sessions: {
      ...state.sessions,
      [sessionId]: { ...session, taskIds: [], approvalIds: [] },
    },
  };
}

function createTask(
  state: SupervisionState,
  event: RuntimeEvent,
  source: RuntimeEventSource,
): SupervisionTask {
  const taskId = event.task_id ?? event.event_id;
  return {
    id: taskId,
    sessionId: event.session_id,
    projectId: sessionProjectId(state, event.session_id),
    status: "accepted",
    activity: "Task accepted",
    promptPreview: "",
    pendingApprovalIds: [],
    recoverable: false,
    startedAt: event.timestamp,
    updatedAt: event.timestamp,
    lastEventId: event.event_id,
    lastEventSource: source,
  };
}

function taskPatchForEvent(
  task: SupervisionTask,
  event: RuntimeEvent,
): Partial<SupervisionTask> {
  switch (event.type) {
    case "task.started":
      return {
        mode: event.data.mode,
        status: event.data.recovered ? "recovering" : "running",
        activity: event.data.recovered ? "Recovering task" : "Analyzing task",
        promptPreview: event.data.prompt_preview,
        recoverable: Boolean(event.data.recovered),
        pendingApprovalIds: event.data.recovered ? [] : task.pendingApprovalIds,
        errorCode: undefined,
        errorMessage: undefined,
        startedAt: event.data.started_at || event.timestamp,
      };
    case "assistant.thinking":
      return event.data.status === "finished"
        ? { activity: "Finalizing response", status: activeStatus(task, "finalizing"), errorMessage: undefined }
        : { activity: event.data.summary || "Thinking", status: activeStatus(task), errorMessage: undefined };
    case "assistant.delta":
      return {
        activity: "Writing response",
        status: activeStatus(task),
        errorCode: undefined,
        errorMessage: undefined,
      };
    case "assistant.completed":
      return {
        activity: "Finalizing task",
        status: activeStatus(task, "finalizing"),
        errorCode: undefined,
        errorMessage: undefined,
      };
    case "tool.started":
      return {
        activity: `Running ${event.data.name}`,
        currentToolName: event.data.name,
        currentToolCallId: event.data.tool_call_id,
        errorCode: undefined,
        errorMessage: undefined,
        status: activeStatus(task),
      };
    case "tool.completed":
      return {
        activity: `${event.data.name} completed`,
        currentToolName: undefined,
        currentToolCallId: undefined,
        errorCode: undefined,
        errorMessage: undefined,
        status: activeStatus(task),
      };
    case "tool.failed":
      return {
        activity: `${event.data.name} failed`,
        currentToolName: undefined,
        currentToolCallId: undefined,
        errorMessage: event.data.error,
        status: activeStatus(task),
      };
    case "plan.planning.started":
      return { activity: "Planning task", status: activeStatus(task), errorMessage: undefined };
    case "plan.planning.delta":
      return { activity: "Planning task", status: activeStatus(task), errorMessage: undefined };
    case "plan.created":
      return { activity: "Plan ready", status: activeStatus(task), errorMessage: undefined };
    case "plan.step.started":
      return {
        activity: `Running plan step ${event.data.step_id}`,
        currentPlanStepId: event.data.step_id,
        errorCode: undefined,
        errorMessage: undefined,
        status: activeStatus(task),
      };
    case "plan.step.delta":
      return {
        activity: `Running plan step ${event.data.step_id}`,
        currentPlanStepId: event.data.step_id,
        errorMessage: undefined,
        status: activeStatus(task),
      };
    case "plan.step.completed":
      return {
        activity: `Plan step ${event.data.step_id} completed`,
        errorCode: undefined,
        errorMessage: undefined,
        status: activeStatus(task),
      };
    case "plan.step.failed":
      return { activity: `Plan step ${event.data.step_id} failed`, errorMessage: event.data.error };
    case "plan.step.skipped":
      return {
        activity: `Plan step ${event.data.step_id} skipped`,
        errorCode: undefined,
        errorMessage: undefined,
      };
    case "team.run.started":
      return { activity: `Team started with ${event.data.worker_count} workers`, status: activeStatus(task), errorMessage: undefined };
    case "team.agent.status":
      return { activity: `${event.data.agent_name}: ${event.data.status}`, status: activeStatus(task), errorMessage: undefined };
    case "team.agent.tool.started":
      return { activity: `${event.data.agent_name}: ${event.data.name}`, status: activeStatus(task), errorMessage: undefined };
    case "history.compaction.started":
      return { activity: "Compacting context", status: activeStatus(task) };
    case "history.compaction.finished":
    case "history.compacted":
      return { activity: "Context compacted", status: activeStatus(task) };
    case "usage.updated":
      return { usage: event.data };
    case "task.finalization.pending":
      return {
        status: "finalizing",
        activity: event.data.message || "Finalizing workspace changes",
        recoverable: event.data.recoverable,
        errorMessage: undefined,
      };
    case "workspace.snapshot.created":
      return { changes: event.data, activity: "Workspace snapshot created" };
    case "task.rollback.started":
      return { activity: "Undoing task changes" };
    case "task.rollback.completed":
      return { activity: "Task changes were undone", changes: event.data };
    case "task.rollback.failed":
      return { activity: "Undo failed", errorMessage: event.data.message };
    case "task.completed":
      return terminalPatch("completed", event.timestamp, event.data.elapsed_ms, event.data.changes);
    case "task.failed":
      return {
        ...terminalPatch("failed", event.timestamp, event.data.elapsed_ms, event.data.changes),
        recoverable: event.data.recoverable,
        errorCode: event.data.error_code,
        errorMessage: event.data.message,
      };
    case "task.cancelled":
      return terminalPatch("cancelled", event.timestamp, event.data.elapsed_ms, event.data.changes);
    default:
      return {};
  }
}

function projectApprovalRequested(
  state: SupervisionState,
  event: Extract<RuntimeEvent, { type: "approval.requested" }>,
  source: RuntimeEventSource,
) {
  const taskId = event.task_id ?? event.data.tool_call_id;
  let next = state;
  const task = next.tasks[taskId] ?? createTask(next, { ...event, task_id: taskId }, source);
  const taskAcceptsApproval = task.status !== "stopping"
    && task.status !== "finalizing"
    && !isTerminalTaskStatus(task.status);
  const approval: SupervisionApproval = {
    id: event.data.approval_id,
    sessionId: event.session_id,
    projectId: task.projectId || sessionProjectId(next, event.session_id),
    taskId,
    toolCallId: event.data.tool_call_id,
    toolName: event.data.name,
    arguments: event.data.arguments,
    changePreview: event.data.change_preview,
    dangerLevel: event.data.danger_level,
    riskDescription: event.data.risk_description,
    status: taskAcceptsApproval ? "pending" : "interrupted",
    requestedAt: event.timestamp,
    updatedAt: event.timestamp,
    expiresAt: event.data.expires_at,
    resolvedAt: taskAcceptsApproval ? undefined : event.timestamp,
    lastEventId: event.event_id,
    lastEventSource: source,
  };
  next = upsertApproval(next, approval);
  if (!taskAcceptsApproval) return next;
  return upsertTask(next, {
    ...task,
    status: "waiting_approval",
    activity: `Waiting for approval: ${event.data.name}`,
    currentToolName: event.data.name,
    currentToolCallId: event.data.tool_call_id,
    pendingApprovalIds: appendUnique(task.pendingApprovalIds, approval.id),
    updatedAt: event.timestamp,
    lastEventId: event.event_id,
    lastEventSource: source,
  });
}

function projectApprovalResolved(
  state: SupervisionState,
  event: Extract<RuntimeEvent, { type: "approval.resolved" }>,
  source: RuntimeEventSource,
) {
  const existing = state.approvals[event.data.approval_id];
  if (!existing) return state;
  const approval = {
    ...existing,
    status: approvalStatus(event.data.decision),
    decision: event.data.decision,
    effectiveArguments: event.data.effective_arguments,
    updatedAt: event.timestamp,
    resolvedAt: event.timestamp,
    lastEventId: event.event_id,
    lastEventSource: source,
  } satisfies SupervisionApproval;
  let next = upsertApproval(state, approval);
  const task = next.tasks[approval.taskId];
  if (!task) return next;
  const pendingApprovalIds = task.pendingApprovalIds.filter((id) => id !== approval.id);
  next = upsertTask(next, {
    ...task,
    pendingApprovalIds,
    status: pendingApprovalIds.length === 0
      ? task.status === "waiting_approval" ? "running" : activeStatus(task)
      : "waiting_approval",
    activity: pendingApprovalIds.length === 0
      ? `Approval ${event.data.decision}`
      : task.activity,
    updatedAt: event.timestamp,
    lastEventId: event.event_id,
    lastEventSource: source,
  });
  return next;
}

function upsertTask(state: SupervisionState, task: SupervisionTask) {
  const session = state.sessions[task.sessionId] ?? emptySession(task.sessionId);
  return {
    ...state,
    tasks: { ...state.tasks, [task.id]: task },
    sessions: {
      ...state.sessions,
      [task.sessionId]: {
        ...session,
        taskIds: appendUnique(session.taskIds, task.id),
      },
    },
  };
}

function removeUncertainSubmissionsForSession(
  state: SupervisionState,
  sessionId: string,
) {
  const session = state.sessions[sessionId];
  if (!session) return state;
  const removedIds = session.taskIds.filter((taskId) => {
    const task = state.tasks[taskId];
    return task?.submissionUncertain === true;
  });
  if (removedIds.length === 0) return state;
  const tasks = { ...state.tasks };
  for (const taskId of removedIds) delete tasks[taskId];
  const removed = new Set(removedIds);
  return {
    ...state,
    tasks,
    sessions: {
      ...state.sessions,
      [sessionId]: {
        ...session,
        taskIds: session.taskIds.filter((taskId) => !removed.has(taskId)),
      },
    },
  };
}

function upsertApproval(state: SupervisionState, approval: SupervisionApproval) {
  const session = state.sessions[approval.sessionId] ?? emptySession(approval.sessionId);
  return {
    ...state,
    approvals: { ...state.approvals, [approval.id]: approval },
    sessions: {
      ...state.sessions,
      [approval.sessionId]: {
        ...session,
        approvalIds: appendUnique(session.approvalIds, approval.id),
      },
    },
  };
}

function interruptPendingTaskApprovals(
  state: SupervisionState,
  taskId: string,
  timestamp: string,
  source: RuntimeEventSource,
  eventId: string,
) {
  let approvals = state.approvals;
  for (const approval of Object.values(state.approvals)) {
    if (
      approval.taskId !== taskId
      || (approval.status !== "pending" && approval.status !== "resolving")
    ) continue;
    if (approvals === state.approvals) approvals = { ...approvals };
    approvals[approval.id] = {
      ...approval,
      status: "interrupted",
      updatedAt: timestamp,
      resolvedAt: timestamp,
      lastEventId: eventId,
      lastEventSource: source,
    };
  }
  return approvals === state.approvals ? state : { ...state, approvals };
}

function activeStatus(
  task: SupervisionTask,
  fallback: SupervisionTaskStatus = "running",
): SupervisionTaskStatus {
  if (task.status === "waiting_approval" && task.pendingApprovalIds.length > 0) {
    return "waiting_approval";
  }
  if (task.status === "stopping") return "stopping";
  if (isTerminalTaskStatus(task.status)) return task.status;
  return fallback;
}

function terminalPatch(
  status: Extract<SupervisionTaskStatus, "completed" | "failed" | "cancelled">,
  timestamp: string,
  elapsedMs?: number,
  changes?: TaskChangeSet,
): Partial<SupervisionTask> {
  return {
    status,
    activity: status === "completed" ? "Completed" : status === "failed" ? "Failed" : "Cancelled",
    currentToolName: undefined,
    currentToolCallId: undefined,
    currentPlanStepId: undefined,
    pendingApprovalIds: [],
    recoverable: false,
    errorCode: undefined,
    errorMessage: undefined,
    completedAt: timestamp,
    elapsedMs,
    changes,
  };
}

function approvalStatus(decision: ApprovalDecision): SupervisionApprovalStatus {
  return {
    approve: "approved",
    reject: "rejected",
    skip: "skipped",
    modify: "modified",
  }[decision] as SupervisionApprovalStatus;
}

function sessionProjectId(state: SupervisionState, sessionId: string) {
  return state.sessions[sessionId]?.meta.projectId ?? "";
}

function appendUnique(values: string[], value: string) {
  return values.includes(value) ? values : [...values, value];
}

function uniqueEvents(events: readonly RuntimeEvent[]) {
  const byId = new Map<string, RuntimeEvent>();
  for (const event of events) byId.set(event.event_id, event);
  return [...byId.values()];
}

function compareEvents(left: RuntimeEvent, right: RuntimeEvent) {
  if (left.session_id === right.session_id) return left.sequence - right.sequence;
  return left.timestamp.localeCompare(right.timestamp);
}

function elapsedBetween(startedAt: string, completedAt: string) {
  const started = Date.parse(startedAt);
  const completed = Date.parse(completedAt);
  return Number.isFinite(started) && Number.isFinite(completed) && completed >= started
    ? completed - started
    : undefined;
}

function definedMeta(meta: SessionMeta): SessionMeta {
  return Object.fromEntries(
    Object.entries(meta).filter(([, value]) => value !== undefined),
  ) as unknown as SessionMeta;
}

function shallowEqualMeta(left: SessionMeta, right: SessionMeta) {
  return left.sessionId === right.sessionId
    && left.projectId === right.projectId
    && left.title === right.title
    && left.workspace === right.workspace;
}

function isTerminalTaskStatus(status: SupervisionTaskStatus) {
  return status === "completed" || status === "failed" || status === "cancelled";
}

function isTerminalTaskEvent(event: RuntimeEvent) {
  return event.type === "task.completed"
    || event.type === "task.failed"
    || event.type === "task.cancelled";
}

function isRecoveredTaskStart(event: RuntimeEvent) {
  return event.type === "task.started" && event.data.recovered === true;
}

function hydratedTaskStatus(phase?: string): SupervisionTaskStatus {
  if (phase === "waiting_approval") return "waiting_approval";
  if (phase === "cancelling" || phase === "stopping") return "stopping";
  if (phase === "finalizing" || phase === "finalize_pending") return "finalizing";
  if (phase === "recovering" || phase === "recovery") return "recovering";
  if (phase === "accepted") return "accepted";
  return "running";
}

function hydratedTaskActivity(status: SupervisionTaskStatus) {
  if (status === "stopping") return "Stopping task";
  if (status === "finalizing") return "Finalizing task";
  if (status === "recovering") return "Recovering task";
  if (status === "accepted") return "Task accepted";
  return "Running task";
}
