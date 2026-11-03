//! One-off moves for the rename from "Plaud V2" to Open Pin. Nothing here
//! touches user data: data, logs, the venv and the Keychain backup keep their
//! old names on purpose (see `paths`). Two things do depend on the app's name:
//!
//! 1. **The bundle's file name.** Installs from before the rename update
//!    themselves with the installer of the version they run, which copies the
//!    new app over `Plaud V2.app` and keeps that name. On the first launch from
//!    a bundle still called exactly `Plaud V2.app`, [`plan_bundle_rename`] asks
//!    for it to be renamed `Open Pin.app` in the same folder: a detached helper
//!    waits for this process to exit, renames the bundle with `mv` (a rename on
//!    the same volume: nothing is copied, nothing can be half-written) and
//!    opens it again. If anything is off (a bundle already has the new name,
//!    another copy is still running from the old one, `mv` fails) it reopens
//!    the old bundle instead. It's tried once (a marker file in Application
//!    Support); after that the updater's installer does the rename on the next
//!    update (see `commands::INSTALL_SCRIPT`). A bundle the user renamed to
//!    anything else is left alone.
//!
//! 2. **The "Open at login" LaunchAgent.** tauri-plugin-autostart names it
//!    after the product: `~/Library/LaunchAgents/Plaud V2.plist` before,
//!    `Open Pin.plist` now, with the executable's full path inside. At startup
//!    [`login_item_fix`] removes the old file and asks for the login item to be
//!    re-enabled under the new name; it also refreshes the new one when it
//!    points at an old path (app moved or renamed).

use std::path::{Path, PathBuf};

use crate::commands::LEGACY_BUNDLE_NAME;

/// The bundle name releases ship with now (`productName` + `.app`).
pub const BUNDLE_NAME: &str = "Open Pin.app";

/// LaunchAgent file the autostart plugin wrote before the rename.
pub const LEGACY_LOGIN_ITEM: &str = "Plaud V2.plist";

/// Marker: the launch-time rename was tried (whatever the outcome).
const RENAME_MARKER: &str = "bundle-rename-tried";

/// Where to rename the running bundle to, if it should be renamed now.
///
/// `app_path` is the running `.app`. Returns `Some(new path)` only when its
/// file name is exactly the old default, it isn't translocated, and nothing
/// exists at the new name yet.
pub fn plan_bundle_rename(app_path: &Path) -> Option<PathBuf> {
    if app_path.file_name()?.to_str()? != LEGACY_BUNDLE_NAME {
        return None;
    }
    if app_path.to_string_lossy().contains("/AppTranslocation/") {
        return None;
    }
    let target = app_path.parent()?.join(BUNDLE_NAME);
    if target.exists() || target.symlink_metadata().is_ok() {
        return None;
    }
    Some(target)
}

/// Waits for the app to quit, renames the bundle and reopens it. Values are
/// positional arguments, never part of the script text.
const RENAME_SCRIPT: &str = r#"
old_app="$1"
new_app="$2"
old_pid="$3"
log_file="$4"
mkdir -p "$(dirname "$log_file")"
exec >>"$log_file" 2>&1
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
log "Rename: $old_app -> $new_app (waiting for pid $old_pid)"
i=0
while kill -0 "$old_pid" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 300 ]; then
    log "Rename: app still running after 30 s; leaving it alone"
    exit 1
  fi
  sleep 0.1
done
if pgrep -f "$old_app/Contents/MacOS/" >/dev/null 2>&1; then
  log "Rename: another copy is running from $old_app; not renaming"
  open "$old_app"
  exit 1
fi
if [ -e "$new_app" ]; then
  log "Rename: $new_app already exists; not renaming"
  open "$old_app"
  exit 1
fi
if mv "$old_app" "$new_app"; then
  log "Rename: done; opening $new_app"
  open "$new_app"
  exit 0
fi
log "Rename: mv failed; opening $old_app"
open "$old_app"
exit 1
"#;

/// Release builds only: if the running bundle should be renamed (see the
/// module docs) and that hasn't been tried yet, start the helper and return
/// true. The caller then exits straight away, before anything else starts.
pub fn start_bundle_rename_if_needed() -> bool {
    if cfg!(debug_assertions) {
        return false;
    }
    let app_path = PathBuf::from(crate::commands::running_app_path());
    let Some(target) = plan_bundle_rename(&app_path) else {
        return false;
    };
    let marker = crate::paths::app_support_dir().join(RENAME_MARKER);
    if marker.exists() {
        return false;
    }
    // Written first, so a failed rename can't turn into a relaunch loop.
    if let Some(dir) = marker.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if std::fs::write(&marker, format!("{} -> {}\n", app_path.display(), target.display())).is_err() {
        log::warn!("[Rename] Couldn't write {}; not renaming", marker.display());
        return false;
    }
    let log_file = crate::paths::log_dir().join("update.log");
    log::info!(
        "[Rename] Renaming {} to {} (relaunching; log: {})",
        app_path.display(),
        target.display(),
        log_file.display()
    );
    let mut cmd = std::process::Command::new("/bin/bash");
    cmd.args(["-c", RENAME_SCRIPT, "plaud-v2-rename-bundle"])
        .arg(&app_path)
        .arg(&target)
        .arg(std::process::id().to_string())
        .arg(&log_file)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    #[cfg(unix)]
    unsafe {
        use std::os::unix::process::CommandExt;
        cmd.pre_exec(|| {
            if libc::setsid() == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    match cmd.spawn() {
        Ok(_) => true,
        Err(e) => {
            log::warn!("[Rename] Couldn't start the helper: {e}");
            false
        }
    }
}

/// What to do about the login item at startup.
#[derive(Debug, PartialEq, Eq)]
pub struct LoginItemFix {
    /// The pre-rename LaunchAgent file to delete.
    pub remove: Option<PathBuf>,
    /// Re-enable the login item (writes the current name and path).
    pub enable: bool,
}

/// Decide how to bring the LaunchAgent up to date. `dir` is
/// `~/Library/LaunchAgents`, `product` the current product name and `exe`
/// the path the plugin registers (the canonical executable path).
pub fn login_item_fix(dir: &Path, product: &str, exe: &str) -> LoginItemFix {
    let legacy = dir.join(LEGACY_LOGIN_ITEM);
    let current = dir.join(format!("{product}.plist"));
    if legacy != current && legacy.is_file() {
        return LoginItemFix {
            remove: Some(legacy),
            enable: true,
        };
    }
    let stale = match std::fs::read_to_string(&current) {
        Ok(text) => !text.contains(&format!("<string>{exe}</string>")),
        Err(_) => false, // not enabled: nothing to refresh
    };
    LoginItemFix {
        remove: None,
        enable: stale,
    }
}

/// Apply `login_item_fix` through the autostart plugin. Logs, never fails.
/// Release builds only: a dev build must never take over the installed app's
/// login item.
pub fn fix_login_item(app: &tauri::AppHandle) {
    use tauri_plugin_autostart::ManagerExt;
    if cfg!(debug_assertions) {
        return;
    }
    let Ok(exe) = std::env::current_exe().and_then(|p| p.canonicalize()) else {
        return;
    };
    let dir = crate::home_dir().join("Library/LaunchAgents");
    let fix = login_item_fix(&dir, &app.package_info().name, &exe.to_string_lossy());
    if let Some(old) = &fix.remove {
        match std::fs::remove_file(old) {
            Ok(()) => log::info!("[LoginItem] Removed the pre-rename login item {}", old.display()),
            Err(e) => log::warn!("[LoginItem] Couldn't remove {}: {e}", old.display()),
        }
    }
    if fix.enable {
        match app.autolaunch().enable() {
            Ok(()) => log::info!("[LoginItem] Open at login re-registered for {}", exe.display()),
            Err(e) => log::warn!("[LoginItem] Couldn't re-register Open at login: {e}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "plaud-v2-migrate-{tag}-{}-{}",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn bundle_name_matches_the_product_name() {
        let conf: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert_eq!(format!("{}.app", conf["productName"].as_str().unwrap()), BUNDLE_NAME);
        assert_eq!(LEGACY_BUNDLE_NAME, "Plaud V2.app");
        assert_eq!(LEGACY_LOGIN_ITEM, "Plaud V2.plist");
    }

    #[test]
    fn renames_only_the_legacy_default_name() {
        let dir = temp("plan");
        let legacy = dir.join("Plaud V2.app");
        assert_eq!(plan_bundle_rename(&legacy), Some(dir.join("Open Pin.app")));
        // Already renamed, or renamed by the user: leave it.
        assert_eq!(plan_bundle_rename(&dir.join("Open Pin.app")), None);
        assert_eq!(plan_bundle_rename(&dir.join("Recorder.app")), None);
        assert_eq!(plan_bundle_rename(&dir.join("Plaud V2 2.app")), None);
        // Translocated copies can't be renamed.
        assert_eq!(
            plan_bundle_rename(Path::new("/private/var/folders/x/AppTranslocation/A/d/Plaud V2.app")),
            None
        );
        // Something already at the new name: don't touch either.
        std::fs::create_dir_all(dir.join("Open Pin.app")).unwrap();
        assert_eq!(plan_bundle_rename(&legacy), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn rename_script_renames_and_reopens() {
        use std::os::unix::fs::PermissionsExt;
        let dir = temp("script");
        let bin = dir.join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        let open = bin.join("open");
        std::fs::write(&open, "#!/bin/bash\necho \"$1\" >> \"$OPENED\"\n").unwrap();
        std::fs::set_permissions(&open, std::fs::Permissions::from_mode(0o755)).unwrap();
        let run = |old: &Path, new: &Path| {
            let mut done = std::process::Command::new("/usr/bin/true").spawn().unwrap();
            let pid = done.id();
            done.wait().unwrap();
            std::process::Command::new("/bin/bash")
                .args(["-c", RENAME_SCRIPT, "test"])
                .arg(old)
                .arg(new)
                .arg(pid.to_string())
                .arg(dir.join("update.log"))
                .env("PATH", format!("{}:/usr/bin:/bin", bin.display()))
                .env("OPENED", dir.join("opened.txt"))
                .status()
                .unwrap()
                .code()
        };
        let opened = || std::fs::read_to_string(dir.join("opened.txt")).unwrap_or_default();

        let old = dir.join("Plaud V2.app");
        let new = dir.join("Open Pin.app");
        std::fs::create_dir_all(old.join("Contents/MacOS")).unwrap();
        std::fs::write(old.join("Contents/MacOS/plaud-v2"), "x").unwrap();
        assert_eq!(run(&old, &new), Some(0));
        assert!(!old.exists());
        assert_eq!(std::fs::read_to_string(new.join("Contents/MacOS/plaud-v2")).unwrap(), "x");
        assert_eq!(opened(), format!("{}\n", new.display()));

        // Target appeared meanwhile: old bundle reopened, nothing moved.
        std::fs::create_dir_all(old.join("Contents/MacOS")).unwrap();
        assert_eq!(run(&old, &new), Some(1));
        assert!(old.exists() && new.exists());
        assert!(opened().ends_with(&format!("{}\n", old.display())));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn login_item_moves_from_the_legacy_name() {
        let dir = temp("login");
        let exe = "/Applications/Open Pin.app/Contents/MacOS/plaud-v2";
        // Nothing registered: nothing to do.
        assert_eq!(
            login_item_fix(&dir, "Open Pin", exe),
            LoginItemFix { remove: None, enable: false }
        );
        // Registered before the rename: remove it and register again.
        let legacy = dir.join("Plaud V2.plist");
        std::fs::write(&legacy, "<string>/Applications/Plaud V2.app/Contents/MacOS/plaud-v2</string>").unwrap();
        assert_eq!(
            login_item_fix(&dir, "Open Pin", exe),
            LoginItemFix { remove: Some(legacy.clone()), enable: true }
        );
        std::fs::remove_file(&legacy).unwrap();
        // Registered under the new name with the current path: leave it.
        let current = dir.join("Open Pin.plist");
        std::fs::write(&current, format!("<array><string>{exe}</string></array>")).unwrap();
        assert_eq!(
            login_item_fix(&dir, "Open Pin", exe),
            LoginItemFix { remove: None, enable: false }
        );
        // Pointing at an old path (app moved or renamed): refresh it.
        assert_eq!(
            login_item_fix(&dir, "Open Pin", "/Users/x/Applications/Open Pin.app/Contents/MacOS/plaud-v2"),
            LoginItemFix { remove: None, enable: true }
        );
        // A dev build still called Plaud V2 never deletes its own file.
        std::fs::write(&legacy, "<string>/x</string>").unwrap();
        assert_eq!(
            login_item_fix(&dir, "Plaud V2", "/x"),
            LoginItemFix { remove: None, enable: false }
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
