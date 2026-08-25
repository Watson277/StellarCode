import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const sourceUrl = new URL("../src/runtime/transcriptOrder.ts", import.meta.url);
const source = await readFile(sourceUrl, "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2020, target: ts.ScriptTarget.ES2020 },
  fileName: sourceUrl.pathname,
}).outputText;
const { orderTerminalTaskSummaries } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

const running = [
  { id: "user", kind: "user" },
  { id: "status", kind: "task-status", taskId: "task-1", phase: "running" },
  { id: "tool", kind: "tool", taskId: "task-1" },
  { id: "answer", kind: "assistant", taskId: "task-1" },
];
assert.deepEqual(orderTerminalTaskSummaries(running).map((entry) => entry.id), [
  "user", "status", "tool", "answer",
]);

const completed = running.map((entry) => entry.id === "status" ? { ...entry, phase: "completed" } : entry);
assert.deepEqual(orderTerminalTaskSummaries(completed).map((entry) => entry.id), [
  "user", "tool", "answer", "status",
]);

const replayedSnapshot = completed.map((entry) => entry.id === "answer"
  ? { id: entry.id, kind: entry.kind }
  : entry);
assert.deepEqual(orderTerminalTaskSummaries(replayedSnapshot).map((entry) => entry.id), [
  "user", "tool", "answer", "status",
]);

const replayedPlanSnapshot = [
  { id: "user", kind: "user" },
  { id: "status", kind: "task-status", taskId: "task-plan", phase: "completed" },
  { id: "plan", kind: "plan", taskId: "task-plan" },
  { id: "tool", kind: "tool", taskId: "task-plan" },
  { id: "answer", kind: "assistant" },
];
assert.deepEqual(orderTerminalTaskSummaries(replayedPlanSnapshot).map((entry) => entry.id), [
  "user", "plan", "tool", "answer", "status",
]);

const replayedTeamSnapshot = [
  { id: "user", kind: "user" },
  { id: "status", kind: "task-status", taskId: "task-team", phase: "completed" },
  { id: "team", kind: "team", taskId: "task-team" },
  { id: "answer", kind: "assistant" },
];
assert.deepEqual(orderTerminalTaskSummaries(replayedTeamSnapshot).map((entry) => entry.id), [
  "user", "team", "answer", "status",
]);

const structuredOnly = replayedTeamSnapshot.slice(0, -1);
assert.deepEqual(orderTerminalTaskSummaries(structuredOnly).map((entry) => entry.id), [
  "user", "team", "status",
]);

const multipleTasks = [
  { id: "user-1", kind: "user" },
  { id: "status-1", kind: "task-status", taskId: "task-1", phase: "completed" },
  { id: "answer-1", kind: "assistant", taskId: "task-1" },
  { id: "user-2", kind: "user" },
  { id: "status-2", kind: "task-status", taskId: "task-2", phase: "failed" },
  { id: "tool-2", kind: "tool", taskId: "task-2" },
];
assert.deepEqual(orderTerminalTaskSummaries(multipleTasks).map((entry) => entry.id), [
  "user-1", "answer-1", "status-1", "user-2", "tool-2", "status-2",
]);

console.log("Transcript terminal summary order: all assertions passed");
