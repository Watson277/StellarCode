import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, ChevronRight, CircleAlert, Clock3, Copy, LoaderCircle } from "lucide-react";
import type { Translator } from "../../i18n";
import { compactText, toolDuration, toolFailure, toolResultSummary, toolTarget, type ToolStatus } from "./toolPresentation";


export function ToolActivityCard({ name, status, detail, elapsed, arguments: args, children, t }: {
  name: string; status: ToolStatus; detail: string; elapsed: string; arguments?: Record<string, unknown>; children?: ReactNode; t: Translator;
}) {
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [expanded, setExpanded] = useState(status === "failed");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  useEffect(() => { if (status === "failed") setExpanded(true); }, [status]);
  const states = {
    waiting_approval: { label: "Waiting for approval", Icon: Clock3 },
    running: { label: "Running", Icon: LoaderCircle },
    completed: { label: "Completed", Icon: Check },
    failed: { label: "Failed", Icon: CircleAlert },
  };
  const { label, Icon } = states[status];
  const failure = status === "failed" ? toolFailure(name, detail, t) : null;
  const target = toolTarget(args);
  const summary = failure?.title ?? toolResultSummary(status, detail, t);
  const hasOutput = status === "completed" || status === "failed";
  async function copy() {
    try {
      await navigator.clipboard.writeText(detail);
      setCopied(true); setCopyFailed(false);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1500);
    } catch { setCopyFailed(true); }
  }
  return <details className={`tool-card tool-${status}`} open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
    <summary aria-label={t("Show tool details for {name}", { name })}>
      <span className="tool-card-header">
        <Icon size={17} className={`tool-state-icon ${status === "running" ? "ui-spin" : ""}`} aria-hidden="true" />
        <span className="tool-card-summary"><strong className="tool-target" title={target || name}>{target ? compactText(target, 120) : name}</strong>
          {target && <span className="tool-name">{name}</span>}
          <span className="tool-detail-preview">{summary}</span>
        </span>
        <span className="tool-result"><span>{t(label)}</span><span className="tool-duration" title={t("Duration")}>{toolDuration(elapsed) || t("Duration unavailable")}</span></span>
        <ChevronRight size={16} className="tool-expand-marker" aria-hidden="true" />
      </span>
    </summary>
    <div className="tool-detail-panel">
      {failure && <div className="tool-failure-summary"><strong>{failure.title}</strong><p>{failure.hint}</p></div>}
      {args && <details className="tool-arguments"><summary>{t("Operation parameters")}</summary><pre>{JSON.stringify(args, null, 2)}</pre></details>}
      {hasOutput && detail ? <>
        <div className="tool-detail-toolbar"><strong>{t("Tool output")}</strong>
          <button type="button" className="secondary-button" onClick={() => void copy()}><Copy size={14} />{t(copied ? "Copied" : "Copy details")}</button>
        </div>
        {copyFailed && <p className="ui-inline-error" role="status">{t("Copy failed. Select the diagnostic text below.")}</p>}
        {failure ? <details className="tool-raw-output"><summary>{t("Technical details")}</summary><pre>{detail}</pre></details> : <pre>{detail}</pre>}
      </> : hasOutput ? <p>{t("No output was returned.")}</p> : null}
      {children}
    </div>
  </details>;
}
