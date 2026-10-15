//! Export public configuration using the same capture and presentation as serve.

use saphyr::{LoadableYamlNode, Yaml, YamlEmitter};
use serde_json::{Value, json};

use super::{Cache, SandboxSettings};

pub(crate) fn configuration(
    captured: super::Captured,
    mut value: Value,
    policy: &SandboxSettings,
    no_sandbox: bool,
    defaults: Option<&mut Value>,
) -> Result<Value, String> {
    if no_sandbox && (captured.sandbox_requested || captured.resolver_sandbox_requested) {
        return Err("explicit sandbox or resolver.sandbox permissions require sandboxing; remove them when using --no-sandbox".into());
    }
    let cache = captured.cache.unwrap_or(if no_sandbox {
        Cache::Host
    } else {
        Cache::Console
    });
    if no_sandbox && matches!(cache, Cache::Console) {
        return Err("cache: console requires sandboxing; use cache: host with --no-sandbox".into());
    }
    let languages = captured
        .languages
        .map(Ok)
        .unwrap_or_else(crate::cell::Languages::from_environment)?;
    if let Some(defaults) = defaults {
        // Removing explicit prose is safe only when this launch would regenerate
        // the same text. Visibility defaults still remain enabled for pruning.
        defaults["tool"] = serde_json::to_value(crate::server::materialized_tool(
            languages,
            policy,
            no_sandbox,
            &captured.tool,
            false,
        ))
        .expect("tool defaults");
    }
    let tool =
        crate::server::materialized_tool(languages, policy, no_sandbox, &captured.tool, true);
    value["tool"] = serde_json::to_value(tool).expect("tool configuration");
    value
        .as_object_mut()
        .expect("configuration mapping")
        .entry("startup")
        .or_insert(Value::Null);
    value["languages"] = json!(languages.fields());
    value["cache"] = json!(match cache {
        Cache::Console => "console",
        Cache::Host => "host",
    });
    value["inherit_environment"] =
        json!(captured.policy.get("inherit_environment") != Some(&Value::Bool(false)));
    value["environment"] = captured
        .policy
        .get("environment")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let r = captured.r.unwrap_or_default();
    value["r"] = json!({ "vanilla": r.vanilla, "resolution": r.resolution.name() });
    if let Some(path) = r.executable {
        value["r"]["executable"] = serde_json::to_value(path)
            .map_err(|error| format!("cannot export r.executable: {error}"))?;
    }
    if let Some(packages) = r.packages {
        value["r"]["packages"] = json!(packages);
    }
    // Runtime-dependent selections and package defaults remain policies. Do not
    // run discovery/preparation merely to print configuration.
    // A fallback chain remains a launch policy; its current winner must not
    // replace active_venv or the remaining existing/managed candidates.
    if value["python"].get("first_available").is_none() {
        value["python"] = match captured.python {
            None => Value::Null,
            Some(python) => match python.executable {
                Some(path) => json!({ "existing": serde_json::to_value(path)
                    .map_err(|error| format!("cannot export python.existing: {error}"))? }),
                None => {
                    let mut managed = json!({ "resolution": python.managed.resolution.name() });
                    if let Some(packages) = python.managed.packages {
                        managed["packages"] = json!(packages);
                    }
                    if let Some(version) = python.managed.version {
                        managed["version"] = json!(version);
                    }
                    json!({ "managed": managed })
                }
            },
        };
    }
    value["sandbox"] = if no_sandbox {
        Value::Null
    } else {
        permissions(&captured.policy)
    };
    value["resolver"] = json!({
        "inherit_environment": captured.resolver.get("inherit_environment") != Some(&Value::Bool(false)),
        "environment": captured.resolver.get("environment").cloned().unwrap_or_else(|| json!({})),
        "sandbox": if no_sandbox || cfg!(windows) { Value::Null } else { permissions(&captured.resolver) },
    });
    Ok(value)
}

pub(crate) fn render(
    mut value: Value,
    defaults: Option<Value>,
    format: crate::cli::ConfigFormat,
) -> Result<String, String> {
    if let Some(mut defaults) = defaults {
        // The resolver inherits the shared setting when omitted. Keep an
        // explicit override when it differs from that conditional default.
        defaults["resolver"]["inherit_environment"] = value["inherit_environment"].clone();
        prune(&mut value, &defaults);
    }
    let serialized = serde_json::to_string_pretty(&value).map_err(|error| error.to_string())?;
    match format {
        crate::cli::ConfigFormat::Json => Ok(serialized),
        crate::cli::ConfigFormat::Yaml => {
            let documents = Yaml::load_from_str(&serialized).map_err(|error| error.to_string())?;
            let mut output = String::new();
            let mut emitter = YamlEmitter::new(&mut output);
            emitter.multiline_strings(true);
            emitter
                .dump(&documents[0])
                .map_err(|error| error.to_string())?;
            Ok(output)
        }
    }
}

/// Mappings merge recursively; arrays, scalars, and null are complete values.
fn prune(value: &mut Value, defaults: &Value) {
    if let (Some(value), Some(defaults)) = (value.as_object_mut(), defaults.as_object()) {
        value.retain(|name, value| {
            let Some(default) = defaults.get(name) else {
                return true;
            };
            if value == default {
                return false;
            }
            prune(value, default);
            !value.as_object().is_some_and(serde_json::Map::is_empty)
        });
    }
}

/// Translate captured application permissions back to their public spelling.
/// Missing filesystem policy retains runtime-owned cache/temporary grants.
fn permissions(settings: &SandboxSettings) -> Value {
    let mut result = json!({ "network": settings["network"], "tempdir": super::Tempdir::from_settings(settings).name() });
    if settings
        .get("extends")
        .is_some_and(|profile| profile == ":workspace")
    {
        result["profile"] = json!("workspace_write");
    }
    if let Some(entries) = settings
        .get("filesystem")
        .and_then(|fs| fs.get("entries"))
        .and_then(Value::as_array)
    {
        let mut filesystem = json!({ "read_only": [], "read_write": [], "deny": [] });
        for entry in entries {
            let access = match entry["access"].as_str().expect("captured access") {
                "read" => "read_only",
                "write" => "read_write",
                "deny" => "deny",
                _ => unreachable!(),
            };
            filesystem[access]
                .as_array_mut()
                .expect("path list")
                .push(entry["path"]["path"].clone());
        }
        result["filesystem"] = filesystem;
    }
    if let Some(proxy) = settings.get("proxy") {
        let mut domains = json!({ "allow": [], "deny": [] });
        for (domain, access) in proxy["domains"].as_object().expect("captured domains") {
            domains[access.as_str().expect("domain access")]
                .as_array_mut()
                .expect("domain list")
                .push(json!(domain));
        }
        let mut network = json!({
            "proxy": { "mode": proxy["mode"], "domains": domains, "allow_upstream_proxy": proxy["allowUpstreamProxy"] },
            "allow_local_binding": proxy["allowLocalBinding"],
            "sockets": { "unix_sockets": if proxy["dangerouslyAllowAllUnixSockets"] == true { json!("dangerously_allow_all") } else {
                json!(proxy["unixSockets"].as_object().expect("captured sockets").keys().collect::<Vec<_>>())
            } },
        });
        if proxy["mode"] == "full" {
            network["proxy"]["socks5"] = json!(if proxy["enableSocks5"] == true {
                "tcp"
            } else {
                "disabled"
            });
        }
        result["network"] = network;
    }
    result
}
