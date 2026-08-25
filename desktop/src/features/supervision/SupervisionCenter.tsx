import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";

import type { FileChangePreview } from "../../protocol/runtimeEvents";

import "./SupervisionCenter.css";

export type SupervisionTaskStatus =
  | "accepted"
  | "running"
  | "waiting_approval"
  | "stopping"
  | "recovering"
  | "finalizing"
  | "failed"
  | "completed"
  | "cancelled";
export type SupervisionRiskLevel = "low" | "medium" | "high";
export type SupervisionTab = "tasks" | "approvals";
export type SupervisionTranslationValues = Record<string, string | number>;
export type SupervisionTranslator = (message: string, values?: SupervisionTranslationValues) => string;

export interface SupervisionUsage {
  inputTokens?: number;
  outputTokens?: number;
  cachedInputTokens?: number;
  reasoningTokens?: number;
  totalTokens?: number;
  estimatedCost?: number | null;
  currency?: string;
  costEstimated?: boolean;
}

export interface SupervisionTask {
  id: string;
  projectId: string;
  projectName: string;
  sessionId: string;
  conversationTitle: string;
  status: SupervisionTaskStatus;
  activity: string;
  startedAt?: number | string | null;
  elapsedMs?: number | null;
  mode?: "react" | "plan" | "team" | string;
  usage?: SupervisionUsage | null;
  failureMessage?: string | null;
  traceAvailable?: boolean;
  stoppable?: boolean;
  actionPending?: "open" | "stop" | "retry" | "trace" | null;
}

export interface SupervisionApproval {
  id: string;
  taskId: string;
  projectId: string;
  projectName: string;
  sessionId: string;
  conversationTitle: string;
  cwd: string;
  toolName: string;
  arguments?: Record<string, unknown> | null;
  riskLevel: SupervisionRiskLevel;
  riskDescription: string;
  queuePosition?: number;
  queueTotal?: number;
  requestedAt?: number | string | null;
  resolving?: "approve" | "reject" | null;
  changePreview?: FileChangePreview | null;
}

export interface SupervisionCenterProps {
  tasks: readonly SupervisionTask[];
  approvals: readonly SupervisionApproval[];
  t?: SupervisionTranslator;
  locale?: string;
  open?: boolean;
  defaultOpen?: boolean;
  initialTab?: SupervisionTab;
  error?: string;
  onOpenChange?: (open: boolean) => void;
  onDismissError?: () => void;
  onOpenTask?: (task: SupervisionTask) => void | Promise<void>;
  onStopTask?: (task: SupervisionTask) => void | Promise<void>;
  onRetryTask?: (task: SupervisionTask) => void | Promise<void>;
  onOpenTrace?: (task: SupervisionTask) => void | Promise<void>;
  onOpenApproval?: (approval: SupervisionApproval) => void | Promise<void>;
  onResolveApproval?: (approval: SupervisionApproval, decision: "approve" | "reject") => void | Promise<void>;
  className?: string;
}

const DEFAULT_TRANSLATOR: SupervisionTranslator = (message, values) => !values
  ? message
  : Object.entries(values).reduce(
    (result, [key, value]) => result.split(`{${key}}`).join(String(value)),
    message,
  );

const ACTIVE_TASK_STATUSES = new Set<SupervisionTaskStatus>([
  "accepted", "running", "waiting_approval", "stopping", "recovering", "finalizing",
]);

export function SupervisionCenter({
  tasks,
  approvals,
  t = DEFAULT_TRANSLATOR,
  locale = "en",
  open: controlledOpen,
  defaultOpen = false,
  initialTab = "tasks",
  error = "",
  onOpenChange,
  onDismissError,
  onOpenTask,
  onStopTask,
  onRetryTask,
  onOpenTrace,
  onOpenApproval,
  onResolveApproval,
  className = "",
}: SupervisionCenterProps) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const [selectedTab, setSelectedTab] = useState<SupervisionTab>(initialTab);
  const [clock, setClock] = useState(() => Date.now());
  const triggerRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  const tasksTabRef = useRef<HTMLButtonElement>(null);
  const approvalsTabRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const tasksPanelId = useId();
  const approvalsPanelId = useId();
  const isOpen = controlledOpen ?? uncontrolledOpen;

  const setOpen = useCallback((next: boolean) => {
    if (controlledOpen === undefined) setUncontrolledOpen(next);
    onOpenChange?.(next);
  }, [controlledOpen, onOpenChange]);

  const activeTaskCount = useMemo(
    () => tasks.filter((task) => ACTIVE_TASK_STATUSES.has(task.status)).length,
    [tasks],
  );
  const failedTaskCount = useMemo(
    () => tasks.filter((task) => task.status === "failed").length,
    [tasks],
  );
  const previousOpenRef = useRef(isOpen);
  const countAnnouncement = t(
    "{active} active task(s), {approvals} approval(s) waiting, {failed} failed task(s)",
    { active: activeTaskCount, approvals: approvals.length, failed: failedTaskCount },
  );

  useEffect(() => {
    if (!isOpen || activeTaskCount === 0) return;
    setClock(Date.now());
    const interval = window.setInterval(() => setClock(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, [activeTaskCount, isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const frame = window.requestAnimationFrame(() => {
      (selectedTab === "tasks" ? tasksTabRef : approvalsTabRef).current?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [isOpen, selectedTab]);

  useEffect(() => {
    const wasOpen = previousOpenRef.current;
    previousOpenRef.current = isOpen;
    if (wasOpen && !isOpen) {
      const frame = window.requestAnimationFrame(() => triggerRef.current?.focus());
      return () => window.cancelAnimationFrame(frame);
    }
  }, [isOpen]);

  const close = useCallback(() => {
    setOpen(false);
  }, [setOpen]);

  function handleDrawerKeyDown(event: ReactKeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== "Tab" || !drawerRef.current) return;
    const focusable = Array.from(drawerRef.current.querySelectorAll<HTMLElement>(
      "button:not([disabled]), [href], summary, [tabindex]:not([tabindex='-1'])",
    )).filter((element) => !element.hasAttribute("hidden"));
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

  function handleTabKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>) {
    const tabs: SupervisionTab[] = ["tasks", "approvals"];
    const currentIndex = tabs.indexOf(selectedTab);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = tabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const next = tabs[nextIndex];
    setSelectedTab(next);
    (next === "tasks" ? tasksTabRef : approvalsTabRef).current?.focus();
  }

  const openFromTrigger = () => {
    if (!isOpen && approvals.length > 0) setSelectedTab("approvals");
    setOpen(!isOpen);
  };

  return <div className={`supervision-center ${className}`.trim()}>
    <span className="supervision-sr-only" role="status" aria-live="polite" aria-atomic="true">
      {countAnnouncement}
    </span>
    <button
      ref={triggerRef}
      type="button"
      className={`supervision-trigger ${approvals.length > 0 ? "needs-attention" : ""}`}
      aria-haspopup="dialog"
      aria-expanded={isOpen}
      title={t("Open task and approval center")}
      onClick={openFromTrigger}
    >
      <span className="supervision-trigger-icon" aria-hidden="true">◎</span>
      <span className="supervision-trigger-label">{t("Tasks")}</span>
      {activeTaskCount > 0 && <CountBadge
        tone="active"
        count={activeTaskCount}
        label={t("{count} active task(s)", { count: activeTaskCount })}
      />}
      {approvals.length > 0 && <CountBadge
        tone="warning"
        count={approvals.length}
        label={t("{count} approval(s) waiting", { count: approvals.length })}
      />}
      {failedTaskCount > 0 && <span
        className="supervision-trigger-failure"
        aria-label={t("{count} failed task(s)", { count: failedTaskCount })}
      >!</span>}
    </button>

    {isOpen && <div
      className="supervision-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <section
        ref={drawerRef}
        className="supervision-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={handleDrawerKeyDown}
      >
        <header className="supervision-header">
          <div>
            <h2 id={titleId}>{t("Agent supervision")}</h2>
            <p>{t("Monitor background work and handle approvals across projects.")}</p>
          </div>
          <button type="button" className="supervision-close" onClick={close} aria-label={t("Close supervision center")}>×</button>
        </header>

        <div className="supervision-error" role={error ? "alert" : undefined}>
          {error && <><span>{error}</span>{onDismissError && <button type="button" onClick={onDismissError}>{t("Dismiss")}</button>}</>}
        </div>

        <div className="supervision-tabs" role="tablist" aria-label={t("Supervision views")}>
          <button
            ref={tasksTabRef}
            type="button"
            role="tab"
            id={`${tasksPanelId}-tab`}
            aria-selected={selectedTab === "tasks"}
            aria-controls={tasksPanelId}
            tabIndex={selectedTab === "tasks" ? 0 : -1}
            className={selectedTab === "tasks" ? "active" : ""}
            onClick={() => setSelectedTab("tasks")}
            onKeyDown={handleTabKeyDown}
          >{t("Tasks")} <span>{tasks.length}</span></button>
          <button
            ref={approvalsTabRef}
            type="button"
            role="tab"
            id={`${approvalsPanelId}-tab`}
            aria-selected={selectedTab === "approvals"}
            aria-controls={approvalsPanelId}
            tabIndex={selectedTab === "approvals" ? 0 : -1}
            className={selectedTab === "approvals" ? "active" : ""}
            onClick={() => setSelectedTab("approvals")}
            onKeyDown={handleTabKeyDown}
          >{t("Approvals")} <span className={approvals.length > 0 ? "warning" : ""}>{approvals.length}</span></button>
        </div>

        {selectedTab === "tasks"
          ? <div id={tasksPanelId} role="tabpanel" aria-labelledby={`${tasksPanelId}-tab`} className="supervision-panel">
            {tasks.length === 0
              ? <EmptyState title={t("No supervised tasks")} detail={t("Background tasks will appear here when they start.")} />
              : <div className="supervision-list" aria-label={t("Tasks")}>
                {tasks.map((task) => <TaskCard
                  key={task.id}
                  task={task}
                  now={clock}
                  locale={locale}
                  t={t}
                  onOpenTask={onOpenTask}
                  onStopTask={onStopTask}
                  onRetryTask={onRetryTask}
                  onOpenTrace={onOpenTrace}
                />)}
              </div>}
          </div>
          : <div id={approvalsPanelId} role="tabpanel" aria-labelledby={`${approvalsPanelId}-tab`} className="supervision-panel">
            {approvals.length === 0
              ? <EmptyState title={t("No approvals waiting")} detail={t("Risky actions that require a decision will appear here.")} />
              : <div className="supervision-list" aria-label={t("Approvals")}>
                {approvals.map((approval, index) => <ApprovalCard
                  key={approval.id}
                  approval={approval}
                  fallbackPosition={index + 1}
                  fallbackTotal={approvals.length}
                  locale={locale}
                  t={t}
                  onOpenApproval={onOpenApproval}
                  onResolveApproval={onResolveApproval}
                />)}
              </div>}
          </div>}
      </section>
    </div>}
  </div>;
}

function TaskCard({ task, now, locale, t, onOpenTask, onStopTask, onRetryTask, onOpenTrace }: {
  task: SupervisionTask;
  now: number;
  locale: string;
  t: SupervisionTranslator;
  onOpenTask?: SupervisionCenterProps["onOpenTask"];
  onStopTask?: SupervisionCenterProps["onStopTask"];
  onRetryTask?: SupervisionCenterProps["onRetryTask"];
  onOpenTrace?: SupervisionCenterProps["onOpenTrace"];
}) {
  const presentation = taskStatusPresentation(task.status, t);
  const isActive = ACTIVE_TASK_STATUSES.has(task.status);
  const canStop = task.stoppable !== false && (
    task.status === "accepted" || task.status === "running"
    || task.status === "waiting_approval" || task.status === "recovering"
  );
  const canRetry = task.status === "failed" || task.status === "cancelled";
  const usage = task.usage;

  return <article className="supervision-card task-card" data-status={task.status}>
    <div className="supervision-card-heading">
      <div className="supervision-card-title">
        <StatusDot status={task.status} active={isActive} />
        <div><strong>{task.projectName}</strong><span aria-hidden="true">/</span><span>{task.conversationTitle}</span></div>
      </div>
      <StatusBadge status={task.status} label={presentation.label} />
    </div>
    <div className="supervision-activity">
      <strong>{task.activity || presentation.description}</strong>
      {task.failureMessage && <p>{task.failureMessage}</p>}
    </div>
    <dl className="supervision-metrics">
      <Metric label={t("Elapsed")} value={formatDuration(resolveElapsedMs(task, now))} />
      <Metric label={t("Tokens")} value={formatTokenCount(resolveTokenTotal(usage), locale)} />
      <Metric label={t("Cost")} value={formatCost(usage, locale, t)} />
      <Metric label={t("Mode")} value={task.mode ? task.mode.toUpperCase() : "—"} />
    </dl>
    <div className="supervision-actions">
      {onOpenTask && <ActionButton label={t("Open")} pending={task.actionPending === "open"} onClick={() => onOpenTask(task)} />}
      {canStop && onStopTask && <ActionButton
        label={t(task.status === "stopping" ? "Stopping..." : "Stop")}
        tone="danger"
        disabled={task.status === "stopping"}
        pending={task.actionPending === "stop"}
        onClick={() => onStopTask(task)}
      />}
      {canRetry && onRetryTask && <ActionButton
        label={t("Retry")}
        tone="primary"
        pending={task.actionPending === "retry"}
        onClick={() => onRetryTask(task)}
      />}
      {task.traceAvailable && onOpenTrace && <ActionButton label={t("Open trace")} pending={task.actionPending === "trace"} onClick={() => onOpenTrace(task)} />}
    </div>
  </article>;
}

function ApprovalCard({ approval, fallbackPosition, fallbackTotal, locale, t, onOpenApproval, onResolveApproval }: {
  approval: SupervisionApproval;
  fallbackPosition: number;
  fallbackTotal: number;
  locale: string;
  t: SupervisionTranslator;
  onOpenApproval?: SupervisionCenterProps["onOpenApproval"];
  onResolveApproval?: SupervisionCenterProps["onResolveApproval"];
}) {
  const position = approval.queuePosition ?? fallbackPosition;
  const total = approval.queueTotal ?? fallbackTotal;
  const resolving = approval.resolving !== null && approval.resolving !== undefined;

  return <article className="supervision-card approval-item" data-risk={approval.riskLevel}>
    <div className="supervision-card-heading">
      <div className="supervision-card-title">
        <span className="supervision-risk-icon" aria-hidden="true">!</span>
        <div><strong>{approval.projectName}</strong><span aria-hidden="true">/</span><span>{approval.conversationTitle}</span></div>
      </div>
      <span className={`supervision-risk-badge risk-${approval.riskLevel}`}>{t("{level} risk", { level: t(approval.riskLevel) })}</span>
    </div>
    <div className="supervision-tool-summary"><strong>{approval.toolName}</strong><span>{approval.riskDescription}</span></div>
    <dl className="supervision-approval-context">
      <div><dt>{t("Project workspace")}</dt><dd title={approval.cwd}>{approval.cwd || "—"}</dd></div>
      <div><dt>{t("Queue")}</dt><dd>{t("{position} of {total}", { position, total })}</dd></div>
      <div><dt>{t("Requested")}</dt><dd>{formatTimestamp(approval.requestedAt, locale)}</dd></div>
    </dl>
    {approval.changePreview && <ApprovalChangePreview preview={approval.changePreview} t={t} />}
    {approval.arguments && Object.keys(approval.arguments).length > 0 && <details className="supervision-arguments">
      <summary>{t("View tool arguments")}</summary><pre>{safeJson(approval.arguments, t("Tool arguments could not be serialized"))}</pre>
    </details>}
    <div className="supervision-actions approval-actions">
      {onOpenApproval && <ActionButton label={t("Open conversation")} onClick={() => onOpenApproval(approval)} />}
      <span className="supervision-action-spacer" />
      {onResolveApproval && <>
        <ActionButton
          label={t(approval.resolving === "reject" ? "Rejecting..." : "Reject")}
          tone="secondary"
          disabled={resolving}
          pending={approval.resolving === "reject"}
          onClick={() => onResolveApproval(approval, "reject")}
        />
        <ActionButton
          label={t(approval.resolving === "approve" ? "Allowing..." : "Allow once")}
          tone={approval.riskLevel === "high" ? "critical" : approval.riskLevel === "medium" ? "warning" : "primary"}
          disabled={resolving}
          pending={approval.resolving === "approve"}
          onClick={() => onResolveApproval(approval, "approve")}
        />
      </>}
    </div>
  </article>;
}

function ApprovalChangePreview({ preview, t }: {
  preview: FileChangePreview;
  t: SupervisionTranslator;
}) {
  const canShowDiff = Boolean(preview.diff) && !preview.sensitive;
  return <section className="supervision-change-preview" aria-label={t("Proposed file change")}>
    <div className="supervision-change-summary">
      <span className={`change-operation operation-${preview.operation}`}>
        {t(changeOperationLabel(preview.operation))}
      </span>
      <code title={preview.path}>{preview.path || t("Unknown path")}</code>
      <span className="change-stat" aria-label={t("{additions} additions, {deletions} deletions", {
        additions: preview.additions,
        deletions: preview.deletions,
      })}>+{preview.additions} −{preview.deletions}</span>
    </div>
    {preview.sensitive && <p className="change-warning">
      {t("Diff hidden for a potentially sensitive file")}
    </p>}
    {preview.error && <p className="change-error">{preview.error}</p>}
    {canShowDiff && <details className="supervision-change-diff">
      <summary>{t("View proposed diff")}{preview.truncated && <span>{t("truncated")}</span>}</summary>
      <pre>{preview.diff}</pre>
    </details>}
  </section>;
}

function CountBadge({ tone, count, label }: { tone: "active" | "warning"; count: number; label: string }) {
  return <span className={`supervision-count ${tone}`} aria-label={label}>{count}</span>;
}
function StatusDot({ status, active }: { status: SupervisionTaskStatus; active: boolean }) {
  return <span className={`supervision-status-dot status-${status} ${active ? "active" : ""}`} aria-hidden="true" />;
}
function StatusBadge({ status, label }: { status: SupervisionTaskStatus; label: string }) {
  return <span className={`supervision-status-badge status-${status}`}>{label}</span>;
}
function Metric({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function ActionButton({ label, tone = "secondary", disabled = false, pending = false, onClick }: {
  label: string;
  tone?: "secondary" | "primary" | "danger" | "warning" | "critical";
  disabled?: boolean;
  pending?: boolean;
  onClick: () => void | Promise<void>;
}) {
  return <button
    type="button"
    className={`supervision-action action-${tone}`}
    disabled={disabled || pending}
    aria-busy={pending}
    onClick={() => void onClick()}
  >{pending && <span className="supervision-spinner" aria-hidden="true" />}{label}</button>;
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="supervision-empty"><span aria-hidden="true">✓</span><strong>{title}</strong><p>{detail}</p></div>;
}

function taskStatusPresentation(status: SupervisionTaskStatus, t: SupervisionTranslator) {
  return ({
    accepted: { label: t("Accepted"), description: t("Waiting for the Runtime to start the task") },
    running: { label: t("Running"), description: t("Agent is working") },
    waiting_approval: { label: t("Waiting approval"), description: t("A tool call needs approval") },
    stopping: { label: t("Stopping"), description: t("Waiting for the task to stop safely") },
    recovering: { label: t("Recovering"), description: t("Restoring the task from its checkpoint") },
    finalizing: { label: t("Finalizing"), description: t("Applying and recording the task result") },
    failed: { label: t("Failed"), description: t("Task execution failed") },
    completed: { label: t("Completed"), description: t("Task completed") },
    cancelled: { label: t("Cancelled"), description: t("Task was cancelled") },
  } satisfies Record<SupervisionTaskStatus, { label: string; description: string }>)[status];
}

function changeOperationLabel(operation: FileChangePreview["operation"]): string {
  return ({
    create: "Create file",
    modify: "Modify file",
    delete: "Delete file",
    no_change: "No file change",
    unknown: "File change",
  } satisfies Record<FileChangePreview["operation"], string>)[operation];
}

function resolveElapsedMs(task: SupervisionTask, now: number): number {
  if (typeof task.elapsedMs === "number" && task.elapsedMs >= 0 && !ACTIVE_TASK_STATUSES.has(task.status)) return task.elapsedMs;
  if (task.startedAt !== null && task.startedAt !== undefined) {
    const startedAt = typeof task.startedAt === "number" ? task.startedAt : Date.parse(task.startedAt);
    if (Number.isFinite(startedAt)) return Math.max(0, now - startedAt);
  }
  return Math.max(0, task.elapsedMs ?? 0);
}

function resolveTokenTotal(usage: SupervisionUsage | null | undefined): number | null {
  if (!usage) return null;
  if (typeof usage.totalTokens === "number") return usage.totalTokens;
  const known = [usage.inputTokens, usage.outputTokens, usage.reasoningTokens]
    .filter((value): value is number => typeof value === "number");
  return known.length > 0 ? known.reduce((sum, value) => sum + value, 0) : null;
}

function formatDuration(elapsedMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1_000));
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatTokenCount(value: number | null, locale: string): string {
  if (value === null) return "—";
  return new Intl.NumberFormat(locale, {
    notation: value >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value);
}

function formatCost(usage: SupervisionUsage | null | undefined, locale: string, t: SupervisionTranslator): string {
  if (!usage || usage.estimatedCost === null || usage.estimatedCost === undefined) return "—";
  const currency = usage.currency || "USD";
  let formatted: string;
  try {
    formatted = new Intl.NumberFormat(locale, {
      style: "currency",
      currency,
      minimumFractionDigits: usage.estimatedCost < 0.01 ? 4 : 2,
      maximumFractionDigits: usage.estimatedCost < 0.01 ? 6 : 2,
    }).format(usage.estimatedCost);
  } catch {
    formatted = `${usage.estimatedCost.toFixed(4)} ${currency}`;
  }
  return usage.costEstimated ? t("~{cost}", { cost: formatted }) : formatted;
}

function formatTimestamp(value: number | string | null | undefined, locale: string): string {
  if (value === null || value === undefined) return "—";
  const timestamp = typeof value === "number" ? value : Date.parse(value);
  if (!Number.isFinite(timestamp)) return "—";
  return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(timestamp);
}

function safeJson(value: Record<string, unknown>, fallback: string): ReactNode {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return fallback;
  }
}
