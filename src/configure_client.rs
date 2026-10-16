//! Client configuration edits. Client trust and tool approvals remain client-owned.

mod launch;

use std::fs;
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};

use serde_json::json;
use toml_edit::{Array, DocumentMut, Item, Table, value};

use crate::cli::{ConfigureClient, ConfigureClientOptions, ConfigureClientScope};
use launch::Launch;

struct Configuration {
    client: ConfigureClient,
    path: PathBuf,
    updated: String,
    snippet: String,
    changed: bool,
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
    let git_root = directory
        .ancestors()
        .find(|ancestor| ancestor.join(".git").exists());
    let project = match options.scope {
        ConfigureClientScope::Auto => git_root,
        ConfigureClientScope::Project => Some(git_root.unwrap_or(&directory)),
        ConfigureClientScope::Global => None,
    };
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
        let previous = match fs::read_to_string(&path) {
            Ok(source) => Some(source),
            Err(error) if error.kind() == ErrorKind::NotFound => None,
            Err(error) => return Err(format!("Cannot read {}: {error}", path.display())),
        };
        let (updated, snippet) = match client {
            ConfigureClient::Codex => edit_codex(previous.as_deref(), options, &launch),
            ConfigureClient::Claude => edit_claude(previous.as_deref(), options, &launch),
        }
        .map_err(|error| format!("Cannot configure {}: {error}", path.display()))?;
        let changed = previous.as_deref() != Some(updated.as_str());
        plans.push(Configuration {
            client,
            path,
            updated,
            snippet,
            changed,
        });
    }
    launch.prepare(options.dry_run)?;
    for plan in plans {
        if plan.changed && !options.dry_run {
            write_configuration(&plan.path, &plan.updated)
                .map_err(|error| format!("Cannot write {}: {error}", plan.path.display()))?;
        }
        print!("{}", report(&plan, options, scope));
    }
    Ok(())
}

fn report(plan: &Configuration, options: &ConfigureClientOptions, scope: &str) -> String {
    let action = if options.dry_run {
        "Would configure"
    } else if plan.changed {
        "Configured"
    } else {
        "Already configured"
    };
    let client = match plan.client {
        ConfigureClient::Codex => "Codex",
        ConfigureClient::Claude => "Claude Code",
    };
    let mut output = format!(
        "{action} Console for {client} ({scope}).\nConfiguration: {}\n\nSet these fields:\n{}\n",
        plan.path.display(),
        plan.snippet,
    );
    if !options.no_auto_load {
        output.push_str("Console tools are configured to load up front. Use --no-auto-load to keep the client's default tool discovery.\n");
    } else {
        output.push_str("Console tools use the client's default tool discovery.\n");
    }
    if options.dry_run {
        output.push_str("Dry run; no files changed. You can apply the settings above manually.\n");
    } else {
        output.push_str(&format!("Restart {client} to load the configuration.\n"));
    }
    if scope == "project" {
        output.push_str(match plan.client {
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
        value(launch.args.iter().map(String::as_str).collect::<Array>()),
    );
    desired.insert("enabled", value(true));
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
    for (key, item) in desired.iter() {
        if let Some(existing) = console.get_mut(key) {
            let mut replacement = item.clone();
            if let (Some(old), Some(new)) = (existing.as_value(), replacement.as_value_mut()) {
                // Managed arrays are replaced in full. Keep outer decoration,
                // but comments inside the old arrays leave with their contents.
                *new.decor_mut() = old.decor().clone();
            }
            // Updating the item in place also keeps the key's own comments.
            *existing = replacement;
        } else {
            console.insert(key, item.clone());
        }
    }
    let mut snippet = DocumentMut::new();
    let mut parent = Table::new();
    parent.set_implicit(true);
    snippet["mcp_servers"] = Item::Table(parent);
    snippet["mcp_servers"][&options.name] = Item::Table(desired);
    Ok((document.to_string(), snippet.to_string()))
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
        "args": launch.args,
        "alwaysLoad": !options.no_auto_load,
    });
    console.extend(desired.as_object().unwrap().clone());
    let snippet = serde_json::to_string_pretty(&json!({"mcpServers": {&options.name: desired}}))
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
