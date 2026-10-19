use rmcp::schemars;
use std::collections::{BTreeMap, BTreeSet};

use super::state::{Environment, ensure_managed_python_available};

#[derive(Clone, Copy, Default, PartialEq, Eq, serde::Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "snake_case")]
#[schemars(inline)]
pub(crate) enum RequirementsAction {
    Get,
    #[default]
    Add,
    Set,
    Reset,
}

#[derive(Clone, Default)]
pub(crate) struct Requirements {
    pub(crate) call_id: Option<u64>,
    pub(crate) action: RequirementsAction,
    pub(crate) python_version: Vec<String>,
    pub(crate) exclude_newer: Option<Option<String>>,
    pub(crate) duckdb: Vec<String>,
    pub(crate) python: Vec<String>,
    pub(crate) r: Vec<String>,
}

impl Requirements {
    pub(in crate::worker_client) fn validate(&self) -> Result<(), String> {
        if self.action == RequirementsAction::Add
            && self.duckdb.is_empty()
            && self.r.is_empty()
            && self.python.is_empty()
            && self.python_version.is_empty()
            && self.exclude_newer.is_none()
        {
            return Err(
                "at least one of `requirements.r`, `requirements.python`, or `requirements.duckdb` is required"
                    .to_string(),
            );
        }
        if self.action == RequirementsAction::Add {
            for (name, length) in [
                ("duckdb", self.duckdb.len()),
                ("r", self.r.len()),
                ("python", self.python.len()),
            ] {
                if length > 64 {
                    let noun = if name == "duckdb" {
                        "extensions"
                    } else {
                        "requirements"
                    };
                    return Err(format!("`requirements.{name}` accepts at most 64 {noun}"));
                }
            }
        }
        validate_duckdb_extensions(&self.duckdb)?;
        validate_r_requirements(&self.r)?;
        crate::python_requirement::validate_all(&self.python)?;
        crate::python_requirement::validate_version_constraints(&self.python_version)
    }
}

fn validate_duckdb_extensions(extensions: &[String]) -> Result<(), String> {
    if extensions.iter().any(|extension| extension.len() > 64) {
        return Err("DuckDB extension names must be at most 64 ASCII characters".to_string());
    }
    if extensions.iter().any(|extension| {
        let mut bytes = extension.bytes();
        !bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
            || bytes
                .any(|byte| !(byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_'))
    }) {
        return Err(
            "DuckDB extension names must start with a lowercase ASCII letter and contain only lowercase ASCII letters, digits, and underscores"
                .to_string(),
        );
    }
    Ok(())
}

pub(crate) fn validate_r_requirements(requirements: &[String]) -> Result<(), String> {
    if requirements
        .iter()
        .any(|requirement| requirement.trim().is_empty())
    {
        return Err("R requirement strings must not be empty".to_string());
    }
    if requirements.iter().any(|requirement| {
        requirement
            .bytes()
            .any(|byte| matches!(byte, b'\0' | b'\r' | b'\n'))
    }) {
        return Err("R requirement strings must not contain NUL or line breaks".to_string());
    }
    Ok(())
}

pub(in crate::worker_client) struct RequirementDelta {
    pub(super) restart_required: bool,
    pub(super) duckdb_extensions: BTreeSet<String>,
    pub(super) duckdb_changed: bool,
    pub(super) python_additions: BTreeSet<String>,
    pub(super) python_candidate: Option<crate::worker_protocol::PythonRequirementManifest>,
    pub(super) r_requirements: Vec<String>,
    pub(super) r_changed: bool,
    pub(super) r_defaults: Vec<String>,
    pub(super) python_defaults: Vec<String>,
    pub(super) duckdb_defaults: Vec<String>,
}

impl RequirementDelta {
    pub(in crate::worker_client) fn calculate(
        environment: &Environment,
        requirements: Requirements,
    ) -> Result<Self, String> {
        let action = requirements.action;
        if matches!(
            requirements.action,
            RequirementsAction::Set | RequirementsAction::Reset
        ) {
            return Self::replacement(environment, requirements);
        }
        let Requirements {
            mut duckdb,
            python,
            r,
            python_version,
            exclude_newer,
            ..
        } = requirements;
        if !python.is_empty() {
            ensure_managed_python_available(environment)?;
        }
        let pending = match &environment.r_resolver {
            super::super::RResolver::Pending(setup) => Some(setup),
            _ => None,
        };
        let startup = environment.startup_declaration();
        let r_defaults = if pending.is_some() && environment.startup.r.is_none() {
            startup
                .r
                .into_iter()
                .filter(|package| !r.contains(package))
                .collect()
        } else {
            Vec::new()
        };
        let python_defaults = if pending.is_some() && environment.startup.python.packages.is_none()
        {
            startup
                .python
                .into_iter()
                .filter(|package| !python.contains(package))
                .collect()
        } else {
            Vec::new()
        };
        let duckdb_defaults = if pending.is_some() {
            startup
                .duckdb
                .iter()
                .filter(|extension| !duckdb.contains(extension))
                .cloned()
                .collect()
        } else {
            Vec::new()
        };
        if pending.is_some() {
            duckdb.extend(startup.duckdb);
        }

        let duckdb_additions = duckdb.into_iter().collect::<BTreeSet<_>>();
        let duckdb_changed = !duckdb_additions.is_subset(&environment.duckdb_extensions);
        let duckdb_extensions = environment
            .duckdb_extensions
            .union(&duckdb_additions)
            .cloned()
            .collect();

        let python_additions = python.into_iter().collect::<BTreeSet<_>>();
        let current_python = environment.declaration().python_manifest();
        let mut candidate = current_python.clone();
        candidate.packages.extend(python_additions.iter().cloned());
        candidate.python_version.extend(python_version);
        if let Some(cutoff) = exclude_newer {
            if current_python.exclude_newer.is_some() && cutoff != current_python.exclude_newer {
                return Err("use requirements.action=\"set\" to replace exclude_newer".into());
            }
            candidate.exclude_newer = cutoff;
        }
        let candidate = candidate.normalized();
        let restart_required = candidate.python_version != current_python.python_version
            || candidate.exclude_newer != current_python.exclude_newer;
        let python_candidate = (candidate != current_python
            || (pending.is_some() && environment.manages_python()))
        .then_some(candidate);
        if restart_required {
            ensure_managed_python_available(environment)?;
        }
        let (r_requirements, r_changed) = merge_r_requirements(environment, r);

        let delta = Self {
            restart_required,
            duckdb_extensions,
            duckdb_changed,
            python_additions,
            python_candidate,
            r_requirements,
            r_changed,
            r_defaults,
            python_defaults,
            duckdb_defaults,
        };
        delta.validate_policy(environment, action)?;
        Ok(delta)
    }

    fn replacement(environment: &Environment, requirements: Requirements) -> Result<Self, String> {
        let current = environment.declaration();
        let candidate = if requirements.action == RequirementsAction::Reset {
            environment.startup_declaration()
        } else {
            super::inspection::Declaration {
                r: requirements.r,
                python: requirements.python,
                duckdb: requirements.duckdb,
                python_version: requirements.python_version,
                exclude_newer: requirements.exclude_newer.flatten(),
            }
            .normalized()
        };
        let changed = candidate != current;
        let pending = matches!(environment.r_resolver, super::super::RResolver::Pending(_));
        let python = candidate.python_manifest();
        let manages_python = environment.manages_python();
        if python != current.python_manifest() && !manages_python {
            ensure_managed_python_available(environment)?;
        }
        let action = requirements.action;
        let defaults = if action == RequirementsAction::Reset {
            candidate.clone()
        } else {
            Default::default()
        };
        let delta = Self {
            restart_required: true,
            duckdb_changed: changed && (candidate.duckdb != current.duckdb || pending),
            duckdb_extensions: candidate.duckdb.into_iter().collect(),
            python_additions: Default::default(),
            python_candidate: (changed
                && manages_python
                && (pending || python != current.python_manifest()))
            .then_some(python),
            r_changed: changed
                && (pending
                    || candidate.r != current.r
                    || (environment.custom_worker && environment.r.is_none())),
            r_requirements: candidate.r,
            r_defaults: if environment.startup.r.is_none() {
                defaults.r
            } else {
                Vec::new()
            },
            python_defaults: if environment.startup.python.packages.is_none() {
                defaults.python
            } else {
                Vec::new()
            },
            duckdb_defaults: defaults.duckdb,
        };
        delta.validate_policy(environment, action)?;
        Ok(delta)
    }

    fn validate_policy(
        &self,
        environment: &Environment,
        action: RequirementsAction,
    ) -> Result<(), String> {
        let current = environment.declaration();
        let operation = match action {
            RequirementsAction::Add => "requirements.action=add",
            RequirementsAction::Set => "requirements.action=set",
            RequirementsAction::Reset => "requirements.action=reset",
            RequirementsAction::Get => unreachable!("inspection does not calculate candidates"),
        };
        // Bootstrap/materialization flags are not logical declaration changes.
        let r_changed = self.r_requirements != current.r;
        let python_changed = self
            .python_candidate
            .as_ref()
            .is_some_and(|candidate| *candidate != current.python_manifest());
        let extensions_changed =
            self.duckdb_extensions.iter().cloned().collect::<Vec<_>>() != current.duckdb;
        let python_provider = environment
            .local_runtime
            .as_ref()
            .is_some_and(crate::local_runtime::Selection::python_only);
        for (changed, policy, language) in [
            (
                r_changed || (extensions_changed && !python_provider),
                environment.startup.r_resolution,
                "R",
            ),
            (
                python_changed || (extensions_changed && python_provider),
                environment.startup.python.resolution,
                "Python",
            ),
        ] {
            // Reset replays captured startup intent, including skipped defaults.
            let permitted = match action {
                RequirementsAction::Reset => policy.prepares_startup(),
                RequirementsAction::Add | RequirementsAction::Set => {
                    policy.permits_deliberate_changes()
                }
                RequirementsAction::Get => unreachable!("inspection does not calculate candidates"),
            };
            if changed && !permitted {
                return Err(policy.denial(language, operation));
            }
        }
        Ok(())
    }

    pub(in crate::worker_client) fn is_empty(&self) -> bool {
        !self.duckdb_changed && self.python_candidate.is_none() && !self.r_changed
    }

    pub(super) fn is_live_duckdb_only(&self) -> bool {
        self.duckdb_changed
            && !self.restart_required
            && self.python_candidate.is_none()
            && !self.r_changed
    }

    pub(super) fn has_live_python_additions(&self) -> bool {
        self.python_candidate.is_some() && !self.restart_required && !self.r_changed
    }

    pub(super) fn validate_live_python_additions(
        &self,
        environment: &Environment,
    ) -> Result<(), String> {
        // This is declaration compatibility, not a resolved-version lock.
        // The complete candidate may resolve different dependency versions;
        // live activation does not promise arbitrary package hot-swapping.
        let retained = environment.declaration().python_manifest();
        let mut names = BTreeMap::new();
        for requirement in &retained.packages {
            names.insert(
                crate::python_requirement::distribution_name(requirement)?,
                requirement,
            );
        }
        for requirement in &self.python_additions {
            if retained.packages.contains(requirement) {
                continue;
            }
            let name = crate::python_requirement::distribution_name(requirement)?;
            if let Some(previous) = names.insert(name, requirement) {
                return Err(format!(
                    "live Python requirements can only add new distributions; `{requirement}` changes already-declared `{previous}`; use control: restart with requirements.action: set"
                ));
            }
        }
        Ok(())
    }
}

pub(super) fn merge_r_requirements(
    environment: &Environment,
    additions: Vec<String>,
) -> (Vec<String>, bool) {
    let mut additions = additions.into_iter().collect::<BTreeSet<_>>();
    let pending = matches!(environment.r_resolver, super::super::RResolver::Pending(_));
    if pending {
        additions.extend(environment.startup_declaration().r);
    }
    let current = environment
        .r
        .as_ref()
        .map(|managed| managed.requirements().iter().cloned().collect())
        .unwrap_or_default();
    let changed = pending
        || !additions.is_subset(&current)
        || (environment.custom_worker && environment.r.is_none());
    (current.union(&additions).cloned().collect(), changed)
}

pub(super) fn select_python_activation(
    current: Option<&crate::resolver::ManagedPython>,
    requirements: crate::worker_protocol::PythonRequirementManifest,
    candidate: Option<crate::resolver::ManagedPython>,
) -> Result<crate::resolver::ManagedPython, String> {
    let requirements = requirements.normalized();
    if let Some(candidate) = candidate {
        return (candidate.requirements() == &requirements)
            .then_some(candidate)
            .ok_or_else(|| {
                "worker activation does not match a resolved Python environment".to_string()
            });
    }
    current
        .cloned()
        .filter(|current| current.requirements() == &requirements)
        .ok_or_else(|| "worker activation does not match a resolved Python environment".to_string())
}

pub(super) fn validate_python_import_resolution(
    resolution: &crate::worker_protocol::PythonImportResolution,
    requirements: &crate::worker_protocol::PythonRequirementManifest,
) -> Result<(), String> {
    let module = resolution.module.as_bytes();
    let distribution = resolution.distribution.as_bytes();
    let valid_module = module
        .first()
        .is_some_and(|byte| byte.is_ascii_alphabetic() || *byte == b'_')
        && module
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'_');
    let valid_distribution = distribution.first().is_some_and(u8::is_ascii_alphanumeric)
        && distribution.last().is_some_and(u8::is_ascii_alphanumeric)
        && distribution
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(*byte, b'-' | b'_' | b'.'));
    if !valid_module
        || !valid_distribution
        || !requirements.packages.contains(&resolution.distribution)
    {
        return Err("invalid automatic Python import resolution metadata".to_string());
    }
    Ok(())
}

pub(super) fn select_r_activation(
    library: &str,
    candidates: &mut Vec<crate::resolver::ManagedR>,
) -> Result<crate::resolver::ManagedR, String> {
    let candidate = candidates
        .iter()
        .rposition(|candidate| candidate.library().to_str() == Some(library))
        .map(|index| candidates.remove(index));
    candidates.clear();
    candidate.ok_or_else(|| "worker activation does not match a resolved R environment".to_string())
}

pub(super) fn push_duckdb_r_target(
    targets: &mut Vec<crate::resolver::ManagedR>,
    candidate: crate::resolver::ManagedR,
) {
    if targets
        .iter()
        .all(|target| target.library() != candidate.library())
    {
        targets.push(candidate);
    }
}
