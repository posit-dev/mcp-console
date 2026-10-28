//! Client registration and send-tool approval settings. Project trust remains client-owned.

mod launch;
mod warmup;

use std::fs;
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};

use serde_json::json;
use toml_edit::{Array, DocumentMut, Item, Table, TableLike, value};

use crate::cli::{ConfigureClient, ConfigureClientOptions};
use launch::Launch;

struct Configuration {
    client: ConfigureClient,
    files: Vec<ConfigurationFile>,
    shadowed_by: Option<PathBuf>,
}

struct ConfigurationFile {
    path: PathBuf,
    updated: String,
    snippet: String,
    changed: bool,
}

impl ConfigurationFile {
    fn prepare(
        path: PathBuf,
        edit: impl FnOnce(Option<&str>) -> Result<(String, String), String>,
    ) -> Result<Self, String> {
        let previous = match fs::read_to_string(&path) {
            Ok(source) => Some(source),
            Err(error) if error.kind() == ErrorKind::NotFound => None,
            Err(error) => return Err(format!("Cannot read {}: {error}", path.display())),
        };
        let (updated, snippet) = edit(previous.as_deref())
            .map_err(|error| format!("Cannot configure {}: {error}", path.display()))?;
        let changed = previous.as_deref() != Some(updated.as_str());
        Ok(Self {
            path,
            updated,
            snippet,
            changed,
        })
    }
}

pub(crate) fn run(options: &ConfigureClientOptions) -> Result<(), String> {
    // Claude Code skips its built-in server names at load time:
    // https://code.claude.com/docs/en/mcp#configuration-warnings
    if options.clients.contains(&ConfigureClient::Claude)
        && matches!(
            options.name.as_str(),
            "workspace" | "claude-in-chrome" | "computer-use" | "Claude Preview" | "Claude Browser"
        )
    {
        return Err(format!(
            "Server name {:?} is reserved by Claude Code; choose another --name",
            options.name
        ));
    }
    if options.clients.contains(&ConfigureClient::Codex) && options.name == "codex_apps" {
        return Err(format!(
            "Server name {:?} is reserved by Codex; choose another --name",
            options.name
        ));
    }
    if options.clients.contains(&ConfigureClient::Codex)
        && !options
            .name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_:@/.-".contains(&byte))
    {
        return Err(format!(
            "Server name {:?} must match ^[a-zA-Z0-9_:@/.-]+$ for Codex; choose another --name",
            options.name
        ));
    }
    let directory = std::env::current_dir().map_err(|error| error.to_string())?;
    let project_directory = options.directory.as_ref().map(|path| directory.join(path));
    if let Some(path) = &project_directory
        && !path.is_dir()
    {
        return Err(format!(
            "Project directory does not exist or is not a directory: {}",
            path.display()
        ));
    }
    let project = project_directory.as_deref();
    let scope = if project.is_some() {
        "project"
    } else {
        "global"
    };
    let launch = Launch::resolve(options, &directory)?;
    let mut plans: Vec<Configuration> = Vec::new();
    // Parse every selected file before installation or writes. Later filesystem
    // failures can still leave earlier files changed; report each as it succeeds.
    for &client in &options.clients {
        if plans.iter().any(|plan| plan.client == client) {
            continue;
        }
        let path = match project {
            Some(project) => match client {
                ConfigureClient::Codex => project.join(".codex").join("config.toml"),
                ConfigureClient::Claude => project.join(".mcp.json"),
            },
            None => global_path(client, &directory)?,
        };
        let registration = ConfigurationFile::prepare(path, |source| match client {
            ConfigureClient::Codex => edit_codex(source, options, &launch),
            ConfigureClient::Claude => edit_claude(source, options, &launch),
        })?;
        let shadowed_by = if project.is_none() {
            shadowing_project_configuration(
                client,
                &directory,
                &registration.path,
                &registration.updated,
                &options.name,
            )?
        } else {
            None
        };
        let mut files = Vec::new();
        if client == ConfigureClient::Claude {
            let settings = match project {
                Some(project) => project.join(".claude").join("settings.json"),
                None if std::env::var_os("CLAUDE_CONFIG_DIR")
                    .is_some_and(|value| !value.is_empty()) =>
                {
                    registration.path.with_file_name("settings.json")
                }
                None => registration
                    .path
                    .with_file_name(".claude")
                    .join("settings.json"),
            };
            files.push(ConfigurationFile::prepare(settings, |source| {
                edit_claude_permissions(source, &options.name)
            })?);
        }
        files.insert(0, registration);
        plans.push(Configuration {
            client,
            files,
            shadowed_by,
        });
    }
    launch.prepare(options.dry_run)?;
    for plan in plans {
        for file in &plan.files {
            if file.changed && !options.dry_run {
                write_configuration(&file.path, &file.updated)
                    .map_err(|error| format!("Cannot write {}: {error}", file.path.display()))?;
            }
            print!("{}", report_file(file, plan.client, options, scope));
        }
        print!("{}", report(plan.client, options, scope));
        if let Some(path) = &plan.shadowed_by {
            eprintln!(
                "Warning: project server {:?} in {} may take precedence over this global registration when loaded. Update or remove that project entry to use the intended settings.",
                options.name,
                path.display()
            );
        }
    }
    if !options.no_warmup
        && let Err(error) = warmup::start(&launch, options, project.unwrap_or(&directory))
    {
        eprintln!(
            "Warning: client configuration is saved, but background warmup could not start: {error}"
        );
    }
    Ok(())
}

fn shadowing_project_configuration(
    client: ConfigureClient,
    directory: &Path,
    global_path: &Path,
    global_config: &str,
    name: &str,
) -> Result<Option<PathBuf>, String> {
    // Resolve existing files before comparing them: client homes can contain
    // parent components or symlinks that alias a discovered project file.
    let global_path = match global_path.canonicalize() {
        Ok(path) => Some(path),
        Err(error) if error.kind() == ErrorKind::NotFound => None,
        Err(error) => {
            return Err(format!(
                "Cannot inspect global configuration {}: {error}",
                global_path.display()
            ));
        }
    };
    // This is a registration diagnostic, not a second client configuration
    // loader: trust, approvals, profiles, and managed settings remain client-owned.
    // Codex's user config can customize the ancestor-search boundary:
    // https://learn.chatgpt.com/docs/config-file/config-advanced#project-root-detection
    let project_root = match client {
        ConfigureClient::Codex => {
            let config = global_config
                .parse::<DocumentMut>()
                .map_err(|error| error.to_string())?;
            let markers = match config.get("project_root_markers") {
                Some(item) => item
                    .as_array()
                    .ok_or("project_root_markers must be an array of strings")?
                    .iter()
                    .map(|marker| {
                        marker
                            .as_str()
                            .ok_or("project_root_markers must be an array of strings")
                    })
                    .collect::<Result<Vec<_>, _>>()?,
                None => vec![".git"],
            };
            Some(
                directory
                    .ancestors()
                    .find(|ancestor| {
                        markers.iter().any(|marker| {
                            let path = ancestor.join(marker);
                            path.exists()
                                && (*marker != ".git"
                                    || !path.is_dir()
                                    || path.join("HEAD").exists())
                        })
                    })
                    .unwrap_or(directory),
            )
        }
        // Claude Code discovers .mcp.json files in ancestor directories,
        // including above a Git root; the nearest same-named entry wins.
        ConfigureClient::Claude => None,
    };
    for ancestor in directory.ancestors() {
        let path = match client {
            ConfigureClient::Codex => ancestor.join(".codex").join("config.toml"),
            ConfigureClient::Claude => ancestor.join(".mcp.json"),
        };
        let resolved_path = match path.canonicalize() {
            Ok(path) => Some(path),
            Err(error) if error.kind() == ErrorKind::NotFound => None,
            Err(error) => {
                return Err(format!(
                    "Cannot inspect project configuration {}: {error}",
                    path.display()
                ));
            }
        };
        // Hard-linked configuration paths share in-place edits. This diagnostic
        // deduplicates relative-path and symlink aliases; hard-link identity is
        // outside its supported scope.
        if resolved_path.is_some() && resolved_path != global_path {
            let source = match fs::read_to_string(&path) {
                Ok(source) => Some(source),
                Err(error) if error.kind() == ErrorKind::NotFound => None,
                Err(error) => {
                    return Err(format!(
                        "Cannot inspect project configuration {}: {error}",
                        path.display()
                    ));
                }
            };
            if let Some(source) = source {
                let contains_server = match client {
                    ConfigureClient::Codex => source
                        .parse::<DocumentMut>()
                        .map(|config| {
                            config
                                .get("mcp_servers")
                                .and_then(|servers| servers.get(name))
                                .is_some()
                        })
                        .map_err(|error| error.to_string()),
                    ConfigureClient::Claude => serde_json::from_str::<serde_json::Value>(&source)
                        .map(|config| {
                            config
                                .get("mcpServers")
                                .and_then(|servers| servers.get(name))
                                .is_some()
                        })
                        .map_err(|error| error.to_string()),
                }
                .map_err(|error| {
                    format!(
                        "Cannot inspect project configuration {}: {error}",
                        path.display()
                    )
                })?;
                if contains_server {
                    return Ok(Some(path));
                }
            }
        }
        if project_root == Some(ancestor) {
            break;
        }
    }
    Ok(None)
}

fn report_file(
    file: &ConfigurationFile,
    client: ConfigureClient,
    options: &ConfigureClientOptions,
    scope: &str,
) -> String {
    let action = if options.dry_run {
        "Would configure"
    } else if file.changed {
        "Configured"
    } else {
        "Already configured"
    };
    let client = match client {
        ConfigureClient::Codex => "Codex",
        ConfigureClient::Claude => "Claude Code",
    };
    format!(
        "{action} Console for {client} ({scope}).\nConfiguration: {}\n\nSet these fields:\n{}\n",
        file.path.display(),
        file.snippet,
    )
}

fn report(client: ConfigureClient, options: &ConfigureClientOptions, scope: &str) -> String {
    let mut output = String::new();
    if !options.no_auto_load {
        output.push_str("Console tools are configured to load up front. Use --no-auto-load to keep the client's default tool discovery.\n");
    } else {
        output.push_str("Console tools use the client's default tool discovery.\n");
    }
    output.push_str("Console's send tool is allowlisted for calls without individual approval; explicit client restrictions still apply.\n");
    if options.dry_run {
        output.push_str("Dry run; no files changed. You can apply the settings above manually.\n");
    } else {
        let client = match client {
            ConfigureClient::Codex => "Codex",
            ConfigureClient::Claude => "Claude Code",
        };
        output.push_str(&format!("Restart {client} to load the configuration.\n"));
    }
    if scope == "project" {
        output.push_str(match client {
            ConfigureClient::Codex => {
                "Codex loads project configuration only after you trust the project.\n"
            }
            ConfigureClient::Claude => "Approve the project server in Claude Code if prompted.\n",
        });
    }
    output
}

fn global_path(client: ConfigureClient, directory: &Path) -> Result<PathBuf, String> {
    let variable = match client {
        ConfigureClient::Codex => "CODEX_HOME",
        ConfigureClient::Claude => "CLAUDE_CONFIG_DIR",
    };
    let home = match std::env::var_os(variable).filter(|value| !value.is_empty()) {
        Some(home) => PathBuf::from(home),
        None => {
            let home = std::env::home_dir().ok_or("Cannot determine the user home directory")?;
            match client {
                ConfigureClient::Codex => home.join(".codex"),
                ConfigureClient::Claude => home,
            }
        }
    };
    let filename = match client {
        ConfigureClient::Codex => "config.toml",
        ConfigureClient::Claude => ".claude.json",
    };
    Ok(directory.join(home).join(filename))
}

fn edit_codex(
    source: Option<&str>,
    options: &ConfigureClientOptions,
    launch: &Launch,
) -> Result<(String, String), String> {
    let mut document = source
        .unwrap_or("")
        .parse::<DocumentMut>()
        .map_err(|error| error.to_string())?;
    let mut parent = Table::new();
    parent.set_implicit(true);
    let servers = document
        .entry("mcp_servers")
        .or_insert(Item::Table(parent))
        .as_table_like_mut()
        .ok_or("mcp_servers must be a table")?;
    let console = servers
        .entry(&options.name)
        .or_insert(Item::Table(Table::new()))
        .as_table_like_mut()
        .ok_or_else(|| format!("mcp_servers.{} must be a table", options.name))?;
    if console.contains_key("url") {
        return Err(format!(
            "mcp_servers.{} is a remote server; remove or rename it before configuring the stdio server",
            options.name
        ));
    }
    let mut desired = Table::new();
    desired.insert("command", value(launch.command.as_str()));
    desired.insert(
        "args",
        value(
            launch
                .arguments(ConfigureClient::Codex, &options.serve_args)
                .iter()
                .map(String::as_str)
                .collect::<Array>(),
        ),
    );
    desired.insert("enabled", value(true));
    // Codex's 5 KB default strips Console's argument descriptions. Leave room
    // for the full schema, including customized descriptions.
    desired.insert("tool_input_schema_max_bytes", value(1_000_000));
    // This per-server policy keeps tools directly exposed without disabling
    // tool search for other servers. Strict Code Mode Only overrides it.
    desired.insert(
        "omit_tools_from",
        value(if !options.no_auto_load {
            ["deferred"].into_iter().collect::<Array>()
        } else {
            Array::new()
        }),
    );
    let mut tools = Table::new();
    tools.set_implicit(true);
    let mut send = Table::new();
    send.insert("approval_mode", value("approve"));
    tools.insert("send", Item::Table(send));
    desired.insert("tools", Item::Table(tools));
    merge_toml(console, &desired)?;
    let mut snippet = DocumentMut::new();
    let mut parent = Table::new();
    parent.set_implicit(true);
    snippet["mcp_servers"] = Item::Table(parent);
    snippet["mcp_servers"][&options.name] = Item::Table(desired);
    Ok((document.to_string(), snippet.to_string()))
}

// Merge managed leaves without replacing other tool settings or TOML decoration.
fn merge_toml(target: &mut dyn TableLike, desired: &Table) -> Result<(), String> {
    for (key, item) in desired.iter() {
        if let Some(table) = item.as_table() {
            let existing = target.entry(key).or_insert(item.clone());
            merge_toml(
                existing
                    .as_table_like_mut()
                    .ok_or_else(|| format!("{key} must be a table"))?,
                table,
            )?;
        } else if let Some(existing) = target.get_mut(key) {
            let mut replacement = item.clone();
            if let (Some(old), Some(new)) = (existing.as_value(), replacement.as_value_mut()) {
                // Managed arrays are replaced in full. Keep outer decoration,
                // but comments inside the old arrays leave with their contents.
                *new.decor_mut() = old.decor().clone();
            }
            // Updating the item in place also keeps the key's own comments.
            *existing = replacement;
        } else {
            target.insert(key, item.clone());
        }
    }
    Ok(())
}

fn edit_claude(
    source: Option<&str>,
    options: &ConfigureClientOptions,
    launch: &Launch,
) -> Result<(String, String), String> {
    let mut document: serde_json::Value = match source {
        Some(source) => serde_json::from_str(source).map_err(|error| error.to_string())?,
        None => json!({}),
    };
    let root = document
        .as_object_mut()
        .ok_or("configuration must be a JSON object")?;
    let servers = root
        .entry("mcpServers")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or("mcpServers must be a JSON object")?;
    let console = servers
        .entry(&options.name)
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or_else(|| format!("mcpServers.{} must be a JSON object", options.name))?;
    if console.contains_key("url") || console.get("type").is_some_and(|kind| kind != "stdio") {
        return Err(format!(
            "mcpServers.{} is not a stdio server; remove or rename it before configuring",
            options.name
        ));
    }
    let desired = json!({
        "type": "stdio",
        "command": launch.command,
        "args": launch.arguments(ConfigureClient::Claude, &options.serve_args),
        "alwaysLoad": !options.no_auto_load,
    });
    console.extend(desired.as_object().unwrap().clone());
    let snippet = serde_json::to_string_pretty(&json!({"mcpServers": {&options.name: desired}}))
        .map_err(|error| error.to_string())?;
    let updated =
        serde_json::to_string_pretty(&document).map_err(|error| error.to_string())? + "\n";
    Ok((updated, snippet + "\n"))
}

fn edit_claude_permissions(source: Option<&str>, name: &str) -> Result<(String, String), String> {
    let mut document: serde_json::Value =
        serde_json::from_str(source.unwrap_or("{}")).map_err(|error| error.to_string())?;
    let permissions = document
        .as_object_mut()
        .ok_or("settings must be a JSON object")?
        .entry("permissions")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or("permissions must be a JSON object")?;
    let allow = permissions
        .entry("allow")
        .or_insert_with(|| json!([]))
        .as_array_mut()
        .ok_or("permissions.allow must be an array of strings")?;
    if !allow.iter().all(serde_json::Value::is_string) {
        return Err("permissions.allow must be an array of strings".to_owned());
    }
    // Claude exposes server names with punctuation replaced by underscores:
    // https://code.claude.com/docs/en/permissions#mcp
    let name: String = name
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '_' | '-') {
                character
            } else {
                '_'
            }
        })
        .collect();
    let rule = json!(format!("mcp__{name}__send"));
    if !allow.contains(&rule) {
        allow.push(rule);
    }
    // Print the complete merged list so the preview can also be applied manually.
    let snippet = serde_json::to_string_pretty(&json!({"permissions": {"allow": allow}}))
        .map_err(|error| error.to_string())?;
    let updated =
        serde_json::to_string_pretty(&document).map_err(|error| error.to_string())? + "\n";
    Ok((updated, snippet + "\n"))
}

fn write_configuration(path: &Path, source: &str) -> Result<(), std::io::Error> {
    // Keep existing file identity, ownership, ACLs/DACLs and symlinks. Recreating
    // only mode bits on a replacement file cannot preserve security metadata.
    // Existing writes are not atomic; assume no concurrent client/setup writer.
    match fs::symlink_metadata(path) {
        Ok(_) => {
            let mut file = fs::OpenOptions::new()
                .write(true)
                .open(path.canonicalize()?)?;
            file.write_all(source.as_bytes())?;
            return file.set_len(source.len() as u64);
        }
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let parent = path.parent().expect("configuration path is absolute");
    fs::create_dir_all(parent)?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    file.write_all(source.as_bytes())?;
    file.persist(path).map_err(|error| error.error)?;
    Ok(())
}
