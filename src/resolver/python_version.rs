use std::cmp::Ordering;
use std::collections::BTreeSet;

use crate::python_requirement::VersionConstraint;
use pep508_rs::pep440_rs::Version;
use serde::Deserialize;

pub(super) struct PythonVersions {
    candidates: Vec<Candidate>,
}

struct Candidate {
    version: String,
    parsed_version: Version,
    major: u64,
    minor: u64,
    patch: u64,
    prerelease: bool,
    latest_patch: bool,
}

#[derive(Deserialize)]
struct UvPython {
    version: String,
    version_parts: VersionParts,
    variant: String,
    implementation: String,
}

#[derive(Deserialize)]
struct VersionParts {
    major: u64,
    minor: u64,
    patch: u64,
}

impl PythonVersions {
    pub(super) fn parse(output: &[u8]) -> Result<Self, String> {
        let rows = serde_json::from_slice::<Vec<UvPython>>(output)
            .map_err(|error| format!("uv returned invalid Python inventory JSON: {error}"))?;
        let mut versions = BTreeSet::new();
        let candidates = rows
            .into_iter()
            .filter_map(Candidate::from_uv)
            .filter(|candidate| versions.insert(candidate.version.clone()))
            .collect::<Vec<_>>();
        Ok(Self { candidates })
    }

    pub(super) fn rank(mut self) -> Self {
        if !self.candidates.is_empty() {
            rank(&mut self.candidates);
        }
        self
    }

    pub(super) fn resolve(&self, constraints: &[String]) -> Result<String, String> {
        let constraints = constraints
            .iter()
            .flat_map(|constraint| constraint.split(','))
            .map(str::trim)
            .filter(|constraint| !constraint.is_empty())
            .collect::<Vec<_>>();

        let Some(default) = self.candidates.first() else {
            return Err("uv did not report a supported CPython interpreter".to_string());
        };
        if constraints.is_empty() {
            return Ok(default.version.clone());
        }
        if constraints.len() == 1
            && self
                .candidates
                .iter()
                .any(|candidate| candidate.version == constraints[0])
        {
            return Ok(constraints[0].to_string());
        }

        let requested = constraints.join(",");
        let constraints = constraints
            .iter()
            .map(|constraint| VersionConstraint::parse(constraint))
            .collect::<Vec<_>>();
        if let Some(candidate) = self.candidates.iter().find(|candidate| {
            constraints
                .iter()
                .all(|constraint| constraint.matches(&candidate.parsed_version, &candidate.version))
        }) {
            return Ok(candidate.version.clone());
        }

        let available = self
            .candidates
            .iter()
            .map(|candidate| candidate.version.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        Err(format!(
            "Requested Python version constraints could not be satisfied.
  constraints: \"{requested}\"
Hint: Call `py_require(python_version = <string>, action = \"set\")` to replace constraints.
Available Python versions found: {available}
"
        ))
    }
}

impl Candidate {
    fn from_uv(row: UvPython) -> Option<Self> {
        if row.variant != "default"
            || row.implementation != "cpython"
            || (row.version_parts.major, row.version_parts.minor) < (3, 10)
        {
            return None;
        }
        let VersionParts {
            major,
            minor,
            patch,
        } = row.version_parts;
        let parsed_version = row.version.parse().ok()?;
        let prerelease = row.version != format!("{major}.{minor}.{patch}");
        Some(Self {
            version: row.version,
            parsed_version,
            major,
            minor,
            patch,
            prerelease,
            latest_patch: false,
        })
    }

    fn preference_score(&self, preferred_minor: i128) -> i128 {
        let minor = i128::from(self.minor);
        -(minor - preferred_minor).abs() * 2 - if minor > preferred_minor { 1 } else { 0 }
    }
}

fn rank(candidates: &mut [Candidate]) {
    candidates.sort_by(initial_order);
    let mut seen = BTreeSet::new();
    for candidate in candidates.iter_mut() {
        candidate.latest_patch = seen.insert((candidate.major, candidate.minor));
    }
    let latest_minor = candidates
        .iter()
        .filter(|candidate| !candidate.prerelease)
        .map(|candidate| candidate.minor)
        .max()
        .or_else(|| candidates.iter().map(|candidate| candidate.minor).max())
        .expect("ranked Python candidates should not be empty");
    let preferred_minor = i128::from(latest_minor) - 2;
    candidates.sort_by(|left, right| final_order(left, right, preferred_minor));
}

fn initial_order(left: &Candidate, right: &Candidate) -> Ordering {
    left.prerelease
        .cmp(&right.prerelease)
        .then_with(|| right.major.cmp(&left.major))
        .then_with(|| right.minor.cmp(&left.minor))
        .then_with(|| right.patch.cmp(&left.patch))
}

fn final_order(left: &Candidate, right: &Candidate, preferred_minor: i128) -> Ordering {
    left.prerelease
        .cmp(&right.prerelease)
        .then_with(|| right.latest_patch.cmp(&left.latest_patch))
        .then_with(|| {
            right
                .preference_score(preferred_minor)
                .cmp(&left.preference_score(preferred_minor))
        })
        .then_with(|| (right.major == 3).cmp(&(left.major == 3)))
        .then_with(|| right.minor.cmp(&left.minor))
        .then_with(|| right.patch.cmp(&left.patch))
}
