use super::Gap;
use std::path::Path;

const LOCATION_BYTES: usize = super::TEXT_BYTES / 16;

/// Keep the generated session/file suffix exact when the configured recording
/// prefix is too long. That suffix is bounded independently of the host path.
fn recording_location(path: &Path, session: &Path, directory: bool) -> String {
    let separator = if directory {
        std::path::MAIN_SEPARATOR_STR
    } else {
        ""
    };
    let full = format!("{}{separator}", path.display());
    if full.len() <= LOCATION_BYTES {
        return full;
    }
    let console = session
        .parent()
        .expect("recording sessions directory")
        .parent()
        .expect("Console recording directory");
    format!(
        "{}{separator} (relative to Console recording directory)",
        path.strip_prefix(console)
            .expect("session-owned recording location")
            .display()
    )
}

#[derive(Clone, Default)]
pub(in crate::worker_client::output) struct Source {
    pub(in crate::worker_client::output) file: Option<crate::transcript::OutputRecord>,
    pub(in crate::worker_client::output) raw_bytes: u64,
    pub(in crate::worker_client::output) retained_bytes: u64,
    pub(in crate::worker_client::output) discarded_bytes: u64,
}

/// A single file, or its directory when several retained files contribute.
/// All receipts in a Console response belong to the same recording session.
#[derive(Clone, Default)]
pub(super) struct Location {
    path: Option<String>,
    directory: bool,
}

impl Location {
    pub(super) fn extend(&mut self, other: Self) {
        if let Some(path) = other.path {
            self.add(&path, other.directory);
        }
    }

    pub(super) fn add(&mut self, path: &str, directory: bool) {
        match &self.path {
            None => {
                self.path = Some(path.to_owned());
                self.directory = directory;
            }
            Some(previous) if !self.directory && (directory || previous != path) => {
                self.path = Some(
                    Path::new(previous)
                        .parent()
                        .expect("recorded file directory")
                        .display()
                        .to_string(),
                );
                self.directory = true;
            }
            _ => {}
        }
    }

    pub(super) fn describe(&self, singular: &str, plural: &str) -> Option<String> {
        self.path.as_ref().map(|path| {
            let path = Path::new(path);
            let directory = if self.directory {
                path
            } else {
                path.parent().expect("recorded file directory")
            };
            let session = directory.parent().expect("recording session directory");
            format!(
                "{}: {}",
                if self.directory { plural } else { singular },
                recording_location(path, session, self.directory)
            )
        })
    }

    fn shared_recording(&self, images: &Self) -> Option<String> {
        let logs = Path::new(self.path.as_ref()?);
        let images_path = Path::new(images.path.as_ref()?);
        let directory = logs.parent().expect("recorded output directory");
        let session = if self.directory {
            directory
        } else {
            directory.parent().expect("recording session directory")
        };
        // Share and bound the session prefix once; filenames stay exact.
        Some(format!(
            "retained output: {}; logs: {}{}; images: {}{}",
            recording_location(session, session, true),
            logs.strip_prefix(session)
                .expect("session-owned logs")
                .display(),
            if self.directory {
                std::path::MAIN_SEPARATOR_STR
            } else {
                ""
            },
            images_path
                .strip_prefix(session)
                .expect("session-owned artifacts")
                .display(),
            if images.directory {
                std::path::MAIN_SEPARATOR_STR
            } else {
                ""
            }
        ))
    }
}

/// Constant-size accounting for fully omitted response intervals. Individual
/// file paths and cumulative raw counts remain owned by their recording receipts.
#[derive(Clone, Default)]
pub(in crate::worker_client::output) struct Summary {
    pub(super) gap: Gap,
    logs: Location,
    partial: bool,
    unavailable: bool,
}

impl Summary {
    pub(super) fn new(source: &Source, gap: Gap) -> Self {
        if let Some(file) = &source.file {
            // Publish the retired interval before releasing its receipt;
            // active polls replay unclaimed output before collecting more.
            file.note_inline_omission(gap.bytes);
            file.publish();
        }
        let mut summary = Self {
            gap,
            ..Self::default()
        };
        summary.source(source);
        summary
    }

    pub(super) fn source(&mut self, source: &Source) {
        if let Some(path) = source.file.as_ref().and_then(|file| file.public_path()) {
            if source.retained_bytes != 0 {
                self.logs.add(&path, false);
            }
            self.partial |= source.discarded_bytes != 0;
        } else {
            self.unavailable |= source.raw_bytes != 0;
        }
    }

    pub(super) fn extend(&mut self, other: Self) {
        self.gap.bytes += other.gap.bytes;
        self.gap.notices += other.gap.notices;
        self.partial |= other.partial;
        self.unavailable |= other.unavailable;
        if let Some(path) = other.logs.path {
            self.logs.add(&path, other.logs.directory);
        }
    }

    pub(super) fn notice(
        &self,
        images: u64,
        image_bytes: u64,
        recorded_images: u64,
        artifacts: &Location,
    ) -> String {
        let mut details = Vec::new();
        if self.gap.bytes != 0 {
            details.push(format!("{} UTF-8 bytes", self.gap.bytes));
        }
        if images != 0 {
            details.push(format!(
                "{images} {} ({image_bytes} encoded bytes)",
                if images == 1 { "image" } else { "images" }
            ));
        }
        let omitted = details.join(", ");
        details.clear();
        let shared = self.logs.shared_recording(artifacts);
        let shared_location = shared.is_some();
        if let Some(location) = shared {
            details.push(location);
        }
        if self.gap.bytes != 0 {
            if !shared_location {
                details.push(
                    self.logs
                        .describe("raw log", "retained logs")
                        .unwrap_or_else(|| "no retained log; omitted text unavailable".to_owned()),
                );
            }
            if self.logs.path.is_some() {
                if self.partial {
                    details.push("logs contain prefixes; later raw text unavailable".to_owned());
                }
                if self.unavailable {
                    details.push("some omitted text unavailable".to_owned());
                }
            }
            if self.gap.notices != 0 {
                details.push(format!(
                    "{} generated notice bytes unavailable",
                    self.gap.notices
                ));
            }
        }
        if !shared_location
            && let Some(location) = artifacts.describe("retained image", "retained images")
        {
            details.push(location);
        }
        let unretained_images = images - recorded_images;
        if unretained_images != 0 {
            details.push(format!(
                "{unretained_images} {} not retained",
                if unretained_images == 1 {
                    "image"
                } else {
                    "images"
                }
            ));
        }
        format!("\n[output omitted: {omitted}; {}]\n", details.join("; "))
    }
}
