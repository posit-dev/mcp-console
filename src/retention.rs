//! Storage maintenance policies captured once for each Console connection.

use std::num::NonZeroU32;

use serde::{Deserialize, Serialize};

pub(crate) mod cache;
pub(crate) mod recordings;

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Settings {
    pub cache_days: Option<NonZeroU32>,
    pub recordings_days: Option<NonZeroU32>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            cache_days: NonZeroU32::new(14),
            recordings_days: NonZeroU32::new(14),
        }
    }
}
