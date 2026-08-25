import { memo, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { invoke } from "@tauri-apps/api/core";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import type { Translator } from "../../i18n";
import type { ChangedFileSummary, TaskDiffResult } from "../../protocol/runtimeEvents";
import { buildReviewDiffFiles, type ReviewDiffFile } from "./diffParser";
import { highlightDiffLine, syntaxLanguageForPath } from "./syntaxHighlight";
import "./ReviewChangesWorkbench.css";

interface ReviewChangesWorkbenchProps {
  result: TaskDiffResult;
  projectId: string;
  workspace: string;
  rollbackBusy: boolean;
  canRollback: boolean;
  t: Translator;
  onClose: () => void;
  onRollback: () => Promise<boolean>;
  embedded?: boolean;
  selectedPath?: string;
  onSelectedPathChange?: (path: string) => void;
  showFileSidebar?: boolean;
}

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "input:not([disabled])",
  "summary",
  "[href]",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

/** Task-scoped file and hunk reviewer backed by the protected Git snapshot. */
export function ReviewChangesWorkbench({
  result,
  projectId,
  workspace,
  rollbackBusy,
  canRollback,
  t,
  onClose,
  onRollback,
  embedded = false,
  selectedPath: controlledSelectedPath,
  onSelectedPathChange,
  showFileSidebar = true,
}: ReviewChangesWorkbenchProps) {
  const files = useMemo(() => buildReviewDiffFiles(result), [result]);
  const [query, setQuery] = useState("");
  const [internalSelectedPath, setInternalSelectedPath] = useState(() => files[0]?.path ?? "");
  const [activeHunkIndex, setActiveHunkIndex] = useState(0);
  const [copiedTarget, setCopiedTarget] = useState<"all" | "file" | null>(null);
  const [actionError, setActionError] = useState("");
  const [localRollbackBusy, setLocalRollbackBusy] = useState(false);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const diffScrollRef = useRef<HTMLDivElement | null>(null);
  const hunkRefs = useRef(new Map<string, HTMLElement>());

  const filteredFiles = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return normalized
      ? files.filter((file) => file.path.toLocaleLowerCase().includes(normalized))
      : files;
  }, [files, query]);
  const selectedPath = controlledSelectedPath ?? internalSelectedPath;
  const selectedFile = files.find((file) => file.path === selectedPath) ?? files[0] ?? null;
  const selectedLanguage = selectedFile ? syntaxLanguageForPath(selectedFile.path) : null;
  const selectedFilePath = selectedFile ? resolveWorkspacePath(workspace, selectedFile.path) : "";
  const selectedRevealPath = selectedFile?.status === "deleted"
    ? parentDirectory(selectedFilePath)
    : selectedFilePath;

  useEffect(() => {
    if (selectedFile || files.length === 0) return;
    setInternalSelectedPath(files[0].path);
    onSelectedPathChange?.(files[0].path);
  }, [files, onSelectedPathChange, selectedFile]);

  useEffect(() => {
    setActiveHunkIndex(0);
    diffScrollRef.current?.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [selectedPath]);

  useEffect(() => {
    if (embedded) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      previousFocus?.focus();
    };
  }, [embedded]);

  function handleDialogKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (embedded) return;
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab" || !dialogRef.current) return;
    const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      .filter((element) => !element.hasAttribute("disabled") && element.offsetParent !== null);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function selectFile(file: ReviewDiffFile) {
    setInternalSelectedPath(file.path);
    onSelectedPathChange?.(file.path);
    setActionError("");
  }

  function goToHunk(nextIndex: number) {
    if (!selectedFile || selectedFile.hunks.length === 0) return;
    const bounded = Math.max(0, Math.min(selectedFile.hunks.length - 1, nextIndex));
    setActiveHunkIndex(bounded);
    window.requestAnimationFrame(() => {
      hunkRefs.current.get(selectedFile.hunks[bounded].id)?.scrollIntoView({ block: "start" });
    });
  }

  async function copyPatch(target: "all" | "file") {
    const content = target === "all" ? result.diff : selectedFile?.patch ?? "";
    if (!content) return;
    try {
      await navigator.clipboard.writeText(content);
      setCopiedTarget(target);
      window.setTimeout(() => setCopiedTarget(null), 1_500);
    } catch (error) {
      setActionError(t("Copy failed: {error}", { error: String(error) }));
    }
  }

  async function openSelectedFile() {
    if (!projectId || !selectedFilePath || selectedFile?.status === "deleted") return;
    setActionError("");
    try {
      await invoke<string>("workspace_file_open", {
        projectId,
        relativePath: selectedFile?.path ?? "",
      });
    } catch (error) {
      setActionError(t("Open file failed: {error}", { error: String(error) }));
    }
  }

  async function revealSelectedFile() {
    if (!selectedRevealPath) return;
    setActionError("");
    try {
      await revealItemInDir(selectedRevealPath);
    } catch (error) {
      setActionError(t("Reveal file failed: {error}", { error: String(error) }));
    }
  }

  async function rollbackTask() {
    if (!canRollback || rollbackBusy || localRollbackBusy) return;
    setLocalRollbackBusy(true);
    setActionError("");
    let completed = false;
    try {
      completed = await onRollback();
      if (!completed) setActionError(t("Task changes were not restored."));
    } finally {
      setLocalRollbackBusy(false);
    }
    if (completed) onClose();
  }

  const rollbackPending = rollbackBusy || localRollbackBusy;
  return <section
    className={embedded ? "review-panel" : "review-overlay"}
    role={embedded ? "region" : "dialog"}
    aria-modal={embedded ? undefined : "true"}
    aria-label={t("Review changes")}
    onMouseDown={(event) => {
      if (!embedded && event.target === event.currentTarget) onClose();
    }}
  >
    <div className={`review-workbench ${embedded ? "embedded" : ""}`} ref={dialogRef} onKeyDown={handleDialogKeyDown}>
      <header className="review-titlebar">
        <div>
          <span className="review-eyebrow">{t("Protected task changes")}</span>
          <strong>{t("Review changes")}</strong>
          <small>{t("{count} files · +{additions} -{deletions}", {
            count: result.changed_files.length,
            additions: result.additions,
            deletions: result.deletions,
          })}</small>
        </div>
        <div className="review-title-actions">
          <button type="button" className="secondary-button" onClick={() => void copyPatch("all")} disabled={!result.diff}>{t(copiedTarget === "all" ? "Copied" : "Copy all Patch")}</button>
          {!embedded && <button type="button" className="secondary-button review-close" ref={closeButtonRef} onClick={onClose}>{t("Close")}</button>}
        </div>
      </header>

      <div className={`review-layout ${showFileSidebar ? "" : "viewer-only"}`}>
        {showFileSidebar && <aside className="review-file-sidebar" aria-label={t("Changed files") }>
          <div className="review-file-filter">
            <input value={query} onChange={(event) => setQuery(event.currentTarget.value)} placeholder={t("Filter changed files")} aria-label={t("Filter changed files")} />
            <span>{filteredFiles.length}/{files.length}</span>
          </div>
          <div className="review-file-list">
            {filteredFiles.map((file) => <button
              type="button"
              className={file.path === selectedFile?.path ? "active" : ""}
              onClick={() => selectFile(file)}
              aria-current={file.path === selectedFile?.path ? "true" : undefined}
              title={file.path}
              key={file.id}
            >
              <span className={`review-file-status status-${file.status}`}>{changeStatusMarker(file.status)}</span>
              <span className="review-file-copy"><strong>{file.path.split(/[\\/]/).pop() || file.path}</strong><small>{parentPathLabel(file.path)}</small></span>
              <span className="review-file-stats"><i>+{file.additions}</i><b>-{file.deletions}</b></span>
            </button>)}
            {filteredFiles.length === 0 && <div className="review-empty-list">{t("No changed files match this filter.")}</div>}
          </div>
        </aside>}

        <main className="review-viewer">
          {selectedFile ? <>
            <header className="review-file-toolbar">
              <div className="review-selected-file">
                <span className={`review-status-badge status-${selectedFile.status}`}>{t(changeStatusLabel(selectedFile.status))}</span>
                <div><strong title={selectedFile.path}>{selectedFile.path}</strong><small>+{selectedFile.additions} -{selectedFile.deletions}{selectedFile.binary ? ` · ${t("Binary file")}` : ""}</small></div>
                <span className="review-language-badge">{selectedLanguage ?? "text"}</span>
              </div>
              <div className="review-file-actions">
                <button type="button" onClick={() => void openSelectedFile()} disabled={!projectId || !selectedFilePath || selectedFile.status === "deleted"}>{t("Open file")}</button>
                <button type="button" onClick={() => void revealSelectedFile()} disabled={!selectedRevealPath}>{t("Reveal")}</button>
                <button type="button" onClick={() => void copyPatch("file")} disabled={!selectedFile.patch}>{t(copiedTarget === "file" ? "Copied" : "Copy file Patch")}</button>
              </div>
            </header>

            <div className="review-hunk-toolbar">
              <span>{t("{count} change blocks", { count: selectedFile.hunks.length })}</span>
              <div>
                <button type="button" onClick={() => goToHunk(activeHunkIndex - 1)} disabled={selectedFile.hunks.length === 0 || activeHunkIndex === 0} aria-label={t("Previous change block")}>↑</button>
                <strong>{selectedFile.hunks.length ? `${activeHunkIndex + 1} / ${selectedFile.hunks.length}` : "0 / 0"}</strong>
                <button type="button" onClick={() => goToHunk(activeHunkIndex + 1)} disabled={selectedFile.hunks.length === 0 || activeHunkIndex >= selectedFile.hunks.length - 1} aria-label={t("Next change block")}>↓</button>
              </div>
            </div>

            <div className="review-diff-scroll" ref={diffScrollRef}>
              {selectedFile.metadata.length > 0 && <details className="review-file-metadata">
                <summary>{t("Git file metadata")}</summary>
                <pre>{selectedFile.metadata.join("\n")}</pre>
              </details>}
              {selectedFile.hunks.length > 0 ? <div className="review-diff-table">
                {selectedFile.hunks.map((hunk, hunkIndex) => <section
                  className={`review-hunk ${hunkIndex === activeHunkIndex ? "active" : ""}`}
                  ref={(element) => {
                    if (element) hunkRefs.current.set(hunk.id, element);
                    else hunkRefs.current.delete(hunk.id);
                  }}
                  key={hunk.id}
                >
                  <button type="button" className="review-hunk-header" onClick={() => setActiveHunkIndex(hunkIndex)}>{hunk.header}</button>
                  {hunk.lines.map((line) => <div className={`review-diff-line line-${line.kind}`} key={line.id}>
                    <span className="review-line-number old">{line.oldLine ?? ""}</span>
                    <span className="review-line-number next">{line.newLine ?? ""}</span>
                    <span className="review-line-marker" aria-hidden="true">{line.marker}</span>
                    <HighlightedDiffCode content={line.content || " "} path={selectedFile.path} plain={line.kind === "meta"} />
                  </div>)}
                </section>)}
              </div> : <div className="review-no-diff">
                <strong>{selectedFile.binary ? t("Binary file changed") : t("No textual diff is available.")}</strong>
                <p>{selectedFile.binary ? t("Binary content cannot be rendered as a line diff.") : t("The protected snapshot records this file, but no textual patch was returned.")}</p>
              </div>}
            </div>
          </> : <div className="review-no-diff"><strong>{t("No changed files")}</strong></div>}
        </main>
      </div>

      <footer className="review-footer">
        <div className="review-footer-notices">
          {result.diff_truncated && <span className="warning">{t("The displayed diff was truncated. The Git snapshot still contains the complete task state.")}</span>}
          {result.rollback_block_reason && <span className="warning">{t(result.rollback_block_reason)}</span>}
          {actionError && <span className="error">{actionError}</span>}
          {!result.diff_truncated && !result.rollback_block_reason && !actionError && <span>{t("Changes are read from the task's protected Git snapshot.")}</span>}
        </div>
        <button
          type="button"
          className="review-rollback-button"
          onClick={() => void rollbackTask()}
          disabled={!canRollback || rollbackPending || result.rolled_back}
          title={result.rollback_block_reason || ""}
        >{t(result.rolled_back ? "Task changes rolled back" : rollbackPending ? "Rolling back..." : "Undo task changes")}</button>
      </footer>
    </div>
  </section>;
}

const HighlightedDiffCode = memo(function HighlightedDiffCode({ content, path, plain }: {
  content: string;
  path: string;
  plain: boolean;
}) {
  const highlighted = useMemo(
    () => plain ? { html: escapeHtml(content), language: "plaintext" } : highlightDiffLine(content, path),
    [content, path, plain],
  );
  return <code
    className={`review-code language-${highlighted.language}`}
    // highlightDiffLine returns only escaped source plus spans created by Highlight.js.
    dangerouslySetInnerHTML={{ __html: highlighted.html }}
  />;
});

function changeStatusLabel(status: ChangedFileSummary["status"]) {
  return ({
    created: "Created",
    modified: "Modified",
    deleted: "Deleted",
    type_changed: "Type changed",
    binary: "Binary",
  } satisfies Record<ChangedFileSummary["status"], string>)[status];
}

function changeStatusMarker(status: ChangedFileSummary["status"]) {
  return ({ created: "A", modified: "M", deleted: "D", type_changed: "T", binary: "B" })[status];
}

function parentPathLabel(path: string) {
  const parts = path.split(/[\\/]/);
  parts.pop();
  return parts.join("/") || ".";
}

function resolveWorkspacePath(workspace: string, relativePath: string) {
  if (!workspace || !relativePath || /^[a-zA-Z]:[\\/]/.test(relativePath) || relativePath.startsWith("\\\\")) return "";
  const parts = relativePath.split(/[\\/]+/).filter((part) => part && part !== ".");
  if (parts.some((part) => part === "..")) return "";
  const separator = workspace.includes("\\") ? "\\" : "/";
  return `${workspace.replace(/[\\/]+$/, "")}${separator}${parts.join(separator)}`;
}

function parentDirectory(path: string) {
  const index = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
  return index > 0 ? path.slice(0, index) : path;
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
