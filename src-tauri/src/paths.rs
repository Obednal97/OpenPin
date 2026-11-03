//! Where the shell keeps things on disk, and how debug builds stay out of the
//! installed app's folders.
//!
//! | What | Release build | Debug build (`tauri dev`) |
//! |---|---|---|
//! | User data (the backend's `DATA_DIR`) | `Application Support/Plaud V2` | `Application Support/Plaud V2 (Dev)` |
//! | Shell state: venv, lockfiles, `server.pid`, prefs, updates, diagnostics | `Application Support/com.plaud.v2` | `Application Support/com.plaud.v2.dev` |
//! | Python runtime (`python/`) and FFmpeg (`bin/`) | `Application Support/com.plaud.v2` | shared with release |
//! | Logs | `Logs/Plaud V2` | `Logs/Plaud V2 (Dev)` |
//!
//! The app was called "Plaud V2" before it became Open Pin. Every folder name
//! above is a fixed literal, never derived from the product name or bundle
//! name, so renaming the app moves nothing: installs keep their recordings,
//! settings, venv and logs. The bundle identifier `com.plaud.v2` stays too
//! (Bluetooth permission and the Keychain pairing backup hang off it). The
//! tests at the bottom pin these names; don't change them.
//!
//! `PLAUD_DATA_DIR`, when set, overrides the data folder in both builds. The
//! shell always passes the resolved folder to Python in `PLAUD_DATA_DIR`, so
//! the backend and the Rust side (the bug report's `wifi_sync.log`) agree.
//! Dev data never defaults to the repo: a checkout may be synced to other
//! machines and the data folder holds API keys and device tokens.

use std::path::{Path, PathBuf};

/// Environment variable that overrides the data folder (also read by
/// `app/utils/constants.py`).
pub const DATA_DIR_ENV: &str = "PLAUD_DATA_DIR";

// Legacy names, kept on purpose (see the module docs). Not `productName`.
const RELEASE_DATA_DIR: &str = "Plaud V2";
const DEV_DATA_DIR: &str = "Plaud V2 (Dev)";
const RELEASE_SUPPORT_DIR: &str = "com.plaud.v2";
const DEV_SUPPORT_DIR: &str = "com.plaud.v2.dev";
const RELEASE_LOG_DIR: &str = "Plaud V2";
const DEV_LOG_DIR: &str = "Plaud V2 (Dev)";

const IS_DEV: bool = cfg!(debug_assertions);

fn library() -> PathBuf {
    crate::home_dir().join("Library")
}

/// Shell-managed state for this build (venv, lockfiles, PID file, prefs,
/// update downloads, diagnostics log).
pub fn app_support_dir() -> PathBuf {
    support_dir_for(&library(), IS_DEV)
}

/// Shared by dev and release builds: the managed Python runtime and the
/// FFmpeg download (`app/utils/system.py` hard-codes that `bin/` path).
pub fn shared_support_dir() -> PathBuf {
    library()
        .join("Application Support")
        .join(RELEASE_SUPPORT_DIR)
}

/// Folder for `app.log` and `update.log`.
pub fn log_dir() -> PathBuf {
    let name = if IS_DEV { DEV_LOG_DIR } else { RELEASE_LOG_DIR };
    library().join("Logs").join(name)
}

/// The backend's data folder (recordings, config, tokens).
pub fn data_dir() -> PathBuf {
    let env = std::env::var(DATA_DIR_ENV).ok();
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/"));
    resolve_data_dir(env.as_deref(), &crate::home_dir(), &cwd, IS_DEV)
}

fn support_dir_for(library: &Path, dev: bool) -> PathBuf {
    let name = if dev {
        DEV_SUPPORT_DIR
    } else {
        RELEASE_SUPPORT_DIR
    };
    library.join("Application Support").join(name)
}

/// `PLAUD_DATA_DIR` if set (with `~` expanded and a relative path made
/// absolute against `cwd`, since Python runs with a different working
/// directory), else the dev or release folder in Application Support.
fn resolve_data_dir(env: Option<&str>, home: &Path, cwd: &Path, dev: bool) -> PathBuf {
    if let Some(raw) = env.map(str::trim).filter(|s| !s.is_empty()) {
        let expanded = if raw == "~" {
            home.to_path_buf()
        } else if let Some(rest) = raw.strip_prefix("~/") {
            home.join(rest)
        } else {
            PathBuf::from(raw)
        };
        return if expanded.is_absolute() {
            expanded
        } else {
            cwd.join(expanded)
        };
    }
    let name = if dev { DEV_DATA_DIR } else { RELEASE_DATA_DIR };
    home.join("Library/Application Support").join(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    const HOME: &str = "/Users/x";
    const CWD: &str = "/work";

    fn resolve(env: Option<&str>, dev: bool) -> PathBuf {
        resolve_data_dir(env, Path::new(HOME), Path::new(CWD), dev)
    }

    #[test]
    fn debug_build_defaults_to_the_dev_data_dir() {
        assert_eq!(
            resolve(None, true),
            Path::new("/Users/x/Library/Application Support/Plaud V2 (Dev)")
        );
    }

    #[test]
    fn release_build_defaults_to_the_installed_data_dir() {
        assert_eq!(
            resolve(None, false),
            Path::new("/Users/x/Library/Application Support/Plaud V2")
        );
    }

    #[test]
    fn env_override_wins_in_both_builds() {
        for dev in [true, false] {
            assert_eq!(resolve(Some("/data/plaud"), dev), Path::new("/data/plaud"));
            assert_eq!(
                resolve(Some("  /data/plaud  "), dev),
                Path::new("/data/plaud")
            );
        }
    }

    #[test]
    fn empty_env_is_ignored() {
        assert_eq!(resolve(Some(""), true), resolve(None, true));
        assert_eq!(resolve(Some("   "), false), resolve(None, false));
    }

    #[test]
    fn env_tilde_and_relative_paths_are_made_absolute() {
        assert_eq!(resolve(Some("~"), true), Path::new(HOME));
        assert_eq!(
            resolve(Some("~/plaud-dev"), true),
            Path::new("/Users/x/plaud-dev")
        );
        assert_eq!(resolve(Some("rel/dir"), false), Path::new("/work/rel/dir"));
    }

    #[test]
    fn dev_and_release_support_dirs_differ() {
        let lib = Path::new("/Users/x/Library");
        assert_eq!(
            support_dir_for(lib, false),
            Path::new("/Users/x/Library/Application Support/com.plaud.v2")
        );
        assert_eq!(
            support_dir_for(lib, true),
            Path::new("/Users/x/Library/Application Support/com.plaud.v2.dev")
        );
    }

    #[test]
    fn this_test_build_is_kept_apart_from_the_installed_app() {
        // `cargo test` is a debug build.
        assert!(IS_DEV);
        assert!(app_support_dir().ends_with("com.plaud.v2.dev"));
        assert!(shared_support_dir().ends_with("com.plaud.v2"));
        assert!(log_dir().ends_with("Plaud V2 (Dev)"));
    }

    #[test]
    fn folder_names_are_pinned_to_the_legacy_name() {
        // Existing installs keep their data only while these stay exactly so.
        assert_eq!(RELEASE_DATA_DIR, "Plaud V2");
        assert_eq!(DEV_DATA_DIR, "Plaud V2 (Dev)");
        assert_eq!(RELEASE_SUPPORT_DIR, "com.plaud.v2");
        assert_eq!(DEV_SUPPORT_DIR, "com.plaud.v2.dev");
        assert_eq!(RELEASE_LOG_DIR, "Plaud V2");
        assert_eq!(DEV_LOG_DIR, "Plaud V2 (Dev)");
        let lib = Path::new("/Users/x/Library");
        assert_eq!(
            resolve(None, false),
            Path::new("/Users/x/Library/Application Support/Plaud V2")
        );
        assert_eq!(
            lib.join("Logs").join(RELEASE_LOG_DIR),
            Path::new("/Users/x/Library/Logs/Plaud V2")
        );
    }

    #[test]
    fn product_rename_kept_the_identifier() {
        let conf: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        // The bundle id carries Bluetooth permission, the Keychain backup and
        // the support folder; the updater insists on it too.
        assert_eq!(conf["identifier"], "com.plaud.v2");
        assert_eq!(RELEASE_SUPPORT_DIR, conf["identifier"].as_str().unwrap());
        // The product name is free to change: nothing on disk is derived from it.
        assert_eq!(conf["productName"], "Open Pin");
        assert_ne!(conf["productName"].as_str(), Some(RELEASE_DATA_DIR));
        // Plaud's sign-in redirect.
        assert_eq!(conf["plugins"]["deep-link"]["desktop"]["schemes"][0], "plaud");
    }

    #[test]
    fn data_dir_env_matches_the_backend() {
        let constants = include_str!("../../app/utils/constants.py");
        assert!(constants.contains(&format!("\"{DATA_DIR_ENV}\"")));
    }
}
