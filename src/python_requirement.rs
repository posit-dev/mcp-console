use std::cmp::Ordering;

use pep508_rs::{
    Requirement, VerbatimUrl, VersionOrUrl,
    pep440_rs::{Operator, Version, VersionSpecifier},
};

pub(crate) fn validate(requirement: &str) -> Result<(), String> {
    parse_named(requirement).map(|_| ())
}

pub(crate) fn distribution_name(requirement: &str) -> Result<String, String> {
    Ok(parse_named(requirement)?.name.to_string())
}

fn parse_named(requirement: &str) -> Result<Requirement<VerbatimUrl>, String> {
    match requirement.parse::<Requirement<VerbatimUrl>>() {
        Ok(parsed)
            if matches!(
                &parsed.version_or_url,
                None | Some(VersionOrUrl::VersionSpecifier(_))
            ) =>
        {
            Ok(parsed)
        }
        _ => Err(format!(
            "Python requirement `{requirement}` is not accepted: host-side managed resolution accepts named package requirements only"
        )),
    }
}

pub(crate) fn validate_all(requirements: &[String]) -> Result<(), String> {
    requirements
        .iter()
        .try_for_each(|requirement| validate(requirement))
}

pub(crate) fn validate_version_constraint(constraint: &str) -> Result<(), String> {
    let is_supported = constraint.split(',').all(|clause| {
        let clause = clause.trim();
        clause.parse::<Version>().is_ok()
            || clause.parse::<VersionSpecifier>().is_ok_and(|specifier| {
                matches!(
                    specifier.operator(),
                    Operator::Equal
                        | Operator::EqualStar
                        | Operator::NotEqual
                        | Operator::NotEqualStar
                        | Operator::LessThan
                        | Operator::LessThanEqual
                        | Operator::GreaterThan
                        | Operator::GreaterThanEqual
                )
            })
    });
    if !constraint.is_empty() && is_supported {
        return Ok(());
    }
    Err(format!(
        "Python version constraint `{constraint}` is not accepted: host-side managed resolution accepts version numbers and supported PEP 440 version specifiers only"
    ))
}

pub(crate) fn validate_version_constraints(constraints: &[String]) -> Result<(), String> {
    constraints
        .iter()
        .try_for_each(|constraint| validate_version_constraint(constraint))
}

#[derive(Clone, Copy)]
enum ConstraintOperator {
    Equal,
    NotEqual,
    LessThan,
    LessThanEqual,
    GreaterThan,
    GreaterThanEqual,
}

pub(crate) struct VersionConstraint<'a> {
    operator: ConstraintOperator,
    numeric_version: Option<Vec<u64>>,
    specifier: Option<VersionSpecifier>,
    exact_version: Option<Version>,
    version_string: &'a str,
}

impl<'a> VersionConstraint<'a> {
    pub(crate) fn parse(value: &'a str) -> Self {
        let (operator, version_string, explicit_operator) =
            if let Some(version) = value.strip_prefix(">=") {
                (ConstraintOperator::GreaterThanEqual, version, true)
            } else if let Some(version) = value.strip_prefix("<=") {
                (ConstraintOperator::LessThanEqual, version, true)
            } else if let Some(version) = value.strip_prefix("==") {
                (ConstraintOperator::Equal, version, true)
            } else if let Some(version) = value.strip_prefix("!=") {
                (ConstraintOperator::NotEqual, version, true)
            } else if let Some(version) = value.strip_prefix('>') {
                (ConstraintOperator::GreaterThan, version, true)
            } else if let Some(version) = value.strip_prefix('<') {
                (ConstraintOperator::LessThan, version, true)
            } else {
                (ConstraintOperator::Equal, value, false)
            };
        let version_string = version_string.trim().trim_end_matches(".*");
        let numeric_version = parse_numeric_version(version_string);
        let specifier = explicit_operator
            .then(|| value.parse::<VersionSpecifier>().ok())
            .flatten();
        let exact_version = (!explicit_operator && numeric_version.is_none())
            .then(|| version_string.parse::<Version>().ok())
            .flatten();
        Self {
            operator,
            numeric_version,
            specifier,
            exact_version,
            version_string,
        }
    }

    pub(crate) fn matches(&self, candidate: &Version, version_string: &str) -> bool {
        let prerelease = parse_numeric_version(version_string).is_none();
        if prerelease && let Some(specifier) = self.specifier.as_ref() {
            return specifier.contains(candidate);
        }
        if let Some(version) = self.exact_version.as_ref() {
            return candidate == version;
        }
        let Some(version) = self.numeric_version.as_ref() else {
            return self.specifier.as_ref().map_or_else(
                || version_string == self.version_string,
                |specifier| specifier.contains(candidate),
            );
        };
        if prerelease {
            return false;
        }
        let mut candidate = candidate.release().to_vec();
        let mut version = version.clone();
        let specified_levels = version.len();
        if specified_levels < 3 {
            version.resize(3, 0);
            candidate[2] = 0;
        }
        if specified_levels < 2 {
            candidate[1] = 0;
        }
        let length = candidate.len().max(version.len());
        candidate.resize(length, 0);
        version.resize(length, 0);
        let ordering = candidate.cmp(&version);
        match self.operator {
            ConstraintOperator::Equal => ordering == Ordering::Equal,
            ConstraintOperator::NotEqual => ordering != Ordering::Equal,
            ConstraintOperator::LessThan => ordering == Ordering::Less,
            ConstraintOperator::LessThanEqual => ordering != Ordering::Greater,
            ConstraintOperator::GreaterThan => ordering == Ordering::Greater,
            ConstraintOperator::GreaterThanEqual => ordering != Ordering::Less,
        }
    }
}

fn parse_numeric_version(version: &str) -> Option<Vec<u64>> {
    let parts = version.split('.').collect::<Vec<_>>();
    if parts.iter().any(|part| part.is_empty()) {
        return None;
    }
    parts
        .into_iter()
        .map(str::parse::<u64>)
        .collect::<Result<Vec<_>, _>>()
        .ok()
}
