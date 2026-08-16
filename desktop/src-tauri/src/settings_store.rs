use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};

const SETTINGS_SCHEMA_VERSION: u32 = 1;
const MAX_AGENT_ITERATIONS: u16 = 128;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct GeneralSettings {
    pub reopen_last_project: bool,
    pub language: String,
    pub send_shortcut: String,
    pub conversation_font_size: u8,
    pub compact_tools: bool,
    pub compact_plans: bool,
}

impl Default for GeneralSettings {
    fn default() -> Self {
        Self {
            reopen_last_project: true,
            language: "zh-CN".into(),
            send_shortcut: "enter".into(),
            conversation_font_size: 12,
            compact_tools: true,
            compact_plans: true,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct AppearanceSettings {
    pub theme: String,
    pub accent_color: String,
    pub background_color: String,
    pub panel_color: String,
    pub text_color: String,
    pub client_font_size: u8,
}

impl Default for AppearanceSettings {
    fn default() -> Self {
        Self {
            theme: "dark".into(),
            accent_color: "#80aaff".into(),
            background_color: "#111318".into(),
            panel_color: "#171a20".into(),
            text_color: "#d9dde7".into(),
            client_font_size: 12,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct ModelSettings {
    pub provider: String,
    pub model: String,
    pub base_url: String,
    pub vision_provider: String,
    pub vision_model: String,
}

impl Default for ModelSettings {
    fn default() -> Self {
        Self {
            provider: "environment".into(),
            model: String::new(),
            base_url: String::new(),
            vision_provider: "environment".into(),
            vision_model: String::new(),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct AgentSettings {
    pub default_mode: String,
    pub max_iterations: u16,
    pub max_parallel_tools: u16,
    pub tool_batch_timeout_seconds: u16,
    pub plan_workers: u16,
    pub team_workers: u16,
    pub team_retries: u16,
    pub context_window: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct RagSettings {
    pub provider: String,
    pub model: String,
    pub base_url: String,
    pub automatic_retrieval: bool,
}

impl Default for RagSettings {
    fn default() -> Self {
        Self {
            provider: "environment".into(),
            model: String::new(),
            base_url: String::new(),
            automatic_retrieval: true,
        }
    }
}

impl Default for AgentSettings {
    fn default() -> Self {
        Self {
            default_mode: "react".into(),
            max_iterations: 8,
            max_parallel_tools: 4,
            tool_batch_timeout_seconds: 90,
            plan_workers: 4,
            team_workers: 2,
            team_retries: 2,
            context_window: 200_000,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct DiagnosticSettings {
    pub python_path: String,
    pub lsp_enabled: bool,
    pub lsp_command: String,
    pub lsp_args: Vec<String>,
    pub lsp_timeout_seconds: u16,
}

impl Default for DiagnosticSettings {
    fn default() -> Self {
        Self {
            python_path: String::new(),
            lsp_enabled: false,
            lsp_command: String::new(),
            lsp_args: Vec::new(),
            lsp_timeout_seconds: 20,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct AppSettings {
    pub schema_version: u32,
    pub general: GeneralSettings,
    pub appearance: AppearanceSettings,
    pub models: ModelSettings,
    pub agent: AgentSettings,
    pub rag: RagSettings,
    pub diagnostics: DiagnosticSettings,
}

impl Default for AppSettings {
    fn default() -> Self {
        Self {
            schema_version: SETTINGS_SCHEMA_VERSION,
            general: GeneralSettings::default(),
            appearance: AppearanceSettings::default(),
            models: ModelSettings::default(),
            agent: AgentSettings::default(),
            rag: RagSettings::default(),
            diagnostics: DiagnosticSettings::default(),
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct SettingsSnapshot {
    pub settings: AppSettings,
    pub settings_path: String,
    pub env_path: String,
    pub app_data_path: String,
    pub image_cache_path: String,
    pub api_keys: BTreeMap<String, bool>,
}

pub struct SettingsStore {
    path: PathBuf,
    app_data_dir: PathBuf,
    project_root: PathBuf,
    settings: Mutex<AppSettings>,
}

impl SettingsStore {
    pub fn load(app_data_dir: &Path, project_root: &Path) -> Result<Self, String> {
        fs::create_dir_all(app_data_dir).map_err(display_error)?;
        let path = app_data_dir.join("settings.json");
        let settings = if path.exists() {
            let content = fs::read_to_string(&path).map_err(display_error)?;
            serde_json::from_str(&content).map_err(display_error)?
        } else {
            AppSettings::default()
        };
        validate(&settings)?;
        let store = Self {
            path,
            app_data_dir: app_data_dir.to_path_buf(),
            project_root: project_root.to_path_buf(),
            settings: Mutex::new(settings),
        };
        if !store.path.exists() {
            store.save(&store.current()?)?;
        }
        Ok(store)
    }

    pub fn current(&self) -> Result<AppSettings, String> {
        Ok(self.lock()?.clone())
    }

    pub fn snapshot(&self) -> Result<SettingsSnapshot, String> {
        let image_cache_path = home_dir().join(".stellarcode").join("cache");
        fs::create_dir_all(&image_cache_path).map_err(display_error)?;
        Ok(SettingsSnapshot {
            settings: self.current()?,
            settings_path: self.path.display().to_string(),
            env_path: self.project_root.join(".env").display().to_string(),
            app_data_path: self.app_data_dir.display().to_string(),
            image_cache_path: image_cache_path.display().to_string(),
            api_keys: configured_api_keys(&self.project_root),
        })
    }

    pub fn update(&self, mut settings: AppSettings) -> Result<SettingsSnapshot, String> {
        settings.schema_version = SETTINGS_SCHEMA_VERSION;
        validate(&settings)?;
        validate_python_path(&settings.diagnostics.python_path)?;
        self.save(&settings)?;
        *self.lock()? = settings;
        self.snapshot()
    }

    pub fn reset(&self) -> Result<SettingsSnapshot, String> {
        self.update(AppSettings::default())
    }

    fn save(&self, settings: &AppSettings) -> Result<(), String> {
        let content = serde_json::to_string_pretty(settings).map_err(display_error)?;
        fs::write(&self.path, content).map_err(display_error)
    }

    fn lock(&self) -> Result<MutexGuard<'_, AppSettings>, String> {
        self.settings
            .lock()
            .map_err(|_| "settings store is poisoned".into())
    }
}

fn validate(settings: &AppSettings) -> Result<(), String> {
    one_of("language", &settings.general.language, &["zh-CN", "en"])?;
    one_of(
        "send shortcut",
        &settings.general.send_shortcut,
        &["enter", "ctrl-enter"],
    )?;
    if !(10..=16).contains(&settings.general.conversation_font_size) {
        return Err("conversation font size must be between 10 and 16".into());
    }
    one_of("theme", &settings.appearance.theme, &["dark", "light"])?;
    validate_color("accent color", &settings.appearance.accent_color)?;
    validate_color("background color", &settings.appearance.background_color)?;
    validate_color("panel color", &settings.appearance.panel_color)?;
    validate_color("text color", &settings.appearance.text_color)?;
    if !(10..=16).contains(&settings.appearance.client_font_size) {
        return Err("client font size must be between 10 and 16".into());
    }
    one_of(
        "model provider",
        &settings.models.provider,
        &["environment", "deepseek", "glm", "agnes"],
    )?;
    one_of(
        "vision provider",
        &settings.models.vision_provider,
        &["environment", "auto", "glm", "agnes", "disabled"],
    )?;
    one_of(
        "default mode",
        &settings.agent.default_mode,
        &["react", "plan", "team"],
    )?;
    one_of(
        "embedding provider",
        &settings.rag.provider,
        &["environment", "local", "ollama", "openai", "glm"],
    )?;
    bounded(
        "max iterations",
        settings.agent.max_iterations,
        1,
        MAX_AGENT_ITERATIONS,
    )?;
    bounded("parallel tools", settings.agent.max_parallel_tools, 1, 16)?;
    bounded(
        "tool timeout",
        settings.agent.tool_batch_timeout_seconds,
        5,
        600,
    )?;
    bounded("plan workers", settings.agent.plan_workers, 1, 16)?;
    bounded("team workers", settings.agent.team_workers, 1, 8)?;
    bounded("team retries", settings.agent.team_retries, 0, 10)?;
    if !(16_000..=2_000_000).contains(&settings.agent.context_window) {
        return Err("context window must be between 16000 and 2000000".into());
    }
    validate_lsp_settings(&settings.diagnostics)?;
    Ok(())
}

fn validate_color(label: &str, value: &str) -> Result<(), String> {
    if value.len() == 7
        && value.starts_with('#')
        && value[1..]
            .bytes()
            .all(|character| character.is_ascii_hexdigit())
    {
        Ok(())
    } else {
        Err(format!("{label} must use #RRGGBB format"))
    }
}

fn validate_python_path(value: &str) -> Result<(), String> {
    if value.trim().is_empty() {
        return Ok(());
    }
    let python = Path::new(value.trim());
    if python.is_file() {
        Ok(())
    } else {
        Err(format!(
            "Python executable does not exist: {}",
            python.display()
        ))
    }
}

pub(crate) fn validate_lsp_settings(settings: &DiagnosticSettings) -> Result<(), String> {
    bounded("LSP timeout", settings.lsp_timeout_seconds, 2, 60)?;
    if settings.lsp_args.len() > 32 {
        return Err("LSP arguments may contain at most 32 items".into());
    }
    for (index, argument) in settings.lsp_args.iter().enumerate() {
        if argument.len() > 2048 {
            return Err(format!(
                "LSP argument {} must not exceed 2048 bytes",
                index + 1
            ));
        }
        if argument.contains('\0') {
            return Err(format!("LSP argument {} must not contain NUL", index + 1));
        }
    }
    if settings.lsp_command.contains('\0') {
        return Err("LSP command must not contain NUL".into());
    }
    if !settings.lsp_enabled {
        return Ok(());
    }
    let command = Path::new(settings.lsp_command.trim());
    if !command.is_absolute() {
        return Err("LSP command must be an absolute path".into());
    }
    let metadata = fs::symlink_metadata(command)
        .map_err(|_| format!("LSP command does not exist: {}", command.display()))?;
    if !metadata.file_type().is_file() {
        return Err(format!(
            "LSP command must be a regular file: {}",
            command.display()
        ));
    }
    Ok(())
}

fn one_of(label: &str, value: &str, allowed: &[&str]) -> Result<(), String> {
    if allowed.contains(&value) {
        Ok(())
    } else {
        Err(format!("unsupported {label}: {value}"))
    }
}

fn bounded(label: &str, value: u16, minimum: u16, maximum: u16) -> Result<(), String> {
    if (minimum..=maximum).contains(&value) {
        Ok(())
    } else {
        Err(format!("{label} must be between {minimum} and {maximum}"))
    }
}

fn configured_api_keys(project_root: &Path) -> BTreeMap<String, bool> {
    let env_file = read_env_file(&project_root.join(".env"));
    [
        ("deepseek", "DEEPSEEK_API_KEY"),
        ("glm", "GLM_API_KEY"),
        ("agnes", "AGNES_API_KEY"),
    ]
    .into_iter()
    .map(|(provider, variable)| {
        let value = std::env::var(variable)
            .ok()
            .or_else(|| env_file.get(variable).cloned())
            .unwrap_or_default();
        (provider.to_string(), is_real_secret(&value))
    })
    .collect()
}

fn read_env_file(path: &Path) -> BTreeMap<String, String> {
    fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                return None;
            }
            let (key, value) = trimmed.split_once('=')?;
            Some((
                key.trim().to_string(),
                value.trim().trim_matches('"').to_string(),
            ))
        })
        .collect()
}

fn is_real_secret(value: &str) -> bool {
    let normalized = value.trim().to_ascii_lowercase();
    !normalized.is_empty()
        && !normalized.starts_with("your_")
        && !normalized.contains("api_key_here")
}

fn home_dir() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn display_error(error: impl std::fmt::Display) -> String {
    error.to_string()
}

#[cfg(test)]
mod tests {
    use super::{validate, validate_lsp_settings, AppSettings, SettingsStore};
    use std::fs;

    #[test]
    fn validates_defaults_and_rejects_unsafe_ranges() {
        let mut settings = AppSettings::default();
        assert!(validate(&settings).is_ok());
        settings.agent.max_iterations = 0;
        assert!(validate(&settings).is_err());
        settings = AppSettings::default();
        settings.general.send_shortcut = "space".into();
        assert!(validate(&settings).is_err());
        settings = AppSettings::default();
        settings.general.language = "fr".into();
        assert!(validate(&settings).is_err());
        settings = AppSettings::default();
        settings.appearance.accent_color = "red".into();
        assert!(validate(&settings).is_err());
        settings = AppSettings::default();
        settings.agent.context_window = 8_000;
        assert!(validate(&settings).is_err());
    }

    #[test]
    fn persists_settings_without_copying_api_keys() {
        let root = std::env::temp_dir().join(format!(
            "stellarcode-settings-test-{}",
            uuid::Uuid::new_v4()
        ));
        let app_data = root.join("app-data");
        fs::create_dir_all(&root).unwrap();
        fs::write(root.join(".env"), "GLM_API_KEY=test-secret\n").unwrap();
        let store = SettingsStore::load(&app_data, &root).unwrap();
        let mut settings = store.current().unwrap();
        settings.general.conversation_font_size = 14;
        settings.general.language = "en".into();
        settings.appearance.theme = "light".into();
        settings.appearance.accent_color = "#2f6fce".into();
        settings.appearance.background_color = "#f3f5f8".into();
        settings.appearance.panel_color = "#e8ebf0".into();
        settings.appearance.text_color = "#242a33".into();
        settings.appearance.client_font_size = 13;
        settings.agent.max_iterations = 64;
        settings.rag.provider = "local".into();
        settings.rag.automatic_retrieval = false;
        let snapshot = store.update(settings).unwrap();

        assert_eq!(snapshot.settings.general.conversation_font_size, 14);
        assert_eq!(snapshot.settings.general.language, "en");
        assert_eq!(snapshot.settings.appearance.theme, "light");
        assert_eq!(snapshot.settings.appearance.text_color, "#242a33");
        assert_eq!(snapshot.settings.appearance.panel_color, "#e8ebf0");
        assert_eq!(snapshot.settings.appearance.client_font_size, 13);
        assert_eq!(snapshot.settings.agent.max_iterations, 64);
        assert_eq!(snapshot.settings.rag.provider, "local");
        assert!(!snapshot.settings.rag.automatic_retrieval);
        assert_eq!(snapshot.api_keys.get("glm"), Some(&true));
        let persisted = fs::read_to_string(app_data.join("settings.json")).unwrap();
        assert!(!persisted.contains("test-secret"));

        let reloaded = SettingsStore::load(&app_data, &root).unwrap();
        assert_eq!(
            reloaded.current().unwrap().general.conversation_font_size,
            14
        );
        assert_eq!(reloaded.current().unwrap().appearance.theme, "light");
        assert_eq!(reloaded.current().unwrap().appearance.text_color, "#242a33");
        assert_eq!(reloaded.current().unwrap().agent.max_iterations, 64);
        assert_eq!(
            reloaded.current().unwrap().appearance.panel_color,
            "#e8ebf0"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn validates_opt_in_lsp_command_and_structured_arguments() {
        let root = std::env::temp_dir().join(format!(
            "stellarcode-lsp-settings-test-{}",
            uuid::Uuid::new_v4()
        ));
        fs::create_dir_all(&root).unwrap();
        let command = root.join("language-server.exe");
        fs::write(&command, b"test").unwrap();

        let mut settings = AppSettings::default().diagnostics;
        assert!(validate_lsp_settings(&settings).is_ok());
        settings.lsp_enabled = true;
        assert!(validate_lsp_settings(&settings).is_err());
        settings.lsp_command = command.display().to_string();
        settings.lsp_args = vec!["--stdio".into()];
        assert!(validate_lsp_settings(&settings).is_ok());
        settings.lsp_timeout_seconds = 1;
        assert!(validate_lsp_settings(&settings).is_err());
        settings.lsp_timeout_seconds = 20;
        settings.lsp_args = vec!["ok".into(); 33];
        assert!(validate_lsp_settings(&settings).is_err());
        settings.lsp_args = vec!["bad\0arg".into()];
        assert!(validate_lsp_settings(&settings).is_err());

        fs::remove_dir_all(root).unwrap();
    }
}
