//! Public configuration sources and command-line layering with runtime shorthands.

use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

pub(crate) mod init;
mod inline;
mod yaml;

pub struct Loaded {
    pub value: Option<Value>,
    pub paths: Vec<PathBuf>,
}

/// Apply defaults, read optional mappings, then apply command-line overrides.
/// Assignments within each CLI layer retain their argument order.
/// Absence is distinct from an explicitly supplied empty configuration.
/// Relative source paths use the captured launch directory.
/// Explicitly selected files are required; discovered files may be absent.
pub fn load(
    directory: &Path,
    paths: &[PathBuf],
    defaults: &[String],
    overrides: &[String],
    required: bool,
) -> Result<Loaded, String> {
    let mut value = None;
    apply_values(&mut value, defaults, "default")?;
    let mut sources = Vec::new();
    let mut identities = std::collections::HashSet::new();
    for path in paths {
        let expanded = crate::console_paths::expand_home(path).map_err(|error| {
            format!("cannot resolve configuration '{}': {error}", path.display())
        })?;
        let absolute = directory.join(expanded);
        match std::fs::symlink_metadata(&absolute) {
            Err(error)
                if !required
                    && matches!(
                        error.kind(),
                        std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                    ) =>
            {
                continue;
            }
            Err(error) => return Err(format!("cannot inspect '{}': {error}", path.display())),
            Ok(_) => {
                // Follow links, but reject non-regular targets before opening:
                // reading a FIFO, for example, could block launcher startup.
                let metadata = std::fs::metadata(&absolute)
                    .map_err(|error| format!("cannot inspect '{}': {error}", path.display()))?;
                if !metadata.is_file() {
                    return Err(format!(
                        "configuration '{}' must be a regular file",
                        path.display()
                    ));
                }
                let identity = std::fs::canonicalize(&absolute)
                    .map_err(|error| format!("cannot resolve '{}': {error}", path.display()))?;
                if !identities.insert(identity) {
                    continue;
                }
                let source = std::fs::read_to_string(&absolute)
                    .map_err(|error| format!("cannot read '{}': {error}", path.display()))?;
                let mut overlay =
                    yaml::load(&source).map_err(|error| format!("{}: {error}", path.display()))?;
                expand_runtime_shorthands(&mut overlay);
                merge(
                    value.get_or_insert_with(|| Value::Object(Map::new())),
                    overlay,
                );
                sources.push(path.clone());
            }
        }
    }
    apply_values(&mut value, overrides, "override")?;
    Ok(Loaded {
        value,
        paths: sources,
    })
}

fn apply_values(value: &mut Option<Value>, arguments: &[String], kind: &str) -> Result<(), String> {
    for argument in arguments {
        let (key, source) = argument
            .split_once('=')
            .ok_or_else(|| format!("invalid configuration {kind}: expected KEY=VALUE"))?;
        let key = key.trim();
        let context = |error| format!("invalid configuration {kind} '{key}': {error}");
        let keys: Vec<_> = key.split('.').map(str::trim).collect();
        if keys.iter().any(|key| key.is_empty()) {
            return Err(context("expected a nonempty dotted key".into()));
        }
        let mut overlay = inline::parse(source).map_err(context)?;
        for key in keys.into_iter().rev() {
            overlay = Value::Object(Map::from_iter([(key.to_string(), overlay)]));
        }
        expand_runtime_shorthands(&mut overlay);
        merge(
            value.get_or_insert_with(|| Value::Object(Map::new())),
            overlay,
        );
    }
    Ok(())
}

/// Select one public configuration document from the launch environment.
/// It replaces file discovery and retains the ordinary CLI precedence.
pub fn load_environment(
    name: &str,
    defaults: &[String],
    overrides: &[String],
) -> Result<Value, String> {
    let source = std::env::var(name)
        .map_err(|error| format!("cannot read configuration environment '{name}': {error}"))?;
    let mut overlay = yaml::load(&source)
        .map_err(|error| format!("configuration environment '{name}': {error}"))?;
    expand_runtime_shorthands(&mut overlay);
    let mut value = None;
    apply_values(&mut value, defaults, "default")?;
    merge(
        value.get_or_insert_with(|| Value::Object(Map::new())),
        overlay,
    );
    apply_values(&mut value, overrides, "override")?;
    Ok(value.expect("selected configuration document"))
}

// Normalize equivalent runtime forms before merging each input layer. Keep the
// existing managed representation for typed capture and configuration exports.
fn expand_runtime_shorthands(value: &mut Value) {
    if let Some(r) = value.get_mut("r")
        && r.is_string()
    {
        *r = serde_json::json!({"executable": r.take()});
    }
    if let Some(python) = value.get_mut("python")
        && let Some(mapping) = python.as_object()
    {
        let selector = ["existing", "managed", "first_available"]
            .into_iter()
            .find(|key| mapping.contains_key(*key));
        let field = ["packages", "version", "resolution"]
            .into_iter()
            .find(|key| mapping.contains_key(*key));
        if selector.is_none() && (field.is_some() || mapping.is_empty()) {
            *python = serde_json::json!({"managed": python.take()});
        }
    }
}

/// Objects merge recursively; arrays, scalars, and null replace the old value.
/// Keys have no special meaning, including discriminator or profile fields.
fn merge(base: &mut Value, overlay: Value) {
    match (base, overlay) {
        (Value::Object(base), Value::Object(overlay)) => {
            for (key, value) in overlay {
                merge(base.entry(key).or_insert(Value::Null), value);
            }
        }
        (base, value) => *base = value,
    }
}

#[cfg(test)]
mod tests {
    use super::load;
    use serde_json::json;
    use std::path::PathBuf;

    #[test]
    fn absent_and_empty_sources_remain_distinct() {
        let directory = tempfile::tempdir().unwrap();
        let paths = [PathBuf::from("global.yaml"), PathBuf::from("project.yaml")];
        let absent = load(directory.path(), &paths, &[], &[], false).unwrap();
        assert_eq!(absent.value, None);
        assert!(absent.paths.is_empty());
        std::fs::write(directory.path().join(&paths[0]), "{}").unwrap();
        let empty = load(directory.path(), &paths, &[], &[], false).unwrap();
        assert_eq!(empty.value, Some(json!({})));
        assert_eq!(empty.paths, paths[..1]);
    }

    #[test]
    fn ordered_sources_and_overrides_merge_raw_values() {
        let directory = tempfile::tempdir().unwrap();
        let paths = [PathBuf::from("global.yaml"), PathBuf::from("project.yaml")];
        std::fs::write(
            directory.path().join(&paths[0]),
            r#"
environment: {KEEP: global, CHANGE: global}
nested: {keep: true, empty: {keep: true}, list: [global], clear: [old]}
reset: {old: true}
"#,
        )
        .unwrap();
        std::fs::write(
            directory.path().join(&paths[1]),
            r#"
environment: {CHANGE: project, ADD: project}
nested: {empty: {}, list: [project], clear: []}
reset: null
"#,
        )
        .unwrap();
        let layered = load(directory.path(), &paths, &[], &[], false).unwrap();
        assert_eq!(layered.paths, paths);
        assert_eq!(
            layered.value,
            Some(json!({
                "environment": {"KEEP": "global", "CHANGE": "project", "ADD": "project"},
                "nested": {"keep": true, "empty": {"keep": true}, "list": ["project"], "clear": []},
                "reset": null,
            }))
        );
        let arguments = [
            "environment.CHANGE=first",
            "reset={new: true}",
            "environment.CHANGE=last",
        ]
        .map(String::from);
        let overridden = load(directory.path(), &paths, &[], &arguments, false).unwrap();
        let mut expected = layered.value.unwrap();
        expected["environment"]["CHANGE"] = json!("last");
        expected["reset"] = json!({"new": true});
        assert_eq!(overridden.value, Some(expected));
        std::fs::write(directory.path().join(&paths[1]), "{}").unwrap();
        let empty_project = load(directory.path(), &paths, &[], &[], false).unwrap();
        let global_only = load(directory.path(), &paths[..1], &[], &[], false).unwrap();
        assert_eq!(empty_project.value, global_only.value);
    }

    #[test]
    fn each_selected_file_must_parse_as_a_mapping() {
        let directory = tempfile::tempdir().unwrap();
        let paths = [PathBuf::from("global.yaml"), PathBuf::from("project.yaml")];
        std::fs::write(directory.path().join(&paths[1]), "{}").unwrap();
        for source in ["sandbox: [", "[]", "null", "---\n{}\n---\n{}", ""] {
            std::fs::write(directory.path().join(&paths[0]), source).unwrap();
            let error = load(directory.path(), &paths, &[], &["sandbox={}".into()], false)
                .err()
                .unwrap();
            assert!(error.contains("global.yaml"), "{error}");
        }
    }

    #[test]
    fn rejects_non_regular_sources_and_deduplicates_identical_paths() {
        let directory = tempfile::tempdir().unwrap();
        let path = PathBuf::from("config.yaml");
        std::fs::create_dir(directory.path().join(&path)).unwrap();
        assert!(
            load(
                directory.path(),
                std::slice::from_ref(&path),
                &[],
                &[],
                false
            )
            .err()
            .unwrap()
            .contains("must be a regular file")
        );
        std::fs::remove_dir(directory.path().join(&path)).unwrap();
        std::fs::write(directory.path().join(&path), "{}").unwrap();
        let loaded = load(
            directory.path(),
            &[directory.path().join(&path), path],
            &[],
            &[],
            false,
        )
        .unwrap();
        assert_eq!(loaded.paths.len(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn follows_regular_symlinks_once_and_rejects_dangling_links_fifos_and_devices() {
        let directory = tempfile::tempdir().unwrap();
        let global = directory.path().join("global.yaml");
        let project = directory.path().join("project.yaml");
        let target = directory.path().join("target.yaml");
        std::fs::write(&target, "{}").unwrap();
        std::os::unix::fs::symlink(&target, &global).unwrap();
        std::os::unix::fs::symlink(&target, &project).unwrap();
        let loaded = load(
            directory.path(),
            &[global.clone(), project.clone()],
            &[],
            &[],
            false,
        )
        .unwrap();
        assert_eq!(loaded.paths.as_slice(), std::slice::from_ref(&global));
        std::fs::remove_file(&target).unwrap();
        let error = load(directory.path(), &[project], &[], &[], false)
            .err()
            .unwrap();
        assert!(
            error.contains("project.yaml") && error.contains("cannot inspect"),
            "{error}"
        );
        let target_c = std::ffi::CString::new(target.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(target_c.as_ptr(), 0o600) }, 0);
        for path in [global, PathBuf::from("/dev/null")] {
            let error = load(directory.path(), &[path], &[], &[], false)
                .err()
                .unwrap();
            assert!(error.contains("must be a regular file"), "{error}");
        }
    }
}
