import type { AppSettings } from "./SettingsPage";

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
