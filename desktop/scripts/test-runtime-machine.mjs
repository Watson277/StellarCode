import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import ts from "typescript";

const sourceUrl = new URL("../src/runtime/frontendRuntimeMachine.ts", import.meta.url);
const source = await readFile(sourceUrl, "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2020,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: sourceUrl.pathname,
}).outputText;
const machine = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

let state = machine.INITIAL_FRONTEND_RUNTIME_STATE;
state = machine.transitionFrontendRuntime(state, {
  type: "conversation.activated",
  sessionId: "session-one",
});
state = machine.transitionFrontendRuntime(state, {
  type: "task.submit.started",
  sessionId: "session-one",
  requestId: "submit-one",
});
assert.equal(machine.selectActiveConversationBusy(state), true);
state = machine.transitionFrontendRuntime(state, {
  type: "task.registered",
  taskId: "task-one",
  sessionId: "session-one",
});
assert.equal(machine.selectActiveTaskId(state), "task-one");
assert.equal(state.submittingSessions["session-one"], undefined);
state = machine.transitionFrontendRuntime(state, {
  type: "task.cancel.started",
  taskId: "task-one",
});
assert.equal(machine.selectActiveTaskCancelling(state), true);

state = machine.transitionFrontendRuntime(state, {
  type: "task.registered",
  taskId: "task-background",
  sessionId: "session-two",
});
state = machine.transitionFrontendRuntime(state, {
  type: "conversation.activated",
  sessionId: "session-two",
});
assert.equal(machine.selectActiveTaskId(state), "task-background");
state = machine.transitionFrontendRuntime(state, {
  type: "task.released",
  taskId: "task-background",
  sessionId: "session-two",
});
assert.equal(machine.selectActiveConversationBusy(state), false);
assert.equal(state.tasksBySession["session-one"], "task-one");

// Submissions in two conversations are correlated independently. Switching the
// visible conversation must neither clear nor overwrite the background request.
state = machine.transitionFrontendRuntime(state, {
  type: "task.submit.started",
  sessionId: "session-three",
  requestId: "submit-three",
});
state = machine.transitionFrontendRuntime(state, {
  type: "task.submit.started",
  sessionId: "session-four",
  requestId: "submit-four",
});
state = machine.transitionFrontendRuntime(state, {
  type: "conversation.activated",
  sessionId: "session-four",
});
assert.equal(state.submittingSessions["session-three"], "submit-three");
assert.equal(state.submittingSessions["session-four"], "submit-four");
assert.equal(machine.selectActiveConversationBusy(state), true);
state = machine.transitionFrontendRuntime(state, {
  type: "task.submit.finished",
  sessionId: "session-three",
  requestId: "submit-three",
});
assert.equal(state.submittingSessions["session-three"], undefined);
assert.equal(state.submittingSessions["session-four"], "submit-four");
// A late response for an older request cannot finish the newer submit.
state = machine.transitionFrontendRuntime(state, {
  type: "task.submit.finished",
  sessionId: "session-four",
  requestId: "stale-submit-four",
});
assert.equal(state.submittingSessions["session-four"], "submit-four");

state = machine.transitionFrontendRuntime(state, {
  type: "replay.started",
  sessionId: "session-three",
});
assert.equal(machine.selectConversationReplaying(state), true);
state = machine.transitionFrontendRuntime(state, {
  type: "replay.finished",
  sessionId: "session-three",
});
assert.equal(machine.selectConversationReplaying(state), false);

state = machine.transitionFrontendRuntime(state, { type: "tasks.cleared" });
assert.deepEqual(state.tasksBySession, {});
assert.deepEqual(state.sessionsByTask, {});
assert.deepEqual(state.submittingSessions, {});

const waitingApprovalReplay = machine.reconcileReplayedTask(
  "task-waiting",
  undefined,
  "task-waiting",
  new Set(),
);
assert.deepEqual(waitingApprovalReplay, {
  retainedTaskId: "task-waiting",
  releaseTaskId: "",
});

const interruptedReplay = machine.reconcileReplayedTask(
  "task-interrupted",
  undefined,
  "",
  new Set(),
);
assert.deepEqual(interruptedReplay, {
  retainedTaskId: "",
  releaseTaskId: "task-interrupted",
});

const terminalReplay = machine.reconcileReplayedTask(
  "task-finished",
  "task-finished",
  "task-finished",
  new Set(["task-finished"]),
);
assert.deepEqual(terminalReplay, {
  retainedTaskId: "",
  releaseTaskId: "task-finished",
});

state = {
  ...machine.INITIAL_FRONTEND_RUNTIME_STATE,
  connection: "online",
};
state = machine.transitionFrontendRuntime(state, {
  type: "workspace.activation.started",
  runtimeAlreadyRunning: true,
  forceRestart: false,
});
assert.equal(state.connection, "online");
state = {
  ...machine.INITIAL_FRONTEND_RUNTIME_STATE,
  connection: "starting",
};
state = machine.transitionFrontendRuntime(state, {
  type: "workspace.activation.started",
  runtimeAlreadyRunning: true,
  forceRestart: false,
});
assert.equal(state.connection, "online", "a live Sidecar must clear stale project-starting UI");
state = machine.transitionFrontendRuntime(state, {
  type: "workspace.activation.started",
  runtimeAlreadyRunning: true,
  forceRestart: true,
});
assert.equal(state.connection, "starting");

const projectRoutes = {
  ...machine.INITIAL_FRONTEND_RUNTIME_STATE,
  tasksBySession: {
    "session-stale": "task-stale",
    "session-live": "task-live",
    "session-other": "task-other",
  },
  sessionsByTask: {
    "task-stale": "session-stale",
    "task-live": "session-live",
    "task-other": "session-other",
  },
};
assert.deepEqual(machine.selectStaleProjectTaskRoutes(
  projectRoutes,
  new Map([
    ["session-stale", "project-a"],
    ["session-live", "project-a"],
    ["session-other", "project-b"],
  ]),
  "project-a",
  new Set(["task-live"]),
), [{ sessionId: "session-stale", taskId: "task-stale" }]);

console.log("frontend Runtime state machine: all assertions passed");
