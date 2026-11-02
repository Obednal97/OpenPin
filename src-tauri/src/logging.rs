//! Rust-side log file: `~/Library/Logs/Plaud V2/app.log` (`Plaud V2 (Dev)` for
//! debug builds, see `paths`), rotated at ~1 MB with one old file kept
//! (`app.log.1`), plus stderr in debug builds.
//!
//! Levels: `info` for this crate (`plaud_v2::*`) and the backend's output
//! (`python` target), `warn` for dependencies. `RUST_LOG` overrides that with
//! `level` and/or `target=level` entries, e.g. `RUST_LOG=debug` or
//! `RUST_LOG=warn,plaud_v2::python=debug` (a target matches by prefix).
//!
//! Small on purpose: tauri-plugin-log would pull in fern/time and only starts
//! logging once the Tauri builder runs, after the panic hook and early setup.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use log::{LevelFilter, Log, Metadata, Record};

/// Rotate once the file would grow past this.
const MAX_BYTES: u64 = 1_000_000;

pub fn log_path() -> PathBuf {
    crate::paths::log_dir().join("app.log")
}

/// Level filter built from defaults or `RUST_LOG`.
#[derive(Debug, Clone, PartialEq)]
struct Filter {
    default: LevelFilter,
    /// (target prefix, level); the longest matching prefix wins.
    rules: Vec<(String, LevelFilter)>,
}

impl Filter {
    fn defaults() -> Filter {
        Filter {
            default: LevelFilter::Warn,
            rules: vec![
                ("plaud_v2".into(), LevelFilter::Info),
                ("python".into(), LevelFilter::Info),
            ],
        }
    }

    /// Parse a `RUST_LOG`-style spec. None (or nothing usable) gives the defaults.
    fn parse(spec: Option<&str>) -> Filter {
        let Some(spec) = spec.map(str::trim).filter(|s| !s.is_empty()) else {
            return Filter::defaults();
        };
        let mut default = None;
        let mut rules = Vec::new();
        for part in spec.split(',').map(str::trim).filter(|p| !p.is_empty()) {
            match part.split_once('=') {
                Some((target, level)) => {
                    if let Ok(level) = level.trim().parse::<LevelFilter>() {
                        rules.push((target.trim().to_string(), level));
                    }
                }
                None => {
                    if let Ok(level) = part.parse::<LevelFilter>() {
                        default = Some(level);
                    } else {
                        // A bare target name means "everything from it".
                        rules.push((part.to_string(), LevelFilter::Trace));
                    }
                }
            }
        }
        if default.is_none() && rules.is_empty() {
            return Filter::defaults();
        }
        Filter {
            default: default.unwrap_or(LevelFilter::Warn),
            rules,
        }
    }

    fn level_for(&self, target: &str) -> LevelFilter {
        self.rules
            .iter()
            .filter(|(prefix, _)| {
                target == prefix
                    || (target.starts_with(prefix.as_str())
                        && target[prefix.len()..].starts_with("::"))
            })
            .max_by_key(|(prefix, _)| prefix.len())
            .map(|(_, level)| *level)
            .unwrap_or(self.default)
    }

    fn max(&self) -> LevelFilter {
        self.rules
            .iter()
            .map(|(_, l)| *l)
            .chain(std::iter::once(self.default))
            .max()
            .unwrap_or(LevelFilter::Info)
    }
}

/// The log file, rotated by size.
struct RotatingFile {
    path: PathBuf,
    file: File,
    size: u64,
    max_bytes: u64,
}

impl RotatingFile {
    fn open(path: &Path, max_bytes: u64) -> std::io::Result<RotatingFile> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        let size = file.metadata().map(|m| m.len()).unwrap_or(0);
        Ok(RotatingFile {
            path: path.to_path_buf(),
            file,
            size,
            max_bytes,
        })
    }

    fn write_line(&mut self, line: &str) {
        let len = line.len() as u64;
        if self.size > 0 && self.size + len > self.max_bytes {
            self.rotate();
        }
        if self.file.write_all(line.as_bytes()).is_ok() {
            self.size += len;
        }
    }

    /// app.log -> app.log.1 (replacing the previous one), then a fresh app.log.
    fn rotate(&mut self) {
        let mut old = self.path.clone().into_os_string();
        old.push(".1");
        let _ = std::fs::rename(&self.path, PathBuf::from(old));
        if let Ok(file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            self.file = file;
            self.size = 0;
        }
    }
}

struct Logger {
    filter: Filter,
    file: Option<Mutex<RotatingFile>>,
    stderr: bool,
}

impl Log for Logger {
    fn enabled(&self, metadata: &Metadata) -> bool {
        metadata.level() <= self.filter.level_for(metadata.target())
    }

    fn log(&self, record: &Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let line = format!(
            "{} {:<5} {}: {}\n",
            chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f"),
            record.level(),
            record.target(),
            record.args()
        );
        if let Some(file) = &self.file {
            file.lock().unwrap_or_else(|e| e.into_inner()).write_line(&line);
        }
        if self.stderr {
            eprint!("{line}");
        }
    }

    fn flush(&self) {
        if let Some(file) = &self.file {
            let _ = file.lock().unwrap_or_else(|e| e.into_inner()).file.flush();
        }
    }
}

/// Install the logger. Call once, at startup.
pub fn init() {
    let filter = Filter::parse(std::env::var("RUST_LOG").ok().as_deref());
    let path = log_path();
    let file = match RotatingFile::open(&path, MAX_BYTES) {
        Ok(f) => Some(Mutex::new(f)),
        Err(e) => {
            eprintln!("[Log] Couldn't open {}: {e}; logging to stderr only", path.display());
            None
        }
    };
    let stderr = cfg!(debug_assertions) || file.is_none();
    let max = filter.max();
    // Leaked once: the logger lives for the whole process (log's `std` feature,
    // which set_boxed_logger needs, isn't enabled).
    let logger: &'static Logger = Box::leak(Box::new(Logger {
        filter,
        file,
        stderr,
    }));
    if log::set_logger(logger).is_ok() {
        log::set_max_level(max);
        log::info!(
            "Plaud V2 {} starting; logging to {}",
            env!("CARGO_PKG_VERSION"),
            path.display()
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_filter_is_info_for_us_warn_for_deps() {
        let f = Filter::parse(None);
        assert_eq!(f, Filter::defaults());
        assert_eq!(f.level_for("plaud_v2::python"), LevelFilter::Info);
        assert_eq!(f.level_for("plaud_v2"), LevelFilter::Info);
        assert_eq!(f.level_for("python"), LevelFilter::Info);
        assert_eq!(f.level_for("tao::platform_impl"), LevelFilter::Warn);
        // Prefix match is by path segment.
        assert_eq!(f.level_for("plaud_v2_other"), LevelFilter::Warn);
        assert_eq!(f.max(), LevelFilter::Info);
        assert_eq!(Filter::parse(Some("  ")), Filter::defaults());
        assert_eq!(Filter::parse(Some("not-a-level=zzz")), Filter::defaults());
    }

    #[test]
    fn rust_log_overrides() {
        let f = Filter::parse(Some("debug"));
        assert_eq!(f.level_for("anything"), LevelFilter::Debug);

        let f = Filter::parse(Some("warn,plaud_v2::python=trace,plaud_v2=error"));
        assert_eq!(f.level_for("plaud_v2::python"), LevelFilter::Trace);
        assert_eq!(f.level_for("plaud_v2::lifecycle"), LevelFilter::Error);
        assert_eq!(f.level_for("reqwest"), LevelFilter::Warn);
        assert_eq!(f.max(), LevelFilter::Trace);
    }

    #[test]
    fn rotates_at_the_size_limit_keeping_one_old_file() {
        let dir = std::env::temp_dir().join(format!(
            "plaud-log-test-{}-{}",
            std::process::id(),
            chrono::Local::now().timestamp_nanos_opt().unwrap_or(0)
        ));
        let path = dir.join("nested/app.log");
        let mut f = RotatingFile::open(&path, 100).unwrap();
        let line = format!("{}\n", "x".repeat(39)); // 40 bytes
        f.write_line(&line);
        f.write_line(&line);
        assert_eq!(std::fs::metadata(&path).unwrap().len(), 80);
        f.write_line(&line); // would be 120 > 100: rotate first
        let old = dir.join("nested/app.log.1");
        assert_eq!(std::fs::metadata(&old).unwrap().len(), 80);
        assert_eq!(std::fs::metadata(&path).unwrap().len(), 40);
        f.write_line(&line); // 80
        f.write_line(&line); // rotate again
        // Second rotation replaced app.log.1; never more than two files.
        assert_eq!(std::fs::metadata(&old).unwrap().len(), 80);
        assert_eq!(std::fs::metadata(&path).unwrap().len(), 40);
        let names: Vec<_> = std::fs::read_dir(dir.join("nested")).unwrap().collect();
        assert_eq!(names.len(), 2);
        // Reopening picks up the existing size.
        drop(f);
        let f = RotatingFile::open(&path, 100).unwrap();
        assert_eq!(f.size, 40);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
