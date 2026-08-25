//! Tauri boundary for the StellarCode desktop shell.
//!
//! Rust owns local settings, project registration, attachment inspection, and the Python
//! Sidecar process. It forwards JSONL without interpreting Agent semantics so protocol
//! state remains consistent with the Python Runtime and its durable journal.

mod project_store;
mod settings_store;

use project_store::{shell_compatible_path, ProjectRecord, ProjectStore, WorkspaceEntry};
use serde::Serialize;
use serde_json::Value;
use settings_store::{
    validate_lsp_settings, validate_worktree_directory, AppSettings, SettingsSnapshot,
    SettingsStore,
};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_opener::OpenerExt;

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;
#[cfg(target_os = "windows")]
const CREATE_NEW_PROCESS_GROUP: u32 = 0x00000200;

struct RuntimeProcess {
    child: Child,
    stdin: ChildStdin,
}

struct RuntimeLauncher {
    executable: PathBuf,
    /// Development starts `python -m stellarcode.runtime.sidecar`; the bundled
    /// release executable already has that module as its entry point.
    uses_python_module: bool,
}

#[derive(Default)]
struct RuntimeState(Mutex<Option<RuntimeProcess>>);

#[derive(Serialize)]
struct RuntimeStartResult {
    workspace: String,
    python: String,
    pid: u32,
}

#[derive(Serialize)]
struct SkillDirectoryResult {
    scope: String,
    path: String,
}

#[derive(Serialize)]
struct WorkspaceFilePreview {
    relative_path: String,
    absolute_path: String,
    file_name: String,
    content: String,
    size_bytes: u64,
    truncated: bool,
}

fn tag_runtime_message(message: &mut Value, pid: u32) {
    if let Some(object) = message.as_object_mut() {
        object.insert("runtime_pid".into(), Value::from(pid));
    }
}

#[derive(Serialize)]
struct AttachmentMetadata {
    id: String,
    kind: &'static str,
    mime_type: String,
    display_name: String,
    local_path: String,
    size_bytes: u64,
}

const MAX_ATTACHMENTS: usize = 10;
const MAX_ATTACHMENT_BYTES: u64 = 50 * 1024 * 1024;
const MAX_PREVIEW_BYTES: u64 = 20 * 1024 * 1024;
const MAX_WORKSPACE_FILE_PREVIEW_BYTES: u64 = 2 * 1024 * 1024;

const REVIEWABLE_TEXT_EXTENSIONS: &[&str] = &[
    "c",
    "cc",
    "cfg",
    "conf",
    "cpp",
    "css",
    "csv",
    "cs",
    "cxx",
    "env",
    "go",
    "graphql",
    "gql",
    "h",
    "hpp",
    "htm",
    "html",
    "ini",
    "java",
    "js",
    "json",
    "jsonc",
    "jsonl",
    "jsx",
    "kt",
    "kts",
    "less",
    "lock",
    "log",
    "lua",
    "md",
    "mjs",
    "php",
    "properties",
    "ps1",
    "py",
    "pyi",
    "rb",
    "rs",
    "rst",
    "sass",
    "scss",
    "sh",
    "sql",
    "svelte",
    "swift",
    "toml",
    "ts",
    "tsx",
    "txt",
    "vue",
    "xml",
    "yaml",
    "yml",
];
const REVIEWABLE_TEXT_FILENAMES: &[&str] = &[
    ".env",
    ".gitignore",
    ".gitattributes",
    "dockerfile",
    "license",
    "makefile",
    "readme",
];

#[tauri::command]
fn project_list(store: State<'_, ProjectStore>) -> Result<Vec<ProjectRecord>, String> {
    store.list()
}

#[tauri::command]
fn project_register(store: State<'_, ProjectStore>, path: String) -> Result<ProjectRecord, String> {
    store.register(Path::new(&path))
}

#[tauri::command]
fn project_create(
    store: State<'_, ProjectStore>,
    parent: String,
    name: String,
) -> Result<ProjectRecord, String> {
    store.create(Path::new(&parent), &name)
}

#[tauri::command]
fn project_remove(
    store: State<'_, ProjectStore>,
    project_id: String,
) -> Result<ProjectRecord, String> {
    store.remove(&project_id)
}

#[tauri::command]
fn project_touch(
    store: State<'_, ProjectStore>,
    project_id: String,
) -> Result<ProjectRecord, String> {
    store.touch(&project_id)
}

#[tauri::command]
fn workspace_list_entries(
    store: State<'_, ProjectStore>,
    project_id: String,
    relative_path: String,
) -> Result<Vec<WorkspaceEntry>, String> {
    store.list_entries(&project_id, &relative_path)
}

/// Opens a reviewed source/text file without giving the webview a general
/// `open_path` capability. Canonicalization prevents symlink or `..` escape;
/// Windows always uses Notepad so opening a script cannot execute it.
#[tauri::command]
fn workspace_file_open(
    app: AppHandle,
    store: State<'_, ProjectStore>,
    project_id: String,
    relative_path: String,
) -> Result<String, String> {
    let resolved = resolve_reviewable_workspace_file(&store, &project_id, &relative_path)?;
    let display_path = shell_compatible_path(&resolved).display().to_string();
    #[cfg(target_os = "windows")]
    app.opener()
        .open_path(display_path.clone(), Some("notepad.exe"))
        .map_err(|error| format!("cannot open reviewed file {display_path}: {error}"))?;
    #[cfg(not(target_os = "windows"))]
    app.opener()
        .open_path(display_path.clone(), None::<&str>)
        .map_err(|error| format!("cannot open reviewed file {display_path}: {error}"))?;
    Ok(display_path)
}

/// Reads a bounded, recognized text/source file for the built-in right sidebar.
/// The same canonical workspace guard used by external opening prevents path and
/// symlink escape, while the byte cap keeps the Tauri IPC response predictable.
#[tauri::command]
fn workspace_file_preview(
    store: State<'_, ProjectStore>,
    project_id: String,
    relative_path: String,
) -> Result<WorkspaceFilePreview, String> {
    let resolved = resolve_reviewable_workspace_file(&store, &project_id, &relative_path)?;
    let metadata = fs::metadata(&resolved)
        .map_err(|error| format!("cannot inspect reviewed file {relative_path}: {error}"))?;
    let truncated = metadata.len() > MAX_WORKSPACE_FILE_PREVIEW_BYTES;
    let mut bytes =
        Vec::with_capacity(metadata.len().min(MAX_WORKSPACE_FILE_PREVIEW_BYTES + 1) as usize);
    fs::File::open(&resolved)
        .map_err(|error| format!("cannot open reviewed file {relative_path}: {error}"))?
        .take(MAX_WORKSPACE_FILE_PREVIEW_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("cannot read reviewed file {relative_path}: {error}"))?;
    bytes.truncate(MAX_WORKSPACE_FILE_PREVIEW_BYTES as usize);
    let content = decode_text_preview(&bytes);
    Ok(WorkspaceFilePreview {
        relative_path,
        absolute_path: shell_compatible_path(&resolved).display().to_string(),
        file_name: resolved
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("file")
            .to_string(),
        content,
        size_bytes: metadata.len(),
        truncated,
    })
}

fn resolve_reviewable_workspace_file(
    store: &ProjectStore,
    project_id: &str,
    relative_path: &str,
) -> Result<PathBuf, String> {
    let project = store.get(project_id)?;
    let root = fs::canonicalize(&project.canonical_path).map_err(|error| {
        format!(
            "cannot resolve workspace {}: {error}",
            project.canonical_path
        )
    })?;
    let relative = Path::new(relative_path);
    if relative.as_os_str().is_empty()
        || relative.is_absolute()
        || relative.components().any(|component| {
            !matches!(
                component,
                std::path::Component::Normal(_) | std::path::Component::CurDir
            )
        })
    {
        return Err("reviewed file path must remain inside the project workspace".into());
    }
    let resolved = fs::canonicalize(root.join(relative))
        .map_err(|error| format!("cannot resolve reviewed file {relative_path}: {error}"))?;
    if !resolved.starts_with(&root) || !resolved.is_file() {
        return Err("reviewed file is outside the project workspace or is not a file".into());
    }
    if !is_reviewable_text_file(&resolved) {
        return Err("only recognized source, configuration, and text files can be opened in the desktop file viewer".into());
    }
    Ok(resolved)
}

fn decode_text_preview(bytes: &[u8]) -> String {
    if bytes.starts_with(&[0xff, 0xfe]) {
        let words = bytes[2..]
            .chunks_exact(2)
            .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
            .collect::<Vec<_>>();
        return String::from_utf16_lossy(&words);
    }
    if bytes.starts_with(&[0xfe, 0xff]) {
        let words = bytes[2..]
            .chunks_exact(2)
            .map(|pair| u16::from_be_bytes([pair[0], pair[1]]))
            .collect::<Vec<_>>();
        return String::from_utf16_lossy(&words);
    }
    let utf8 = bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(bytes);
    String::from_utf8_lossy(utf8).into_owned()
}

fn is_reviewable_text_file(path: &Path) -> bool {
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if REVIEWABLE_TEXT_FILENAMES.contains(&file_name.as_str())
        || REVIEWABLE_TEXT_FILENAMES
            .iter()
            .any(|name| file_name.starts_with(&format!("{name}.")))
    {
        return true;
    }
    path.extension()
        .and_then(|value| value.to_str())
        .map(|extension| {
            REVIEWABLE_TEXT_EXTENSIONS.contains(&extension.to_ascii_lowercase().as_str())
        })
        .unwrap_or(false)
}

#[tauri::command]
fn settings_get(store: State<'_, SettingsStore>) -> Result<SettingsSnapshot, String> {
    store.snapshot()
}

#[tauri::command]
fn settings_update(
    store: State<'_, SettingsStore>,
    settings: AppSettings,
) -> Result<SettingsSnapshot, String> {
    store.update(settings)
}

#[tauri::command]
fn settings_reset(store: State<'_, SettingsStore>) -> Result<SettingsSnapshot, String> {
    store.reset()
}

#[tauri::command]
fn skill_directory_open(
    app: AppHandle,
    store: State<'_, ProjectStore>,
    scope: String,
    project_id: Option<String>,
) -> Result<SkillDirectoryResult, String> {
    let directory = match scope.as_str() {
        "user" => user_home_dir()?.join(".stellarcode").join("skills"),
        "project" => {
            let project_id = project_id
                .as_deref()
                .filter(|value| !value.is_empty())
                .ok_or_else(|| "open a project before selecting Project Skills".to_string())?;
            let project = store.get(project_id)?;
            PathBuf::from(project.canonical_path)
                .join(".stellarcode")
                .join("skills")
        }
        _ => return Err("skill directory scope must be user or project".into()),
    };
    fs::create_dir_all(&directory).map_err(|error| {
        format!(
            "cannot create {} Skills directory {}: {error}",
            scope,
            directory.display()
        )
    })?;
    let canonical = shell_compatible_path(
        &fs::canonicalize(&directory)
            .map_err(|error| format!("cannot resolve {}: {error}", directory.display()))?,
    );
    let display_path = canonical.display().to_string();
    app.opener()
        .open_path(display_path.clone(), None::<&str>)
        .map_err(|error| format!("cannot open Skills directory {display_path}: {error}"))?;
    Ok(SkillDirectoryResult {
        scope,
        path: display_path,
    })
}

fn user_home_dir() -> Result<PathBuf, String> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .ok_or_else(|| "cannot locate the current user home directory".into())
}

#[tauri::command]
fn attachment_inspect(paths: Vec<String>) -> Result<Vec<AttachmentMetadata>, String> {
    if paths.len() > MAX_ATTACHMENTS {
        return Err(format!("attach at most {MAX_ATTACHMENTS} files at once"));
    }
    let mut attachments = Vec::with_capacity(paths.len());
    for value in paths {
        let canonical = shell_compatible_path(
            &std::fs::canonicalize(&value)
                .map_err(|error| format!("cannot open attachment {value}: {error}"))?,
        );
        let metadata = std::fs::metadata(&canonical).map_err(|error| {
            format!("cannot inspect attachment {}: {error}", canonical.display())
        })?;
        if !metadata.is_file() {
            return Err(format!("attachment is not a file: {}", canonical.display()));
        }
        if metadata.len() == 0 {
            return Err(format!("attachment is empty: {}", canonical.display()));
        }
        if metadata.len() > MAX_ATTACHMENT_BYTES {
            return Err(format!(
                "attachment exceeds the 50 MB limit: {}",
                canonical.display()
            ));
        }
        let display_name = canonical
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("attachment")
            .to_string();
        let (kind, mime_type) = attachment_type(&canonical);
        attachments.push(AttachmentMetadata {
            id: format!("attachment-{}", uuid::Uuid::new_v4()),
            kind,
            mime_type: mime_type.to_string(),
            display_name,
            local_path: canonical.display().to_string(),
            size_bytes: metadata.len(),
        });
    }
    Ok(attachments)
}

#[tauri::command]
fn attachment_preview_data(path: String) -> Result<String, String> {
    let canonical = shell_compatible_path(
        &std::fs::canonicalize(&path)
            .map_err(|error| format!("cannot open image preview {path}: {error}"))?,
    );
    let metadata = std::fs::metadata(&canonical).map_err(|error| {
        format!(
            "cannot inspect image preview {}: {error}",
            canonical.display()
        )
    })?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > MAX_PREVIEW_BYTES {
        return Err(format!("invalid image preview: {}", canonical.display()));
    }
    let (kind, mime_type) = attachment_type(&canonical);
    if kind != "image" {
        return Err(format!(
            "attachment is not an image: {}",
            canonical.display()
        ));
    }
    let bytes = std::fs::read(&canonical)
        .map_err(|error| format!("cannot read image preview {}: {error}", canonical.display()))?;
    Ok(format!("data:{mime_type};base64,{}", encode_base64(&bytes)))
}

fn encode_base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut encoded = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let value = ((chunk[0] as u32) << 16)
            | ((chunk.get(1).copied().unwrap_or(0) as u32) << 8)
            | chunk.get(2).copied().unwrap_or(0) as u32;
        encoded.push(TABLE[((value >> 18) & 0x3f) as usize] as char);
        encoded.push(TABLE[((value >> 12) & 0x3f) as usize] as char);
        encoded.push(if chunk.len() > 1 {
            TABLE[((value >> 6) & 0x3f) as usize] as char
        } else {
            '='
        });
        encoded.push(if chunk.len() > 2 {
            TABLE[(value & 0x3f) as usize] as char
        } else {
            '='
        });
    }
    encoded
}

fn attachment_type(path: &Path) -> (&'static str, &'static str) {
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    match extension.as_str() {
        "png" => ("image", "image/png"),
        "jpg" | "jpeg" => ("image", "image/jpeg"),
        "gif" => ("image", "image/gif"),
        "webp" => ("image", "image/webp"),
        "bmp" => ("image", "image/bmp"),
        "tif" | "tiff" => ("image", "image/tiff"),
        "json" => ("file", "application/json"),
        "html" | "htm" => ("file", "text/html"),
        "css" => ("file", "text/css"),
        "js" | "mjs" | "cjs" => ("file", "text/javascript"),
        "ts" | "tsx" => ("file", "text/typescript"),
        "md" | "markdown" => ("file", "text/markdown"),
        "csv" => ("file", "text/csv"),
        "xml" => ("file", "application/xml"),
        "yaml" | "yml" => ("file", "application/yaml"),
        "pdf" => ("file", "application/pdf"),
        "txt" | "py" | "java" | "rs" | "go" | "c" | "h" | "cpp" | "hpp" | "cs" | "rb" | "php"
        | "sh" | "ps1" | "toml" | "ini" | "cfg" | "env" | "sql" | "log" => ("file", "text/plain"),
        _ => ("file", "application/octet-stream"),
    }
}

fn project_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("src-tauri must be inside the StellarCode repository")
        .to_path_buf()
}

fn ensure_environment_file(root: &Path) -> std::io::Result<()> {
    fs::create_dir_all(root)?;
    let env_path = root.join(".env");
    if !env_path.exists() {
        // Never copy a developer `.env`: it may contain real API keys. A release
        // creates this safe template in the user's application-data directory.
        fs::write(
            env_path,
            "# StellarCode user configuration (API keys are stored locally)\n\
# Choose one provider and fill in its key.\n\
# LLM_PROVIDER=glm\n\
# GLM_API_KEY=\n\
# DEEPSEEK_API_KEY=\n\
# AGNES_API_KEY=\n\
# EMBEDDING_API_KEY=\n",
        )?;
    }
    Ok(())
}

fn python_executable(root: &Path, configured: &str) -> PathBuf {
    if !configured.trim().is_empty() {
        return PathBuf::from(configured.trim());
    }
    if let Some(configured) = std::env::var_os("STELLARCODE_PYTHON") {
        return PathBuf::from(configured);
    }
    #[cfg(target_os = "windows")]
    let virtualenv_python = root.join(".venv").join("Scripts").join("python.exe");
    #[cfg(not(target_os = "windows"))]
    let virtualenv_python = root.join(".venv").join("bin").join("python");
    if virtualenv_python.exists() {
        virtualenv_python
    } else {
        PathBuf::from("python")
    }
}

fn bundled_sidecar_executable(app: &AppHandle) -> Result<PathBuf, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| error.to_string())?;
    let executable_name = if cfg!(target_os = "windows") {
        "stellarcode-sidecar.exe"
    } else {
        "stellarcode-sidecar"
    };
    // Tauri preserves the resource's source path for normal desktop bundles.
    // The second location handles packagers that strip the leading `resources/`.
    let candidates = [
        resource_dir
            .join("resources")
            .join("stellarcode-sidecar")
            .join(executable_name),
        resource_dir
            .join("stellarcode-sidecar")
            .join(executable_name),
    ];
    candidates
        .iter()
        .find(|path| path.is_file())
        .cloned()
        .ok_or_else(|| {
            format!(
                "Bundled StellarCode Runtime is missing. Rebuild this installer with desktop/scripts/build-windows-release.ps1. Expected one of: {}",
                candidates
                    .iter()
                    .map(|path| path.display().to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        })
}

fn runtime_launcher(
    app: &AppHandle,
    development_root: &Path,
    configured_python: &str,
) -> Result<RuntimeLauncher, String> {
    if cfg!(debug_assertions) {
        return Ok(RuntimeLauncher {
            executable: python_executable(development_root, configured_python),
            uses_python_module: true,
        });
    }
    Ok(RuntimeLauncher {
        executable: bundled_sidecar_executable(app)?,
        uses_python_module: false,
    })
}

#[tauri::command]
fn runtime_start(
    app: AppHandle,
    state: State<'_, RuntimeState>,
    settings_store: State<'_, SettingsStore>,
    workspace: Option<String>,
) -> Result<RuntimeStartResult, String> {
    // Start exactly one Sidecar process per desktop instance. Higher-level project/session
    // concurrency is handled inside Python, not by spawning one process per conversation.
    let mut slot = state.0.lock().map_err(|_| "runtime state is poisoned")?;
    if let Some(process) = slot.as_mut() {
        if process
            .child
            .try_wait()
            .map_err(|error| error.to_string())?
            .is_none()
        {
            return Err("StellarCode runtime is already running".into());
        }
        *slot = None;
    }

    let development_root = project_root();
    let root = if cfg!(debug_assertions) {
        development_root.clone()
    } else {
        app.path()
            .app_data_dir()
            .map_err(|error| error.to_string())?
    };
    let workspace_path = workspace.map(PathBuf::from).unwrap_or_else(|| root.clone());
    let runtime_data_dir = app
        .path()
        .app_data_dir()
        .map_err(|error| error.to_string())?
        .join("runtime");
    let settings = settings_store.current()?;
    // Revalidate immediately before spawning the sidecar so deleting or replacing
    // the configured executable after saving cannot bypass the opt-in boundary.
    validate_lsp_settings(&settings.diagnostics)?;
    validate_worktree_directory(&settings.general.worktree_directory)?;
    let launcher = runtime_launcher(&app, &development_root, &settings.diagnostics.python_path)?;
    let inherited_pythonpath = std::env::var_os("PYTHONPATH").unwrap_or_default();
    let runtime_pythonpath = development_root.join("src");
    let mut command = Command::new(&launcher.executable);
    if launcher.uses_python_module {
        command.arg("-m").arg("stellarcode.runtime.sidecar");
    }
    command
        .arg("--workspace")
        .arg(&workspace_path)
        .arg("--data-dir")
        .arg(&runtime_data_dir)
        .arg("--worktree-dir")
        .arg(settings.general.worktree_directory.trim())
        .arg("--max-iterations")
        .arg(settings.agent.max_iterations.to_string())
        .arg("--max-parallel-tools")
        .arg(settings.agent.max_parallel_tools.to_string())
        .arg("--tool-batch-timeout")
        .arg(settings.agent.tool_batch_timeout_seconds.to_string())
        .arg("--plan-workers")
        .arg(settings.agent.plan_workers.to_string())
        .arg("--team-workers")
        .arg(settings.agent.team_workers.to_string())
        .arg("--team-retries")
        .arg(settings.agent.team_retries.to_string())
        .arg("--context-window")
        .arg(settings.agent.context_window.to_string())
        .arg("--rag-auto-retrieval")
        .arg(settings.rag.automatic_retrieval.to_string())
        .arg("--diagnostics-lsp-enabled")
        .arg(settings.diagnostics.lsp_enabled.to_string())
        .arg("--diagnostics-lsp-command")
        .arg(&settings.diagnostics.lsp_command)
        .arg("--diagnostics-lsp-args-json")
        .arg(
            serde_json::to_string(&settings.diagnostics.lsp_args)
                .map_err(|error| format!("failed to encode LSP arguments: {error}"))?,
        )
        .arg("--diagnostics-lsp-timeout")
        .arg(settings.diagnostics.lsp_timeout_seconds.to_string())
        .current_dir(if launcher.uses_python_module {
            root.as_path()
        } else {
            workspace_path.as_path()
        })
        .env("STELLARCODE_ENV_FILE", settings_store.environment_path())
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if launcher.uses_python_module {
        command
            .env("PYTHONPATH", &runtime_pythonpath)
            .env("STELLARCODE_RUNTIME_PYTHONPATH", &runtime_pythonpath)
            .env("STELLARCODE_TOOL_PYTHONPATH", inherited_pythonpath);
    }
    apply_model_settings(&mut command, &settings);
    apply_rag_settings(&mut command, &settings);
    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);

    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to start StellarCode runtime with {}: {error}",
            launcher.executable.display()
        )
    })?;
    let pid = child.id();
    let stdin = child.stdin.take().ok_or("runtime stdin is unavailable")?;
    let stdout = child.stdout.take().ok_or("runtime stdout is unavailable")?;
    let stderr = child.stderr.take().ok_or("runtime stderr is unavailable")?;

    let event_app = app.clone();
    std::thread::spawn(move || {
        // stdout is reserved for one JSON envelope per line. Keep parsing in a
        // dedicated reader so a slow React render never blocks Python workers.
        for line in BufReader::new(stdout).lines() {
            match line {
                Ok(line) => match serde_json::from_str::<Value>(&line) {
                    Ok(mut message) => {
                        tag_runtime_message(&mut message, pid);
                        let _ = event_app.emit("runtime-message", message);
                    }
                    Err(error) => {
                        let _ = event_app.emit(
                            "runtime-transport-error",
                            format!("invalid JSON from sidecar: {error}"),
                        );
                    }
                },
                Err(error) => {
                    let _ = event_app.emit("runtime-transport-error", error.to_string());
                    break;
                }
            }
        }
        let _ = event_app.emit("runtime-exited", pid);
    });

    let log_app = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            let _ = log_app.emit("runtime-log", line);
        }
    });

    *slot = Some(RuntimeProcess { child, stdin });
    Ok(RuntimeStartResult {
        workspace: workspace_path.display().to_string(),
        python: launcher.executable.display().to_string(),
        pid,
    })
}

fn apply_model_settings(command: &mut Command, settings: &AppSettings) {
    let models = &settings.models;
    if models.provider != "environment" {
        command.env("LLM_PROVIDER", &models.provider);
        if !models.model.trim().is_empty() {
            let variable = match models.provider.as_str() {
                "deepseek" => "DEEPSEEK_MODEL",
                "glm" => "GLM_MODEL",
                "agnes" => "AGNES_MODEL",
                _ => return,
            };
            command.env(variable, models.model.trim());
        }
        if !models.base_url.trim().is_empty() {
            let variable = match models.provider.as_str() {
                "deepseek" => "DEEPSEEK_BASE_URL",
                "glm" => "GLM_BASE_URL",
                "agnes" => "AGNES_BASE_URL",
                _ => return,
            };
            command.env(variable, models.base_url.trim());
        }
    }
    if models.vision_provider != "environment" {
        command.env("VISION_PROVIDER", &models.vision_provider);
        if !models.vision_model.trim().is_empty() {
            let variable = match models.vision_provider.as_str() {
                "glm" => Some("GLM_VISION_MODEL"),
                "agnes" => Some("AGNES_VISION_MODEL"),
                _ => None,
            };
            if let Some(variable) = variable {
                command.env(variable, models.vision_model.trim());
            }
        }
    }
}

fn apply_rag_settings(command: &mut Command, settings: &AppSettings) {
    let rag = &settings.rag;
    if rag.provider == "environment" {
        return;
    }
    command.env("EMBEDDING_PROVIDER", &rag.provider);
    if !rag.model.trim().is_empty() {
        command.env("EMBEDDING_MODEL", rag.model.trim());
    }
    if !rag.base_url.trim().is_empty() {
        command.env("EMBEDDING_BASE_URL", rag.base_url.trim());
    }
}

#[tauri::command]
fn runtime_send(state: State<'_, RuntimeState>, message: Value) -> Result<(), String> {
    let mut slot = state.0.lock().map_err(|_| "runtime state is poisoned")?;
    let process = slot.as_mut().ok_or("StellarCode runtime is not running")?;
    // The same newline-delimited framing is used in both directions; Python
    // flushes one response/event per line and can process requests incrementally.
    let encoded = serde_json::to_string(&message).map_err(|error| error.to_string())?;
    process
        .stdin
        .write_all(format!("{encoded}\n").as_bytes())
        .and_then(|_| process.stdin.flush())
        .map_err(|error| format!("failed to write to runtime: {error}"))
}

#[tauri::command]
fn runtime_stop(state: State<'_, RuntimeState>) -> Result<(), String> {
    let mut slot = state.0.lock().map_err(|_| "runtime state is poisoned")?;
    if let Some(mut process) = slot.take() {
        let shutdown = serde_json::json!({
            "kind": "request",
            "protocol_version": 1,
            "request_id": "desktop-shutdown",
            "method": "runtime.shutdown",
            "params": {}
        });
        if let Ok(encoded) = serde_json::to_string(&shutdown) {
            let _ = writeln!(process.stdin, "{encoded}");
            let _ = process.stdin.flush();
        }
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        loop {
            if process
                .child
                .try_wait()
                .map_err(|error| error.to_string())?
                .is_some()
            {
                break;
            }
            if std::time::Instant::now() >= deadline {
                terminate_runtime_tree(&mut process.child)?;
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
    }
    Ok(())
}

#[cfg(target_os = "windows")]
fn terminate_runtime_tree(child: &mut Child) -> Result<(), String> {
    let system_root = std::env::var_os("SystemRoot")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
    let taskkill = system_root.join("System32").join("taskkill.exe");
    let status = Command::new(taskkill)
        .args(["/PID", &child.id().to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    if status.as_ref().is_ok_and(std::process::ExitStatus::success) {
        let _ = child.wait();
        return Ok(());
    }
    child.kill().map_err(|error| {
        format!(
            "failed to terminate Python runtime process tree: {error}; taskkill status: {status:?}"
        )
    })
}

#[cfg(not(target_os = "windows"))]
fn terminate_runtime_tree(child: &mut Child) -> Result<(), String> {
    child.kill().map_err(|error| error.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(RuntimeState::default())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let app_data_dir = app.path().app_data_dir()?;
            let development_root = project_root();
            let (root, development_project) = if cfg!(debug_assertions) {
                let development_project = development_root
                    .join("pyproject.toml")
                    .exists()
                    .then_some(development_root.clone());
                (development_root, development_project)
            } else {
                // A release must not rely on the build machine's source tree.
                // Keep the user-editable `.env` beside settings in app data.
                (app_data_dir.clone(), None)
            };
            ensure_environment_file(&root)?;
            let store = ProjectStore::load(&app_data_dir, development_project.as_deref())
                .map_err(std::io::Error::other)?;
            let settings =
                SettingsStore::load(&app_data_dir, &root).map_err(std::io::Error::other)?;
            app.manage(store);
            app.manage(settings);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            project_list,
            project_register,
            project_create,
            project_remove,
            project_touch,
            workspace_list_entries,
            workspace_file_open,
            workspace_file_preview,
            settings_get,
            settings_update,
            settings_reset,
            skill_directory_open,
            attachment_inspect,
            attachment_preview_data,
            runtime_start,
            runtime_send,
            runtime_stop
        ])
        .run(tauri::generate_context!())
        .expect("error while running StellarCode desktop");
}

#[cfg(test)]
mod attachment_tests {
    use super::{
        apply_model_settings, apply_rag_settings, attachment_type, decode_text_preview,
        encode_base64, is_reviewable_text_file, tag_runtime_message,
    };
    use crate::settings_store::AppSettings;
    use std::path::Path;
    use std::process::Command;

    #[test]
    fn classifies_image_text_and_binary_attachments() {
        assert_eq!(
            attachment_type(Path::new("diagram.png")),
            ("image", "image/png")
        );
        assert_eq!(
            attachment_type(Path::new("agent.py")),
            ("file", "text/plain")
        );
        assert_eq!(
            attachment_type(Path::new("archive.zip")),
            ("file", "application/octet-stream")
        );
    }

    #[test]
    fn encodes_preview_bytes_as_base64() {
        assert_eq!(encode_base64(b""), "");
        assert_eq!(encode_base64(b"f"), "Zg==");
        assert_eq!(encode_base64(b"fo"), "Zm8=");
        assert_eq!(encode_base64(b"foo"), "Zm9v");
    }

    #[test]
    fn only_allows_reviewable_text_files_to_open() {
        assert!(is_reviewable_text_file(Path::new("src/agent.py")));
        assert!(is_reviewable_text_file(Path::new("README.md")));
        assert!(is_reviewable_text_file(Path::new(".env.local")));
        assert!(is_reviewable_text_file(Path::new("trace/session.jsonl")));
        assert!(!is_reviewable_text_file(Path::new("build/agent.exe")));
        assert!(!is_reviewable_text_file(Path::new("assets/archive.zip")));
    }

    #[test]
    fn decodes_utf8_and_utf16_workspace_previews() {
        assert_eq!(decode_text_preview(b"hello"), "hello");
        assert_eq!(decode_text_preview(&[0xef, 0xbb, 0xbf, b'o', b'k']), "ok");
        assert_eq!(decode_text_preview(&[0xff, 0xfe, b'h', 0, b'i', 0]), "hi");
        assert_eq!(decode_text_preview(&[0xfe, 0xff, 0, b'h', 0, b'i']), "hi");
    }

    #[test]
    fn applies_non_secret_model_overrides_to_runtime_process() {
        let mut settings = AppSettings::default();
        settings.models.provider = "deepseek".into();
        settings.models.model = "deepseek-test".into();
        settings.models.base_url = "https://example.test/v1".into();
        settings.models.vision_provider = "glm".into();
        settings.models.vision_model = "glm-vision-test".into();
        let mut command = Command::new("python");

        apply_model_settings(&mut command, &settings);

        let values = command
            .get_envs()
            .filter_map(|(key, value)| Some((key.to_str()?, value?.to_str()?)))
            .collect::<std::collections::HashMap<_, _>>();
        assert_eq!(values.get("LLM_PROVIDER"), Some(&"deepseek"));
        assert_eq!(values.get("DEEPSEEK_MODEL"), Some(&"deepseek-test"));
        assert_eq!(values.get("VISION_PROVIDER"), Some(&"glm"));
        assert_eq!(values.get("GLM_VISION_MODEL"), Some(&"glm-vision-test"));
    }

    #[test]
    fn applies_non_secret_rag_overrides_to_runtime_process() {
        let mut settings = AppSettings::default();
        settings.rag.provider = "ollama".into();
        settings.rag.model = "nomic-embed-text".into();
        settings.rag.base_url = "http://localhost:11434".into();
        let mut command = Command::new("python");

        apply_rag_settings(&mut command, &settings);

        let values = command
            .get_envs()
            .filter_map(|(key, value)| Some((key.to_str()?, value?.to_str()?)))
            .collect::<std::collections::HashMap<_, _>>();
        assert_eq!(values.get("EMBEDDING_PROVIDER"), Some(&"ollama"));
        assert_eq!(values.get("EMBEDDING_MODEL"), Some(&"nomic-embed-text"));
        assert_eq!(
            values.get("EMBEDDING_BASE_URL"),
            Some(&"http://localhost:11434")
        );
    }

    #[test]
    fn tags_forwarded_runtime_messages_with_their_process_id() {
        let mut message = serde_json::json!({
            "kind": "event",
            "type": "runtime.ready",
            "sequence": 1
        });

        tag_runtime_message(&mut message, 4242);

        assert_eq!(message["runtime_pid"], 4242);
        assert_eq!(message["type"], "runtime.ready");
    }
}
