use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Language {
    R,
    Python,
    Sql,
}

pub(crate) struct Cell {
    pub(crate) language: Language,
    pub(crate) source: String,
}

// Internal eval configuration; intentionally not exposed through the CLI.
pub(crate) const LANGUAGES_ENV: &str = "MCP_CONSOLE_LANGUAGES";

#[derive(Clone, Copy, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Languages {
    pub(crate) r: bool,
    pub(crate) python: bool,
    pub(crate) sql: bool,
}

impl Languages {
    pub(crate) fn fields(self) -> Vec<&'static str> {
        [Language::R, Language::Python, Language::Sql]
            .into_iter()
            .filter(|language| self.enables(*language))
            .map(Self::field)
            .collect()
    }

    pub(crate) fn cell_fields(self) -> String {
        let fields = self.fields();
        let quoted = fields
            .iter()
            .map(|field| format!("`{field}`"))
            .collect::<Vec<_>>();
        match quoted.as_slice() {
            [only] => only.clone(),
            [first, last] => format!("{first} or {last}"),
            [first, second, last] => format!("{first}, {second}, or {last}"),
            _ => unreachable!("a configured interface has at least one language"),
        }
    }

    pub(crate) fn from_environment() -> Result<Self, String> {
        let Some(value) = std::env::var_os(LANGUAGES_ENV) else {
            return Ok(Self::all());
        };
        let value = value
            .into_string()
            .map_err(|_| Self::invalid_configuration())?;
        let mut languages = Self::default();
        for language in value.split(',') {
            match language {
                "r" => languages.r = true,
                "python" => languages.python = true,
                "sql" => languages.sql = true,
                _ => return Err(Self::invalid_configuration()),
            }
        }
        Ok(languages)
    }

    pub(crate) fn all() -> Self {
        Self {
            r: true,
            python: true,
            sql: true,
        }
    }

    pub(crate) fn configure(self, command: &mut std::process::Command) {
        let fields = [Language::R, Language::Python, Language::Sql]
            .into_iter()
            .filter(|language| self.enables(*language))
            .map(Self::field)
            .collect::<Vec<_>>();
        command.env(LANGUAGES_ENV, fields.join(","));
    }

    pub(crate) fn enables(self, language: crate::cell::Language) -> bool {
        match language {
            crate::cell::Language::R => self.r,
            crate::cell::Language::Python => self.python,
            crate::cell::Language::Sql => self.sql,
        }
    }

    pub(crate) fn field(language: crate::cell::Language) -> &'static str {
        match language {
            crate::cell::Language::R => "r",
            crate::cell::Language::Python => "python",
            crate::cell::Language::Sql => "sql",
        }
    }

    fn invalid_configuration() -> String {
        format!("`{LANGUAGES_ENV}` must be a comma-separated subset of `r`, `python`, and `sql`")
    }
}
