export interface TranscriptOrderEntry {
  kind: string;
  taskId?: string;
  phase?: string;
}

/**
 * Keeps live task progress in its original position, but projects terminal
 * task summaries after the task's final Agent output. This is presentation
 * ordering only; durable Runtime event order remains unchanged.
 */
export function orderTerminalTaskSummaries<T extends TranscriptOrderEntry>(entries: T[]): T[] {
  const deferred = new Map<number, T[]>();
  const terminalStatuses = new Set<T>();

  for (let statusIndex = 0; statusIndex < entries.length; statusIndex += 1) {
    const entry = entries[statusIndex];
    if (entry.kind !== "task-status" || entry.phase === "running" || !entry.taskId) continue;
    const answerIndex = findLastTaskEntry(entries, entry.taskId, ["assistant"]);
    const unscopedAnswerIndex = answerIndex >= 0 ? -1 : findUnscopedTurnAnswer(entries, statusIndex);
    const structuredOutputIndex = answerIndex >= 0 || unscopedAnswerIndex >= 0
      ? -1
      : findLastTaskEntry(entries, entry.taskId, ["plan", "team"]);
    const fallbackIndex = answerIndex >= 0
      ? answerIndex
      : unscopedAnswerIndex >= 0
        ? unscopedAnswerIndex
        : structuredOutputIndex >= 0
          ? structuredOutputIndex
          : findLastTaskEntry(entries, entry.taskId);
    if (fallbackIndex < 0) continue;
    terminalStatuses.add(entry);
    const pending = deferred.get(fallbackIndex) ?? [];
    pending.push(entry);
    deferred.set(fallbackIndex, pending);
  }

  if (terminalStatuses.size === 0) return entries;
  const ordered: T[] = [];
  entries.forEach((entry, index) => {
    if (!terminalStatuses.has(entry)) ordered.push(entry);
    const summaries = deferred.get(index);
    if (summaries) ordered.push(...summaries);
  });
  return ordered;
}

/** Older compact snapshots did not persist taskId on assistant messages. */
function findUnscopedTurnAnswer<T extends TranscriptOrderEntry>(entries: T[], statusIndex: number) {
  let turnEnd = entries.length;
  for (let index = statusIndex + 1; index < entries.length; index += 1) {
    if (entries[index].kind === "user") {
      turnEnd = index;
      break;
    }
  }
  for (let index = turnEnd - 1; index > statusIndex; index -= 1) {
    const candidate = entries[index];
    if (!candidate.taskId && candidate.kind === "assistant") return index;
  }
  return -1;
}

function findLastTaskEntry<T extends TranscriptOrderEntry>(
  entries: T[],
  taskId: string,
  allowedKinds?: readonly string[],
) {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const candidate = entries[index];
    if (candidate.taskId !== taskId || candidate.kind === "task-status") continue;
    if (!allowedKinds || allowedKinds.includes(candidate.kind)) {
      return index;
    }
  }
  return -1;
}
