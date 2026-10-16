//! Permission to prepare dependencies, independent of resolver availability.

#[derive(Clone, Copy, Default, PartialEq, Eq, serde::Deserialize)]
#[serde(try_from = "String")]
pub(crate) enum Resolution {
    #[default]
    Implicit,
    Explicit,
    StartupOnly,
    Disabled,
}

impl TryFrom<String> for Resolution {
    type Error = String;

    fn try_from(mode: String) -> Result<Self, Self::Error> {
        match mode.as_str() {
            "implicit" => Ok(Self::Implicit),
            "explicit" => Ok(Self::Explicit),
            "startup_only" => Ok(Self::StartupOnly),
            "disabled" => Ok(Self::Disabled),
            _ => Err(format!(
                "unknown resolution policy `{mode}`; expected implicit, explicit, startup_only, or disabled"
            )),
        }
    }
}

impl Resolution {
    pub(crate) fn prepares_startup(self) -> bool {
        self != Self::Disabled
    }

    pub(crate) fn permits_deliberate_changes(self) -> bool {
        matches!(self, Self::Implicit | Self::Explicit)
    }

    pub(crate) fn permits_implicit_additions(self) -> bool {
        self == Self::Implicit
    }

    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Implicit => "implicit",
            Self::Explicit => "explicit",
            Self::StartupOnly => "startup_only",
            Self::Disabled => "disabled",
        }
    }

    pub(crate) fn denial(self, language: &str, operation: &str) -> String {
        let key = match language {
            "R" => "r.resolution",
            "Python" => "python.managed.resolution",
            _ => unreachable!("resolution policies belong to R or Python"),
        };
        let mode = self.name();
        format!("{language} requirements cannot change under {key}={mode} ({operation})")
    }
}

pub(super) fn managed<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Resolution, D::Error> {
    use serde::{Deserialize as _, de::Error as _};
    let policy = Resolution::deserialize(deserializer)?;
    if policy == Resolution::Disabled {
        return Err(D::Error::custom(
            "managed Python resolution cannot be disabled; use an existing Python environment or startup_only",
        ));
    }
    Ok(policy)
}
