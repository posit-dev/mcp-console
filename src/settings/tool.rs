//! Captured overrides for the model-facing tool definition.

use std::collections::BTreeMap;

use serde::Deserialize;
use serde_json::{Map, Value};

#[derive(Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Tool {
    pub description: Option<String>,
    pub instructions: Option<String>,
    parameters: BTreeMap<String, Parameter>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Parameter {
    #[serde(default = "enabled_by_default")]
    enabled: bool,
    #[serde(default)]
    description: Option<String>,
    #[serde(default)]
    parameters: BTreeMap<String, Parameter>,
}

fn enabled_by_default() -> bool {
    true
}

impl Tool {
    pub fn validate(&self, languages: Option<crate::cell::Languages>) -> Result<(), String> {
        if self.parameters.is_empty() {
            return Ok(());
        }
        let languages = match languages {
            Some(languages) => languages,
            None => crate::cell::Languages::from_environment()?,
        };
        self.configure_schema(&mut crate::server::parameter_schema(languages))
    }

    pub fn enabled(&self, field: &str) -> bool {
        self.parameters.get(field).is_none_or(|field| field.enabled)
    }

    pub fn configure_schema(&self, schema: &mut Map<String, Value>) -> Result<(), String> {
        configure_parameters(&self.parameters, schema, "tool.parameters")
    }

    pub fn validate_arguments(&self, arguments: Option<&Map<String, Value>>) -> Result<(), String> {
        if let Some(arguments) = arguments {
            validate_arguments(&self.parameters, arguments, "")?;
        }
        Ok(())
    }
}

fn configure_parameters(
    parameters: &BTreeMap<String, Parameter>,
    schema: &mut Map<String, Value>,
    path: &str,
) -> Result<(), String> {
    if parameters.is_empty() {
        return Ok(());
    }
    let properties = schema
        .get_mut("properties")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| format!("{path}: this parameter has no nested parameters"))?;
    for (field, settings) in parameters {
        let path = format!("{path}.{field}");
        let property = properties
            .get_mut(field)
            .ok_or_else(|| format!("{path}: unknown or unavailable parameter"))?
            .as_object_mut()
            .expect("parameter schemas are objects");
        if let Some(description) = &settings.description {
            property.insert("description".into(), description.clone().into());
        }
        configure_parameters(
            &settings.parameters,
            property,
            &format!("{path}.parameters"),
        )?;
        if !settings.enabled {
            properties.shift_remove(field);
        }
    }
    Ok(())
}

fn validate_arguments(
    parameters: &BTreeMap<String, Parameter>,
    arguments: &Map<String, Value>,
    parent: &str,
) -> Result<(), String> {
    for (field, value) in arguments {
        let Some(settings) = parameters.get(field) else {
            continue;
        };
        let path = if parent.is_empty() {
            field.clone()
        } else {
            format!("{parent}.{field}")
        };
        if !settings.enabled {
            return Err(format!("`{path}` is hidden by `tool.parameters`"));
        }
        if let Some(arguments) = value.as_object() {
            validate_arguments(&settings.parameters, arguments, &path)?;
        }
    }
    Ok(())
}
