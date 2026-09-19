import { Component, type ErrorInfo, type ReactNode } from "react";
import { Copy, RotateCcw, X } from "lucide-react";
import { translator, type Translator } from "../i18n";

interface Props {
  children: ReactNode;
  t?: Translator;
  resetKey?: string;
  onDismiss?: () => void;
  fullPage?: boolean;
  overlay?: boolean;
}

export class ErrorBoundary extends Component<Props, { error: Error | null; copied: boolean; copyFailed: boolean }> {
  state = { error: null as Error | null, copied: false, copyFailed: false };
  private componentStack = "";

  static getDerivedStateFromError(error: Error) { return { error }; }

  componentDidCatch(_error: Error, info: ErrorInfo) {
    this.componentStack = info.componentStack ?? "";
  }

  componentDidUpdate(previous: Props) {
    if (previous.resetKey !== this.props.resetKey && this.state.error) this.retry();
  }

  retry = () => this.setState({ error: null, copied: false, copyFailed: false });

  copy = async () => {
    try {
      await navigator.clipboard.writeText([
        "StellarCode UI render error", this.state.error?.stack ?? String(this.state.error), this.componentStack,
      ].join("\n"));
      this.setState({ copied: true, copyFailed: false });
    } catch {
      this.setState({ copyFailed: true });
    }
  };

  render() {
    if (!this.state.error) return this.props.children;
    const t = this.props.t ?? translator(navigator.language.startsWith("zh") ? "zh-CN" : "en");
    return <section className={`ui-error-boundary ${this.props.fullPage ? "full-page" : ""} ${this.props.overlay ? "overlay" : ""}`} role="alert">
      <strong>{t("This view could not be displayed.")}</strong>
      <p>{t("Retry displaying this view. This does not resubmit a task or repeat a tool call.")}</p>
      <div className="ui-actions">
        <button type="button" className="secondary-button" onClick={this.retry}><RotateCcw size={16} />{t("Retry display")}</button>
        <button type="button" className="secondary-button" onClick={() => void this.copy()}><Copy size={16} />{t(this.state.copied ? "Copied" : "Copy diagnostics")}</button>
        {this.props.onDismiss && <button type="button" className="secondary-button" onClick={this.props.onDismiss}><X size={16} />{t("Close")}</button>}
      </div>
      {this.state.copyFailed && <p role="status">{t("Copy failed. Select the diagnostic text below.")}</p>}
      <details><summary>{t("Technical details")}</summary><pre>{String(this.state.error)}</pre></details>
    </section>;
  }
}
