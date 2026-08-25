/**
 * Pure frontend state machine for Runtime ownership.
 *
 * The React UI uses this reducer for live state only. Durable task recovery comes from
 * the Python event journal and is reconciled separately after a Sidecar reconnect.
 */
export type RuntimeConnectionState = "starting" | "restarting" | "online" | "offline" | "error";

export type FrontendRuntimeState = {
  connection: RuntimeConnectionState;
  activeSessionId: string;
  tasksBySession: Record<string, string>;
  sessionsByTask: Record<string, string>;
  /** Sessions whose task.submit request has not produced an authoritative task id yet. */
  submittingSessions: Record<string, string>;
  cancellingTasks: Record<string, true>;
  replaySessionId: string;
};

export type FrontendRuntimeAction =
  | { type: "connection.changed"; connection: RuntimeConnectionState }
  | { type: "conversation.activated"; sessionId: string }
  | { type: "task.submit.started"; sessionId: string; requestId: string }
  | { type: "task.submit.finished"; sessionId: string; requestId?: string }
  | { type: "task.registered"; taskId: string; sessionId: string }
  | { type: "task.released"; taskId?: string; sessionId?: string }
  | { type: "task.cancel.started"; taskId: string }
  | { type: "task.cancel.finished"; taskId?: string }
  | { type: "replay.started"; sessionId: string }
  | { type: "replay.finished"; sessionId?: string }
  | { type: "workspace.activation.started"; runtimeAlreadyRunning: boolean; forceRestart: boolean }
  | { type: "tasks.cleared" }
  | { type: "workspace.reset"; connection?: RuntimeConnectionState };

export const INITIAL_FRONTEND_RUNTIME_STATE: FrontendRuntimeState = {
  connection: "starting",
  activeSessionId: "",
  tasksBySession: {},
  sessionsByTask: {},
  submittingSessions: {},
  cancellingTasks: {},
  replaySessionId: "",
};

export function transitionFrontendRuntime(
  state: FrontendRuntimeState,
  action: FrontendRuntimeAction,
): FrontendRuntimeState {
  // Keep this reducer side-effect free: process launches, RPC requests, and replay are
  // coordinated by App.tsx so transitions remain unit-testable and deterministic.
  if (action.type === "connection.changed") {
    return state.connection === action.connection
      ? state
      : { ...state, connection: action.connection };
  }
  if (action.type === "conversation.activated") {
    return {
      ...state,
      activeSessionId: action.sessionId,
    };
  }
  if (action.type === "task.submit.started") {
    if (!action.sessionId || !action.requestId) return state;
    return {
      ...state,
      submittingSessions: {
        ...state.submittingSessions,
        [action.sessionId]: action.requestId,
      },
    };
  }
  if (action.type === "task.submit.finished") {
    const currentRequestId = state.submittingSessions[action.sessionId];
    if (!currentRequestId || action.requestId && currentRequestId !== action.requestId) return state;
    const submittingSessions = { ...state.submittingSessions };
    delete submittingSessions[action.sessionId];
    return { ...state, submittingSessions };
  }
  if (action.type === "task.registered") {
    if (!action.taskId || !action.sessionId) return state;
    const tasksBySession = { ...state.tasksBySession };
    const sessionsByTask = { ...state.sessionsByTask };
    const previousTask = tasksBySession[action.sessionId];
    if (previousTask && previousTask !== action.taskId) delete sessionsByTask[previousTask];
    const previousSession = sessionsByTask[action.taskId];
    if (previousSession && previousSession !== action.sessionId) delete tasksBySession[previousSession];
    tasksBySession[action.sessionId] = action.taskId;
    sessionsByTask[action.taskId] = action.sessionId;
    const submittingSessions = { ...state.submittingSessions };
    delete submittingSessions[action.sessionId];
    return {
      ...state,
      tasksBySession,
      sessionsByTask,
      submittingSessions,
    };
  }
  if (action.type === "task.released") {
    const resolvedSessionId = action.sessionId
      || (action.taskId ? state.sessionsByTask[action.taskId] ?? "" : "");
    const resolvedTaskId = action.taskId
      || (resolvedSessionId ? state.tasksBySession[resolvedSessionId] ?? "" : "");
    if (!resolvedSessionId && !resolvedTaskId) return state;
    const tasksBySession = { ...state.tasksBySession };
    const sessionsByTask = { ...state.sessionsByTask };
    const cancellingTasks = { ...state.cancellingTasks };
    const submittingSessions = { ...state.submittingSessions };
    if (
      resolvedSessionId
      && (!resolvedTaskId || tasksBySession[resolvedSessionId] === resolvedTaskId)
    ) delete tasksBySession[resolvedSessionId];
    if (resolvedTaskId) {
      delete sessionsByTask[resolvedTaskId];
      delete cancellingTasks[resolvedTaskId];
    }
    if (resolvedSessionId) delete submittingSessions[resolvedSessionId];
    return {
      ...state,
      tasksBySession,
      sessionsByTask,
      cancellingTasks,
      submittingSessions,
    };
  }
  if (action.type === "task.cancel.started") {
    if (!action.taskId) return state;
    return {
      ...state,
      cancellingTasks: { ...state.cancellingTasks, [action.taskId]: true },
    };
  }
  if (action.type === "task.cancel.finished") {
    const taskId = action.taskId || selectActiveTaskId(state);
    if (!taskId || !state.cancellingTasks[taskId]) return state;
    const cancellingTasks = { ...state.cancellingTasks };
    delete cancellingTasks[taskId];
    return { ...state, cancellingTasks };
  }
  if (action.type === "replay.started") {
    return { ...state, replaySessionId: action.sessionId };
  }
  if (action.type === "replay.finished") {
    if (action.sessionId && state.replaySessionId !== action.sessionId) return state;
    return { ...state, replaySessionId: "" };
  }
  if (action.type === "workspace.activation.started") {
    // Project activation does not restart an already-live Sidecar. Restore an
    // accidentally stale `starting` projection immediately; workspace.open
    // will still move the state to `error` if the project itself cannot load.
    return action.runtimeAlreadyRunning && !action.forceRestart
      ? state.connection === "online" ? state : { ...state, connection: "online" }
      : { ...state, connection: "starting" };
  }
  if (action.type === "tasks.cleared") {
    return {
      ...state,
      tasksBySession: {},
      sessionsByTask: {},
      submittingSessions: {},
      cancellingTasks: {},
    };
  }
  return {
    ...INITIAL_FRONTEND_RUNTIME_STATE,
    connection: action.connection ?? state.connection,
  };
}

export function selectActiveTaskId(state: FrontendRuntimeState) {
  return state.tasksBySession[state.activeSessionId] ?? "";
}

export function selectActiveConversationBusy(state: FrontendRuntimeState) {
  return Boolean(
    selectActiveTaskId(state)
    || state.activeSessionId && state.submittingSessions[state.activeSessionId],
  );
}

export function selectActiveTaskCancelling(state: FrontendRuntimeState) {
  const taskId = selectActiveTaskId(state);
  return Boolean(taskId && state.cancellingTasks[taskId]);
}

export function selectConversationReplaying(state: FrontendRuntimeState) {
  return Boolean(state.replaySessionId);
}

export function reconcileReplayedTask(
  previousActiveTaskId: string,
  recoveryTaskId: string | undefined,
  liveTaskId: string,
  terminalTaskIds: ReadonlySet<string>,
) {
  const reachedTerminal = Boolean(
    previousActiveTaskId && terminalTaskIds.has(previousActiveTaskId),
  );
  const retainedTaskId = reachedTerminal
    ? ""
    : recoveryTaskId || liveTaskId;
  return {
    retainedTaskId,
    releaseTaskId: previousActiveTaskId && !retainedTaskId
      ? previousActiveTaskId
      : "",
  };
}

export function selectStaleProjectTaskRoutes(
  state: FrontendRuntimeState,
  projectBySession: ReadonlyMap<string, string>,
  projectId: string,
  authoritativeTaskIds: ReadonlySet<string>,
) {
  if (!projectId) return [];
  return Object.entries(state.tasksBySession)
    .filter(([sessionId, taskId]) => (
      projectBySession.get(sessionId) === projectId
      && !authoritativeTaskIds.has(taskId)
    ))
    .map(([sessionId, taskId]) => ({ sessionId, taskId }));
}
