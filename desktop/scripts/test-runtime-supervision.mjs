import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const desktopRoot = resolve(fileURLToPath(new URL("..", import.meta.url)));
const sourceRoot = join(desktopRoot, "src");
const outputRoot = await mkdtemp(join(tmpdir(), "stellarcode-runtime-test-"));
const sourceFiles = [
  "protocol/runtimeEvents.ts",
  "runtime/runtimeClient.ts",
  "runtime/eventProjector.ts",
  "runtime/supervisionStore.ts",
].map((path) => join(sourceRoot, path));

try {
  const program = ts.createProgram(sourceFiles, {
    target: ts.ScriptTarget.ES2020,
    module: ts.ModuleKind.CommonJS,
    moduleResolution: ts.ModuleResolutionKind.Node10,
    rootDir: sourceRoot,
    outDir: outputRoot,
    strict: true,
    skipLibCheck: true,
    esModuleInterop: true,
  });
  const emit = program.emit();
  const diagnostics = ts
    .getPreEmitDiagnostics(program)
    .concat(emit.diagnostics)
    .filter((diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error);
  if (diagnostics.length > 0) {
    const host = {
      getCanonicalFileName: (path) => path,
      getCurrentDirectory: () => desktopRoot,
      getNewLine: () => "\n",
    };
    throw new Error(ts.formatDiagnosticsWithColorAndContext(diagnostics, host));
  }

  const require = createRequire(import.meta.url);
  const clientModule = require(join(outputRoot, "runtime/runtimeClient.js"));
  const projector = require(join(outputRoot, "runtime/eventProjector.js"));
  const storeModule = require(join(outputRoot, "runtime/supervisionStore.js"));

  await testRuntimeClient(clientModule);
  testProjector(projector, storeModule);
  console.log("RuntimeClient + supervision projector/store: all assertions passed");
} finally {
  await rm(outputRoot, { recursive: true, force: true });
}

async function testRuntimeClient(runtime) {
  const sent = [];
  let id = 0;
  const client = new runtime.RuntimeClient(
    async (request) => { sent.push(request); },
    { createRequestId: () => `request-${++id}`, defaultTimeoutMs: 100 },
  );
  const ping = client.request("runtime.ping", {});
  assert.equal(sent[0].request_id, "request-1");
  assert.equal(client.pendingCount, 1);
  assert.equal(client.accept(response("request-1", { runtime_version: "1.2.3" })), true);
  assert.deepEqual(await ping, { runtime_version: "1.2.3" });
  assert.equal(client.pendingCount, 0);

  const failure = client.request("runtime.ping", {});
  client.accept({
    kind: "response",
    protocol_version: 1,
    request_id: "request-2",
    ok: false,
    error: { code: "offline", message: "Sidecar unavailable" },
  });
  await assert.rejects(failure, (error) => (
    error instanceof runtime.RuntimeRequestError && error.code === "offline"
  ));

  const timedOut = client.request("runtime.ping", {}, { timeoutMs: 5 });
  await assert.rejects(timedOut, runtime.RuntimeRequestTimeoutError);
  assert.equal(client.pendingCount, 0);

  const previous = client.request("session.list", {}, { scope: "session-list" });
  const previousRejected = assert.rejects(previous, runtime.RuntimeRequestSupersededError);
  const current = client.request("session.list", {}, { scope: "session-list" });
  await previousRejected;
  client.accept(response("request-5", { conversations: [] }));
  assert.deepEqual(await current, { conversations: [] });

  const controller = new AbortController();
  const aborted = client.request("runtime.ping", {}, { signal: controller.signal });
  controller.abort();
  await assert.rejects(aborted, (error) => error.code === "request_aborted");
  assert.equal(
    client.accept(response("request-6", { runtime_version: "late" })),
    true,
    "late responses for timed-out/aborted client requests must stay consumed",
  );

  const projectScoped = client.request("session.list", {}, {
    scope: "project:old:session.list",
    supersede: false,
  });
  const backgroundTask = client.request("task.cancel", {
    session_id: "session-background",
    task_id: "task-background",
  }, {
    scope: "task:task-background:cancel",
    supersede: false,
  });
  assert.equal(client.rejectScopePrefix(
    "project:old:",
    new runtime.RuntimeClientError("project switched", "project_switched"),
  ), 1);
  await assert.rejects(projectScoped, (error) => error.code === "project_switched");
  assert.equal(client.pendingCount, 1, "project switching must not cancel background task actions");
  client.accept(response("request-8", { task_id: "task-background", accepted: true }));
  assert.equal((await backgroundTask).accepted, true);

  const pendingAtDispose = client.request("runtime.ping", {});
  client.dispose("test disposal");
  await assert.rejects(pendingAtDispose, runtime.RuntimeClientDisposedError);
  await assert.rejects(client.request("runtime.ping", {}), runtime.RuntimeClientDisposedError);
  assert.equal(client.accept(response("unknown", {})), false);

  const broken = new runtime.RuntimeClient(async () => { throw new Error("transport failed"); });
  await assert.rejects(broken.request("runtime.ping", {}), /transport failed/);
  const synchronouslyBroken = new runtime.RuntimeClient(() => { throw new Error("sync failure"); });
  await assert.rejects(synchronouslyBroken.request("runtime.ping", {}), /sync failure/);
}

function testProjector(projector, storeModule) {
  const replayPartition = projector.partitionRuntimeReplay(
    [event("task.started", 1, {
      mode: "react",
      prompt_preview: "Replay prefix",
      started_at: "2026-08-23T10:00:00Z",
    })],
    [event("assistant.completed", 2, { content: "Live answer" })],
    ["task.started"],
  );
  assert.deepEqual(replayPartition.replayEvents.map((item) => item.type), ["task.started"]);
  assert.deepEqual(
    replayPartition.liveEvents.map((item) => item.type),
    ["assistant.completed"],
    "buffered live events must never be filtered by the replay allow-list",
  );
  const mergedLanes = projector.mergeRuntimeReplayLanes(
    [event("task.completed", 4, { status: "completed", elapsed_ms: 100 })],
    [event("assistant.completed", 2, { content: "Buffered answer" })],
  );
  assert.deepEqual(
    mergedLanes.map((lane) => [lane.event.sequence, lane.source]),
    [[2, "live"], [4, "replay"]],
    "a lower-sequence buffered answer must project before a newer replayed terminal event",
  );

  let state = projector.createInitialSupervisionState();
  state = projector.registerSupervisionSession(state, {
    sessionId: "session-a",
    projectId: "project-a",
    title: "Conversation A",
    workspace: "E:\\project-a",
  });
  const started = event("task.started", 1, {
    mode: "react",
    prompt_preview: "Fix the tests",
    started_at: "2026-08-23T10:00:00Z",
  });
  state = projector.projectRuntimeEvent(state, started);
  assert.equal(state.tasks["task-a"].status, "running");
  assert.equal(state.tasks["task-a"].projectId, "project-a");
  const afterStarted = state;
  state = projector.projectRuntimeEvent(state, started, { source: "replay" });
  assert.equal(state, afterStarted, "duplicate live/replay envelope must be referentially idempotent");

  const requested = event("approval.requested", 2, {
    approval_id: "approval-a",
    tool_call_id: "tool-a",
    name: "write_file",
    arguments: { path: "README.md" },
    danger_level: "medium",
    risk_description: "Writes a workspace file",
  });
  state = projector.projectRuntimeEvent(state, requested);
  assert.equal(state.tasks["task-a"].status, "waiting_approval");
  assert.equal(state.approvals["approval-a"].status, "pending");

  state = projector.projectRuntimeEvent(state, event("approval.resolved", 3, {
    approval_id: "approval-a",
    decision: "approve",
  }));
  assert.equal(state.approvals["approval-a"].status, "approved");
  assert.equal(state.tasks["task-a"].status, "running");

  state = projector.projectRuntimeEvent(state, event("task.completed", 4, {
    status: "completed",
    elapsed_ms: 1200,
  }));
  assert.equal(state.tasks["task-a"].status, "completed");
  assert.equal(state.tasks["task-a"].elapsedMs, 1200);
  state = projector.registerActiveSupervisionTask(state, {
    taskId: "task-a",
    sessionId: "session-a",
    projectId: "project-a",
    phase: "running",
  });
  assert.equal(state.tasks["task-a"].status, "completed", "active snapshot must not resurrect terminal tasks");
  const terminalState = state;
  state = projector.projectRuntimeEvent(state, event("tool.started", 2, {
    tool_call_id: "stale-tool",
    name: "read_file",
    arguments: {},
    iteration: 1,
  }));
  assert.equal(state, terminalState, "out-of-order events must not regress terminal state");

  const deleted = {
    ...event("session.deleted", 5, { title: "Conversation A" }),
    task_id: undefined,
  };
  state = projector.projectRuntimeEvent(state, deleted);
  assert.equal(state.sessions["session-a"], undefined);
  const deletedState = state;
  state = projector.projectRuntimeEvent(state, deleted, { source: "replay" });
  assert.equal(state, deletedState, "session deletion tombstone must deduplicate replay");

  let cancelledWithApproval = projector.createInitialSupervisionState();
  cancelledWithApproval = projector.projectRuntimeEvent(cancelledWithApproval, event("task.started", 1, {
    mode: "react",
    prompt_preview: "Cancel me",
    started_at: "2026-08-23T10:00:00Z",
  }, "session-d", "task-d"));
  cancelledWithApproval = projector.projectRuntimeEvent(cancelledWithApproval, event("approval.requested", 2, {
    approval_id: "approval-d",
    tool_call_id: "tool-d",
    name: "write_file",
    arguments: { path: "danger.txt" },
    danger_level: "high",
    risk_description: "Writes a file",
  }, "session-d", "task-d"));
  cancelledWithApproval = projector.projectRuntimeEvent(cancelledWithApproval, event("task.cancelled", 3, {
    status: "cancelled",
    reason: "user",
  }, "session-d", "task-d"));
  assert.equal(cancelledWithApproval.approvals["approval-d"].status, "interrupted");
  assert.deepEqual(cancelledWithApproval.tasks["task-d"].pendingApprovalIds, []);

  const store = new storeModule.SupervisionStore();
  let notifications = 0;
  const unsubscribe = store.subscribe(() => { notifications += 1; });
  store.registerActiveTask({
    taskId: "task-active",
    sessionId: "session-b",
    projectId: "project-b",
    phase: "cancelling",
    registeredAt: "2026-08-23T11:00:00Z",
  });
  assert.equal(store.getSnapshot().tasks["task-active"].status, "stopping");
  store.ingest(event("assistant.delta", 1, { delta: "stream" }, "session-b", "task-active"));
  assert.equal(
    Object.keys(store.getSnapshot().seenEventIds).length,
    0,
    "live store dedupe must stay outside the immutable projection snapshot",
  );
  assert.equal(storeModule.selectActiveTasks(store.getSnapshot()).length, 1);
  store.reconcileProjectActiveTasks("project-b", []);
  assert.equal(store.getSnapshot().tasks["task-active"].status, "failed");
  assert.equal(store.getSnapshot().tasks["task-active"].errorCode, "runtime_interrupted");
  assert.equal(storeModule.selectActiveTasks(store.getSnapshot()).length, 0);

  store.recordSubmissionFailure({
    requestId: "submit-background",
    sessionId: "session-submit",
    projectId: "project-submit",
    promptPreview: "Run in the background",
    errorCode: "task_busy",
    errorMessage: "Conversation already has a running task",
    startedAt: "2026-08-23T11:30:00Z",
    failedAt: "2026-08-23T11:30:01Z",
  });
  const failedSubmissionId = "submission:submit-background";
  assert.equal(store.getSnapshot().tasks[failedSubmissionId].status, "failed");
  assert.equal(store.getSnapshot().tasks[failedSubmissionId].sessionId, "session-submit");
  assert.equal(store.getSnapshot().tasks[failedSubmissionId].elapsedMs, 1000);
  // Opening the background conversation replays its durable journal. Local RPC
  // failures have no journal event, so the per-session store must retain them.
  store.replay("session-submit", [], {
    sessionId: "session-submit",
    projectId: "project-submit",
    title: "Background conversation",
  });
  assert.equal(store.getSnapshot().tasks[failedSubmissionId].status, "failed");
  assert.equal(store.getSnapshot().sessions["session-submit"].meta.title, "Background conversation");

  const uncertainStore = new storeModule.SupervisionStore();
  uncertainStore.recordSubmissionFailure({
    requestId: "submit-unknown",
    sessionId: "session-unknown",
    projectId: "project-unknown",
    promptPreview: "May already be running",
    errorCode: "request_timeout",
    errorMessage: "The submit response timed out",
    uncertain: true,
  });
  const uncertainId = "submission:submit-unknown";
  assert.equal(uncertainStore.getSnapshot().tasks[uncertainId].status, "recovering");
  assert.equal(uncertainStore.getSnapshot().tasks[uncertainId].submissionUncertain, true);
  uncertainStore.ingest(event("task.started", 1, {
    mode: "react",
    prompt_preview: "May already be running",
    started_at: "2026-08-23T11:45:00Z",
  }, "session-unknown", "task-authoritative"));
  assert.equal(
    uncertainStore.getSnapshot().tasks[uncertainId],
    undefined,
    "an authoritative task route must consume the uncertain submit placeholder",
  );
  assert.equal(uncertainStore.getSnapshot().tasks["task-authoritative"].status, "running");

  const snapshotStore = new storeModule.SupervisionStore();
  snapshotStore.recordSubmissionFailure({
    requestId: "submit-snapshot-unknown",
    sessionId: "session-snapshot-unknown",
    projectId: "project-snapshot-unknown",
    errorCode: "runtime_transport_error",
    errorMessage: "Transport disconnected",
    uncertain: true,
  });
  snapshotStore.registerActiveTask({
    taskId: "task-from-workspace-open",
    sessionId: "session-snapshot-unknown",
    projectId: "project-snapshot-unknown",
    phase: "recovering",
  });
  assert.equal(snapshotStore.getSnapshot().tasks["submission:submit-snapshot-unknown"], undefined);
  assert.equal(snapshotStore.getSnapshot().tasks["task-from-workspace-open"].status, "recovering");

  const disconnectedStore = new storeModule.SupervisionStore();
  disconnectedStore.registerActiveTask({
    taskId: "task-project-one",
    sessionId: "session-project-one",
    projectId: "project-one",
    phase: "running",
  });
  disconnectedStore.registerActiveTask({
    taskId: "task-project-two",
    sessionId: "session-project-two",
    projectId: "project-two",
    phase: "waiting_approval",
  });
  disconnectedStore.markRuntimeDisconnected(false);
  assert.equal(disconnectedStore.getSnapshot().tasks["task-project-one"].status, "recovering");
  assert.equal(disconnectedStore.getSnapshot().tasks["task-project-two"].status, "recovering");
  disconnectedStore.markRuntimeDisconnected(true);
  assert.equal(disconnectedStore.getSnapshot().tasks["task-project-one"].status, "failed");
  assert.equal(disconnectedStore.getSnapshot().tasks["task-project-two"].status, "failed");

  const replayed = [
    event("task.started", 1, {
      mode: "plan",
      prompt_preview: "Replay me",
      started_at: "2026-08-23T12:00:00Z",
    }, "session-c", "task-c"),
    event("approval.requested", 2, {
      approval_id: "approval-c",
      tool_call_id: "tool-c",
      name: "execute_command",
      arguments: { command: "npm test" },
      danger_level: "high",
      risk_description: "Executes a command",
    }, "session-c", "task-c"),
  ];
  store.replay("session-c", replayed, {
    sessionId: "session-c",
    projectId: "project-current",
    title: "Current title",
    workspace: "E:\\current",
  });
  const replayState = store.getSnapshot();
  assert.equal(replayState.tasks["task-c"].lastEventSource, "replay");
  assert.equal(replayState.tasks["task-c"].status, "waiting_approval");
  assert.equal(storeModule.selectPendingApprovals(replayState)[0].id, "approval-c");
  assert.equal(replayState.sessions["session-c"].meta.title, "Current title");
  assert.equal(replayState.sessions["session-c"].meta.projectId, "project-current");

  store.replay("session-c", [], {
    sessionId: "session-c",
    projectId: "project-current",
    title: "Newest title",
    workspace: "E:\\current",
  });
  assert.equal(store.getSnapshot().tasks["task-c"].status, "waiting_approval");
  assert.equal(store.getSnapshot().approvals["approval-c"].status, "pending");
  assert.equal(store.getSnapshot().sessions["session-c"].meta.title, "Newest title");

  store.markApprovalResolving("approval-c", true);
  assert.equal(store.getSnapshot().approvals["approval-c"].status, "resolving");
  store.markApprovalResolving("approval-c", false);
  assert.equal(store.getSnapshot().approvals["approval-c"].status, "pending");
  store.markTaskStopping("task-c");
  assert.equal(store.getSnapshot().tasks["task-c"].status, "stopping");
  assert.equal(store.getSnapshot().approvals["approval-c"].status, "interrupted");
  assert.equal(storeModule.selectPendingApprovals(store.getSnapshot()).length, 0);
  store.ingest(event("approval.requested", 3, {
    approval_id: "approval-c-late",
    tool_call_id: "tool-c-late",
    name: "write_file",
    arguments: { path: "late.txt" },
    danger_level: "high",
    risk_description: "Late approval after cancellation",
  }, "session-c", "task-c"));
  assert.equal(store.getSnapshot().approvals["approval-c-late"].status, "interrupted");
  assert.equal(store.getSnapshot().tasks["task-c"].status, "stopping");

  const recoveredStore = new storeModule.SupervisionStore();
  recoveredStore.replay("session-recovered", [
    event("task.started", 1, {
      mode: "react",
      prompt_preview: "Interrupted task",
      started_at: "2026-08-23T13:00:00Z",
    }, "session-recovered", "task-recovered"),
    event("approval.requested", 2, {
      approval_id: "approval-recovered",
      tool_call_id: "tool-recovered",
      name: "execute_command",
      arguments: { command: "npm test" },
      danger_level: "high",
      risk_description: "Executes a command",
    }, "session-recovered", "task-recovered"),
  ], { sessionId: "session-recovered", projectId: "project-recovered" });
  recoveredStore.ingest(event("task.started", 3, {
    mode: "react",
    prompt_preview: "Interrupted task",
    started_at: "2026-08-23T13:00:00Z",
    recovered: true,
  }, "session-recovered", "task-recovered"));
  assert.equal(recoveredStore.getSnapshot().approvals["approval-recovered"].status, "interrupted");
  assert.equal(storeModule.selectPendingApprovals(recoveredStore.getSnapshot()).length, 0);

  const staleReplayStore = new storeModule.SupervisionStore();
  staleReplayStore.replay("session-stale", replayed.map((item) => ({
    ...item,
    event_id: item.event_id.replace("session-c", "session-stale"),
    session_id: "session-stale",
    task_id: "task-stale",
    data: item.type === "approval.requested"
      ? { ...item.data, approval_id: "approval-stale", tool_call_id: "tool-stale" }
      : item.data,
  })), { sessionId: "session-stale", projectId: "project-stale" });
  staleReplayStore.reconcileProjectActiveTasks("project-stale", []);
  assert.equal(staleReplayStore.getSnapshot().tasks["task-stale"].status, "failed");
  assert.equal(staleReplayStore.getSnapshot().approvals["approval-stale"].status, "interrupted");
  assert.ok(notifications >= 4);
  unsubscribe();
}

function response(requestId, result) {
  return {
    kind: "response",
    protocol_version: 1,
    request_id: requestId,
    ok: true,
    result,
  };
}

function event(type, sequence, data, sessionId = "session-a", taskId = "task-a") {
  return {
    kind: "event",
    protocol_version: 1,
    event_id: `${sessionId}:${sequence}:${type}`,
    session_id: sessionId,
    task_id: taskId,
    sequence,
    timestamp: `2026-08-23T10:00:0${Math.min(sequence, 9)}Z`,
    type,
    data,
  };
}
