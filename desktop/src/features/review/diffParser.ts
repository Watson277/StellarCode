import type { ChangedFileSummary, TaskDiffResult } from "../../protocol/runtimeEvents";

export type ReviewDiffLineKind = "context" | "addition" | "deletion" | "meta";

export interface ReviewDiffLine {
  id: string;
  kind: ReviewDiffLineKind;
  marker: string;
  content: string;
  oldLine?: number;
  newLine?: number;
}

export interface ReviewDiffHunk {
  id: string;
  header: string;
  oldStart: number;
  newStart: number;
  lines: ReviewDiffLine[];
}

export interface ReviewDiffFile {
  id: string;
  path: string;
  oldPath: string;
  newPath: string;
  status: ChangedFileSummary["status"];
  additions: number;
  deletions: number;
  binary: boolean;
  patch: string;
  metadata: string[];
  hunks: ReviewDiffHunk[];
}

/** Converts Git's unified patch into file/hunk/line structures for the desktop reviewer. */
export function buildReviewDiffFiles(result: Pick<TaskDiffResult, "diff" | "changed_files">): ReviewDiffFile[] {
  const summaries = result.changed_files;
  const sections = splitFileSections(result.diff || "");
  const parsed = sections.map((section, index) => parseFileSection(section, index, summaries));
  const knownPaths = new Set(parsed.map((file) => normalizePath(file.path)));

  for (const summary of summaries) {
    if (knownPaths.has(normalizePath(summary.path))) continue;
    parsed.push(emptyReviewFile(summary, parsed.length));
  }

  return parsed.sort((left, right) => {
    const leftIndex = summaries.findIndex((summary) => samePath(summary.path, left.path));
    const rightIndex = summaries.findIndex((summary) => samePath(summary.path, right.path));
    if (leftIndex >= 0 && rightIndex >= 0) return leftIndex - rightIndex;
    if (leftIndex >= 0) return -1;
    if (rightIndex >= 0) return 1;
    return left.path.localeCompare(right.path);
  });
}

function splitFileSections(patch: string): string[][] {
  const lines = patch.replace(/\r\n/g, "\n").split("\n");
  const sections: string[][] = [];
  let current: string[] = [];

  for (const line of lines) {
    if (line.startsWith("diff --git ") && current.length > 0) {
      sections.push(current);
      current = [];
    }
    if (line || current.length > 0) current.push(line);
  }
  if (current.length > 0 && current.some((line) => line.length > 0)) sections.push(current);
  return sections;
}

function parseFileSection(
  lines: string[],
  fileIndex: number,
  summaries: ChangedFileSummary[],
): ReviewDiffFile {
  const oldHeader = lines.find((line) => line.startsWith("--- "))?.slice(4) ?? "";
  const newHeader = lines.find((line) => line.startsWith("+++ "))?.slice(4) ?? "";
  const gitHeader = lines.find((line) => line.startsWith("diff --git ")) ?? "";
  const headerPaths = parseGitHeaderPaths(gitHeader);
  const oldPath = cleanGitPath(oldHeader || headerPaths.oldPath);
  const newPath = cleanGitPath(newHeader || headerPaths.newPath);
  const candidatePath = newPath && newPath !== "/dev/null"
    ? newPath
    : oldPath && oldPath !== "/dev/null" ? oldPath : `change-${fileIndex + 1}`;
  const summary = summaries.find((item) => samePath(item.path, candidatePath))
    ?? summaries[fileIndex];
  const path = summary?.path || candidatePath;
  const metadata: string[] = [];
  const hunks: ReviewDiffHunk[] = [];
  let currentHunk: ReviewDiffHunk | null = null;
  let oldLine = 0;
  let newLine = 0;

  for (const raw of lines) {
    const range = parseHunkRange(raw);
    if (range) {
      currentHunk = {
        id: `${stableId(path)}-hunk-${hunks.length + 1}`,
        header: raw,
        oldStart: range.oldStart,
        newStart: range.newStart,
        lines: [],
      };
      hunks.push(currentHunk);
      oldLine = range.oldStart;
      newLine = range.newStart;
      continue;
    }
    if (!currentHunk) {
      metadata.push(raw);
      continue;
    }

    const lineIndex = currentHunk.lines.length;
    if (raw.startsWith("+") && !raw.startsWith("+++")) {
      currentHunk.lines.push({
        id: `${currentHunk.id}-line-${lineIndex + 1}`,
        kind: "addition",
        marker: "+",
        content: raw.slice(1),
        newLine,
      });
      newLine += 1;
    } else if (raw.startsWith("-") && !raw.startsWith("---")) {
      currentHunk.lines.push({
        id: `${currentHunk.id}-line-${lineIndex + 1}`,
        kind: "deletion",
        marker: "-",
        content: raw.slice(1),
        oldLine,
      });
      oldLine += 1;
    } else if (raw.startsWith(" ")) {
      currentHunk.lines.push({
        id: `${currentHunk.id}-line-${lineIndex + 1}`,
        kind: "context",
        marker: " ",
        content: raw.slice(1),
        oldLine,
        newLine,
      });
      oldLine += 1;
      newLine += 1;
    } else {
      currentHunk.lines.push({
        id: `${currentHunk.id}-line-${lineIndex + 1}`,
        kind: "meta",
        marker: "",
        content: raw,
      });
    }
  }

  const countedAdditions = hunks.flatMap((hunk) => hunk.lines).filter((line) => line.kind === "addition").length;
  const countedDeletions = hunks.flatMap((hunk) => hunk.lines).filter((line) => line.kind === "deletion").length;
  const binary = summary?.status === "binary" || lines.some((line) => line.startsWith("Binary files "));

  return {
    id: `review-file-${fileIndex + 1}-${stableId(path)}`,
    path,
    oldPath,
    newPath,
    status: summary?.status ?? inferStatus(lines),
    additions: summary?.additions ?? countedAdditions,
    deletions: summary?.deletions ?? countedDeletions,
    binary,
    patch: lines.join("\n").replace(/\n+$/, ""),
    metadata,
    hunks,
  };
}

function emptyReviewFile(summary: ChangedFileSummary, index: number): ReviewDiffFile {
  return {
    id: `review-file-${index + 1}-${stableId(summary.path)}`,
    path: summary.path,
    oldPath: summary.status === "created" ? "/dev/null" : summary.path,
    newPath: summary.status === "deleted" ? "/dev/null" : summary.path,
    status: summary.status,
    additions: summary.additions,
    deletions: summary.deletions,
    binary: summary.status === "binary",
    patch: "",
    metadata: [],
    hunks: [],
  };
}

function parseHunkRange(line: string) {
  const match = line.match(/^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/);
  if (!match) return null;
  return { oldStart: Number(match[1]), newStart: Number(match[2]) };
}

function parseGitHeaderPaths(header: string) {
  const payload = header.replace(/^diff --git\s+/, "");
  const divider = payload.lastIndexOf(" b/");
  if (divider < 0) return { oldPath: "", newPath: "" };
  return {
    oldPath: payload.slice(0, divider),
    newPath: payload.slice(divider + 1),
  };
}

function cleanGitPath(value: string) {
  let path = value.trim();
  if (path.startsWith('"') && path.endsWith('"')) {
    path = path.slice(1, -1)
      .replace(/\\"/g, '"')
      .replace(/\\t/g, "\t")
      .replace(/\\n/g, "\n")
      .replace(/\\\\/g, "\\");
  }
  if (path.startsWith("a/") || path.startsWith("b/")) path = path.slice(2);
  return path;
}

function inferStatus(lines: string[]): ChangedFileSummary["status"] {
  if (lines.some((line) => line.startsWith("new file mode "))) return "created";
  if (lines.some((line) => line.startsWith("deleted file mode "))) return "deleted";
  if (lines.some((line) => line.startsWith("old mode ") || line.startsWith("new mode "))) return "type_changed";
  if (lines.some((line) => line.startsWith("Binary files "))) return "binary";
  return "modified";
}

function samePath(left: string, right: string) {
  return normalizePath(left) === normalizePath(right);
}

function normalizePath(value: string) {
  return value.replace(/\\/g, "/").replace(/^\.\//, "").toLocaleLowerCase();
}

function stableId(value: string) {
  return value.replace(/[^a-zA-Z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "change";
}
