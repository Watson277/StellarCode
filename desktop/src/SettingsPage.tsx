/**
 * Settings and management center.
 *
 * Preferences are edited locally, while Memory/Skill/MCP/Browser actions are immediate
 * Runtime operations. Keeping those two models distinct avoids implying that a second
 * "Save" click is required after an operational action has already taken effect.
 */
import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { confirm as confirmDialog, open } from "@tauri-apps/plugin-dialog";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import type {
  AgentMode,
  BrowserProbeSnapshot,
  BrowserSnapshot,
  DiagnosticsSnapshot,
  MemorySnapshot,
  PromptSnapshot,
  McpInstallConfig,
  McpServerInfo,
  McpSnapshot,
  RagSnapshot,
  SkillDiff,
  SkillSnapshot,
  SkillDirectoryResult,
  SkillInstallScope,
} from "./protocol/runtimeEvents";
import { translateDiagnosticRuntimeText, translator } from "./i18n";

export interface AppSettings {
  schema_version: number;
  general: {
    reopen_last_project: boolean;
    language: "zh-CN" | "en";
    send_shortcut: "enter" | "ctrl-enter";
    conversation_font_size: number;
    compact_tools: boolean;
    compact_plans: boolean;
    worktree_directory: string;
  };
  appearance: {
    theme: "dark" | "light";
    accent_color: string;
    background_color: string;
    panel_color: string;
    text_color: string;
    client_font_size: number;
  };
  models: {
    model: string;
    base_url: string;
    vision_model: string;
    vision_base_url: string;
  };
  agent: {
    default_mode: AgentMode;
    max_iterations: number;
    max_parallel_tools: number;
    tool_batch_timeout_seconds: number;
    plan_workers: number;
    team_workers: number;
    team_retries: number;
    context_window: number;
  };
  rag: {
    model: string;
    base_url: string;
    automatic_retrieval: boolean;
  };
  diagnostics: {
    python_path: string;
    lsp_enabled: boolean;
    lsp_command: string;
    lsp_args: string[];
    lsp_timeout_seconds: number;
  };
}

export interface SettingsSnapshot {
  settings: AppSettings;
  settings_path: string;
  env_path: string;
  app_data_path: string;
  image_cache_path: string;
  default_worktree_path: string;
  api_keys: Record<string, boolean>;
}

export const DEFAULT_APP_SETTINGS: AppSettings = {
  schema_version: 2,
  general: {
    reopen_last_project: true,
    language: "zh-CN",
    send_shortcut: "enter",
    conversation_font_size: 12,
    compact_tools: true,
    compact_plans: true,
    worktree_directory: "",
  },
  appearance: {
    theme: "dark",
    accent_color: "#80aaff",
    background_color: "#111318",
    panel_color: "#171a20",
    text_color: "#d9dde7",
    client_font_size: 12,
  },
  models: {
    model: "",
    base_url: "",
    vision_model: "",
    vision_base_url: "",
  },
  agent: {
    default_mode: "react",
    max_iterations: 8,
    max_parallel_tools: 4,
    tool_batch_timeout_seconds: 90,
    plan_workers: 4,
    team_workers: 2,
    team_retries: 2,
    context_window: 200000,
  },
  rag: {
    model: "",
    base_url: "",
    automatic_retrieval: true,
  },
  diagnostics: {
    python_path: "",
    lsp_enabled: false,
    lsp_command: "",
    lsp_args: [],
    lsp_timeout_seconds: 20,
  },
};

const MAX_AGENT_ITERATIONS = 128;

export type SettingsSection =
  | "general"
  | "appearance"
  | "models"
  | "agent"
  | "prompt"
  | "memory"
  | "skills"
  | "mcp"
  | "browser"
  | "rag"
  | "diagnostics";

const SETTINGS_SECTIONS: SettingsSection[] = [
  "general",
  "appearance",
  "models",
  "agent",
  "prompt",
  "memory",
  "skills",
  "mcp",
  "browser",
  "rag",
  "diagnostics",
];

const IMMEDIATE_MANAGEMENT_SECTIONS = new Set<SettingsSection>([
  "memory",
  "skills",
  "mcp",
  "browser",
  "prompt",
]);

export interface SettingsPageProps {
  initialSection?: SettingsSection;
  snapshot: SettingsSnapshot;
  runtimeOnline: boolean;
  runtimePython: string;
  runtimeRestartPending: boolean;
  mcpSnapshot: McpSnapshot;
  ragSnapshot: RagSnapshot;
  memorySnapshot: MemorySnapshot;
  skillSnapshot: SkillSnapshot;
  browserSnapshot: BrowserSnapshot;
  diagnosticsSnapshot: DiagnosticsSnapshot;
  busy: boolean;
  onClose: () => void;
  onSaved: (snapshot: SettingsSnapshot) => void;
  onRestartRuntime: () => Promise<void>;
  onMcpRefresh: () => Promise<McpSnapshot>;
  onMcpInstall: (name: string, config: McpInstallConfig, overwrite: boolean, confirmed: boolean) => Promise<McpSnapshot>;
  onMcpSetEnabled: (name: string, enabled: boolean) => Promise<McpSnapshot>;
  onMcpRestart: (name: string) => Promise<McpSnapshot>;
  onMcpRemove: (name: string) => Promise<McpSnapshot>;
  onMcpLogs: (name: string) => Promise<{ name: string; logs: string }>;
  onRagRefresh: () => Promise<RagSnapshot>;
  onRagAddSources: (paths: string[]) => Promise<RagSnapshot>;
  onRagRemoveSource: (path: string) => Promise<RagSnapshot>;
  onRagIndex: () => Promise<RagSnapshot>;
  onRagClear: (confirmed: boolean) => Promise<RagSnapshot>;
  onMemoryRefresh: () => Promise<MemorySnapshot>;
  onMemorySave: (content: string) => Promise<MemorySnapshot>;
  onMemoryDelete: (id: string) => Promise<MemorySnapshot>;
  onMemoryClear: (confirmed: boolean) => Promise<MemorySnapshot>;
  onPromptRefresh: (includeMemory: boolean) => Promise<PromptSnapshot>;
  onSkillRefresh: () => Promise<SkillSnapshot>;
  onSkillDiff: (name: string) => Promise<SkillDiff>;
  onSkillSetEnabled: (name: string, enabled: boolean) => Promise<SkillSnapshot>;
  onSkillReload: () => Promise<SkillSnapshot>;
  onSkillUpdate: (name: string, currentHash: string, builtinHash: string) => Promise<SkillSnapshot>;
  onSkillKeepCustom: (name: string, currentHash: string, builtinHash: string) => Promise<SkillSnapshot>;
  onSkillRestoreDefault: (name: string, currentHash: string, builtinHash: string) => Promise<SkillSnapshot>;
  onSkillPrepareDirectory: (scope: SkillInstallScope) => Promise<SkillDirectoryResult>;
  onBrowserRefresh: () => Promise<BrowserSnapshot>;
  onBrowserProbe: (port: number) => Promise<BrowserProbeSnapshot>;
  onBrowserConnect: (port?: number) => Promise<BrowserSnapshot>;
  onBrowserDisconnect: () => Promise<BrowserSnapshot>;
  onBrowserTabs: () => Promise<{ output: string }>;
  onDiagnosticsRefresh: () => Promise<DiagnosticsSnapshot>;
  onDiagnosticsRun: (profile?: "safe" | "build") => Promise<DiagnosticsSnapshot>;
  onDiagnosticsCancel: () => Promise<DiagnosticsSnapshot>;
}

export function SettingsPage({
  initialSection = "general",
  snapshot,
  runtimeOnline,
  runtimePython,
  runtimeRestartPending,
  mcpSnapshot,
  ragSnapshot,
  memorySnapshot,
  skillSnapshot,
  browserSnapshot,
  diagnosticsSnapshot,
  busy,
  onClose,
  onSaved,
  onRestartRuntime,
  onMcpRefresh,
  onMcpInstall,
  onMcpSetEnabled,
  onMcpRestart,
  onMcpRemove,
  onMcpLogs,
  onRagRefresh,
  onRagAddSources,
  onRagRemoveSource,
  onRagIndex,
  onRagClear,
  onMemoryRefresh,
  onMemorySave,
  onMemoryDelete,
  onMemoryClear,
  onPromptRefresh,
  onSkillRefresh,
  onSkillDiff,
  onSkillSetEnabled,
  onSkillReload,
  onSkillUpdate,
  onSkillKeepCustom,
  onSkillRestoreDefault,
  onSkillPrepareDirectory,
  onBrowserRefresh,
  onBrowserProbe,
  onBrowserConnect,
  onBrowserDisconnect,
  onBrowserTabs,
  onDiagnosticsRefresh,
  onDiagnosticsRun,
  onDiagnosticsCancel,
}: SettingsPageProps) {
  const [section, setSection] = useState<SettingsSection>(initialSection);
  const [draft, setDraft] = useState<AppSettings>(() => structuredClone(snapshot.settings));
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const restartRequired = runtimeRestartPending || runtimeSettingsChanged(snapshot.settings, draft);
  const t = translator(draft.general.language);

  useEffect(() => {
    setDraft(structuredClone(snapshot.settings));
  }, [snapshot]);

  useEffect(() => {
    setSection(initialSection);
  }, [initialSection]);

  async function save(restart = false) {
    if (saving || busy) return;
    setSaving(true);
    setNotice("");
    try {
      const updated = await invoke<SettingsSnapshot>("settings_update", { settings: draft });
      onSaved(updated);
      setDraft(structuredClone(updated.settings));
      setNotice(t("Settings saved."));
      if (restart && runtimeOnline) await onRestartRuntime();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }

  async function resetSettings() {
    if (
      saving
      || busy
      || !await confirmDialog(
        t("Reset all StellarCode settings to their defaults?"),
        { title: t("Reset settings"), kind: "warning" },
      )
    ) return;
    setSaving(true);
    try {
      const reset = await invoke<SettingsSnapshot>("settings_reset");
      setDraft(structuredClone(reset.settings));
      onSaved(reset);
      setNotice(t("Default settings restored. Restart Runtime to apply model and Agent defaults."));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }

  async function choosePython() {
    const selected = await open({
      directory: false,
      multiple: false,
      title: t("Select Python executable"),
      filters: [{ name: "Python", extensions: ["exe"] }],
    });
    if (typeof selected === "string") {
      setDraft((current) => ({ ...current, diagnostics: { ...current.diagnostics, python_path: selected } }));
    }
  }

  async function chooseLspCommand() {
    const selected = await open({
      directory: false,
      multiple: false,
      title: t("Select Language Server executable"),
      filters: [{ name: "Executable", extensions: ["exe"] }],
    });
    if (typeof selected === "string") {
      setDraft((current) => ({ ...current, diagnostics: { ...current.diagnostics, lsp_command: selected } }));
    }
  }

  function checkModelConfiguration() {
    const available = [["llm", "Text"], ["vision", "Vision"], ["embedding", "Embedding"]]
      .filter(([key]) => snapshot.api_keys[key])
      .map(([, label]) => t(label));
    setNotice(available.length
      ? t("Configured API keys: {categories}.", { categories: available.join(", ") })
      : t("No API key is configured. This is valid only for a keyless local endpoint."));
  }

  const immediateManagement = IMMEDIATE_MANAGEMENT_SECTIONS.has(section);

  return <div className="settings-overlay" role="dialog" aria-modal="true" aria-label={t("Settings & Management")}>
    <div className="settings-window">
      <header className="settings-titlebar"><div><strong>{t("Settings & Management")}</strong><span>{t("StellarCode Desktop")}</span></div><button onClick={onClose} aria-label={t("Close settings")}>x</button></header>
      <div className="settings-layout">
        <nav className="settings-nav">
          {SETTINGS_SECTIONS.map((item) => <button className={section === item ? "active" : ""} onClick={() => setSection(item)} key={item}><span>{settingsIcon(item)}</span>{t(settingsSectionLabel(item))}</button>)}
          <div className="settings-nav-spacer" />
          <small>{t("Protocol v{version}", { version: 1 })}<br />{t("Settings schema v{version}", { version: draft.schema_version })}</small>
        </nav>
        <main className="settings-content">
          {section === "general" && <GeneralSettingsForm draft={draft} setDraft={setDraft} defaultWorktreePath={snapshot.default_worktree_path} />}
          {section === "appearance" && <AppearanceSettingsForm draft={draft} setDraft={setDraft} />}
          {section === "models" && <ModelSettingsForm draft={draft} setDraft={setDraft} apiKeys={snapshot.api_keys} onCheck={checkModelConfiguration} onOpenEnv={() => void revealItemInDir(snapshot.env_path)} />}
          {section === "agent" && <AgentSettingsForm draft={draft} setDraft={setDraft} />}
          {section === "prompt" && <PromptObservabilityForm
            language={draft.general.language}
            runtimeOnline={runtimeOnline}
            onRefresh={onPromptRefresh}
          />}
          {section === "memory" && <MemoryManagementForm
            language={draft.general.language}
            snapshot={memorySnapshot}
            runtimeOnline={runtimeOnline}
            busy={busy}
            onRefresh={onMemoryRefresh}
            onSave={onMemorySave}
            onDelete={onMemoryDelete}
            onClear={onMemoryClear}
          />}
          {section === "skills" && <SkillManagementForm
            language={draft.general.language}
            snapshot={skillSnapshot}
            runtimeOnline={runtimeOnline}
            busy={busy}
            onRefresh={onSkillRefresh}
            onDiff={onSkillDiff}
            onSetEnabled={onSkillSetEnabled}
            onReload={onSkillReload}
            onUpdate={onSkillUpdate}
            onKeepCustom={onSkillKeepCustom}
            onRestoreDefault={onSkillRestoreDefault}
            onPrepareDirectory={onSkillPrepareDirectory}
          />}
          {section === "rag" && <RagSettingsForm
            draft={draft}
            setDraft={setDraft}
            snapshot={ragSnapshot}
            configurationRestartRequired={restartRequired}
            runtimeOnline={runtimeOnline}
            busy={busy}
            onRefresh={onRagRefresh}
            onAddSources={onRagAddSources}
            onRemoveSource={onRagRemoveSource}
            onIndex={onRagIndex}
            onClear={onRagClear}
          />}
          {section === "mcp" && <McpSettingsForm
            language={draft.general.language}
            snapshot={mcpSnapshot}
            runtimeOnline={runtimeOnline}
            busy={busy}
            onRefresh={onMcpRefresh}
            onInstall={onMcpInstall}
            onSetEnabled={onMcpSetEnabled}
            onRestart={onMcpRestart}
            onRemove={onMcpRemove}
            onLogs={onMcpLogs}
          />}
          {section === "browser" && <BrowserManagementForm
            language={draft.general.language}
            snapshot={browserSnapshot}
            runtimeOnline={runtimeOnline}
            busy={busy}
            onRefresh={onBrowserRefresh}
            onProbe={onBrowserProbe}
            onConnect={onBrowserConnect}
            onDisconnect={onBrowserDisconnect}
            onTabs={onBrowserTabs}
          />}
          {section === "diagnostics" && <DiagnosticsSettingsForm
            draft={draft}
            setDraft={setDraft}
            snapshot={snapshot}
            diagnosticsSnapshot={diagnosticsSnapshot}
            runtimeOnline={runtimeOnline}
            runtimePython={runtimePython}
            busy={busy}
            onChoosePython={choosePython}
            onChooseLspCommand={chooseLspCommand}
            onRefresh={onDiagnosticsRefresh}
            onRun={onDiagnosticsRun}
            onCancel={onDiagnosticsCancel}
          />}
        </main>
      </div>
      <footer className="settings-footer">
        {immediateManagement ? <>
          <span className="settings-notice">{t("Changes on this management page apply immediately.")}</span>
          <button className="primary-button" onClick={onClose}>{t("Close")}</button>
        </> : <>
          <span className={`settings-notice ${notice.toLowerCase().includes("missing") || notice.toLowerCase().includes("error") ? "error" : ""}`}>{notice || (restartRequired ? t("Runtime settings changed. Restart required to apply them.") : t("Settings are stored locally on this computer."))}</span>
          <button className="secondary-button" onClick={() => void resetSettings()} disabled={saving || busy}>{t("Reset")}</button>
          {restartRequired && runtimeOnline && <button className="secondary-button" onClick={() => void save(true)} disabled={saving || busy}>{t("Save & Restart Runtime")}</button>}
          <button className="primary-button" onClick={() => void save()} disabled={saving || busy}>{saving ? t("Saving...") : t("Save Settings")}</button>
        </>}
      </footer>
    </div>
  </div>;
}

function GeneralSettingsForm({ draft, setDraft, defaultWorktreePath }: FormProps & { defaultWorktreePath: string }) {
  const general = draft.general;
  const t = translator(general.language);
  const update = (patch: Partial<AppSettings["general"]>) => setDraft((current) => ({ ...current, general: { ...current.general, ...patch } }));
  const chooseWorktreeDirectory = async () => {
    const selected = await open({ directory: true, multiple: false, title: t("Choose temporary worktree folder") });
    if (typeof selected === "string") update({ worktree_directory: selected });
  };
  return <SettingsSectionView title={t("General")} description={t("Conversation appearance and desktop behavior. Worktree storage changes require a Runtime restart.")}>
    <SettingsRow label={t("Language")} description={t("Controls every built-in label, status, dialog, and settings page in the desktop client.")}><select value={general.language} onChange={(event) => update({ language: event.target.value as AppSettings["general"]["language"] })}><option value="zh-CN">{t("Chinese")}</option><option value="en">{t("English")}</option></select></SettingsRow>
    <SettingsRow label={t("Reopen last project")} description={t("Start the most recently used workspace when StellarCode opens.")}><Toggle checked={general.reopen_last_project} onChange={(checked) => update({ reopen_last_project: checked })} /></SettingsRow>
    <SettingsRow label={t("Send message")} description={t("Choose whether Enter sends or inserts a new line.")}><select value={general.send_shortcut} onChange={(event) => update({ send_shortcut: event.target.value as AppSettings["general"]["send_shortcut"] })}><option value="enter">{t("Enter")}</option><option value="ctrl-enter">{t("Ctrl+Enter")}</option></select></SettingsRow>
    <SettingsRow label={t("Conversation font")} description={t("Controls message, Markdown, composer, and plan text size.")}><div className="range-control"><input type="range" min="10" max="16" value={general.conversation_font_size} onChange={(event) => update({ conversation_font_size: Number(event.target.value) })} /><strong>{general.conversation_font_size}px</strong></div></SettingsRow>
    <SettingsRow label={t("Compact tool activity")} description={t("Reduce padding around tool calls and progress messages.")}><Toggle checked={general.compact_tools} onChange={(checked) => update({ compact_tools: checked })} /></SettingsRow>
    <SettingsRow label={t("Compact plans")} description={t("Keep Plan steps dense while retaining live status and dependencies.")}><Toggle checked={general.compact_plans} onChange={(checked) => update({ compact_plans: checked })} /></SettingsRow>
    <SettingsRow label={t("Temporary worktree location")} description={t("Leave empty to use the default C drive application data directory. New tasks use this location after Runtime restarts; existing task data is not moved.")}>
      <div className="worktree-location-control">
        <input value={general.worktree_directory} onChange={(event) => update({ worktree_directory: event.target.value })} placeholder={defaultWorktreePath} aria-label={t("Temporary worktree location")} />
        <div><button className="secondary-button" type="button" onClick={() => void chooseWorktreeDirectory()}>{t("Browse")}</button><button className="secondary-button" type="button" onClick={() => update({ worktree_directory: "" })} disabled={!general.worktree_directory}>{t("Use default")}</button></div>
      </div>
    </SettingsRow>
  </SettingsSectionView>;
}

function AppearanceSettingsForm({ draft, setDraft }: FormProps) {
  const appearance = draft.appearance;
  const t = translator(draft.general.language);
  const update = (patch: Partial<AppSettings["appearance"]>) => setDraft((current) => ({ ...current, appearance: { ...current.appearance, ...patch } }));
  const changeTheme = (theme: AppSettings["appearance"]["theme"]) => {
    update({ theme, ...THEME_PALETTES[theme] });
  };
  const resetColors = () => update(THEME_PALETTES[appearance.theme]);
  return <SettingsSectionView title={t("Appearance")} description={t("Dark and light are fixed color presets. Custom colors apply after saving without restarting Runtime.")}>
    <div className="theme-choice" role="group" aria-label={t("Color theme")}>
      <button className={appearance.theme === "dark" ? "active" : ""} onClick={() => changeTheme("dark")}><span className="theme-preview preview-dark"><i /><i /><i /></span><strong>{t("Dark")}</strong><small>{t("Low-light workspace")}</small></button>
      <button className={appearance.theme === "light" ? "active" : ""} onClick={() => changeTheme("light")}><span className="theme-preview preview-light"><i /><i /><i /></span><strong>{t("Light")}</strong><small>{t("Bright workspace")}</small></button>
    </div>
    <SettingsRow label={t("Theme color")} description={t("Used for buttons, selections, progress, and active indicators.")}><ColorControl value={appearance.accent_color} onChange={(accent_color) => update({ accent_color })} /></SettingsRow>
    <SettingsRow label={t("Background color")} description={t("Base color behind the conversation and main workspace.")}><ColorControl value={appearance.background_color} onChange={(background_color) => update({ background_color })} /></SettingsRow>
    <SettingsRow label={t("Panel color")} description={t("Controls sidebars, top and bottom bars, composer, selected rows, and raised surfaces.")}><ColorControl value={appearance.panel_color} onChange={(panel_color) => update({ panel_color })} /></SettingsRow>
    <SettingsRow label={t("Font color")} description={t("Base color for every client label and message. Secondary and placeholder text is dimmed automatically.")}><ColorControl value={appearance.text_color} onChange={(text_color) => update({ text_color })} /></SettingsRow>
    <SettingsRow label={t("Client font size")} description={t("Scales all text and controls in the desktop client.")}><div className="range-control"><input type="range" min="10" max="16" value={appearance.client_font_size} onChange={(event) => update({ client_font_size: Number(event.target.value) })} /><strong>{appearance.client_font_size}px</strong></div></SettingsRow>
    <div className="settings-inline-actions"><button className="secondary-button" onClick={resetColors}>{t("Restore theme colors")}</button><small>{t("Conversation font size can still be adjusted independently under General.")}</small></div>
  </SettingsSectionView>;
}

function ModelSettingsForm({ draft, setDraft, apiKeys, onCheck, onOpenEnv }: FormProps & { apiKeys: Record<string, boolean>; onCheck: () => void; onOpenEnv: () => void }) {
  const models = draft.models;
  const t = translator(draft.general.language);
  const update = (patch: Partial<AppSettings["models"]>) => setDraft((current) => ({ ...current, models: { ...current.models, ...patch } }));
  return <SettingsSectionView title={t("Models")} description={t("Select text and vision routing. Secrets continue to load from .env or system environment variables.")}>
    <div className="settings-key-strip">{[["llm", "Text"], ["vision", "Vision"], ["embedding", "Embedding"]].map(([key, label]) => <span className={apiKeys[key] ? "configured" : "unset"} key={key}><i />{t(apiKeys[key] ? "{category} key configured" : "{category} key not set", { category: t(label) })}</span>)}</div>
    <SettingsRow label={t("Text model")} description={t("OpenAI-compatible model name. Leave empty to read LLM_MODEL_NAME from .env.")}><input value={models.model} onChange={(event) => update({ model: event.target.value })} placeholder="LLM_MODEL_NAME" /></SettingsRow>
    <SettingsRow label={t("Base URL")} description={t("OpenAI-compatible base URL. Leave empty to read LLM_BASE_URL from .env.")}><input value={models.base_url} onChange={(event) => update({ base_url: event.target.value })} placeholder="https://example.com/v1" /></SettingsRow>
    <SettingsRow label={t("Vision model")} description={t("Optional vision model name. Leave both vision fields empty to disable image routing.")}><input value={models.vision_model} onChange={(event) => update({ vision_model: event.target.value })} placeholder="VISION_MODEL_NAME" /></SettingsRow>
    <SettingsRow label={t("Vision base URL")} description={t("OpenAI-compatible vision endpoint. Leave empty to read VISION_BASE_URL from .env.")}><input value={models.vision_base_url} onChange={(event) => update({ vision_base_url: event.target.value })} placeholder="https://example.com/v1" /></SettingsRow>
    <div className="settings-inline-actions"><button className="secondary-button" onClick={onCheck}>{t("Check configuration")}</button><button className="secondary-button" onClick={onOpenEnv}>{t("Show .env")}</button><small>{t("API keys are never copied into settings.json.")}</small></div>
  </SettingsSectionView>;
}

function AgentSettingsForm({ draft, setDraft }: FormProps) {
  const agent = draft.agent;
  const t = translator(draft.general.language);
  const update = (patch: Partial<AppSettings["agent"]>) => setDraft((current) => ({ ...current, agent: { ...current.agent, ...patch } }));
  return <SettingsSectionView title={t("Agent")} description={t("Execution budgets and concurrency. Changes apply after Runtime restarts.")}>
    <SettingsRow label={t("Default conversation mode")} description={t("Used for newly created conversations; existing conversations keep their own mode.")}><select value={agent.default_mode} onChange={(event) => update({ default_mode: event.target.value as AgentMode })}><option value="react">ReAct</option><option value="plan">{t("Plan")}</option><option value="team">{t("Team")}</option></select></SettingsRow>
    <NumberSetting label={t("Maximum iterations")} description={t("Maximum LLM tool-call rounds per ReAct Agent.")} value={agent.max_iterations} min={1} max={MAX_AGENT_ITERATIONS} onChange={(value) => update({ max_iterations: value })} />
    <NumberSetting label={t("Context window")} description={t("Model context capacity used for preflight compression. Set this to the selected model's limit.")} value={agent.context_window} min={16000} max={2000000} onChange={(value) => update({ context_window: value })} suffix={t("tokens")} />
    <NumberSetting label={t("Parallel tools")} description={t("Maximum independent tool calls in one batch.")} value={agent.max_parallel_tools} min={1} max={16} onChange={(value) => update({ max_parallel_tools: value })} />
    <NumberSetting label={t("Tool batch timeout")} description={t("Timeout for a parallel tool batch, in seconds.")} value={agent.tool_batch_timeout_seconds} min={5} max={600} onChange={(value) => update({ tool_batch_timeout_seconds: value })} suffix={t("seconds")} />
    <NumberSetting label={t("Plan workers")} description={t("Maximum independent DAG steps executed concurrently.")} value={agent.plan_workers} min={1} max={16} onChange={(value) => update({ plan_workers: value })} />
    <NumberSetting label={t("Team workers")} description={t("Worker agents available in Team mode.")} value={agent.team_workers} min={1} max={8} onChange={(value) => update({ team_workers: value })} />
    <NumberSetting label={t("Team retries")} description={t("Maximum reviewer-requested retries per Team step.")} value={agent.team_retries} min={0} max={10} onChange={(value) => update({ team_retries: value })} />
  </SettingsSectionView>;
}

interface PromptObservabilityFormProps {
  language: AppSettings["general"]["language"];
  runtimeOnline: boolean;
  onRefresh: (includeMemory: boolean) => Promise<PromptSnapshot>;
}

function PromptObservabilityForm({ language, runtimeOnline, onRefresh }: PromptObservabilityFormProps) {
  const t = translator(language);
  const [snapshot, setSnapshot] = useState<PromptSnapshot | null>(null);
  const [includeMemory, setIncludeMemory] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load(showSensitive: boolean): Promise<boolean> {
    if (!runtimeOnline || loading) return false;
    setLoading(true);
    setError("");
    try {
      setSnapshot(await onRefresh(showSensitive));
      return true;
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : String(loadError));
      return false;
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    setIncludeMemory(false);
    void load(false);
    // This form is mounted only while the Prompt page is visible. Reloading here
    // guarantees every visit starts from a backend-redacted snapshot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runtimeOnline]);

  async function changeMemoryVisibility(checked: boolean) {
    if (!checked) {
      setIncludeMemory(false);
      // Remove the sensitive response from component state before the redacted
      // replacement completes, so switching off never leaves visible stale data.
      setSnapshot(null);
      await load(false);
      return;
    }
    setIncludeMemory(await load(true));
  }

  return <SettingsSectionView
    title={t("Prompt observability")}
    description={t("Inspect the exact layered Prompt used by the current conversation. Token counts are preflight estimates; provider usage remains authoritative.")}
  >
    <div className="prompt-observability-toolbar">
      <div>
        <strong>{t("Current assembled Prompt")}</strong>
        <small>{snapshot ? t("Generated {time}", { time: new Date(snapshot.generated_at).toLocaleString(language) }) : t("No Prompt snapshot loaded.")}</small>
      </div>
      <button className="secondary-button" onClick={() => void load(includeMemory)} disabled={!runtimeOnline || loading}>{t(loading ? "Loading..." : "Refresh")}</button>
    </div>

    {!runtimeOnline && <div className="management-empty">{t("Open a project and wait for Python Runtime first.")}</div>}
    {error && <p className="management-error">{error}</p>}
    {snapshot && <>
      <div className="prompt-metrics-grid">
        <div><span>{t("Prompt version")}</span><strong>{snapshot.version}</strong></div>
        <div><span>{t("Mode")}</span><strong>{snapshot.mode}</strong></div>
        <div><span>{t("Characters")}</span><strong>{snapshot.total.char_count.toLocaleString(language)}</strong></div>
        <div><span>{t("Estimated tokens")}</span><strong>{snapshot.total.estimated_tokens.toLocaleString(language)}</strong></div>
        <div className="wide"><span>SHA-256</span><code title={snapshot.total.sha256}>{snapshot.total.sha256}</code></div>
      </div>

      <SettingsRow
        label={t("Show Memory sensitive content")}
        description={t("Off by default. When enabled, retrieved Memory and compressed conversation summaries are returned by Runtime and displayed locally until this page closes.")}
      >
        <Toggle checked={includeMemory} onChange={(checked) => void changeMemoryVisibility(checked)} disabled={loading} />
      </SettingsRow>

      <section className="prompt-layer-panel">
        <header><div><h3>{t("Prompt layers")}</h3><p>{t("Each layer records its role, size, estimated tokens, and content hash.")}</p></div><span>{t("{count} layers", { count: snapshot.layers.length })}</span></header>
        <div className="prompt-layer-list">
          {snapshot.layers.map((layer) => <article key={`${layer.role}-${layer.name}`}>
            <div><strong>{layer.name}</strong><small>{layer.role}{layer.sensitive ? ` · ${t("sensitive")}` : ""}</small></div>
            <span>{layer.char_count.toLocaleString(language)} {t("chars")}</span>
            <span>{layer.estimated_tokens.toLocaleString(language)} {t("tokens")}</span>
            <code title={layer.sha256}>{layer.sha256.slice(0, 12)}</code>
          </article>)}
        </div>
      </section>

      <section className="prompt-preview-panel">
        <header><div><h3>{t("Assembled preview")}</h3><p>{snapshot.memory_hidden ? t("Memory and summary bodies are hidden by Runtime.") : t("Sensitive Memory and summary bodies are currently visible.")}</p></div></header>
        <pre>{snapshot.assembled_preview}</pre>
      </section>
    </>}
  </SettingsSectionView>;
}

interface RagSettingsFormProps extends FormProps {
  snapshot: RagSnapshot;
  runtimeOnline: boolean;
  busy: boolean;
  configurationRestartRequired: boolean;
  onRefresh: () => Promise<RagSnapshot>;
  onAddSources: (paths: string[]) => Promise<RagSnapshot>;
  onRemoveSource: (path: string) => Promise<RagSnapshot>;
  onIndex: () => Promise<RagSnapshot>;
  onClear: (confirmed: boolean) => Promise<RagSnapshot>;
}

function RagSettingsForm({ draft, setDraft, snapshot, runtimeOnline, busy, configurationRestartRequired, onRefresh, onAddSources, onRemoveSource, onIndex, onClear }: RagSettingsFormProps) {
  const [working, setWorking] = useState("");
  const [notice, setNotice] = useState("");
  const rag = draft.rag;
  const t = translator(draft.general.language);
  const indexing = snapshot.status === "indexing";
  const controlsDisabled = busy || Boolean(working) || !runtimeOnline || indexing;
  const indexState = ragIndexState(snapshot, t);
  const update = (patch: Partial<AppSettings["rag"]>) => setDraft((current) => ({
    ...current,
    rag: { ...current.rag, ...patch },
  }));

  async function run(label: string, action: () => Promise<unknown>) {
    if (controlsDisabled) return;
    setWorking(label);
    setNotice("");
    try {
      await action();
      setNotice(t("{label} completed.", { label: translateActionLabel(label, t) }));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setWorking("");
    }
  }

  async function chooseSources(directory: boolean) {
    const selected = await open({
      directory,
      multiple: true,
      title: t(directory ? "Add folders to the RAG index" : "Add files to the RAG index"),
    });
    const paths = typeof selected === "string" ? [selected] : selected ?? [];
    if (paths.length > 0) await run(directory ? "Add folders" : "Add files", () => onAddSources(paths));
  }

  async function clearIndex() {
    const confirmed = await confirmDialog(
      t("Clear the generated RAG index for this project? The source list will be kept so it can be rebuilt."),
      { title: t("Clear RAG index"), kind: "warning" },
    );
    if (confirmed) await run("Clear index", () => onClear(true));
  }

  async function startIndex() {
    if (controlsDisabled || configurationRestartRequired || snapshot.sources.length === 0) return;
    setWorking("Start index");
    setNotice("");
    try {
      await onIndex();
      setNotice(t("Index job started. Progress will update below."));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setWorking("");
    }
  }

  return <SettingsSectionView title={t("Code RAG")} description={t("Select project knowledge sources, build a semantic code index, and choose the Embedding model used by search_code.")}>
    <SettingsRow label={t("Embedding model")} description={t("Leave empty with no base URL to use local deterministic hashing.")}><input value={rag.model} onChange={(event) => update({ model: event.target.value })} placeholder="EMBEDDING_MODEL_NAME" /></SettingsRow>
    <SettingsRow label={t("Embedding base URL")} description={t("OpenAI-compatible embedding endpoint. Leave empty to use local hashing.")}><input value={rag.base_url} onChange={(event) => update({ base_url: event.target.value })} placeholder="https://example.com/v1" /></SettingsRow>
    <SettingsRow label={t("Automatic retrieval")} description={t("Allow the Agent to call search_code proactively for architecture, behavior, and symbol-location questions. When off, it only uses RAG when you explicitly request it.")}><Toggle checked={rag.automatic_retrieval} onChange={(checked) => update({ automatic_retrieval: checked })} /></SettingsRow>

    <div className="rag-runtime-summary">
      <div><span>{t("Index status")}</span><strong><span className={`rag-status-badge ${indexState.tone}`}>{indexState.label}</span></strong></div>
      <div><span>{t("Runtime model")}</span><strong>{snapshot.embedding_model}</strong></div>
      <div><span>{t("Indexed")}</span><strong>{t("{files} files / {chunks} chunks", { files: snapshot.indexed_file_count, chunks: snapshot.chunk_count })}</strong></div>
      <div><span>{t("Relations")}</span><strong>{snapshot.relation_count}</strong></div>
      <div><span>{t("Last rebuild")}</span><strong>{snapshot.last_indexed_at ? new Date(snapshot.last_indexed_at).toLocaleString(draft.general.language) : t("Never")}</strong></div>
    </div>

    <section className="rag-sources">
      <header><div><h3>{t("Index sources")}</h3><p>{t("All selected files and folders are de-duplicated and rebuilt into this project's own index.")}</p></div><div>
        <button className="secondary-button" onClick={() => void chooseSources(false)} disabled={controlsDisabled}>{t("Add files")}</button>
        <button className="secondary-button" onClick={() => void chooseSources(true)} disabled={controlsDisabled}>{t("Add folders")}</button>
        {snapshot.workspace && !snapshot.sources.some((source) => source.path.toLowerCase() === snapshot.workspace.toLowerCase()) && <button className="secondary-button" onClick={() => void run("Add workspace", () => onAddSources([snapshot.workspace]))} disabled={controlsDisabled}>{t("Add workspace")}</button>}
      </div></header>
      <div className="rag-source-list">
        {snapshot.sources.length === 0 && <div className="rag-empty">{t("No sources selected. Add files, folders, or the full workspace.")}</div>}
        {snapshot.sources.map((source) => <div className="rag-source-row" key={source.path}><span className="rag-source-kind">{t(source.kind === "directory" ? "Directory" : "File")}</span><code title={source.path}>{source.path}</code><span className={`rag-source-status ${indexState.tone}`} title={indexState.detail}>{indexState.label}</span><button className="secondary-button" onClick={() => void run("Remove source", () => onRemoveSource(source.path))} disabled={controlsDisabled}>{t("Remove")}</button></div>)}
      </div>
    </section>

    <div className="rag-index-actions">
      <button className="primary-button" onClick={() => void startIndex()} disabled={controlsDisabled || configurationRestartRequired || snapshot.sources.length === 0}>{t(indexing ? "Indexing..." : working === "Start index" ? "Starting..." : snapshot.chunk_count > 0 ? "Rebuild index" : "Build index")}</button>
      <button className="secondary-button" onClick={() => void run("Refresh RAG", onRefresh)} disabled={controlsDisabled}>{t("Refresh")}</button>
      <button className="secondary-button danger-button" onClick={() => void clearIndex()} disabled={controlsDisabled || snapshot.chunk_count === 0}>{t("Clear index")}</button>
      {snapshot.needs_rebuild && <span className="rag-rebuild-warning">{t("Source/model changes require a rebuild.")}</span>}
    </div>
    {configurationRestartRequired && <p className="rag-rebuild-warning">{t("Save settings and restart Runtime before building with the selected Embedding configuration.")}</p>}
    {(indexing || snapshot.progress) && <p className="rag-progress"><i />{snapshot.progress || t("Building semantic index...")}</p>}
    {(snapshot.error || notice) && <p className={`mcp-notice ${snapshot.error ? "error" : ""}`}>{snapshot.error || notice}</p>}
    <PathRow label={t("RAG database")} path={snapshot.storage_path} t={t} />
  </SettingsSectionView>;
}

function ragIndexState(snapshot: RagSnapshot, t: ReturnType<typeof translator>): { label: string; tone: "ready" | "working" | "warning" | "error" | "muted"; detail: string } {
  if (snapshot.status === "indexing") return { label: t("Indexing..."), tone: "working", detail: snapshot.progress || t("Building semantic index...") };
  if (snapshot.status === "error" || ((snapshot.last_result?.error_count ?? 0) > 0 && snapshot.chunk_count === 0)) return { label: t("Index failed"), tone: "error", detail: snapshot.error || snapshot.last_result?.message || t("No code chunks were created.") };
  if ((snapshot.last_result?.error_count ?? 0) > 0) return { label: t("Indexed with errors"), tone: "warning", detail: snapshot.last_result?.message || t("Some selected files could not be indexed.") };
  if (snapshot.needs_rebuild) return { label: t("Needs rebuild"), tone: "warning", detail: t("Source/model changes require a rebuild.") };
  if (snapshot.chunk_count > 0) return { label: t("Search ready"), tone: "ready", detail: snapshot.last_result?.message || t("The current sources are available to search_code.") };
  return { label: t("Not indexed"), tone: "muted", detail: t("Add a source and build the index.") };
}

interface McpSettingsFormProps {
  language: AppSettings["general"]["language"];
  snapshot: McpSnapshot;
  runtimeOnline: boolean;
  busy: boolean;
  onRefresh: () => Promise<McpSnapshot>;
  onInstall: (name: string, config: McpInstallConfig, overwrite: boolean, confirmed: boolean) => Promise<McpSnapshot>;
  onSetEnabled: (name: string, enabled: boolean) => Promise<McpSnapshot>;
  onRestart: (name: string) => Promise<McpSnapshot>;
  onRemove: (name: string) => Promise<McpSnapshot>;
  onLogs: (name: string) => Promise<{ name: string; logs: string }>;
}

function McpSettingsForm({ language, snapshot, runtimeOnline, busy, onRefresh, onInstall, onSetEnabled, onRestart, onRemove, onLogs }: McpSettingsFormProps) {
  const t = translator(language);
  const [transport, setTransport] = useState<"stdio" | "http">("stdio");
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [variables, setVariables] = useState("{}");
  const [working, setWorking] = useState("");
  const [notice, setNotice] = useState("");
  const [selectedLogs, setSelectedLogs] = useState<{ name: string; logs: string } | null>(null);
  const controlsDisabled = busy || Boolean(working) || !runtimeOnline;
  const noticeIsError = Boolean(notice && !notice.endsWith("completed."));

  async function run(label: string, action: () => Promise<unknown>) {
    if (controlsDisabled) return;
    setWorking(label);
    setNotice("");
    try {
      await action();
      setNotice(t("{label} completed.", { label: translateActionLabel(label, t) }));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setWorking("");
    }
  }

  async function installServer() {
    const serverName = name.trim();
    if (!serverName) {
      setNotice(t("Server name is required."));
      return;
    }
    let keyValues: Record<string, string>;
    try {
      keyValues = parseStringMap(variables, t(transport === "stdio" ? "Environment" : "Headers"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
      return;
    }
    const config: McpInstallConfig = transport === "stdio"
      ? {
        command: command.trim(),
        args: args.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
        env: keyValues,
      }
      : { url: url.trim(), headers: keyValues };
    if ((transport === "stdio" && !config.command) || (transport === "http" && !config.url)) {
      setNotice(t(transport === "stdio" ? "Command is required." : "HTTP URL is required."));
      return;
    }
    const existing = snapshot.servers.find((server) => server.name === serverName);
    const overwrite = Boolean(existing);
    if (existing && !await confirmDialog(
      t('Replace the project MCP configuration for "{name}"?', { name: serverName }),
      { title: t("Replace MCP server"), kind: "warning" },
    )) return;
    const confirmed = transport !== "stdio" || await confirmDialog(
      t("Start this local MCP command?\n\n{command}\n\nThe process runs with your Windows account permissions.", { command: `${config.command} ${(config.args ?? []).join(" ")}` }),
      { title: t("Start local MCP server"), kind: "warning" },
    );
    if (!confirmed) return;
    await run(`Install ${serverName}`, async () => {
      await onInstall(serverName, config, overwrite, confirmed);
      setName("");
      setCommand("");
      setArgs("");
      setUrl("");
      setVariables("{}");
    });
  }

  async function showLogs(server: McpServerInfo) {
    await run(`Load ${server.name} logs`, async () => {
      setSelectedLogs(await onLogs(server.name));
    });
  }

  async function removeServer(server: McpServerInfo) {
    if (!await confirmDialog(
      t('Remove project MCP server "{name}"?', { name: server.name }),
      { title: t("Remove MCP server"), kind: "warning" },
    )) return;
    await run(`Remove ${server.name}`, () => onRemove(server.name));
  }

  return <SettingsSectionView title={t("MCP Servers")} description={t("Connect project-scoped stdio or Streamable HTTP servers. Ready tools are registered as mcp__server__tool and use the same approval and audit path as built-in tools.")}>
    <div className="mcp-summary">
      <span><strong>{snapshot.ready_servers}</strong> {t("ready")}</span>
      <span><strong>{snapshot.total_servers}</strong> {t("servers")}</span>
      <span><strong>{snapshot.total_tools}</strong> {t("tools")}</span>
      <button className="secondary-button" onClick={() => void run("Refresh MCP", onRefresh)} disabled={controlsDisabled}>{t(working === "Refresh MCP" ? "Refreshing..." : "Refresh")}</button>
    </div>

    <div className="mcp-server-list">
      {snapshot.servers.length === 0 && <div className="mcp-empty">{t("No MCP servers are configured for this project or user.")}</div>}
      {snapshot.servers.map((server) => <article className="mcp-server-card" key={server.name}>
        <header>
          <span className={`mcp-status ${server.status}`} aria-label={server.status} />
          <div><strong>{server.name}</strong><small>{t("{transport} · {source} · {count} tools", { transport: server.transport, source: t(server.source), count: server.tool_count })}</small></div>
          <span className={`mcp-state-label ${server.status}`}>{t(server.status)}</span>
        </header>
        <code className="mcp-endpoint">{server.transport === "stdio" ? [server.command, ...server.args].join(" ") : server.url}</code>
        {(server.env_keys.length > 0 || server.header_keys.length > 0) && <p className="mcp-key-note">{t("Configured names: {names} (values hidden)", { names: [...server.env_keys, ...server.header_keys].join(", ") })}</p>}
        {server.error && <p className="mcp-error">{server.error}</p>}
        <div className="mcp-card-actions">
          <button className="secondary-button" onClick={() => void run(`${server.disabled ? "Enable" : "Disable"} ${server.name}`, () => onSetEnabled(server.name, server.disabled))} disabled={controlsDisabled}>{t(server.disabled ? "Enable" : "Disable")}</button>
          <button className="secondary-button" onClick={() => void run(`Restart ${server.name}`, () => onRestart(server.name))} disabled={controlsDisabled || server.disabled}>{t("Restart")}</button>
          <button className="secondary-button" onClick={() => void showLogs(server)} disabled={controlsDisabled}>{t("Logs")}</button>
          {server.source === "project" && <button className="secondary-button danger-button" onClick={() => void removeServer(server)} disabled={controlsDisabled}>{t("Remove")}</button>}
        </div>
        <details className="mcp-tools">
          <summary>{server.tools.length > 0 ? t("View {count} available tools", { count: server.tools.length }) : t("No tools available")}</summary>
          <div>{server.tools.map((tool) => <article key={tool.namespaced_name}><code>{tool.namespaced_name}</code><p>{tool.description || t("No description supplied by server.")}</p><details><summary>{t("Input schema")}</summary><pre>{JSON.stringify(tool.input_schema, null, 2)}</pre></details></article>)}</div>
        </details>
      </article>)}
    </div>

    {selectedLogs && <div className="mcp-log-view"><header><strong>{t("{name} logs", { name: selectedLogs.name })}</strong><button onClick={() => setSelectedLogs(null)}>x</button></header><pre>{selectedLogs.logs || t("No stderr output recorded.")}</pre></div>}

    <section className="mcp-installer">
      <header><h3>{t("Add custom server")}</h3><p>{t("This writes to the active project's .stellarcode/mcp.json and starts the server. It does not run a package installer separately.")}</p></header>
      <div className="mcp-install-grid">
        <label><span>{t("Server name")}</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="my-server" /></label>
        <label><span>{t("Transport")}</span><select value={transport} onChange={(event) => { setTransport(event.target.value as "stdio" | "http"); setVariables("{}"); }}><option value="stdio">{t("Local command (stdio)")}</option><option value="http">{t("Streamable HTTP")}</option></select></label>
        {transport === "stdio" ? <>
          <label className="wide"><span>{t("Command")}</span><input value={command} onChange={(event) => setCommand(event.target.value)} placeholder="npx" /></label>
          <label className="wide"><span>{t("Arguments (one per line)")}</span><textarea value={args} onChange={(event) => setArgs(event.target.value)} placeholder={"-y\n@modelcontextprotocol/server-filesystem\n${PROJECT_DIR}"} rows={4} /></label>
          <label className="wide"><span>{t("Environment JSON")}</span><textarea value={variables} onChange={(event) => setVariables(event.target.value)} placeholder={'{"API_TOKEN":"${MY_MCP_TOKEN}"}'} rows={3} /></label>
        </> : <>
          <label className="wide"><span>{t("Server URL")}</span><input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com/mcp" /></label>
          <label className="wide"><span>{t("Headers JSON")}</span><textarea value={variables} onChange={(event) => setVariables(event.target.value)} placeholder={'{"Authorization":"Bearer ${MY_MCP_TOKEN}"}'} rows={3} /></label>
        </>}
      </div>
      <div className="mcp-install-footer"><small>{t("Use ${VARIABLE} for secrets. Values resolve from system variables, project .env, or user .env and are never returned to the UI.")}</small><button className="primary-button" onClick={() => void installServer()} disabled={controlsDisabled}>{t(working.startsWith("Install ") ? "Installing..." : "Add & Start")}</button></div>
    </section>

    <div className="mcp-config-paths">
      <PathRow label={t("Project MCP config")} path={snapshot.project_config_path} t={t} />
      <PathRow label={t("User MCP config")} path={snapshot.user_config_path} t={t} />
    </div>
    <p className={`mcp-notice ${noticeIsError ? "error" : ""}`}>{notice || t(!runtimeOnline ? "Python Runtime must be online to manage MCP servers." : "MCP changes apply immediately and do not require saving desktop settings.")}</p>
  </SettingsSectionView>;
}

export interface MemoryManagementFormProps {
  language: AppSettings["general"]["language"];
  snapshot: MemorySnapshot;
  runtimeOnline: boolean;
  busy: boolean;
  onRefresh: () => Promise<MemorySnapshot>;
  onSave: (content: string) => Promise<MemorySnapshot>;
  onDelete: (id: string) => Promise<MemorySnapshot>;
  onClear: (confirmed: boolean) => Promise<MemorySnapshot>;
}

export function MemoryManagementForm({ language, snapshot, runtimeOnline, busy, onRefresh, onSave, onDelete, onClear }: MemoryManagementFormProps) {
  const t = translator(language);
  const [fact, setFact] = useState("");
  const [filter, setFilter] = useState("");
  const [working, setWorking] = useState("");
  const [notice, setNotice] = useState("");
  const [noticeIsError, setNoticeIsError] = useState(false);
  const controlsDisabled = busy || Boolean(working) || !runtimeOnline;
  const normalizedFilter = filter.trim().toLocaleLowerCase(language);
  const entries = normalizedFilter
    ? snapshot.entries.filter((entry) => (
      entry.content.toLocaleLowerCase(language).includes(normalizedFilter)
      || entry.type.toLocaleLowerCase(language).includes(normalizedFilter)
    ))
    : snapshot.entries;

  async function run(label: string, action: () => Promise<unknown>) {
    if (controlsDisabled) return;
    setWorking(label);
    setNotice("");
    setNoticeIsError(false);
    try {
      await action();
      setNotice(t("{label} completed.", { label: translateActionLabel(label, t) }));
    } catch (error) {
      setNoticeIsError(true);
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setWorking("");
    }
  }

  async function saveFact() {
    const content = fact.trim();
    if (!content || controlsDisabled) return;
    await run("Save memory", async () => {
      await onSave(content);
      setFact("");
    });
  }

  async function deleteMemory(id: string, content: string) {
    if (!await confirmDialog(
      t("Delete this project memory?\n\n{content}", { content: abbreviateText(content, 180) }),
      { title: t("Delete memory"), kind: "warning" },
    )) return;
    await run("Delete memory", () => onDelete(id));
  }

  async function clearMemory() {
    if (!await confirmDialog(
      t("Clear all long-term memory for this project? This cannot be undone."),
      { title: t("Clear project memory"), kind: "warning" },
    )) return;
    await run("Clear memory", () => onClear(true));
  }

  return <SettingsSectionView title={t("Memory")} description={t("Review and manage the active project's shared long-term facts. Conversation history and compressed context are managed separately per conversation.")}>
    <div className="management-summary">
      <div><span>{t("Scope")}</span><strong>{t("Project")}</strong></div>
      <div><span>{t("Saved facts")}</span><strong>{snapshot.count}</strong></div>
      <div><span>{t("Estimated tokens")}</span><strong>{snapshot.token_count.toLocaleString(language)}</strong></div>
      <button className="secondary-button" onClick={() => void run("Refresh memory", onRefresh)} disabled={controlsDisabled}>{t(working === "Refresh memory" ? "Refreshing..." : "Refresh")}</button>
    </div>

    <section className="management-composer">
      <header><div><h3>{t("Save a long-term fact")}</h3><p>{t("Only save stable information that should remain available across conversations in this project.")}</p></div></header>
      <textarea value={fact} onChange={(event) => setFact(event.target.value)} placeholder={t("For example: Tests run with pytest and require Python 3.10.")} rows={3} disabled={controlsDisabled} />
      <footer><small>{t("Duplicate facts are ignored by the Runtime.")}</small><button className="primary-button" onClick={() => void saveFact()} disabled={controlsDisabled || !fact.trim()}>{t(working === "Save memory" ? "Saving..." : "Save memory")}</button></footer>
    </section>

    <div className="management-toolbar">
      <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t("Filter saved memory")} />
      <span>{t("Showing {shown} of {total}", { shown: entries.length, total: snapshot.entries.length })}</span>
      <button className="secondary-button danger-button" onClick={() => void clearMemory()} disabled={controlsDisabled || snapshot.count === 0}>{t("Clear all")}</button>
    </div>
    <div className="management-card-list memory-list">
      {entries.length === 0 && <div className="management-empty">{t(normalizedFilter ? "No memory matches this filter." : "No long-term facts are saved for this project.")}</div>}
      {entries.map((entry) => <article className="management-card memory-card" key={entry.id}>
        <header><div><strong>{t(entry.type.toLowerCase())}</strong><small>{formatMemoryTimestamp(entry.timestamp, language)}</small></div><span>{t("{count} tokens", { count: entry.token_count })}</span></header>
        <p>{entry.content}</p>
        {Object.keys(entry.metadata).length > 0 && <small>{Object.entries(entry.metadata).map(([key, value]) => `${key}: ${value}`).join(" · ")}</small>}
        <footer><code>{entry.id}</code><button className="secondary-button danger-button" onClick={() => void deleteMemory(entry.id, entry.content)} disabled={controlsDisabled}>{t("Delete")}</button></footer>
      </article>)}
    </div>
    {snapshot.warnings.length > 0 && <details className="management-warnings" open>
      <summary>{t("View {count} memory storage warnings", { count: snapshot.warnings.length })}</summary>
      <pre>{snapshot.warnings.join("\n")}</pre>
    </details>}
    <PathRow label={t("Project memory file")} path={snapshot.storage_path} t={t} />
    <p className={`mcp-notice ${noticeIsError ? "error" : ""}`}>{notice || t(!runtimeOnline ? "Python Runtime must be online to manage memory." : "Memory changes apply immediately to every loaded conversation in this project.")}</p>
  </SettingsSectionView>;
}

export interface SkillManagementFormProps {
  language: AppSettings["general"]["language"];
  snapshot: SkillSnapshot;
  runtimeOnline: boolean;
  busy: boolean;
  onRefresh: () => Promise<SkillSnapshot>;
  onDiff: (name: string) => Promise<SkillDiff>;
  onSetEnabled: (name: string, enabled: boolean) => Promise<SkillSnapshot>;
  onReload: () => Promise<SkillSnapshot>;
  onUpdate: (name: string, currentHash: string, builtinHash: string) => Promise<SkillSnapshot>;
  onKeepCustom: (name: string, currentHash: string, builtinHash: string) => Promise<SkillSnapshot>;
  onRestoreDefault: (name: string, currentHash: string, builtinHash: string) => Promise<SkillSnapshot>;
  onPrepareDirectory: (scope: SkillInstallScope) => Promise<SkillDirectoryResult>;
}

export function SkillManagementForm({
  language,
  snapshot,
  runtimeOnline,
  busy,
  onRefresh,
  onDiff,
  onSetEnabled,
  onReload,
  onUpdate,
  onKeepCustom,
  onRestoreDefault,
  onPrepareDirectory,
}: SkillManagementFormProps) {
  const t = translator(language);
  const [filter, setFilter] = useState("");
  const [working, setWorking] = useState("");
  const [notice, setNotice] = useState("");
  const [noticeIsError, setNoticeIsError] = useState(false);
  const [activeDiff, setActiveDiff] = useState<SkillDiff | null>(null);
  const [installScope, setInstallScope] = useState<SkillInstallScope>("project");
  const [directoryNotice, setDirectoryNotice] = useState("");
  const [directoryNoticeIsError, setDirectoryNoticeIsError] = useState(false);
  const controlsDisabled = busy || Boolean(working) || !runtimeOnline;
  const viewDisabled = Boolean(working) || !runtimeOnline;
  const normalizedFilter = filter.trim().toLocaleLowerCase(language);
  const skills = normalizedFilter
    ? snapshot.skills.filter((skill) => [skill.name, skill.description, skill.source, skill.upgrade_state || "", ...skill.tags]
      .some((value) => value.toLocaleLowerCase(language).includes(normalizedFilter)))
    : snapshot.skills;

  async function run(label: string, action: () => Promise<unknown>) {
    if (controlsDisabled) return;
    setWorking(label);
    setNotice("");
    setNoticeIsError(false);
    try {
      await action();
      setNotice(t("{label} completed.", { label: translateActionLabel(label, t) }));
    } catch (error) {
      setNoticeIsError(true);
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setWorking("");
    }
  }

  async function openSkillDiff(name: string) {
    if (viewDisabled) return;
    setWorking(`View diff:${name}`);
    setNotice("");
    setNoticeIsError(false);
    try {
      setActiveDiff(await onDiff(name));
    } catch (error) {
      setNoticeIsError(true);
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setWorking("");
    }
  }

  async function applyDiffAction(action: "update" | "keep_custom" | "restore_default") {
    if (!activeDiff || controlsDisabled) return;
    if (action === "update") {
      const confirmed = await confirmDialog(
        t("Update {name} to built-in version {version}? Your customized Skill files will be replaced.", {
          name: activeDiff.name,
          version: activeDiff.builtin_version || "—",
        }),
        { title: t("Update built-in Skill"), kind: "warning" },
      );
      if (!confirmed) return;
    }
    if (action === "restore_default") {
      const confirmed = await confirmDialog(
        t("Restore {name} to the latest built-in default? Your customized Skill files will be replaced.", {
          name: activeDiff.name,
        }),
        { title: t("Restore built-in Skill"), kind: "warning" },
      );
      if (!confirmed) return;
    }

    const label = action === "update"
      ? "Update built-in Skill"
      : action === "keep_custom"
        ? "Keep custom Skill"
        : "Restore built-in Skill";
    const operation = action === "update"
      ? () => onUpdate(activeDiff.name, activeDiff.current_hash, activeDiff.builtin_hash)
      : action === "keep_custom"
        ? () => onKeepCustom(activeDiff.name, activeDiff.current_hash, activeDiff.builtin_hash)
        : () => onRestoreDefault(activeDiff.name, activeDiff.current_hash, activeDiff.builtin_hash);
    await run(label, async () => {
      await operation();
      setActiveDiff(null);
    });
  }

  async function openInstallDirectory() {
    if (controlsDisabled) return;
    setWorking("Open Skill directory");
    setNotice("");
    setNoticeIsError(false);
    setDirectoryNotice(t("Opening selected Skills folder..."));
    setDirectoryNoticeIsError(false);
    try {
      const result = await onPrepareDirectory(installScope);
      setDirectoryNotice(t("Opened {scope} Skills folder: {path}", {
        scope: t(result.scope === "project" ? "Project" : "User"),
        path: result.path,
      }));
    } catch (error) {
      setDirectoryNoticeIsError(true);
      setDirectoryNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setWorking("");
    }
  }

  return <>
    <SettingsSectionView title={t("Skills")} description={t("Inspect built-in, user, and project skills and control which instructions the Agent may load on its next turn.")}>
      <div className="management-summary">
        <div><span>{t("Enabled")}</span><strong>{snapshot.enabled_count}</strong></div>
        <div><span>{t("Discovered")}</span><strong>{snapshot.total_count}</strong></div>
        <div><span>{t("Updates")}</span><strong className={(snapshot.updates_available ?? 0) > 0 ? "status-warning" : ""}>{snapshot.updates_available ?? 0}</strong></div>
        <div className="management-summary-actions"><button className="secondary-button" onClick={() => void run("Refresh skills", onRefresh)} disabled={controlsDisabled}>{t("Refresh")}</button><button className="primary-button" onClick={() => void run("Reload skills", onReload)} disabled={controlsDisabled}>{t(working === "Reload skills" ? "Reloading..." : "Reload")}</button></div>
      </div>
      <div className="management-callout skill-installer-panel">
        <strong>{t("Install a custom skill")}</strong>
        <p>{t("Choose where the skill should be available. The folder is created when you open it; add one subfolder containing SKILL.md, then select Reload.")}</p>
        <div className="skill-install-targets" role="radiogroup" aria-label={t("Skill installation scope")}>
          {(["user", "project"] as const).map((scope) => {
            const selected = installScope === scope;
            const path = scope === "user" ? snapshot.user_dir : snapshot.project_dir;
            return <button
              className={`skill-install-target ${selected ? "selected" : ""}`}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => {
                setInstallScope(scope);
                setDirectoryNotice("");
                setDirectoryNoticeIsError(false);
              }}
              disabled={!path}
              key={scope}
            >
              <span className="skill-install-radio">{selected ? "✓" : ""}</span>
              <span><strong>{t(scope === "user" ? "User Skills" : "Project Skills")}</strong><small>{t(scope === "user" ? "Available in every project" : "Available only in the current project")}</small><code title={path}>{path || t("Path unavailable")}</code></span>
            </button>;
          })}
        </div>
        <div className="skill-install-actions">
          <span>{t("Selected: {scope}", { scope: t(installScope === "project" ? "Project Skills" : "User Skills") })}</span>
          <button className="primary-button" type="button" onClick={() => void openInstallDirectory()} disabled={controlsDisabled || !(installScope === "user" ? snapshot.user_dir : snapshot.project_dir)}>{t(working === "Open Skill directory" ? "Opening..." : "Open selected folder")}</button>
        </div>
        {directoryNotice && <p className={`skill-install-notice ${directoryNoticeIsError ? "error" : "success"}`}>{directoryNotice}</p>}
      </div>
      <div className="management-toolbar"><input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t("Filter skills by name, tag, or source")} /><span>{t("Showing {shown} of {total}", { shown: skills.length, total: snapshot.skills.length })}</span></div>
      {snapshot.warnings.length > 0 && <details className="management-warnings"><summary>{t("View {count} loading warnings", { count: snapshot.warnings.length })}</summary><pre>{snapshot.warnings.join("\n")}</pre></details>}
      <div className="management-card-list skill-list">
        {skills.length === 0 && <div className="management-empty">{t(normalizedFilter ? "No skills match this filter." : "No skills were discovered.")}</div>}
        {skills.map((skill) => {
          const upgradeState = skill.upgrade_state || (skill.builtin ? "error" : "not_bundled");
          const stateLabel = {
            not_bundled: "Custom Skill",
            current: "Up to date",
            customized: "Customized",
            update_available: "New version available",
            custom_kept: "Custom version kept",
            error: "Upgrade status error",
          }[upgradeState];
          const canViewDiff = skill.builtin && ["customized", "update_available", "custom_kept"].includes(upgradeState);
          const diffLoading = working === `View diff:${skill.name}`;
          return <article className={`management-card skill-card ${skill.enabled ? "enabled" : "disabled"}`} key={skill.name}>
            <header>
              <div>
                <strong>{skill.name}</strong>
                <small>{t(skill.source)}{skill.current_version ? ` · v${skill.current_version}` : skill.version ? ` · v${skill.version}` : ""}{skill.author ? ` · ${skill.author}` : ""}</small>
              </div>
              <span className={`skill-upgrade-state ${upgradeState}`}>{t(stateLabel)}</span>
              <Toggle checked={skill.enabled} disabled={controlsDisabled} onChange={(enabled) => void run(`${enabled ? "Enable" : "Disable"} skill`, () => onSetEnabled(skill.name, enabled))} />
            </header>
            <p>{skill.description || t("No description supplied by this skill.")}</p>
            {skill.tags.length > 0 && <div className="management-tags">{skill.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>}
            {skill.builtin && <div className={`skill-upgrade-panel ${upgradeState}`}>
              <div>
                <span>{t(skillUpgradeDescription(upgradeState))}</span>
                <code title={`${skill.current_hash} -> ${skill.builtin_hash}`}>{shortHash(skill.current_hash)} → {shortHash(skill.builtin_hash)}</code>
              </div>
              {canViewDiff && <button className="secondary-button" type="button" onClick={() => void openSkillDiff(skill.name)} disabled={viewDisabled}>{t(diffLoading ? "Loading Diff..." : "View diff")}</button>}
              {upgradeState === "error" && skill.error && <small>{skill.error}</small>}
            </div>}
            <footer><code title={skill.skill_md_path}>{skill.skill_md_path}</code><button className="secondary-button" onClick={() => void revealItemInDir(skill.skill_md_path)}>{t("Reveal")}</button></footer>
          </article>;
        })}
      </div>
      <div className="management-paths"><PathRow label={t("Skill state file")} path={snapshot.state_path} t={t} /></div>
      <p className={`mcp-notice ${noticeIsError ? "error" : ""}`}>{notice || t(!runtimeOnline ? "Python Runtime must be online to manage skills." : "Skill enablement and reload changes apply on the next Agent turn.")}</p>
    </SettingsSectionView>
    {activeDiff && <section className="diff-overlay" role="dialog" aria-modal="true" aria-label={t("Skill update Diff")}>
      <div className="diff-dialog skill-diff-dialog">
        <header>
          <div>
            <strong>{t("Skill update Diff")}: {activeDiff.name}</strong>
            <span>{t("Current v{current} → built-in v{builtin} · +{additions} -{deletions}", {
              current: activeDiff.current_version || "—",
              builtin: activeDiff.builtin_version || "—",
              additions: activeDiff.additions,
              deletions: activeDiff.deletions,
            })}</span>
          </div>
          <button className="secondary-button" type="button" onClick={() => setActiveDiff(null)} disabled={Boolean(working)}>{t("Close")}</button>
        </header>
        <pre className="task-diff-content">{visibleSkillDiff(activeDiff.diff, activeDiff.truncated) || t("No Skill differences are available.")}</pre>
        <footer className="skill-diff-footer">
          <div>
            <code title={activeDiff.current_hash}>{t("Current hash")}: {shortHash(activeDiff.current_hash)}</code>
            <code title={activeDiff.builtin_hash}>{t("Built-in hash")}: {shortHash(activeDiff.builtin_hash)}</code>
            {activeDiff.truncated && <small>{t("The displayed Skill Diff was truncated. Update safety still uses the complete content hashes.")}</small>}
          </div>
          <div className="skill-upgrade-actions">
            {snapshot.skills.find((skill) => skill.name === activeDiff.name)?.upgrade_state === "update_available" && <>
              <button className="secondary-button" type="button" onClick={() => void applyDiffAction("keep_custom")} disabled={controlsDisabled}>{t("Keep custom")}</button>
              <button className="primary-button" type="button" onClick={() => void applyDiffAction("update")} disabled={controlsDisabled}>{t("Update")}</button>
            </>}
            {["customized", "custom_kept"].includes(snapshot.skills.find((skill) => skill.name === activeDiff.name)?.upgrade_state || "") && <button className="secondary-button danger-button" type="button" onClick={() => void applyDiffAction("restore_default")} disabled={controlsDisabled}>{t("Restore default")}</button>}
          </div>
        </footer>
      </div>
    </section>}
  </>;
}

export interface BrowserManagementFormProps {
  language: AppSettings["general"]["language"];
  snapshot: BrowserSnapshot;
  runtimeOnline: boolean;
  busy: boolean;
  onRefresh: () => Promise<BrowserSnapshot>;
  onProbe: (port: number) => Promise<BrowserProbeSnapshot>;
  onConnect: (port?: number) => Promise<BrowserSnapshot>;
  onDisconnect: () => Promise<BrowserSnapshot>;
  onTabs: () => Promise<{ output: string }>;
}

export function BrowserManagementForm({ language, snapshot, runtimeOnline, busy, onRefresh, onProbe, onConnect, onDisconnect, onTabs }: BrowserManagementFormProps) {
  const t = translator(language);
  const [port, setPort] = useState(9222);
  const [working, setWorking] = useState("");
  const [notice, setNotice] = useState("");
  const [noticeIsError, setNoticeIsError] = useState(false);
  const [probe, setProbe] = useState<BrowserProbeSnapshot | null>(snapshot.legacy_probe ?? null);
  const [tabs, setTabs] = useState(snapshot.tabs_output ?? "");
  const controlsDisabled = busy || Boolean(working) || !runtimeOnline;

  useEffect(() => {
    if (snapshot.legacy_probe) setProbe(snapshot.legacy_probe);
    if (snapshot.tabs_output !== undefined) setTabs(snapshot.tabs_output);
    if (snapshot.mode === "isolated") setTabs("");
  }, [snapshot]);

  async function run(label: string, action: () => Promise<unknown>) {
    if (controlsDisabled) return;
    setWorking(label);
    setNotice("");
    setNoticeIsError(false);
    try {
      await action();
      setNotice(t("{label} completed.", { label: translateActionLabel(label, t) }));
    } catch (error) {
      setNoticeIsError(true);
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setWorking("");
    }
  }

  async function connectShared(useLegacyPort: boolean) {
    if (snapshot.mode === "shared") return;
    const confirmed = await confirmDialog(
      t("Connect StellarCode to your shared Chrome session?\n\nThe Agent may read and interact with signed-in tabs. Existing user tabs are protected from close operations, but page content can contain sensitive data."),
      { title: t("Connect shared Chrome"), kind: "warning" },
    );
    if (!confirmed) return;
    await run(useLegacyPort ? "Connect legacy browser" : "Connect browser", () => onConnect(useLegacyPort ? port : undefined));
  }

  async function probePort() {
    await run("Probe browser", async () => setProbe(await onProbe(port)));
  }

  async function loadTabs() {
    await run("Load browser tabs", async () => setTabs((await onTabs()).output));
  }

  const chromeGood = snapshot.chrome_server.status === "ready";
  return <SettingsSectionView title={t("Browser")} description={t("Manage the Chrome DevTools MCP connection used for authenticated pages and browser automation.")}>
    <div className="browser-mode-card">
      <span className={`browser-mode-indicator ${snapshot.mode}`}><i />{t(snapshot.mode === "shared" ? "Shared Chrome" : "Isolated browser")}</span>
      <strong>{snapshot.mode === "shared" ? snapshot.browser_url || t("Chrome autoConnect") : t("Temporary profile without your existing login state")}</strong>
      <p>{t(snapshot.mode === "shared" ? "Shared mode can access your existing signed-in tabs. Disconnect when the task is complete." : "Isolated mode keeps automation separate from your regular Chrome profile.")}</p>
      <button className="secondary-button" onClick={() => void run("Refresh browser", onRefresh)} disabled={controlsDisabled}>{t("Refresh")}</button>
    </div>
    <div className="management-summary browser-summary">
      <div><span>{t("Chrome DevTools server")}</span><strong className={chromeGood ? "status-good" : "status-error"}>{t(snapshot.chrome_server.status || "not configured")}</strong></div>
      <div><span>{t("Browser tools")}</span><strong>{snapshot.chrome_server.tool_count}</strong></div>
      <div><span>{t("Agent-opened pages")}</span><strong>{snapshot.agent_opened_pages.length}</strong></div>
      <div><span>{t("Last navigation")}</span><strong title={snapshot.last_navigated_url}>{snapshot.last_navigated_url || t("None")}</strong></div>
    </div>
    {snapshot.chrome_server.error && <p className="management-error">{snapshot.chrome_server.error}</p>}
    <section className="browser-connect-panel">
      <article><h3>{t("Chrome autoConnect")}</h3><p>{t("Recommended for current Chrome versions. Enable remote debugging at chrome://inspect/#remote-debugging and approve Chrome's connection dialog.")}</p><button className="primary-button" onClick={() => void connectShared(false)} disabled={controlsDisabled || snapshot.mode === "shared"}>{t(working === "Connect browser" ? "Connecting..." : "Connect")}</button></article>
      <article><h3>{t("Legacy CDP port")}</h3><p>{t("Use a Chrome instance started with --remote-debugging-port and a separate user-data directory.")}</p><div className="browser-port-control"><input type="number" min="1024" max="65535" value={port} onChange={(event) => { setPort(Number(event.target.value)); setProbe(null); }} /><button className="secondary-button" onClick={() => void probePort()} disabled={controlsDisabled || !Number.isInteger(port) || port < 1024 || port > 65535}>{t("Probe")}</button><button className="secondary-button" onClick={() => void connectShared(true)} disabled={controlsDisabled || snapshot.mode === "shared" || !probe?.connected || probe.port !== port}>{t("Connect")}</button></div></article>
    </section>
    {probe && <div className={`browser-probe ${probe.connected ? "connected" : "failed"}`}><i /><div><strong>{t(probe.connected ? "CDP endpoint available" : "CDP endpoint unavailable")}</strong><p>{probe.connected ? [probe.browser_version, probe.browser_url].filter(Boolean).join(" · ") : probe.error}</p></div></div>}
    {snapshot.mode === "shared" && <div className="browser-shared-actions"><button className="secondary-button" onClick={() => void loadTabs()} disabled={controlsDisabled}>{t("List shared tabs")}</button><button className="secondary-button danger-button" onClick={() => void run("Disconnect browser", onDisconnect)} disabled={controlsDisabled}>{t(working === "Disconnect browser" ? "Disconnecting..." : "Disconnect")}</button></div>}
    {tabs && <details className="browser-tabs" open><summary>{t("Shared browser tabs")}</summary><pre>{tabs}</pre></details>}
    <p className={`mcp-notice ${noticeIsError ? "error" : ""}`}>{notice || t(!runtimeOnline ? "Python Runtime must be online to manage the browser." : "Browser connection changes apply immediately and use the Chrome DevTools MCP server.")}</p>
  </SettingsSectionView>;
}

function DiagnosticsSettingsForm({ draft, setDraft, snapshot, diagnosticsSnapshot, runtimeOnline, runtimePython, busy, onChoosePython, onChooseLspCommand, onRefresh, onRun, onCancel }: FormProps & {
  snapshot: SettingsSnapshot;
  diagnosticsSnapshot: DiagnosticsSnapshot;
  runtimeOnline: boolean;
  runtimePython: string;
  busy: boolean;
  onChoosePython: () => void;
  onChooseLspCommand: () => void;
  onRefresh: () => Promise<DiagnosticsSnapshot>;
  onRun: (profile?: "safe" | "build") => Promise<DiagnosticsSnapshot>;
  onCancel: () => Promise<DiagnosticsSnapshot>;
}) {
  const t = translator(draft.general.language);
  const updateDiagnostics = (patch: Partial<AppSettings["diagnostics"]>) => setDraft((current) => ({
    ...current,
    diagnostics: { ...current.diagnostics, ...patch },
  }));
  return <SettingsSectionView title={t("Data & Diagnostics")} description={t("Runtime executable and local data locations. Full access remains session-only and is never made a default here.")}>
    <div className="management-summary diagnostics-summary">
      <div><span>{t("Status")}</span><strong>{t(diagnosticsSnapshot.status)}</strong></div>
      <div><span>{t("Errors")}</span><strong>{diagnosticsSnapshot.error_count}</strong></div>
      <div><span>{t("Warnings")}</span><strong>{diagnosticsSnapshot.warning_count}</strong></div>
      <div className="management-summary-actions">
        <button className="secondary-button" onClick={() => void onRefresh()} disabled={!runtimeOnline || busy}>{t("Refresh")}</button>
        {diagnosticsSnapshot.status === "running"
          ? <button className="secondary-button danger-button" onClick={() => void onCancel()}>{t("Cancel")}</button>
          : <button className="primary-button" onClick={() => void onRun("safe")} disabled={!runtimeOnline || busy}>{t("Run checks")}</button>}
      </div>
    </div>
    <section className="diagnostics-provider-list">
      <header><h3>{t("Diagnostic providers")}</h3><p>{t("Only results produced by an available provider are shown in Problems. Unavailable LSP support is reported explicitly.")}</p></header>
      {diagnosticsSnapshot.providers.map((provider) => <article key={provider.id}>
        <i className={provider.available ? "available" : "unavailable"} />
        <div><strong>{translateDiagnosticRuntimeText(draft.general.language, provider.label)}</strong><small>{provider.kind.toUpperCase()}</small></div>
        <p>{provider.detail ? translateDiagnosticRuntimeText(draft.general.language, provider.detail) : t(provider.available ? "Available" : "Unavailable")}</p>
      </article>)}
    </section>
    {diagnosticsSnapshot.detected_projects.length > 0 && <details className="management-warnings"><summary>{t("Detected projects ({count})", { count: diagnosticsSnapshot.detected_projects.length })}</summary><pre>{diagnosticsSnapshot.detected_projects.map((project) => `${project.kind}: ${project.root} [${project.markers.join(", ")}]`).join("\n")}</pre></details>}
    <SettingsRow label={t("Python executable")} description={t("Leave empty to use STELLARCODE_PYTHON, the repository .venv, or system Python.")}><div className="path-input"><input value={draft.diagnostics.python_path} onChange={(event) => updateDiagnostics({ python_path: event.target.value })} placeholder={t("Automatic")} /><button className="secondary-button" onClick={onChoosePython}>{t("Browse")}</button></div></SettingsRow>
    <SettingsRow label={t("Python Language Server")} description={t("Disabled by default. StellarCode only starts the local Language Server you explicitly configure.")}><Toggle checked={draft.diagnostics.lsp_enabled} onChange={(lsp_enabled) => updateDiagnostics({ lsp_enabled })} /></SettingsRow>
    <SettingsRow label={t("Language Server executable")} description={t("Use an existing absolute executable path. StellarCode never installs a Language Server or searches the shell PATH for this setting.")}><div className="path-input"><input value={draft.diagnostics.lsp_command} onChange={(event) => updateDiagnostics({ lsp_command: event.target.value })} placeholder={t("Absolute executable path")} /><button className="secondary-button" onClick={onChooseLspCommand}>{t("Browse")}</button></div></SettingsRow>
    <SettingsRow label={t("Language Server arguments")} description={t("Enter one literal argument per line. Arguments are passed directly without shell parsing.")}><textarea rows={4} value={draft.diagnostics.lsp_args.join("\n")} onChange={(event) => updateDiagnostics({ lsp_args: event.target.value.split(/\r?\n/).filter((argument) => argument.length > 0) })} placeholder={t("For example: --stdio")} /></SettingsRow>
    <NumberSetting label={t("Language Server timeout")} description={t("Maximum time for one diagnostic request. Workspace edits and Language Server commands are never applied.")} value={draft.diagnostics.lsp_timeout_seconds} min={2} max={60} suffix={t("seconds")} onChange={(lsp_timeout_seconds) => updateDiagnostics({ lsp_timeout_seconds })} />
    <SettingsRow label={t("Runtime status")} description={t("Model and Agent changes require a restart.")}><span className={`diagnostic-status ${runtimeOnline ? "online" : "offline"}`}><i />{t(runtimeOnline ? "Online" : "Offline")}</span></SettingsRow>
    <SettingsRow label={t("Active Python")} description={runtimePython || t("Runtime has not started yet.")}><span className="settings-readonly-value">{t(runtimePython ? "Detected" : "Not available")}</span></SettingsRow>
    <PathRow label={t("Settings file")} path={snapshot.settings_path} t={t} />
    <PathRow label={t("Application data")} path={snapshot.app_data_path} t={t} />
    <PathRow label={t("Image cache")} path={snapshot.image_cache_path} t={t} />
    <PathRow label={t("Environment file")} path={snapshot.env_path} t={t} />
  </SettingsSectionView>;
}

interface FormProps { draft: AppSettings; setDraft: React.Dispatch<React.SetStateAction<AppSettings>>; }

function SettingsSectionView({ title, description, children }: { title: string; description: string; children: React.ReactNode }) { return <section className="settings-section-view"><header><h2>{title}</h2><p>{description}</p></header><div className="settings-fields">{children}</div></section>; }
function SettingsRow({ label, description, children }: { label: string; description: string; children: React.ReactNode }) { return <div className="settings-row"><div><strong>{label}</strong><p>{description}</p></div><div className="settings-control">{children}</div></div>; }
function Toggle({ checked, onChange, disabled = false }: { checked: boolean; onChange: (checked: boolean) => void; disabled?: boolean }) { return <button className={`settings-toggle ${checked ? "on" : ""}`} role="switch" aria-checked={checked} onClick={() => onChange(!checked)} disabled={disabled}><span /></button>; }
function ColorControl({ value, onChange }: { value: string; onChange: (value: string) => void }) { return <label className="color-control"><input type="color" value={value} onChange={(event) => onChange(event.target.value)} /><code>{value.toUpperCase()}</code></label>; }
function NumberSetting({ label, description, value, min, max, suffix, onChange }: { label: string; description: string; value: number; min: number; max: number; suffix?: string; onChange: (value: number) => void }) {
  const updateValue = (input: HTMLInputElement) => {
    const next = input.valueAsNumber;
    if (Number.isFinite(next)) onChange(Math.trunc(next));
  };
  return <SettingsRow label={label} description={description}><label className="number-control"><input type="number" min={min} max={max} step={1} value={value} onInput={(event) => updateValue(event.currentTarget)} onChange={(event) => updateValue(event.currentTarget)} />{suffix && <span>{suffix}</span>}</label></SettingsRow>;
}
function PathRow({ label, path, t }: { label: string; path: string; t: ReturnType<typeof translator> }) { return <SettingsRow label={label} description={path || t("Not available until a project is open.")}><button className="secondary-button" onClick={() => void revealItemInDir(path)} disabled={!path}>{t("Reveal")}</button></SettingsRow>; }
function settingsIcon(section: SettingsSection) { return { general: "◎", appearance: "◐", models: "◇", agent: "✦", prompt: "P", memory: "M", skills: "S", mcp: "⌘", browser: "B", rag: "⌕", diagnostics: "⚙" }[section]; }
function settingsSectionLabel(section: SettingsSection) { return { general: "General", appearance: "Appearance", models: "Models", agent: "Agent", prompt: "Prompt", memory: "Memory", skills: "Skills", mcp: "MCP Servers", browser: "Browser", rag: "Code RAG", diagnostics: "Data & Diagnostics" }[section]; }
function abbreviateText(value: string, limit: number) { return value.length <= limit ? value : `${value.slice(0, limit)}...`; }
function shortHash(value?: string | null) { return value ? value.slice(0, 12) : "—"; }
function visibleSkillDiff(value: string, truncated: boolean) {
  const normalized = truncated ? value.replace(/\n*\[diff truncated\]\s*$/i, "") : value;
  return normalized.trim() === "No differences." ? "" : normalized;
}
function skillUpgradeDescription(state: NonNullable<SkillSnapshot["skills"][number]["upgrade_state"]>) {
  return {
    not_bundled: "This is a user or project Skill and is not managed by built-in upgrades.",
    current: "This managed built-in Skill is up to date and has not been modified.",
    customized: "This Skill differs from its installed built-in default.",
    update_available: "A newer built-in version is available. Review the Diff before choosing an action.",
    custom_kept: "The customized copy is being kept for the current built-in version.",
    error: "StellarCode could not verify this built-in Skill's upgrade state.",
  }[state];
}
function formatMemoryTimestamp(timestamp: number, language: AppSettings["general"]["language"]) {
  const milliseconds = timestamp > 10_000_000_000 ? timestamp : timestamp * 1000;
  const date = new Date(milliseconds);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString(language);
}
function translateActionLabel(label: string, t: ReturnType<typeof translator>) {
  const direct = t(label);
  if (direct !== label) return direct;
  const logs = label.match(/^Load (.+) logs$/);
  if (logs) return `${t("Load")} ${logs[1]} ${t("Logs")}`;
  const prefix = ["Install", "Load", "Remove", "Enable", "Disable", "Restart", "Save", "Delete", "Clear", "Refresh", "Reload", "Connect", "Disconnect", "Probe"].find((item) => label.startsWith(`${item} `));
  return prefix ? `${t(prefix)} ${label.slice(prefix.length + 1)}` : t(label);
}
function runtimeSettingsChanged(left: AppSettings, right: AppSettings) { return JSON.stringify([left.general.worktree_directory, left.models, left.agent, left.rag, left.diagnostics]) !== JSON.stringify([right.general.worktree_directory, right.models, right.agent, right.rag, right.diagnostics]); }

function parseStringMap(raw: string, label: string): Record<string, string> {
  const parsed: unknown = JSON.parse(raw || "{}");
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${label} must be a JSON object.`);
  const entries = Object.entries(parsed);
  if (!entries.every(([, value]) => typeof value === "string")) throw new Error(`${label} values must all be strings.`);
  return Object.fromEntries(entries) as Record<string, string>;
}

const THEME_PALETTES: Record<AppSettings["appearance"]["theme"], Pick<AppSettings["appearance"], "accent_color" | "background_color" | "panel_color" | "text_color">> = {
  dark: { accent_color: "#80aaff", background_color: "#111318", panel_color: "#171a20", text_color: "#d9dde7" },
  light: { accent_color: "#2f6fce", background_color: "#f3f5f8", panel_color: "#e8ebf0", text_color: "#242a33" },
};
