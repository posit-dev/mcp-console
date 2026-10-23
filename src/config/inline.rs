//! Inline collections with YAML scalars and either `:` or `=` object separators.
//! Parse one grammar; malformed collections never become string values.

use serde_json::{Map, Value};

use super::yaml;

pub(super) fn parse(source: &str) -> Result<Value, String> {
    if source.contains(['\n', '\r']) {
        return Err("expected an inline value".into());
    }
    let mut parser = Parser(source.trim());
    let value = parser.value()?;
    if !parser.0.is_empty() {
        return Err("unexpected text after value".into());
    }
    Ok(value)
}

struct Parser<'a>(&'a str);

impl<'a> Parser<'a> {
    fn value(&mut self) -> Result<Value, String> {
        if self.take('{') {
            let mut values = Map::new();
            if !self.take('}') {
                loop {
                    let key = self.token(true)?;
                    let key = if key.starts_with(['\'', '"']) {
                        let Value::String(key) = yaml::quoted(key)? else {
                            return Err("expected a string key".into());
                        };
                        key
                    } else {
                        key.to_string()
                    };
                    if !(self.take(':') || self.take('=')) {
                        return Err("expected ':' or '=' after object key".into());
                    }
                    values.insert(key, self.value()?);
                    if self.end('}')? {
                        break;
                    }
                }
            }
            Ok(Value::Object(values))
        } else if self.take('[') {
            let mut values = Vec::new();
            if !self.take(']') {
                loop {
                    values.push(self.value()?);
                    if self.end(']')? {
                        break;
                    }
                }
            }
            Ok(Value::Array(values))
        } else {
            let token = self.token(false)?;
            if token.starts_with(['\'', '"']) {
                yaml::quoted(token)
            } else {
                yaml::scalar(&saphyr::Scalar::parse_from_cow(token.into()))
            }
        }
    }

    fn take(&mut self, delimiter: char) -> bool {
        if let Some(rest) = self.0.strip_prefix(delimiter) {
            self.0 = rest.trim_start();
            true
        } else {
            false
        }
    }

    fn end(&mut self, delimiter: char) -> Result<bool, String> {
        if self.take(delimiter) {
            Ok(true)
        } else if self.take(',') {
            Ok(self.take(delimiter))
        } else {
            Err(format!("expected ',' or '{delimiter}'"))
        }
    }

    fn token(&mut self, key: bool) -> Result<&'a str, String> {
        let source = self.0;
        let length = if source.starts_with(['\'', '"']) {
            let mut characters = source.char_indices().peekable();
            let (_, quote) = characters.next().expect("opening quote");
            loop {
                let Some((index, character)) = characters.next() else {
                    return Err("unterminated quoted string".into());
                };
                if quote == '"' && character == '\\' {
                    characters.next();
                } else if character == quote {
                    if quote == '\'' && characters.next_if(|(_, next)| *next == quote).is_some() {
                        continue;
                    }
                    break index + 1;
                }
            }
        } else {
            source
                .find(|character| {
                    matches!(character, '{' | '}' | '[' | ']' | ',')
                        || (key && matches!(character, ':' | '='))
                })
                .unwrap_or(source.len())
        };
        let token = source[..length].trim();
        if token.is_empty() {
            return Err(if key {
                "expected an object key"
            } else {
                "expected a value"
            }
            .into());
        }
        self.0 = source[length..].trim_start();
        Ok(token)
    }
}
