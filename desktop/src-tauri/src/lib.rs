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

const USER_ENV_TEMPLATE: &str = "# StellarCode user configuration (secrets stay on this device)\n\
# Configure any OpenAI-compatible Chat Completions endpoint.\n\
# LLM_API_KEY=\n\
# LLM_BASE_URL=\n\
# LLM_MODEL_NAME=\n\
\n\
# Optional OpenAI-compatible vision endpoint. Leave URL and model empty to disable.\n\
# VISION_API_KEY=\n\
# VISION_BASE_URL=\n\
# VISION_MODEL_NAME=\n\
\n\
# Optional OpenAI-compatible embedding endpoint. Leave URL empty for local hashing.\n\
# EMBEDDING_API_KEY=\n\
# EMBEDDING_BASE_URL=\n\
# EMBEDDING_MODEL_NAME=\n";

const LEGACY_USER_ENV_TEMPLATE: &str =
    "# StellarCode user configuration (API keys are stored locally)\n\
# Choose one provider and fill in its key.\n\
# LLM_PROVIDER=glm\n\
# GLM_API_KEY=\n\
# DEEPSEEK_API_KEY=\n\
# AGNES_API_KEY=\n\
# EMBEDDING_API_KEY=\n";

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
const MAX_WORKSPACE_FILE_SEARCH_ENTRIES: usize = 25_000;
const WORKSPACE_FILE_SEARCH_IGNORED_DIRS: &[&str] = &[
    ".git",
    ".venv",
    "node_modules",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
];

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
    let resolved = match fs::canonicalize(root.join(relative)) {
        Ok(path) => path,
        Err(exact_error) => {
            resolve_unique_workspace_file_suffix(&root, relative)?.ok_or_else(|| {
                format!("cannot resolve reviewed file {relative_path}: {exact_error}")
            })?
        }
    };
    if !resolved.starts_with(&root) || !resolved.is_file() {
        return Err("reviewed file is outside the project workspace or is not a file".into());
    }
    if !is_reviewable_text_file(&resolved) {
        return Err("only recognized source, configuration, and text files can be opened in the desktop file viewer".into());
    }
    Ok(resolved)
}

/// Resolves model-rendered shorthand such as `base.py` to `tools/base.py` only
/// when exactly one safe workspace file has that path suffix. This keeps file
/// links useful without guessing when two directories contain the same name.
fn resolve_unique_workspace_file_suffix(
    root: &Path,
    requested: &Path,
) -> Result<Option<PathBuf>, String> {
    let mut directories = vec![root.to_path_buf()];
    let mut visited = 0usize;
    let mut match_path: Option<PathBuf> = None;

    while let Some(directory) = directories.pop() {
        let entries = fs::read_dir(&directory).map_err(|error| {
            format!(
                "cannot search workspace directory {}: {error}",
                directory.display()
            )
        })?;
        for entry in entries {
            let entry = entry.map_err(|error| format!("cannot search workspace files: {error}"))?;
            visited += 1;
            if visited > MAX_WORKSPACE_FILE_SEARCH_ENTRIES {
                return Err(format!(
                    "workspace file search exceeded {MAX_WORKSPACE_FILE_SEARCH_ENTRIES} entries; use a more specific path"
                ));
            }
            let file_type = entry
                .file_type()
                .map_err(|error| format!("cannot inspect workspace entry: {error}"))?;
            if file_type.is_symlink() {
                continue;
            }
            let path = entry.path();
            if file_type.is_dir() {
                let name = entry.file_name().to_string_lossy().to_ascii_lowercase();
                if !WORKSPACE_FILE_SEARCH_IGNORED_DIRS.contains(&name.as_str()) {
                    directories.push(path);
                }
                continue;
            }
            if !file_type.is_file() || !is_reviewable_text_file(&path) {
                continue;
            }
            let relative = path.strip_prefix(root).map_err(|error| {
                format!("cannot compare workspace file {}: {error}", path.display())
            })?;
            if !workspace_path_has_suffix(relative, requested) {
                continue;
            }
            let canonical = fs::canonicalize(&path).map_err(|error| {
                format!("cannot resolve reviewed file {}: {error}", path.display())
            })?;
            if !canonical.starts_with(root) {
                continue;
            }
            if match_path.is_some() {
                return Err(format!(
                    "reviewed file path {} is ambiguous; use a path including its parent directory",
                    requested.display()
                ));
            }
            match_path = Some(canonical);
        }
    }
    Ok(match_path)
}

fn workspace_path_has_suffix(candidate: &Path, requested: &Path) -> bool {
    let candidate_parts = candidate
        .components()
        .filter_map(|component| match component {
            std::path::Component::Normal(value) => Some(value.to_string_lossy().into_owned()),
            _ => None,
        })
        .collect::<Vec<_>>();
    let requested_parts = requested
        .components()
        .filter_map(|component| match component {
            std::path::Component::Normal(value) => Some(value.to_string_lossy().into_owned()),
            _ => None,
        })
        .collect::<Vec<_>>();
    if requested_parts.is_empty() || requested_parts.len() > candidate_parts.len() {
        return false;
    }
    let suffix = &candidate_parts[candidate_parts.len() - requested_parts.len()..];
    suffix.iter().zip(&requested_parts).all(|(left, right)| {
        if cfg!(windows) {
            left.eq_ignore_ascii_case(right)
        } else {
            left == right
        }
    })
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
        fs::write(env_path, USER_ENV_TEMPLATE)?;
    } else {
        let existing = fs::read_to_string(&env_path)?;
        if existing.trim() == LEGACY_USER_ENV_TEMPLATE.trim() {
            // Safely migrate the untouched template created by older releases.
            fs::write(env_path, USER_ENV_TEMPLATE)?;
        } else {
            let legacy_text_provider = env_text_value(&existing, "LLM_PROVIDER");
            let legacy_vision_provider = env_text_value(&existing, "VISION_PROVIDER");
            // Provider selection was removed in schema v2. Preserve endpoint
            // credentials while dropping obsolete selector lines.
            let mut migrated = existing
                .lines()
                .filter(|line| {
                    let key = line.trim().trim_start_matches('#').trim();
                    !key.starts_with("LLM_PROVIDER=")
                        && !key.starts_with("VISION_PROVIDER=")
                        && !key.starts_with("EMBEDDING_PROVIDER=")
                })
                .collect::<Vec<_>>()
                .join("\n");
            migrate_legacy_model_values(
                &existing,
                &mut migrated,
                &legacy_text_provider,
                &legacy_vision_provider,
            );
            if !migrated.ends_with('\n') {
                migrated.push('\n');
            }
            if !migrated.contains("LLM_API_KEY") {
                // Preserve customized legacy values and append the current
                // schema so users can migrate without losing working credentials.
                migrated.push_str("\n# Provider-free configuration (preferred)\n");
                migrated.push_str(USER_ENV_TEMPLATE);
            }
            if migrated != existing {
                fs::write(env_path, migrated)?;
            }
        }
    }
    Ok(())
}

fn migrate_legacy_model_values(
    original: &str,
    migrated: &mut String,
    text_provider: &str,
    vision_provider: &str,
) {
    let text_prefix = legacy_provider_prefix(text_provider);
    if !text_prefix.is_empty() {
        append_env_value_if_missing(
            migrated,
            "LLM_API_KEY",
            &env_text_value(original, &format!("{text_prefix}_API_KEY")),
        );
        append_env_value_if_missing(
            migrated,
            "LLM_BASE_URL",
            &env_text_value(original, &format!("{text_prefix}_BASE_URL")),
        );
        append_env_value_if_missing(
            migrated,
            "LLM_MODEL_NAME",
            &env_text_value(original, &format!("{text_prefix}_MODEL")),
        );
    }
    let vision_prefix = legacy_provider_prefix(vision_provider);
    if !vision_prefix.is_empty() {
        let legacy_vision_key =
            env_text_value(original, &format!("{vision_prefix}_VISION_API_KEY"));
        let fallback_key = env_text_value(original, &format!("{vision_prefix}_API_KEY"));
        append_env_value_if_missing(
            migrated,
            "VISION_API_KEY",
            if legacy_vision_key.is_empty() {
                &fallback_key
            } else {
                &legacy_vision_key
            },
        );
        append_env_value_if_missing(
            migrated,
            "VISION_BASE_URL",
            &env_text_value(original, &format!("{vision_prefix}_BASE_URL")),
        );
        append_env_value_if_missing(
            migrated,
            "VISION_MODEL_NAME",
            &env_text_value(original, &format!("{vision_prefix}_VISION_MODEL")),
        );
    }
    append_env_value_if_missing(
        migrated,
        "EMBEDDING_MODEL_NAME",
        &env_text_value(original, "EMBEDDING_MODEL"),
    );
}

fn append_env_value_if_missing(content: &mut String, name: &str, value: &str) {
    if value.is_empty() || !env_text_value(content, name).is_empty() {
        return;
    }
    if !content.ends_with('\n') {
        content.push('\n');
    }
    content.push_str(name);
    content.push('=');
    content.push_str(value);
    content.push('\n');
}

fn env_text_value(content: &str, name: &str) -> String {
    content
        .lines()
        .filter_map(|line| {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                return None;
            }
            let (key, value) = trimmed.split_once('=')?;
            (key.trim() == name).then(|| value.trim().trim_matches('"').to_string())
        })
        .next()
        .unwrap_or_default()
}

fn legacy_provider_prefix(value: &str) -> String {
    let normalized = value.trim().to_ascii_uppercase().replace('-', "_");
    if ["DEEPSEEK", "GLM", "AGNES"].contains(&normalized.as_str()) {
        normalized
    } else {
        String::new()
    }
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
    if !models.model.trim().is_empty() {
        command.env("LLM_MODEL_NAME", models.model.trim());
    }
    if !models.base_url.trim().is_empty() {
        command.env("LLM_BASE_URL", models.base_url.trim());
    }
    if !models.vision_model.trim().is_empty() {
        command.env("VISION_MODEL_NAME", models.vision_model.trim());
    }
    if !models.vision_base_url.trim().is_empty() {
        command.env("VISION_BASE_URL", models.vision_base_url.trim());
    }
}

fn apply_rag_settings(command: &mut Command, settings: &AppSettings) {
    let rag = &settings.rag;
    if !rag.model.trim().is_empty() {
        command.env("EMBEDDING_MODEL_NAME", rag.model.trim());
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
        encode_base64, ensure_environment_file, is_reviewable_text_file,
        resolve_reviewable_workspace_file, tag_runtime_message, LEGACY_USER_ENV_TEMPLATE,
    };
    use crate::project_store::ProjectStore;
    use crate::settings_store::AppSettings;
    use std::path::Path;
    use std::process::Command;

    #[test]
    fn creates_and_migrates_provider_neutral_environment_template() {
        let root = std::env::temp_dir().join(format!(
            "stellarcode-env-template-test-{}",
            uuid::Uuid::new_v4()
        ));

        ensure_environment_file(&root).unwrap();
        let env_path = root.join(".env");
        let created = std::fs::read_to_string(&env_path).unwrap();
        assert!(created.contains("# LLM_API_KEY="));
        assert!(created.contains("# LLM_BASE_URL="));
        assert!(created.contains("# LLM_MODEL_NAME="));
        assert!(created.contains("# VISION_MODEL_NAME="));
        assert!(created.contains("# EMBEDDING_MODEL_NAME="));
        assert!(!created.contains("# DEEPSEEK_API_KEY="));

        std::fs::write(&env_path, LEGACY_USER_ENV_TEMPLATE).unwrap();
        ensure_environment_file(&root).unwrap();
        let migrated = std::fs::read_to_string(&env_path).unwrap();
        assert!(migrated.contains("# LLM_API_KEY="));
        assert!(!migrated.contains("# DEEPSEEK_API_KEY="));

        std::fs::write(&env_path, "DEEPSEEK_API_KEY=keep-me\n").unwrap();
        ensure_environment_file(&root).unwrap();
        let preserved = std::fs::read_to_string(&env_path).unwrap();
        assert!(preserved.contains("DEEPSEEK_API_KEY=keep-me"));
        assert!(preserved.contains("# LLM_API_KEY="));

        std::fs::write(
            &env_path,
            "LLM_PROVIDER=deepseek\nLLM_API_KEY=keep-generic\nVISION_PROVIDER=disabled\n",
        )
        .unwrap();
        ensure_environment_file(&root).unwrap();
        let provider_free = std::fs::read_to_string(&env_path).unwrap();
        assert!(!provider_free.contains("LLM_PROVIDER"));
        assert!(!provider_free.contains("VISION_PROVIDER"));
        assert!(provider_free.contains("LLM_API_KEY=keep-generic"));

        std::fs::write(
            &env_path,
            "LLM_PROVIDER=deepseek\nDEEPSEEK_API_KEY=legacy-key\nDEEPSEEK_BASE_URL=https://legacy.example/v1\nDEEPSEEK_MODEL=legacy-model\n",
        )
        .unwrap();
        ensure_environment_file(&root).unwrap();
        let converted = std::fs::read_to_string(&env_path).unwrap();
        assert!(!converted.contains("LLM_PROVIDER"));
        assert!(converted.contains("LLM_API_KEY=legacy-key"));
        assert!(converted.contains("LLM_BASE_URL=https://legacy.example/v1"));
        assert!(converted.contains("LLM_MODEL_NAME=legacy-model"));

        std::fs::remove_dir_all(root).unwrap();
    }

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
    fn resolves_a_unique_nested_workspace_file_from_model_shorthand() {
        let root = std::env::temp_dir().join(format!(
            "stellarcode-nested-preview-test-{}",
            uuid::Uuid::new_v4()
        ));
        let data = root.join("data");
        let workspace = root.join("workspace");
        let tools = workspace.join("tools");
        std::fs::create_dir_all(&tools).unwrap();
        std::fs::write(tools.join("read_file.py"), "print('nested')\n").unwrap();
        let store = ProjectStore::load(&data, None).unwrap();
        let project = store.register(&workspace).unwrap();

        let resolved =
            resolve_reviewable_workspace_file(&store, &project.id, "read_file.py").unwrap();
        assert_eq!(
            resolved,
            std::fs::canonicalize(tools.join("read_file.py")).unwrap()
        );

        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rejects_ambiguous_nested_workspace_file_shorthand() {
        let root = std::env::temp_dir().join(format!(
            "stellarcode-ambiguous-preview-test-{}",
            uuid::Uuid::new_v4()
        ));
        let data = root.join("data");
        let workspace = root.join("workspace");
        std::fs::create_dir_all(workspace.join("backend")).unwrap();
        std::fs::create_dir_all(workspace.join("scripts")).unwrap();
        std::fs::write(workspace.join("backend/app.py"), "backend = True\n").unwrap();
        std::fs::write(workspace.join("scripts/app.py"), "script = True\n").unwrap();
        let store = ProjectStore::load(&data, None).unwrap();
        let project = store.register(&workspace).unwrap();

        let error = resolve_reviewable_workspace_file(&store, &project.id, "app.py").unwrap_err();
        assert!(error.contains("ambiguous"));

        std::fs::remove_dir_all(root).unwrap();
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
        settings.models.model = "deepseek-test".into();
        settings.models.base_url = "https://example.test/v1".into();
        settings.models.vision_model = "glm-vision-test".into();
        settings.models.vision_base_url = "https://vision.example.test/v1".into();
        let mut command = Command::new("python");

        apply_model_settings(&mut command, &settings);

        let values = command
            .get_envs()
            .filter_map(|(key, value)| Some((key.to_str()?, value?.to_str()?)))
            .collect::<std::collections::HashMap<_, _>>();
        assert_eq!(values.get("LLM_MODEL_NAME"), Some(&"deepseek-test"));
        assert_eq!(values.get("LLM_BASE_URL"), Some(&"https://example.test/v1"));
        assert_eq!(values.get("VISION_MODEL_NAME"), Some(&"glm-vision-test"));
        assert_eq!(
            values.get("VISION_BASE_URL"),
            Some(&"https://vision.example.test/v1")
        );
    }

    #[test]
    fn applies_non_secret_rag_overrides_to_runtime_process() {
        let mut settings = AppSettings::default();
        settings.rag.model = "nomic-embed-text".into();
        settings.rag.base_url = "http://localhost:11434".into();
        let mut command = Command::new("python");

        apply_rag_settings(&mut command, &settings);

        let values = command
            .get_envs()
            .filter_map(|(key, value)| Some((key.to_str()?, value?.to_str()?)))
            .collect::<std::collections::HashMap<_, _>>();
        assert_eq!(
            values.get("EMBEDDING_MODEL_NAME"),
            Some(&"nomic-embed-text")
        );
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
