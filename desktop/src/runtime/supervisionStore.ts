import type { RuntimeEvent } from "../protocol/runtimeEvents";
import {
  createInitialSupervisionState,
  mergeRuntimeReplayLanes,
  projectRuntimeEvent,
  registerActiveSupervisionTask,
  registerFailedSupervisionSubmission,
  registerSupervisionSession,
  removeSupervisionSession,
  type SessionMeta,
  type ActiveTaskRegistration,
  type FailedSubmissionRegistration,
  type SupervisionApproval,
  type SupervisionState,
  type SupervisionTask,
  type SupervisionTaskStatus,
} from "./eventProjector";

export type SupervisionListener = () => void;

/** Small external store intended for React.useSyncExternalStore. */
export class SupervisionStore {
  private state: SupervisionState;
  private readonly listeners = new Set<SupervisionListener>();
  private readonly eventIdsBySession = new Map<string, Set<string>>();
  private readonly lastSequenceBySession = new Map<string, number>();
  private notificationFrame: number | null = null;

  constructor(initialState = createInitialSupervisionState()) {
    this.state = initialState;
  }

  getSnapshot = () => this.state;

  subscribe = (listener: SupervisionListener) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  ingest(event: RuntimeEvent) {
    if (!this.acceptEvent(event)) return;
    this.commit(
      projectRuntimeEvent(this.state, event, { source: "live", externalDedupe: true }),
      HIGH_FREQUENCY_EVENTS.has(event.type),
    );
  }

  ingestMany(events: readonly RuntimeEvent[]) {
    let next = this.state;
    for (const event of [...events].sort(compareRuntimeEvents)) {
      if (!this.acceptEvent(event)) continue;
      next = projectRuntimeEvent(next, event, { source: "live", externalDedupe: true });
    }
    this.commit(next);
  }

  /** Atomically replaces one session projection with its durable journal replay. */
  replay(
    sessionId: string,
    events: readonly RuntimeEvent[],
    metadata?: SessionMeta,
    bufferedLive: readonly RuntimeEvent[] = [],
  ) {
    const previousSession = this.state.sessions[sessionId];
    const retainedTasks = (previousSession?.taskIds ?? [])
      .map((taskId) => this.state.tasks[taskId])
      .filter((task): task is SupervisionTask => Boolean(
        task && ACTIVE_TASK_STATUSES.has(task.status),
      ));
    const retainedLocalFailures = (previousSession?.taskIds ?? [])
      .map((taskId) => this.state.tasks[taskId])
      .filter((task): task is SupervisionTask => Boolean(
        task && task.lastEventSource === "local" && task.status === "failed",
      ));
    const retainedApprovals = (previousSession?.approvalIds ?? [])
      .map((approvalId) => this.state.approvals[approvalId])
      .filter((approval): approval is SupervisionApproval => Boolean(
        approval && (approval.status === "pending" || approval.status === "resolving"),
      ));
    this.resetSessionDedupe(sessionId);
    let next = removeSupervisionSession(this.state, sessionId);
    next = registerSupervisionSession(next, metadata ?? { sessionId });
    // Seed live entities before replay. This preserves hydrated task metadata
    // when the journal was truncated, while terminal/reset events can still
    // deterministically replace or clear the seed in sequence order.
    for (const task of retainedTasks) {
      next = registerActiveSupervisionTask(next, {
        taskId: task.id,
        sessionId,
        projectId: metadata?.projectId ?? task.projectId,
        phase: task.status,
        registeredAt: task.startedAt,
      });
      next = {
        ...next,
        tasks: {
          ...next.tasks,
          [task.id]: {
            ...task,
            projectId: metadata?.projectId ?? task.projectId,
          },
        },
      };
    }
    for (const task of retainedLocalFailures) {
      next = registerFailedSupervisionSubmission(next, {
        requestId: task.id.startsWith("submission:")
          ? task.id.slice("submission:".length)
          : task.id,
        sessionId,
        projectId: metadata?.projectId ?? task.projectId,
        promptPreview: task.promptPreview,
        errorCode: task.errorCode ?? "task_submit_failed",
        errorMessage: task.errorMessage ?? "Task submission failed",
        startedAt: task.startedAt,
        failedAt: task.completedAt ?? task.updatedAt,
      });
    }
    for (const approval of retainedApprovals) {
      if (!next.tasks[approval.taskId] || next.approvals[approval.id]) continue;
      const session = next.sessions[sessionId];
      next = {
        ...next,
        approvals: {
          ...next.approvals,
          [approval.id]: {
            ...approval,
            projectId: metadata?.projectId ?? approval.projectId,
          },
        },
        sessions: {
          ...next.sessions,
          [sessionId]: {
            ...session,
            approvalIds: session.approvalIds.includes(approval.id)
              ? session.approvalIds
              : [...session.approvalIds, approval.id],
          },
        },
      };
    }
    const lanes = mergeRuntimeReplayLanes(
      events.filter((event) => event.session_id === sessionId),
      bufferedLive.filter((event) => event.session_id === sessionId),
    );
    for (const lane of lanes) {
      if (!this.acceptEvent(lane.event)) continue;
      next = projectRuntimeEvent(next, lane.event, {
        source: lane.source,
        externalDedupe: true,
      });
    }
    if (metadata) next = registerSupervisionSession(next, metadata);
    this.commit(next);
  }

  registerSession(metadata: SessionMeta) {
    this.commit(registerSupervisionSession(this.state, metadata));
  }

  registerActiveTask(registration: ActiveTaskRegistration) {
    this.commit(registerActiveSupervisionTask(this.state, registration));
  }

  recordSubmissionFailure(registration: FailedSubmissionRegistration) {
    this.commit(registerFailedSupervisionSubmission(this.state, registration));
  }

  /** Reconciles projected work with workspace.open's authoritative active routes. */
  reconcileProjectActiveTasks(
    projectId: string,
    registrations: readonly ActiveTaskRegistration[],
  ) {
    if (!projectId) return;
    const activeIds = new Set(registrations.map((item) => item.taskId));
    const finishedAt = new Date().toISOString();
    let next = this.state;
    let tasks = next.tasks;
    let approvals = next.approvals;
    for (const task of Object.values(next.tasks)) {
      const taskProjectId = task.projectId
        || next.sessions[task.sessionId]?.meta.projectId
        || "";
      if (
        taskProjectId !== projectId
        || !ACTIVE_TASK_STATUSES.has(task.status)
        || activeIds.has(task.id)
      ) continue;
      if (tasks === next.tasks) tasks = { ...tasks };
      tasks[task.id] = {
        ...task,
        projectId,
        status: "failed",
        activity: "Runtime no longer reports this task as active",
        recoverable: false,
        errorCode: "runtime_interrupted",
        errorMessage: "Runtime no longer reports this task as active.",
        pendingApprovalIds: [],
        currentToolName: undefined,
        currentToolCallId: undefined,
        completedAt: finishedAt,
        updatedAt: finishedAt,
        elapsedMs: elapsedSince(task.startedAt, finishedAt),
      };
      for (const approvalId of task.pendingApprovalIds) {
        const approval = approvals[approvalId];
        if (!approval || (approval.status !== "pending" && approval.status !== "resolving")) continue;
        if (approvals === next.approvals) approvals = { ...approvals };
        approvals[approvalId] = {
          ...approval,
          status: "interrupted",
          decision: undefined,
          resolvedAt: finishedAt,
          updatedAt: finishedAt,
        };
      }
    }
    if (tasks !== next.tasks || approvals !== next.approvals) {
      next = { ...next, tasks, approvals };
    }
    for (const registration of registrations) {
      next = registerActiveSupervisionTask(next, {
        ...registration,
        projectId,
      });
    }
    this.commit(next);
  }

  markTaskStopping(taskId: string) {
    const task = this.state.tasks[taskId];
    if (!task || isTerminal(task.status) || task.status === "stopping") return;
    const timestamp = new Date().toISOString();
    let approvals = this.state.approvals;
    for (const approvalId of task.pendingApprovalIds) {
      const approval = approvals[approvalId];
      if (!approval || (approval.status !== "pending" && approval.status !== "resolving")) continue;
      if (approvals === this.state.approvals) approvals = { ...approvals };
      approvals[approvalId] = {
        ...approval,
        status: "interrupted",
        resolvedAt: timestamp,
        updatedAt: timestamp,
      };
    }
    this.commit({
      ...this.state,
      approvals,
      tasks: {
        ...this.state.tasks,
        [taskId]: {
          ...task,
          status: "stopping",
          activity: "Stopping task",
          pendingApprovalIds: [],
          updatedAt: timestamp,
        },
      },
    });
  }

  /**
   * Applies one process-level disconnect to every loaded project. A Sidecar is
   * shared by all workspaces, so reconciling only the visible project would
   * leave background tasks and approvals permanently actionable.
   */
  markRuntimeDisconnected(terminal = false) {
    const timestamp = new Date().toISOString();
    let tasks = this.state.tasks;
    let approvals = this.state.approvals;
    for (const task of Object.values(this.state.tasks)) {
      if (!ACTIVE_TASK_STATUSES.has(task.status)) continue;
      if (tasks === this.state.tasks) tasks = { ...tasks };
      tasks[task.id] = {
        ...task,
        status: terminal ? "failed" : "recovering",
        activity: terminal ? "Runtime recovery failed" : "Runtime disconnected; waiting for recovery",
        recoverable: !terminal,
        errorCode: terminal ? "runtime_restart_failed" : undefined,
        errorMessage: terminal ? "Runtime automatic restart failed." : undefined,
        pendingApprovalIds: [],
        currentToolName: undefined,
        currentToolCallId: undefined,
        completedAt: terminal ? timestamp : undefined,
        elapsedMs: terminal ? elapsedSince(task.startedAt, timestamp) : task.elapsedMs,
        updatedAt: timestamp,
      };
      for (const approvalId of task.pendingApprovalIds) {
        const approval = approvals[approvalId];
        if (!approval || (approval.status !== "pending" && approval.status !== "resolving")) continue;
        if (approvals === this.state.approvals) approvals = { ...approvals };
        approvals[approvalId] = {
          ...approval,
          status: "interrupted",
          decision: undefined,
          resolvedAt: timestamp,
          updatedAt: timestamp,
        };
      }
    }
    if (tasks !== this.state.tasks || approvals !== this.state.approvals) {
      this.commit({ ...this.state, tasks, approvals });
    }
  }

  markApprovalResolving(approvalId: string, resolving: boolean) {
    const approval = this.state.approvals[approvalId];
    if (!approval || approval.status !== (resolving ? "pending" : "resolving")) return;
    this.commit({
      ...this.state,
      approvals: {
        ...this.state.approvals,
        [approvalId]: {
          ...approval,
          status: resolving ? "resolving" : "pending",
        },
      },
    });
  }

  reset() {
    this.eventIdsBySession.clear();
    this.lastSequenceBySession.clear();
    this.commit(createInitialSupervisionState());
  }

  private acceptEvent(event: RuntimeEvent) {
    const sessionId = event.session_id;
    const eventIds = this.eventIdsBySession.get(sessionId) ?? new Set<string>();
    if (eventIds.has(event.event_id)) return false;
    const previousSequence = this.lastSequenceBySession.get(sessionId) ?? 0;
    if (event.sequence <= previousSequence) return false;
    eventIds.add(event.event_id);
    while (eventIds.size > MAX_TRACKED_EVENT_IDS_PER_SESSION) {
      const oldest = eventIds.values().next().value;
      if (oldest === undefined) break;
      eventIds.delete(oldest);
    }
    this.eventIdsBySession.set(sessionId, eventIds);
    this.lastSequenceBySession.set(sessionId, event.sequence);
    return true;
  }

  private resetSessionDedupe(sessionId: string) {
    this.eventIdsBySession.delete(sessionId);
    this.lastSequenceBySession.delete(sessionId);
  }

  private commit(next: SupervisionState, deferNotification = false) {
    if (next === this.state) return;
    this.state = next;
    if (deferNotification && typeof requestAnimationFrame === "function") {
      if (this.notificationFrame === null) {
        this.notificationFrame = requestAnimationFrame(() => {
          this.notificationFrame = null;
          this.notify();
        });
      }
      return;
    }
    if (this.notificationFrame !== null && typeof cancelAnimationFrame === "function") {
      cancelAnimationFrame(this.notificationFrame);
      this.notificationFrame = null;
    }
    this.notify();
  }

  private notify() {
    for (const listener of [...this.listeners]) listener();
  }
}

const MAX_TRACKED_EVENT_IDS_PER_SESSION = 4_096;
const HIGH_FREQUENCY_EVENTS = new Set<RuntimeEvent["type"]>([
  "assistant.delta",
  "plan.planning.delta",
  "plan.step.delta",
  "team.agent.delta",
]);

function compareRuntimeEvents(left: RuntimeEvent, right: RuntimeEvent) {
  if (left.session_id === right.session_id) return left.sequence - right.sequence;
  return left.timestamp.localeCompare(right.timestamp);
}

const ACTIVE_TASK_STATUSES = new Set<SupervisionTaskStatus>([
  "accepted",
  "running",
  "waiting_approval",
  "stopping",
  "recovering",
  "finalizing",
]);

export function selectActiveTasks(state: SupervisionState): SupervisionTask[] {
  return Object.values(state.tasks)
    .filter((task) => ACTIVE_TASK_STATUSES.has(task.status))
    .sort(sortNewestFirst);
}

export function selectPendingApprovals(state: SupervisionState): SupervisionApproval[] {
  return Object.values(state.approvals)
    .filter((approval) => approval.status === "pending" || approval.status === "resolving")
    .sort((left, right) => timestamp(left.requestedAt) - timestamp(right.requestedAt));
}

export function selectSessionTasks(state: SupervisionState, sessionId: string) {
  const session = state.sessions[sessionId];
  if (!session) return [];
  return session.taskIds
    .map((taskId) => state.tasks[taskId])
    .filter((task): task is SupervisionTask => Boolean(task))
    .sort(sortNewestFirst);
}

export function selectSessionApprovals(state: SupervisionState, sessionId: string) {
  const session = state.sessions[sessionId];
  if (!session) return [];
  return session.approvalIds
    .map((approvalId) => state.approvals[approvalId])
    .filter((approval): approval is SupervisionApproval => Boolean(approval))
    .sort((left, right) => timestamp(left.requestedAt) - timestamp(right.requestedAt));
}

export function selectTaskForSession(state: SupervisionState, sessionId: string) {
  const tasks = selectSessionTasks(state, sessionId);
  return tasks.find((task) => ACTIVE_TASK_STATUSES.has(task.status)) ?? tasks[0];
}

function sortNewestFirst(left: SupervisionTask, right: SupervisionTask) {
  return timestamp(right.updatedAt) - timestamp(left.updatedAt);
}

function timestamp(value: string) {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function elapsedSince(startedAt: string, finishedAt: string) {
  const started = timestamp(startedAt);
  const finished = timestamp(finishedAt);
  return started > 0 && finished >= started ? finished - started : undefined;
}

function isTerminal(status: SupervisionTaskStatus) {
  return status === "completed" || status === "failed" || status === "cancelled";
}

export type {
  ActiveTaskRegistration,
  RuntimeEventSource,
  SessionMeta,
  SessionSupervision,
  SupervisionApproval,
  SupervisionApprovalStatus,
  SupervisionState,
  SupervisionTask,
  SupervisionTaskStatus,
} from "./eventProjector";
