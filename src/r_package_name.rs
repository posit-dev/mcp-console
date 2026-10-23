pub(crate) const MAX_RUNTIME_PACKAGES: usize = 64;

pub(crate) fn validate_all(packages: &[String]) -> Result<(), String> {
    if packages.is_empty() {
        return Err("automatic R package resolution requires at least one package".to_string());
    }
    if packages.len() > MAX_RUNTIME_PACKAGES {
        return Err(format!(
            "automatic R package resolution accepts at most {MAX_RUNTIME_PACKAGES} packages"
        ));
    }
    packages.iter().try_for_each(|package| validate(package))
}

fn validate(package: &str) -> Result<(), String> {
    let bytes = package.as_bytes();
    let valid = bytes.first().is_some_and(u8::is_ascii_alphabetic)
        && bytes.last().is_some_and(u8::is_ascii_alphanumeric)
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'.');
    if valid {
        return Ok(());
    }
    Err(format!(
        "automatic R package name `{package}` is not accepted: names must start with an ASCII letter, end with an ASCII letter or digit, and contain only ASCII letters, digits, and dots"
    ))
}

#[cfg(test)]
mod tests {
    use super::validate;

    #[test]
    fn accepts_plain_package_names() {
        for package in ["a", "data.table", "R6", "foo.bar2"] {
            assert_eq!(validate(package), Ok(()));
        }
    }

    #[test]
    fn rejects_non_package_references() {
        for package in [
            "",
            ".foo",
            "foo.",
            "foo/bar",
            r"foo\bar",
            "github::owner/repo",
            "foo@1",
            "foo bar",
            "foo\n",
            "foo\0bar",
            "fóo",
        ] {
            assert!(validate(package).is_err(), "accepted {package:?}");
        }
    }
}
