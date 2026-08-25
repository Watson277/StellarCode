//! Project registry and safe workspace tree enumeration for the desktop shell.
//!
//! This module treats project paths as untrusted input: canonicalization and relative-path
//! validation prevent the UI's project browser from escaping an opened workspace.

use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::sync::{Mutex, MutexGuard};
use std::time::{SystemTime, UNIX_EPOCH};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};
use uuid::Uuid;

const STORE_SCHEMA_VERSION: u32 = 1;
const IGNORED_NAMES: &[&str] = &[
    ".git",
    ".venv",
    "node_modules",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
];

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ProjectRecord {
    pub id: String,
    pub name: String,
    pub path: String,
    pub canonical_path: String,
    pub created_at: String,
    pub last_opened_at: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct WorkspaceEntry {
    pub name: String,
    pub relative_path: String,
    pub kind: &'static str,
    pub size: Option<u64>,
    pub modified_at_ms: Option<u64>,
    pub has_children: bool,
}

#[derive(Default, Deserialize, Serialize)]
struct ProjectStoreFile {
    schema_version: u32,
    projects: Vec<ProjectRecord>,
}

pub struct ProjectStore {
    path: PathBuf,
    projects: Mutex<Vec<ProjectRecord>>,
}

impl ProjectStore {
    pub fn load(app_data_dir: &Path, development_project: Option<&Path>) -> Result<Self, String> {
        fs::create_dir_all(app_data_dir).map_err(display_error)?;
        let path = app_data_dir.join("projects.json");
        let mut projects = if path.exists() {
            let content = fs::read_to_string(&path).map_err(display_error)?;
            let parsed: ProjectStoreFile = serde_json::from_str(&content).map_err(display_error)?;
            parsed.projects
        } else {
            Vec::new()
        };
        let mut migrated_paths = false;
        for project in &mut projects {
            let normalized_path = normalize_windows_path_string(&project.path);
            let normalized_canonical = normalize_windows_path_string(&project.canonical_path);
            if normalized_path != project.path || normalized_canonical != project.canonical_path {
                project.path = normalized_path;
                project.canonical_path = normalized_canonical;
                migrated_paths = true;
            }
        }
        let store = Self {
            path,
            projects: Mutex::new(projects),
        };
        if migrated_paths {
            let snapshot = store.lock()?.clone();
            store.save(&snapshot)?;
        }
        if store.list()?.is_empty() {
            if let Some(project) = development_project.filter(|path| path.is_dir()) {
                store.register(project)?;
            }
        }
        Ok(store)
    }

    pub fn list(&self) -> Result<Vec<ProjectRecord>, String> {
        Ok(self.lock()?.clone())
    }

    pub fn get(&self, project_id: &str) -> Result<ProjectRecord, String> {
        self.lock()?
            .iter()
            .find(|project| project.id == project_id)
            .cloned()
            .ok_or_else(|| format!("project not found: {project_id}"))
    }

    pub fn register(&self, input_path: &Path) -> Result<ProjectRecord, String> {
        let canonical = canonical_directory(input_path)?;
        let canonical_key = path_key(&canonical);
        let mut projects = self.lock()?;
        if let Some(existing) = projects
            .iter_mut()
            .find(|project| project.canonical_path == canonical_key)
        {
            existing.last_opened_at = timestamp();
            let result = existing.clone();
            self.save(&projects)?;
            return Ok(result);
        }
        let now = timestamp();
        let project = ProjectRecord {
            id: format!("project-{}", Uuid::new_v4()),
            name: canonical
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("workspace")
                .to_string(),
            path: canonical.display().to_string(),
            canonical_path: canonical_key,
            created_at: now.clone(),
            last_opened_at: now,
        };
        projects.push(project.clone());
        self.save(&projects)?;
        Ok(project)
    }

    pub fn create(&self, parent: &Path, name: &str) -> Result<ProjectRecord, String> {
        validate_project_name(name)?;
        let canonical_parent = canonical_directory(parent)?;
        let target = canonical_parent.join(name);
        if target.exists() {
            return Err(format!(
                "project directory already exists: {}",
                target.display()
            ));
        }
        fs::create_dir(&target).map_err(display_error)?;
        match self.register(&target) {
            Ok(project) => Ok(project),
            Err(error) => {
                let _ = fs::remove_dir(&target);
                Err(error)
            }
        }
    }

    pub fn remove(&self, project_id: &str) -> Result<ProjectRecord, String> {
        let mut projects = self.lock()?;
        let index = projects
            .iter()
            .position(|project| project.id == project_id)
            .ok_or_else(|| format!("project not found: {project_id}"))?;
        let removed = projects.remove(index);
        self.save(&projects)?;
        Ok(removed)
    }

    pub fn touch(&self, project_id: &str) -> Result<ProjectRecord, String> {
        let mut projects = self.lock()?;
        let project = projects
            .iter_mut()
            .find(|project| project.id == project_id)
            .ok_or_else(|| format!("project not found: {project_id}"))?;
        project.last_opened_at = timestamp();
        let result = project.clone();
        self.save(&projects)?;
        Ok(result)
    }

    pub fn list_entries(
        &self,
        project_id: &str,
        relative_path: &str,
    ) -> Result<Vec<WorkspaceEntry>, String> {
        let project = self.get(project_id)?;
        let root = canonical_directory(Path::new(&project.path))?;
        let relative = safe_relative_path(relative_path)?;
        let directory =
            shell_compatible_path(&fs::canonicalize(root.join(&relative)).map_err(display_error)?);
        if !is_within(&root, &directory) || !directory.is_dir() {
            return Err("requested directory is outside the project workspace".into());
        }

        let mut entries = Vec::new();
        for item in fs::read_dir(&directory).map_err(display_error)? {
            let item = item.map_err(display_error)?;
            let name = item.file_name().to_string_lossy().to_string();
            if ignored(&name) {
                continue;
            }
            let file_type = item.file_type().map_err(display_error)?;
            if file_type.is_symlink() {
                continue;
            }
            let metadata = item.metadata().map_err(display_error)?;
            let is_directory = metadata.is_dir();
            let child_relative = relative.join(&name);
            entries.push(WorkspaceEntry {
                name,
                relative_path: portable_path(&child_relative),
                kind: if is_directory { "directory" } else { "file" },
                size: (!is_directory).then_some(metadata.len()),
                modified_at_ms: metadata.modified().ok().and_then(system_time_ms),
                has_children: is_directory && directory_has_visible_children(&item.path()),
            });
        }
        entries.sort_by(|left, right| match (left.kind, right.kind) {
            ("directory", "file") => Ordering::Less,
            ("file", "directory") => Ordering::Greater,
            _ => left.name.to_lowercase().cmp(&right.name.to_lowercase()),
        });
        Ok(entries)
    }

    fn lock(&self) -> Result<MutexGuard<'_, Vec<ProjectRecord>>, String> {
        self.projects
            .lock()
            .map_err(|_| "project store lock is poisoned".into())
    }

    fn save(&self, projects: &[ProjectRecord]) -> Result<(), String> {
        let payload = ProjectStoreFile {
            schema_version: STORE_SCHEMA_VERSION,
            projects: projects.to_vec(),
        };
        let encoded = serde_json::to_vec_pretty(&payload).map_err(display_error)?;
        let temporary = self.path.with_extension("json.tmp");
        let backup = self.path.with_extension("json.bak");
        fs::write(&temporary, encoded).map_err(display_error)?;
        if self.path.exists() {
            fs::copy(&self.path, &backup).map_err(display_error)?;
            fs::remove_file(&self.path).map_err(display_error)?;
        }
        if let Err(error) = fs::rename(&temporary, &self.path) {
            if backup.exists() {
                let _ = fs::copy(&backup, &self.path);
            }
            return Err(display_error(error));
        }
        if backup.exists() {
            let _ = fs::remove_file(backup);
        }
        Ok(())
    }
}

fn canonical_directory(path: &Path) -> Result<PathBuf, String> {
    let canonical = shell_compatible_path(&fs::canonicalize(path).map_err(display_error)?);
    if !canonical.is_dir() {
        return Err(format!("not a directory: {}", canonical.display()));
    }
    Ok(canonical)
}

pub(crate) fn shell_compatible_path(path: &Path) -> PathBuf {
    PathBuf::from(normalize_windows_path_string(&path.to_string_lossy()))
}

fn normalize_windows_path_string(value: &str) -> String {
    const VERBATIM_UNC_PREFIX: &str = r"\\?\UNC\";
    const VERBATIM_PREFIX: &str = r"\\?\";
    if value.len() >= VERBATIM_UNC_PREFIX.len()
        && value[..VERBATIM_UNC_PREFIX.len()].eq_ignore_ascii_case(VERBATIM_UNC_PREFIX)
    {
        return format!(r"\\{}", &value[VERBATIM_UNC_PREFIX.len()..]);
    }
    value
        .strip_prefix(VERBATIM_PREFIX)
        .unwrap_or(value)
        .to_string()
}

fn validate_project_name(name: &str) -> Result<(), String> {
    let trimmed = name.trim();
    if trimmed.is_empty() || trimmed == "." || trimmed == ".." {
        return Err("project name must not be empty, '.' or '..'".into());
    }
    if trimmed.contains(['/', '\\']) || trimmed.contains('\0') {
        return Err("project name must not contain path separators".into());
    }
    Ok(())
}

fn safe_relative_path(value: &str) -> Result<PathBuf, String> {
    let path = PathBuf::from(value);
    if path.is_absolute()
        || path.components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return Err("file tree path must stay inside the project workspace".into());
    }
    Ok(path)
}

fn path_key(path: &Path) -> String {
    path.display()
        .to_string()
        .replace('/', "\\")
        .trim_end_matches('\\')
        .to_lowercase()
}

fn is_within(root: &Path, candidate: &Path) -> bool {
    let root_key = path_key(root);
    let candidate_key = path_key(candidate);
    candidate_key == root_key || candidate_key.starts_with(&format!("{root_key}\\"))
}

fn ignored(name: &str) -> bool {
    IGNORED_NAMES
        .iter()
        .any(|ignored| name.eq_ignore_ascii_case(ignored))
}

fn directory_has_visible_children(path: &Path) -> bool {
    fs::read_dir(path)
        .ok()
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .any(|entry| !ignored(&entry.file_name().to_string_lossy()))
}

fn portable_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn timestamp() -> String {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_else(|_| "1970-01-01T00:00:00Z".into())
}

fn system_time_ms(value: SystemTime) -> Option<u64> {
    value
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|duration| duration.as_millis() as u64)
}

fn display_error(error: impl std::fmt::Display) -> String {
    error.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temporary_directory(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!("stellarcode-{label}-{}", Uuid::new_v4()));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn registers_projects_without_duplicates_and_never_deletes_workspace() {
        let data = temporary_directory("project-store-data");
        let workspace = temporary_directory("project-store-workspace");
        let store = ProjectStore::load(&data, None).unwrap();

        let first = store.register(&workspace).unwrap();
        let second = store.register(&workspace).unwrap();
        assert_eq!(first.id, second.id);
        assert_eq!(store.list().unwrap().len(), 1);

        store.remove(&first.id).unwrap();
        assert!(workspace.exists());
        let _ = fs::remove_dir_all(data);
        let _ = fs::remove_dir_all(workspace);
    }

    #[test]
    fn keeps_registration_order_when_a_project_is_opened() {
        let data = temporary_directory("stable-project-order-data");
        let first_workspace = temporary_directory("stable-project-order-first");
        let second_workspace = temporary_directory("stable-project-order-second");
        let store = ProjectStore::load(&data, None).unwrap();

        let first = store.register(&first_workspace).unwrap();
        let second = store.register(&second_workspace).unwrap();
        store.touch(&second.id).unwrap();

        let projects = store.list().unwrap();
        assert_eq!(projects[0].id, first.id);
        assert_eq!(projects[1].id, second.id);

        let _ = fs::remove_dir_all(data);
        let _ = fs::remove_dir_all(first_workspace);
        let _ = fs::remove_dir_all(second_workspace);
    }

    #[test]
    fn file_tree_is_lazy_sorted_and_rejects_parent_escape() {
        let data = temporary_directory("tree-data");
        let workspace = temporary_directory("tree-workspace");
        fs::create_dir(workspace.join("src")).unwrap();
        fs::create_dir(workspace.join(".git")).unwrap();
        fs::write(workspace.join("README.md"), "hello").unwrap();
        let store = ProjectStore::load(&data, None).unwrap();
        let project = store.register(&workspace).unwrap();

        let entries = store.list_entries(&project.id, "").unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].name, "src");
        assert_eq!(entries[1].name, "README.md");
        assert!(store.list_entries(&project.id, "..").is_err());

        let _ = fs::remove_dir_all(data);
        let _ = fs::remove_dir_all(workspace);
    }

    #[test]
    fn removes_windows_verbatim_prefixes_from_shell_paths() {
        assert_eq!(
            normalize_windows_path_string(r"\\?\E:\workspace\project"),
            r"E:\workspace\project"
        );
        assert_eq!(
            normalize_windows_path_string(r"\\?\UNC\server\share\project"),
            r"\\server\share\project"
        );
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn load_migrates_existing_verbatim_project_paths() {
        let data = temporary_directory("path-migration-data");
        let workspace = temporary_directory("path-migration-workspace");
        let normal_path = workspace.display().to_string();
        let verbatim_path = format!(r"\\?\{normal_path}");
        let payload = ProjectStoreFile {
            schema_version: STORE_SCHEMA_VERSION,
            projects: vec![ProjectRecord {
                id: "project-existing".into(),
                name: "existing".into(),
                path: verbatim_path.clone(),
                canonical_path: verbatim_path.to_lowercase(),
                created_at: "2026-01-01T00:00:00Z".into(),
                last_opened_at: "2026-01-01T00:00:00Z".into(),
            }],
        };
        fs::write(
            data.join("projects.json"),
            serde_json::to_vec_pretty(&payload).unwrap(),
        )
        .unwrap();

        let store = ProjectStore::load(&data, None).unwrap();
        let project = store.get("project-existing").unwrap();
        assert_eq!(project.path, normal_path);
        assert!(!project.canonical_path.starts_with(r"\\?\"));
        let persisted = fs::read_to_string(data.join("projects.json")).unwrap();
        assert!(!persisted.contains(r"\\\\?\\"));

        let _ = fs::remove_dir_all(data);
        let _ = fs::remove_dir_all(workspace);
    }
}
