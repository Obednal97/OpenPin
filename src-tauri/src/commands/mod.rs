use serde::Serialize;
use std::sync::atomic::Ordering;

use crate::python;

// ---- API Info ----

#[derive(Serialize)]
pub struct ApiInfo {
    port: u16,
    url: String,
    error: Option<python::StartupError>,
    status: String,
    /// Per-launch API token; the frontend sends it as `X-Plaud-Token`
    /// (or `?token=` on GET /audio). See `python::PythonServer::api_token`.
    token: String,
}

#[tauri::command]
pub fn get_api_info() -> ApiInfo {
    let server = python::get_server().lock().unwrap();
    let has_process = server.process.is_some();
    let has_error = server.startup_error.is_some();
    let status = if has_error {
        "error"
    } else if has_process {
        if python::SERVER_READY.load(Ordering::Acquire) {
            "ready"
        } else {
            "starting"
        }
    } else {
        "stopped"
    };
    ApiInfo {
        port: server.port,
        url: server.url.clone(),
        error: server.startup_error.clone(),
        status: status.to_string(),
        token: server.api_token.clone(),
    }
}

// ---- Theme ----

use std::sync::Mutex;

static THEME_PREF: std::sync::OnceLock<Mutex<String>> = std::sync::OnceLock::new();

fn theme_pref() -> &'static Mutex<String> {
    THEME_PREF.get_or_init(|| Mutex::new("auto".to_string()))
}

#[derive(Serialize)]
pub struct ThemeInfo {
    preference: String,
    resolved: String,
}

fn resolve_theme(pref: &str) -> String {
    if pref == "auto" {
        // Check macOS system appearance
        let output = std::process::Command::new("defaults")
            .args(["read", "-g", "AppleInterfaceStyle"])
            .output();
        match output {
            Ok(o) if String::from_utf8_lossy(&o.stdout).trim().eq_ignore_ascii_case("dark") => {
                "dark".to_string()
            }
            _ => "light".to_string(),
        }
    } else {
        pref.to_string()
    }
}

#[tauri::command]
pub async fn cycle_theme(app: tauri::AppHandle) -> ThemeInfo {
    // Update the preference under the lock, then drop it before resolving — a
    // slow `defaults read` subprocess inside resolve_theme would otherwise hold
    // THEME_PREF and block every concurrent get_theme/cycle_theme IPC call.
    let preference = {
        let mut pref = theme_pref().lock().unwrap();
        *pref = match pref.as_str() {
            "auto" => "light".to_string(),
            "light" => "dark".to_string(),
            "dark" => "auto".to_string(),
            _ => "auto".to_string(),
        };
        pref.clone()
    };
    let resolved = resolve_theme(&preference);

    // Dispatch dock-icon update to the main thread — AppKit requires it.
    #[cfg(target_os = "macos")]
    {
        let is_dark = resolved == "dark";
        app.run_on_main_thread(move || {
            crate::set_dock_icon_for_theme(is_dark);
        })
        .expect("run_on_main_thread should not fail");
    }
    #[cfg(not(target_os = "macos"))]
    let _ = app;

    ThemeInfo {
        resolved,
        preference,
    }
}

#[tauri::command]
pub fn get_theme() -> ThemeInfo {
    // Snapshot the preference under the lock, drop, then resolve outside —
    // see cycle_theme for the rationale.
    let preference = theme_pref().lock().unwrap().clone();
    let resolved = resolve_theme(&preference);
    ThemeInfo {
        resolved,
        preference,
    }
}

// ---- Login Item ----

#[derive(Serialize)]
pub struct LoginItemInfo {
    enabled: bool,
}

#[tauri::command]
pub fn get_login_item(app: tauri::AppHandle) -> LoginItemInfo {
    use tauri_plugin_autostart::ManagerExt;
    let manager = app.autolaunch();
    let enabled = manager.is_enabled().unwrap_or(false);
    LoginItemInfo { enabled }
}

#[tauri::command]
pub fn set_login_item(app: tauri::AppHandle, enabled: bool) -> LoginItemInfo {
    use tauri_plugin_autostart::ManagerExt;
    let manager = app.autolaunch();
    if enabled {
        let _ = manager.enable();
    } else {
        let _ = manager.disable();
    }
    let actual = manager.is_enabled().unwrap_or(false);
    LoginItemInfo { enabled: actual }
}

// ---- Dock Visibility ----

#[derive(Serialize)]
pub struct DockInfo {
    visible: bool,
}

#[tauri::command]
pub async fn get_dock_visible(app: tauri::AppHandle) -> DockInfo {
    #[cfg(target_os = "macos")]
    {
        use objc2::MainThreadMarker;
        use objc2_app_kit::{NSApplication, NSApplicationActivationPolicy};
        let (tx, rx) = tokio::sync::oneshot::channel::<bool>();
        app.run_on_main_thread(move || {
            let mtm = MainThreadMarker::new()
                .expect("run_on_main_thread guarantees execution on the main thread");
            let ns_app = NSApplication::sharedApplication(mtm);
            let visible =
                ns_app.activationPolicy() == NSApplicationActivationPolicy::Regular;
            let _ = tx.send(visible);
        })
        .expect("run_on_main_thread should not fail");
        let visible = rx.await.expect("oneshot sender closed before sending");
        return DockInfo { visible };
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app; // suppress unused-variable warning on non-macOS
        DockInfo { visible: true }
    }
}

#[tauri::command]
pub async fn set_dock_visible(app: tauri::AppHandle, visible: bool) -> DockInfo {
    #[cfg(target_os = "macos")]
    {
        use objc2::MainThreadMarker;
        use objc2_app_kit::{NSApplication, NSApplicationActivationPolicy};
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        app.run_on_main_thread(move || {
            let mtm = MainThreadMarker::new()
                .expect("run_on_main_thread guarantees execution on the main thread");
            let ns_app = NSApplication::sharedApplication(mtm);
            let policy = if visible {
                NSApplicationActivationPolicy::Regular
            } else {
                NSApplicationActivationPolicy::Accessory
            };
            ns_app.setActivationPolicy(policy);
            if !visible {
                if let Some(w) = ns_app.mainWindow() {
                    w.makeKeyAndOrderFront(None);
                }
            }
            let _ = tx.send(());
        })
        .expect("run_on_main_thread should not fail");
        rx.await.expect("oneshot sender closed before sending");
    }
    // Persist preference
    let mut prefs = crate::load_prefs();
    prefs["dockVisible"] = serde_json::json!(visible);
    crate::save_prefs(&prefs);
    DockInfo { visible }
}

// ---- Open External ----

#[tauri::command]
pub fn open_external(url: String) -> Result<(), String> {
    let lower = url.to_ascii_lowercase();
    let allowed = lower.starts_with("https://")
        || lower.starts_with("http://")
        || lower.starts_with("mailto:");
    if !allowed {
        return Err(format!("Refused to open URL with disallowed scheme: {url}"));
    }
    open::that(&url).map_err(|e| format!("Failed to open URL: {e}"))?;
    Ok(())
}

// ---- App Version ----

#[tauri::command]
pub fn get_app_version(app: tauri::AppHandle) -> String {
    app.package_info().version.to_string()
}

// ---- Auto-Updater ----
//
// All GitHub access goes through the `plaud-v2-updates` Cloudflare Worker
// (infra/update-worker/). The app ships no GitHub token: the Worker holds it as
// a secret, so it can be rotated without an app release.

mod sha256;

/// Base URL of the update / bug-report Worker (infra/update-worker/).
pub(crate) const WORKER_BASE: &str = "https://plaud-v2-updates.leave-hacker-feedback.workers.dev";

/// Bundle identity the installer insists on before swapping the app.
const EXPECTED_BUNDLE_ID: &str = "com.plaud.v2";
const EXPECTED_TEAM_ID: &str = "4SN93PWF7Z";

fn worker_user_agent() -> String {
    format!("Plaud-V2/{}", app_version())
}

/// Client for small JSON calls to the Worker.
fn worker_client() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .connect_timeout(std::time::Duration::from_secs(15))
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .unwrap_or_else(|_| reqwest::blocking::Client::new())
}

/// Client for the DMG download. In reqwest's *blocking* client `timeout` is
/// applied to each wait (sending the request, then every `read()` call), so it
/// acts as an idle timeout, not a cap on the whole transfer — a slow but
/// steady download never times out.
fn download_client() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .connect_timeout(std::time::Duration::from_secs(15))
        .timeout(std::time::Duration::from_secs(60))
        .build()
        .unwrap_or_else(|_| reqwest::blocking::Client::new())
}

/// `major.minor.patch` → numbers. A leading `v` and anything after the first
/// `-` or `+` (pre-release / build metadata) are ignored, so `2.3.0-beta`
/// parses as `2.3.0` and is never "newer" than `2.3.0`.
fn parse_version(v: &str) -> Vec<u64> {
    let core = v.trim().trim_start_matches(['v', 'V']);
    let core = core.split(['-', '+']).next().unwrap_or("");
    core.split('.')
        .map(|part| {
            let digits: String = part.chars().take_while(|c| c.is_ascii_digit()).collect();
            digits.parse::<u64>().unwrap_or(0)
        })
        .collect()
}

/// True if `latest` is strictly newer than `current` (missing parts = 0).
fn is_newer_version(latest: &str, current: &str) -> bool {
    let mut l = parse_version(latest);
    let mut c = parse_version(current);
    let n = l.len().max(c.len());
    l.resize(n, 0);
    c.resize(n, 0);
    l > c
}

/// A release asset name we're willing to write into the updates directory.
fn is_safe_asset_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 150
        && name.ends_with(".dmg")
        && !name.starts_with('.')
        && !name.contains('/')
        && !name.contains('\\')
        && !name.contains("..")
}

fn is_hex_sha256(s: &str) -> bool {
    s.len() == 64 && s.chars().all(|c| c.is_ascii_hexdigit())
}

/// macOS runs quarantined apps opened from Downloads/DMGs from a randomised
/// read-only path; the updater can't replace the app there.
fn is_translocated(app_path: &str) -> bool {
    app_path.contains("/AppTranslocation/")
}

const TRANSLOCATED_MESSAGE: &str =
    "Plaud V2 is running from a temporary location, so it can't update itself. Quit it, drag Plaud V2 into your Applications folder, and open it from there.";

#[derive(serde::Deserialize, Debug, Clone)]
struct LatestAsset {
    name: String,
    size: u64,
    #[serde(default)]
    sha256: Option<String>,
}

#[derive(serde::Deserialize, Debug, Clone)]
struct LatestRelease {
    version: String,
    asset: LatestAsset,
    download_url: String,
}

fn fetch_latest_release() -> Result<LatestRelease, String> {
    let resp = worker_client()
        .get(format!("{WORKER_BASE}/v1/latest"))
        .header("User-Agent", worker_user_agent())
        .header("Accept", "application/json")
        .send()
        .map_err(|e| format!("Couldn't reach the update server: {e}"))?;
    let status = resp.status();
    if !status.is_success() {
        return Err(format!("Update server returned HTTP {}", status.as_u16()));
    }
    let release: LatestRelease = resp
        .json()
        .map_err(|e| format!("Update server sent an unexpected response: {e}"))?;
    if release.version.trim().is_empty() {
        return Err("Update server sent no version".to_string());
    }
    if !is_safe_asset_name(&release.asset.name) {
        return Err(format!("Refusing update asset with unexpected name: {}", release.asset.name));
    }
    if !release.download_url.starts_with(&format!("{WORKER_BASE}/")) {
        return Err("Update server sent an unexpected download URL".to_string());
    }
    if let Some(sha) = &release.asset.sha256 {
        if !is_hex_sha256(sha) {
            return Err("Update server sent a malformed checksum".to_string());
        }
    }
    Ok(release)
}

/// Cached pending update info
static PENDING_UPDATE: std::sync::OnceLock<std::sync::Mutex<Option<PendingUpdate>>> =
    std::sync::OnceLock::new();

/// Only one check/download at a time (startup timer, 4-hourly timer and the
/// Settings button can overlap and would otherwise write the same `.part`).
static UPDATE_CHECK_RUNNING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

struct PendingUpdate {
    version: String,
    dmg_path: String,
}

fn pending_update() -> &'static std::sync::Mutex<Option<PendingUpdate>> {
    PENDING_UPDATE.get_or_init(|| std::sync::Mutex::new(None))
}

fn updates_dir() -> std::path::PathBuf {
    crate::home_dir().join("Library/Application Support/com.plaud.v2/updates")
}

fn update_log_path() -> std::path::PathBuf {
    crate::home_dir().join("Library/Logs/Plaud V2/update.log")
}

fn running_app_path() -> String {
    // /Applications/Plaud V2.app/Contents/MacOS/plaud-v2 → /Applications/Plaud V2.app
    let exe_path = std::env::current_exe().unwrap_or_default();
    exe_path
        .parent()
        .and_then(|p| p.parent())
        .and_then(|p| p.parent())
        .unwrap_or(&exe_path)
        .to_string_lossy()
        .to_string()
}

/// SHA-256 of a file on disk (hex).
fn file_sha256(path: &std::path::Path) -> Result<String, String> {
    use std::io::Read;
    let mut f = std::fs::File::open(path).map_err(|e| format!("Couldn't open {}: {e}", path.display()))?;
    let mut h = sha256::Sha256::new();
    let mut buf = vec![0u8; 1 << 16];
    loop {
        let n = f.read(&mut buf).map_err(|e| format!("Read error: {e}"))?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(h.finalize_hex())
}

/// Does an already-downloaded file match the release asset?
fn cached_download_matches(path: &std::path::Path, asset: &LatestAsset) -> bool {
    match path.metadata() {
        Ok(meta) if meta.is_file() && meta.len() == asset.size => match &asset.sha256 {
            Some(expected) => file_sha256(path).map(|h| h.eq_ignore_ascii_case(expected)).unwrap_or(false),
            None => true,
        },
        _ => false,
    }
}

/// Remove stale downloads (old versions, interrupted `.part` files).
fn prune_updates_dir(dir: &std::path::Path, keep: &std::path::Path) {
    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p != keep && p.is_file() {
                let _ = std::fs::remove_file(&p);
            }
        }
    }
}

#[tauri::command]
pub fn check_for_updates(app: tauri::AppHandle) {
    use std::sync::atomic::Ordering as AtomicOrdering;
    use tauri::Emitter;
    let current = app.package_info().version.to_string();

    let emit_now = {
        let app = app.clone();
        move |status: &str, info: serde_json::Value| {
            let mut payload = info;
            payload["status"] = serde_json::json!(status);
            let _ = app.emit("update-status", &payload);
        }
    };

    // A finished download is waiting — just re-announce it.
    if let Some(p) = pending_update().lock().unwrap().as_ref() {
        emit_now("ready", serde_json::json!({ "version": p.version }));
        return;
    }
    if UPDATE_CHECK_RUNNING.swap(true, AtomicOrdering::AcqRel) {
        log::info!("[Updater] Check already in progress");
        return;
    }

    std::thread::spawn(move || {
        struct Reset;
        impl Drop for Reset {
            fn drop(&mut self) {
                UPDATE_CHECK_RUNNING.store(false, std::sync::atomic::Ordering::Release);
            }
        }
        let _reset = Reset;
        let emit = emit_now;

        let release = match fetch_latest_release() {
            Ok(r) => r,
            Err(e) => {
                log::error!("[Updater] Check failed: {e}");
                emit("error", serde_json::json!({ "message": e }));
                return;
            }
        };
        let latest = release.version.trim_start_matches('v').to_string();
        if !is_newer_version(&latest, &current) {
            log::info!("[Updater] App is up to date ({current}; latest {latest})");
            emit("up-to-date", serde_json::json!({}));
            return;
        }

        if is_translocated(&running_app_path()) {
            log::warn!("[Updater] v{latest} available but the app is translocated");
            emit("error", serde_json::json!({ "message": TRANSLOCATED_MESSAGE, "version": latest }));
            return;
        }

        log::info!("[Updater] Update available: v{latest}");
        emit("available", serde_json::json!({ "version": latest }));

        let cache_dir = updates_dir();
        if let Err(e) = std::fs::create_dir_all(&cache_dir) {
            emit("error", serde_json::json!({ "message": format!("Couldn't create the updates folder: {e}") }));
            return;
        }
        let dmg_path = cache_dir.join(&release.asset.name);

        if cached_download_matches(&dmg_path, &release.asset) {
            log::info!("[Updater] Update already downloaded");
        } else {
            log::info!("[Updater] Downloading {}...", release.asset.name);
            if let Err(e) = download_update(&release, &dmg_path, |percent| {
                emit("downloading", serde_json::json!({ "percent": percent }));
            }) {
                log::error!("[Updater] Download failed: {e}");
                emit("error", serde_json::json!({ "message": e }));
                return;
            }
            log::info!("[Updater] Update downloaded: v{latest}");
        }
        prune_updates_dir(&cache_dir, &dmg_path);

        *pending_update().lock().unwrap() = Some(PendingUpdate {
            version: latest.clone(),
            dmg_path: dmg_path.to_string_lossy().to_string(),
        });
        emit("ready", serde_json::json!({ "version": latest }));
    });
}

/// Download the release DMG to `<dest>.part`, check its size (and SHA-256 when
/// the release publishes one), then atomically rename it to `dest`.
fn download_update(
    release: &LatestRelease,
    dest: &std::path::Path,
    on_progress: impl Fn(u32),
) -> Result<(), String> {
    use std::io::{Read, Write};

    let part = {
        let mut name = dest.file_name().unwrap_or_default().to_os_string();
        name.push(".part");
        dest.with_file_name(name)
    };
    let _ = std::fs::remove_file(&part);

    // The Worker answers with a 302 to GitHub's signed, token-free URL;
    // reqwest follows it.
    let mut resp = download_client()
        .get(&release.download_url)
        .header("User-Agent", worker_user_agent())
        .send()
        .map_err(|e| format!("Download request failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("Download failed: HTTP {}", resp.status().as_u16()));
    }

    let expected = release.asset.size;
    let total = resp.content_length().unwrap_or(expected);
    let result = (|| -> Result<(), String> {
        let mut file = std::fs::File::create(&part).map_err(|e| format!("Failed to create file: {e}"))?;
        let mut hasher = sha256::Sha256::new();
        let mut downloaded: u64 = 0;
        let mut last_pct: u32 = u32::MAX;
        let mut buf = vec![0u8; 1 << 16];
        loop {
            let n = resp.read(&mut buf).map_err(|e| format!("Download interrupted: {e}"))?;
            if n == 0 {
                break;
            }
            file.write_all(&buf[..n]).map_err(|e| format!("Write error: {e}"))?;
            hasher.update(&buf[..n]);
            downloaded += n as u64;
            if downloaded > expected.saturating_add(1024 * 1024) {
                return Err("Download is larger than expected".to_string());
            }
            if total > 0 {
                let pct = ((downloaded.min(total) * 100) / total) as u32;
                if pct != last_pct {
                    last_pct = pct;
                    on_progress(pct);
                }
            }
        }
        file.sync_all().map_err(|e| format!("Write error: {e}"))?;
        if downloaded != expected {
            return Err(format!("Download incomplete: got {downloaded} of {expected} bytes"));
        }
        let digest = hasher.finalize_hex();
        if let Some(want) = &release.asset.sha256 {
            if !digest.eq_ignore_ascii_case(want) {
                return Err("Downloaded update failed its checksum — not installing it".to_string());
            }
        }
        log::info!("[Updater] Downloaded {downloaded} bytes, sha256 {digest}");
        std::fs::rename(&part, dest).map_err(|e| format!("Couldn't finalise download: {e}"))
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&part);
    }
    result
}

#[derive(Serialize)]
pub struct UpdateResult {
    installing: bool,
    error: Option<String>,
}

/// Installer run by a detached bash after the app quits. Every value is a
/// positional argument ($1…$5), never interpolated into the script body, so a
/// crafted asset name can't inject shell.
const INSTALL_SCRIPT: &str = r#"
dmg_path="$1"
app_path="$2"
expected_version="$3"
old_pid="$4"
log_file="$5"
EXPECTED_BUNDLE_ID="__BUNDLE_ID__"
EXPECTED_TEAM="__TEAM_ID__"

mkdir -p "$(dirname "$log_file")"
exec >>"$log_file" 2>&1
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

MNT=""
cleanup() {
  if [ -n "$MNT" ]; then
    hdiutil detach "$MNT" -quiet 2>/dev/null || hdiutil detach "$MNT" -force -quiet 2>/dev/null
    rmdir "$MNT" 2>/dev/null
  fi
}
abort() {
  log "ABORT: $*"
  cleanup
  log "Relaunching the existing app"
  open "$app_path"
  exit 1
}

log "Update to v$expected_version: dmg=$dmg_path app=$app_path pid=$old_pid"

# Wait (up to 30 s) for the running app to exit before touching its bundle.
i=0
while kill -0 "$old_pid" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 300 ]; then
    log "ABORT: app (pid $old_pid) is still running after 30 s; leaving it alone"
    exit 1
  fi
  sleep 0.1
done

[ -f "$dmg_path" ] || abort "update file missing"

MNT=$(mktemp -d "${TMPDIR:-/tmp}/plaud-v2-update.XXXXXX") || abort "mktemp failed"
hdiutil attach -nobrowse -noautoopen -readonly -mountpoint "$MNT" "$dmg_path" || abort "hdiutil attach failed"

NEW_APP=$(find "$MNT" -maxdepth 1 -name "*.app" -type d | head -1)
[ -n "$NEW_APP" ] && [ -d "$NEW_APP/Contents/MacOS" ] || abort "no .app found in the update"

plist="$NEW_APP/Contents/Info.plist"
bundle_id=$(plutil -extract CFBundleIdentifier raw -o - "$plist" 2>/dev/null)
[ "$bundle_id" = "$EXPECTED_BUNDLE_ID" ] || abort "bundle id is '$bundle_id', expected '$EXPECTED_BUNDLE_ID'"
new_version=$(plutil -extract CFBundleShortVersionString raw -o - "$plist" 2>/dev/null)
[ "$new_version" = "$expected_version" ] || abort "bundle version is '$new_version', expected '$expected_version'"

codesign --verify --deep --strict "$NEW_APP" || abort "code signature invalid"
codesign -dv --verbose=4 "$NEW_APP" 2>&1 | grep -qx "TeamIdentifier=$EXPECTED_TEAM" \
  || abort "not signed by team $EXPECTED_TEAM"
spctl -a -t exec -vv "$NEW_APP" || abort "Gatekeeper (spctl) rejected the update"
log "Verified $NEW_APP (v$new_version, team $EXPECTED_TEAM)"

backup="${app_path}.old-update"
rm -rf "$backup"
mv "$app_path" "$backup" || abort "couldn't move the old app aside (permissions?)"

if ditto "$NEW_APP" "$app_path" \
   && [ "$(plutil -extract CFBundleShortVersionString raw -o - "$app_path/Contents/Info.plist" 2>/dev/null)" = "$expected_version" ] \
   && codesign --verify --deep --strict "$app_path"; then
  rm -rf "$backup"
  xattr -dr com.apple.quarantine "$app_path" 2>/dev/null
  cleanup
  rm -f "$dmg_path"
  log "Installed v$expected_version; relaunching"
  open "$app_path"
  exit 0
fi

log "Copy failed or the copied app didn't verify; restoring the old app"
rm -rf "$app_path"
if ! mv "$backup" "$app_path"; then
  log "CRITICAL: couldn't restore the old app; it is at $backup"
fi
abort "install failed"
"#;

fn install_script() -> String {
    INSTALL_SCRIPT
        .replace("__BUNDLE_ID__", EXPECTED_BUNDLE_ID)
        .replace("__TEAM_ID__", EXPECTED_TEAM_ID)
}

#[tauri::command]
pub fn install_update(app: tauri::AppHandle) -> UpdateResult {
    use tauri::Emitter;
    let pending = match pending_update().lock().unwrap().take() {
        Some(p) => p,
        None => {
            log::warn!("[Updater] No pending update to install");
            return UpdateResult { installing: false, error: Some("No update is ready to install".into()) };
        }
    };

    let app_path = running_app_path();
    let refuse = |pending: PendingUpdate, msg: String| {
        log::error!("[Updater] {msg}");
        let _ = app.emit("update-status", serde_json::json!({ "status": "error", "message": msg }));
        *pending_update().lock().unwrap() = Some(pending);
        UpdateResult { installing: false, error: Some(msg) }
    };
    if is_translocated(&app_path) {
        return refuse(pending, TRANSLOCATED_MESSAGE.to_string());
    }
    if !app_path.ends_with(".app") {
        return refuse(pending, format!("Not running from an app bundle ({app_path}); can't self-update"));
    }

    let log_file = update_log_path();
    if let Some(dir) = log_file.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    log::info!(
        "[Updater] Installing v{} from {} to {app_path} (log: {})",
        pending.version,
        pending.dmg_path,
        log_file.display()
    );

    // Spawn detached so it survives app.exit(0): pre_exec runs between fork
    // and exec, and setsid() makes the child its own session leader.
    // bash -c '<script>' '<$0>' "$1".."$5" — values never enter the script body.
    let mut cmd = std::process::Command::new("/bin/bash");
    cmd.args(["-c", &install_script(), "plaud-v2-install-update"])
        .arg(&pending.dmg_path)
        .arg(&app_path)
        .arg(&pending.version)
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
    if let Err(e) = cmd.spawn() {
        return refuse(pending, format!("Couldn't start the installer: {e}"));
    }

    // Quit the app so the script can replace it. quit_app hides the window,
    // stops the backend exactly once off the main thread, then exits.
    crate::quit_app(&app, 0);

    UpdateResult { installing: true, error: None }
}

// ---- Bug-report transport (via the Worker) ----

/// What the Worker said about a report.
struct ReportOutcome {
    issue_number: Option<u64>,
    url: Option<String>,
    action: String,
}

fn post_report(payload: &serde_json::Value) -> Result<ReportOutcome, String> {
    let resp = worker_client()
        .post(format!("{WORKER_BASE}/v1/report"))
        .header("User-Agent", worker_user_agent())
        .json(payload)
        .send()
        .map_err(|e| format!("Couldn't reach the report server: {e}"))?;
    let status = resp.status();
    let data = resp.json::<serde_json::Value>().unwrap_or(serde_json::Value::Null);
    if !status.is_success() || data["ok"].as_bool() != Some(true) {
        let detail = data["error"].as_str().unwrap_or("").to_string();
        return Err(if detail.is_empty() {
            format!("Report server returned HTTP {}", status.as_u16())
        } else {
            format!("Report server returned HTTP {}: {detail}", status.as_u16())
        });
    }
    Ok(ReportOutcome {
        issue_number: data["issue_number"].as_u64(),
        url: data["url"].as_str().map(String::from),
        action: data["action"].as_str().unwrap_or("").to_string(),
    })
}

// ---- System Info for Bug Reports ----

/// The Mac's IOPlatformUUID. Never sent anywhere: it's hashed into
/// `device_id()` and used as the secret salt for identifier hashes in reports.
fn platform_uuid() -> Option<String> {
    static UUID: std::sync::OnceLock<Option<String>> = std::sync::OnceLock::new();
    UUID.get_or_init(|| {
        let o = std::process::Command::new("ioreg")
            .args(["-rd1", "-c", "IOPlatformExpertDevice"])
            .output()
            .ok()?;
        let text = String::from_utf8_lossy(&o.stdout);
        let cap = text.split("IOPlatformUUID").nth(1)?;
        cap.split('"').nth(1).map(|s| s.to_string()).filter(|s| !s.is_empty())
    })
    .clone()
}

/// Anonymous device ID — SHA-256 of the hardware UUID, first 8 hex chars.
/// Same value the old `shasum`-based version produced, so it stays stable.
fn device_id() -> String {
    match platform_uuid() {
        Some(uuid) => sha256::hex_digest(uuid.as_bytes()).chars().take(8).collect(),
        None => "unknown".to_string(),
    }
}

// ---- Report scrubbing ----

/// Truncate to at most `max` characters (never splits a UTF-8 character).
fn truncate_chars(s: &str, max: usize) -> String {
    match s.char_indices().nth(max) {
        Some((idx, _)) => format!("{}…", &s[..idx]),
        None => s.to_string(),
    }
}

/// Truncate to at most `max_bytes` bytes on a character boundary.
fn truncate_bytes(s: &str, max_bytes: usize) -> &str {
    if s.len() <= max_bytes {
        return s;
    }
    let mut end = max_bytes;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
}

/// Keep only the last `max_lines` lines of a traceback.
fn tail_lines(s: &str, max_lines: usize) -> String {
    let lines: Vec<&str> = s.trim_end().lines().collect();
    if lines.len() <= max_lines {
        return lines.join("\n");
    }
    let omitted = lines.len() - max_lines;
    format!("… ({omitted} earlier lines omitted)\n{}", lines[omitted..].join("\n"))
}

/// Salted 8-hex-char tag for an identifier (serial number, BLE UUID). The salt
/// is this Mac's hardware UUID, which is never sent, so the tag can't be
/// brute-forced back to the serial but still groups reports from one device.
fn id_tag(salt: &str, value: &str) -> String {
    let mut h = sha256::Sha256::new();
    h.update(b"plaud-v2-report-id:");
    h.update(salt.as_bytes());
    h.update(b":");
    h.update(value.to_ascii_uppercase().as_bytes());
    h.finalize_hex().chars().take(8).collect()
}

fn is_word_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

/// Replace `/Users/<name>` with `/Users/~` (any user, not just this one).
fn scrub_user_dirs(s: &str) -> String {
    const PREFIX: &str = "/Users/";
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(pos) = rest.find(PREFIX) {
        out.push_str(&rest[..pos + PREFIX.len()]);
        rest = &rest[pos + PREFIX.len()..];
        let end = rest
            .find(|c: char| matches!(c, '/' | '"' | '\'' | '`' | ')' | ']' | ',' | ':' | ';') || c.is_whitespace())
            .unwrap_or(rest.len());
        let name = &rest[..end];
        if name.is_empty() || name == "~" || name == "Shared" {
            continue;
        }
        out.push('~');
        rest = &rest[end..];
    }
    out.push_str(rest);
    out
}

/// Bluetooth SIG base-UUID suffix: standard service/characteristic UUIDs are
/// protocol constants, not device identifiers, so they're left alone.
const BLE_BASE_UUID_SUFFIX: &str = "-0000-1000-8000-00805f9b34fb";

fn is_uuid_at(b: &[u8], i: usize) -> bool {
    // 8-4-4-4-12
    if i + 36 > b.len() {
        return false;
    }
    (0..36).all(|k| {
        let c = b[i + k];
        match k {
            8 | 13 | 18 | 23 => c == b'-',
            _ => c.is_ascii_hexdigit(),
        }
    })
}

/// Hash device identifiers: `[SN: …]` tags, standalone 16-hex-digit tokens
/// (the Plaud serial format, e.g. `8820B50256947317`), and CoreBluetooth
/// peripheral UUIDs.
fn scrub_identifiers(s: &str, salt: &str) -> String {
    let b = s.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    let mut last = 0; // start of the not-yet-copied slice
    while i < b.len() {
        // [SN: <anything up to ]>]
        if b[i..].starts_with(b"[SN: ") {
            if let Some(close) = s[i + 5..].find(']') {
                let sn = s[i + 5..i + 5 + close].trim();
                out.push_str(&s[last..i]);
                out.push_str(&format!("[SN: {}]", id_tag(salt, sn)));
                i += 5 + close + 1;
                last = i;
                continue;
            }
        }
        let boundary_before = i == 0 || !is_word_byte(b[i - 1]);
        if boundary_before && is_uuid_at(b, i) && (i + 36 == b.len() || !is_word_byte(b[i + 36])) {
            let uuid = &s[i..i + 36];
            if !uuid.to_ascii_lowercase().ends_with(BLE_BASE_UUID_SUFFIX) {
                out.push_str(&s[last..i]);
                out.push_str(&format!("uuid-{}", id_tag(salt, uuid)));
                i += 36;
                last = i;
                continue;
            }
            i += 36;
            continue;
        }
        if boundary_before && b[i].is_ascii_hexdigit() {
            let mut j = i;
            while j < b.len() && b[j].is_ascii_hexdigit() {
                j += 1;
            }
            let after_ok = j == b.len() || !is_word_byte(b[j]);
            if j - i == 16 && after_ok && b[i..j].iter().any(|c| c.is_ascii_digit()) {
                out.push_str(&s[last..i]);
                out.push_str(&format!("sn-{}", id_tag(salt, &s[i..j])));
                last = j;
            }
            i = j.max(i + 1);
            continue;
        }
        // advance one UTF-8 character
        i += s[i..].chars().next().map(|c| c.len_utf8()).unwrap_or(1);
    }
    out.push_str(&s[last..]);
    out
}

/// Everything that leaves the machine in a bug report goes through this.
fn scrub_report_text(s: &str, home: &str, salt: &str) -> String {
    let mut t = s.to_string();
    let home = home.trim_end_matches('/');
    if home.len() > 1 {
        t = t.replace(home, "~");
    }
    t = scrub_user_dirs(&t);
    scrub_identifiers(&t, salt)
}

fn scrub_for_report(s: &str) -> String {
    let home = crate::home_dir().to_string_lossy().to_string();
    let salt = platform_uuid().unwrap_or_else(|| "no-platform-uuid".to_string());
    scrub_report_text(s, &home, &salt)
}

/// Stable fingerprint of an error message: normalise the variable parts
/// (see `error_fingerprint`), SHA-256, first 16 hex chars. Identical across
/// machines and launches for the same kind of error.
fn report_fingerprint(scrubbed_message: &str) -> String {
    sha256::hex_digest(error_fingerprint(scrubbed_message).as_bytes())
        .chars()
        .take(16)
        .collect()
}

/// Has the user left "Send anonymous crash reports" on? Read from the backend
/// config (`crash_reports_enabled`, default true) so the Rust panic hook,
/// which has no frontend, respects it too.
fn crash_reports_enabled() -> bool {
    let path = crate::home_dir().join("Library/Application Support/Plaud V2/.plaud_v2_config.json");
    crash_reports_enabled_in(std::fs::read_to_string(path).ok().as_deref())
}

fn crash_reports_enabled_in(config_json: Option<&str>) -> bool {
    config_json
        .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok())
        .and_then(|v| v.get("crash_reports_enabled").and_then(|b| b.as_bool()))
        .unwrap_or(true)
}

/// Cached app version (set once during startup so report_error can use it without AppHandle)
static APP_VERSION: std::sync::OnceLock<String> = std::sync::OnceLock::new();

pub fn set_app_version(version: String) {
    let _ = APP_VERSION.set(version);
}

fn app_version() -> String {
    APP_VERSION.get().cloned().unwrap_or_else(|| "unknown".to_string())
}

/// Whether we're running in dev mode (not packaged).
/// Uses the compile-time `debug_assertions` flag — true for `cargo build`
/// / `cargo tauri dev`, false for `cargo build --release`.
fn is_dev_mode() -> bool {
    cfg!(debug_assertions)
}

fn macos_name() -> String {
    let major = std::process::Command::new("sw_vers")
        .args(["-productVersion"])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();
    major
}

/// Get Python env diagnostics (pip version, key packages) — recomputed fresh each call
fn python_env_info() -> String {
    let server = python::get_server().lock().unwrap();
    let py_path = server.python_path.clone();
    drop(server);
    if py_path.is_empty() {
        return "Python not found".to_string();
    }

    let mut info = Vec::new();

    // pip version
    if let Ok(o) = std::process::Command::new(&py_path)
        .args(["-m", "pip", "--version"])
        .output()
    {
        info.push(format!(
            "pip: {}",
            String::from_utf8_lossy(&o.stdout).trim()
        ));
    }

    // Key packages
    if let Ok(o) = std::process::Command::new(&py_path)
        .args(["-m", "pip", "list", "--format=columns"])
        .output()
    {
        let output = String::from_utf8_lossy(&o.stdout);
        let key_pkgs: Vec<&str> = output
            .lines()
            .filter(|l| {
                let lower = l.to_lowercase();
                ["flask", "requests", "bleak", "cryptography", "whisper", "transformers", "diarize"]
                    .iter()
                    .any(|k| lower.starts_with(k))
            })
            .collect();
        if !key_pkgs.is_empty() {
            info.push(format!("packages:\n```\n{}\n```", key_pkgs.join("\n")));
        }
    }

    // site-packages
    if let Ok(o) = std::process::Command::new(&py_path)
        .args(["-c", "import site; print('; '.join(site.getsitepackages()))"])
        .output()
    {
        info.push(format!(
            "site-packages: `{}`",
            String::from_utf8_lossy(&o.stdout).trim()
        ));
    }

    info.join("\n")
}

fn system_info_block(version: &str) -> String {
    let server = python::get_server().lock().unwrap();
    let python_running = server.process.is_some();
    let port = server.port;
    let py_path = server.python_path.clone();
    let py_ver = server.python_version.clone();
    drop(server);

    let mem_mb = {
        std::process::Command::new("sysctl")
            .args(["-n", "hw.memsize"])
            .output()
            .map(|o| {
                String::from_utf8_lossy(&o.stdout)
                    .trim()
                    .parse::<u64>()
                    .unwrap_or(0)
                    / 1024
                    / 1024
            })
            .unwrap_or(0)
    };

    let py_env = python_env_info();

    format!(
        "- **Device:** `{}`\n- **Version:** {version}\n- **Framework:** Tauri\n- **macOS:** {}\n- **Arch:** {}\n- **RAM:** {} MB\n- **API port:** {port}\n- **Python:** {py_ver}\n- **Python path:** `{py_path}`\n- **Python server:** {}\n\n**Python environment**\n{py_env}",
        device_id(),
        macos_name(),
        std::env::consts::ARCH,
        mem_mb,
        if python_running { "running" } else { "not running" }
    )
}

// ---- Diagnostics Log ----

const DIAGNOSTICS_MAX_BYTES: u64 = 1024 * 1024;

/// Serialises all concurrent writes to the diagnostics log file.
/// O_APPEND atomicity on macOS is guaranteed only for writes ≤ ~512 bytes;
/// larger JSON lines from concurrent report_error calls can interleave without
/// this lock.
static DIAG_LOG_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn diagnostics_log_path() -> std::path::PathBuf {
    crate::home_dir().join("Library/Application Support/com.plaud.v2/diagnostics.log")
}

fn append_diagnostic(context: &str, message: &str, stack: &str, fingerprint: &str) {
    let path = diagnostics_log_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }

    // Format the full log line into a String BEFORE acquiring the lock so that
    // the critical section contains only the write syscall.
    let stack_trunc: String = stack.chars().take(4096).collect();
    let mut line = serde_json::json!({
        "ts": chrono::Utc::now().to_rfc3339(),
        "version": app_version(),
        "context": context,
        "message": message,
        "fingerprint": fingerprint,
        "stackTruncated": stack_trunc,
    }).to_string();
    line.push('\n');

    // Acquire lock, then rotate (if needed) and write as a single operation so
    // that rotation and the subsequent write are also serialised.
    let _guard = DIAG_LOG_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    // Rotate if oversize
    if let Ok(meta) = std::fs::metadata(&path) {
        if meta.len() >= DIAGNOSTICS_MAX_BYTES {
            let mut rotated = path.clone();
            rotated.set_extension("log.1");
            let _ = std::fs::rename(&path, &rotated);
        }
    }
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        let _ = f.write_all(line.as_bytes());
    }
    // _guard drops here, releasing the lock
}

fn is_abort_error(message: &str) -> bool {
    let lower = message.to_lowercase();
    lower.contains("aborted") || lower.contains("aborterror") || lower.contains("signal is aborted")
}

// ---- Error/Bug Reporting ----

/// Deduplicate errors within a session
static REPORTED_ERRORS: std::sync::OnceLock<std::sync::Mutex<std::collections::HashSet<String>>> =
    std::sync::OnceLock::new();

fn reported_errors() -> &'static std::sync::Mutex<std::collections::HashSet<String>> {
    REPORTED_ERRORS.get_or_init(|| std::sync::Mutex::new(std::collections::HashSet::new()))
}

// ---- Backend Health State ----

static BACKEND_HEALTH: std::sync::OnceLock<std::sync::Mutex<String>> = std::sync::OnceLock::new();

fn backend_health() -> &'static std::sync::Mutex<String> {
    BACKEND_HEALTH.get_or_init(|| std::sync::Mutex::new("unknown".to_string()))
}

#[tauri::command]
pub fn set_backend_health(state: String) -> Result<String, String> {
    if state == "up" || state == "down" || state == "unknown" {
        let mut current = backend_health().lock().unwrap();
        *current = state.clone();
        Ok(state)
    } else {
        Err(format!("Invalid state: {}", state))
    }
}

#[tauri::command]
pub fn open_diagnostics_log() -> Result<(), String> {
    let path = diagnostics_log_path();
    // Ensure parent dir + file exist so the OS handler doesn't error on first run.
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let _ = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| e.to_string())?;
    // macOS: use the system `open` command. Plaud V2 is macOS-only, so this is fine.
    std::process::Command::new("open")
        .arg(&path)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

fn error_fingerprint(message: &str) -> String {
    // Strip variable parts for deduplication:
    // BLE UUIDs and long hex strings → HEX, digits → N.
    // (Digits are hex digits too, so short runs must have their digits
    // replaced when flushed — they used to be kept verbatim, so
    // "timeout after 30s" and "after 45s" fingerprinted differently.)
    fn flush(result: &mut String, run: &mut String) {
        if run.len() >= 8 {
            result.push_str("HEX");
        } else {
            result.extend(run.chars().map(|c| if c.is_ascii_digit() { 'N' } else { c }));
        }
        run.clear();
    }
    let mut result = String::new();
    let mut hex_run = String::new();
    for c in message.chars() {
        if c.is_ascii_hexdigit() {
            hex_run.push(c);
        } else {
            flush(&mut result, &mut hex_run);
            result.push(c);
        }
    }
    flush(&mut result, &mut hex_run);
    result.chars().take(100).collect()
}

/// Most of a message that goes into a report (the full text stays in the
/// local diagnostics log).
const REPORT_MESSAGE_MAX_CHARS: usize = 2000;
/// Traceback lines kept in a report (the innermost frames).
const REPORT_STACK_MAX_LINES: usize = 15;
/// The Worker rejects bodies over 20 KB; stay under it.
const REPORT_BODY_MAX_BYTES: usize = 19 * 1024;
const REPORT_TITLE_MAX_CHARS: usize = 190;

fn macos_version_short() -> String {
    let v = macos_name();
    if v.is_empty() {
        "unknown".to_string()
    } else {
        truncate_chars(&v, 60)
    }
}

/// Build the auto-bug payload sent to the Worker. Everything user- or
/// machine-specific is scrubbed (home paths, user names, serials, BLE UUIDs);
/// only the error message, its context, the last few traceback lines and
/// system info go out — never recording, transcript or summary text.
fn build_auto_report(
    message: &str,
    stack: &str,
    ctx: &str,
    sys_info: &str,
    version: &str,
    is_dev: bool,
    scrub: impl Fn(&str) -> String,
) -> serde_json::Value {
    let msg = scrub(&truncate_chars(message, REPORT_MESSAGE_MAX_CHARS));
    let fingerprint = report_fingerprint(&msg);
    let dev_tag = if is_dev { "[DEV] " } else { "" };
    let title = truncate_chars(&format!("[Auto] {dev_tag}{}", msg.replace(['\r', '\n'], " ")), REPORT_TITLE_MAX_CHARS);
    let stack_block = if stack.trim().is_empty() {
        String::new()
    } else {
        format!("### Stack trace (last {REPORT_STACK_MAX_LINES} lines)\n```\n{}\n```\n", scrub(&tail_lines(stack, REPORT_STACK_MAX_LINES)))
    };
    let body = format!(
        "## Auto-generated bug report{}\n\n### System\n{}\n\n### Error\n**Context:** `{}`\n```\n{msg}\n```\n\n{stack_block}",
        if is_dev { " (DEV)" } else { "" },
        scrub(sys_info),
        scrub(&truncate_chars(ctx, 200)),
    );
    serde_json::json!({
        "kind": "auto",
        "fingerprint": fingerprint,
        "title": title,
        "body": truncate_bytes(&body, REPORT_BODY_MAX_BYTES),
        "app_version": version,
        "os": macos_version_short(),
    })
}

#[tauri::command]
pub fn report_error(message: String, stack: Option<String>, context: Option<String>) {
    let ctx = context.unwrap_or_else(|| "unknown".to_string());
    log::error!("[BugReporter] {ctx}: {message}");

    let fingerprint = error_fingerprint(&message);
    let stack_str = stack.unwrap_or_default();

    // Always capture locally before deciding whether to escalate
    append_diagnostic(&ctx, &message, &stack_str, &fingerprint);

    // FILTER: legitimate aborts — log only
    if is_abort_error(&message) {
        return;
    }

    // FILTER: backend is known to be down — log only
    if backend_health().lock().unwrap().as_str() == "down" {
        return;
    }

    // FILTER: the user turned off "Send anonymous crash reports"
    if !crash_reports_enabled() {
        return;
    }

    // Existing dedupe (per session)
    {
        let mut set = reported_errors().lock().unwrap();
        if set.contains(&fingerprint) {
            return;
        }
        set.insert(fingerprint);
    }

    let version = app_version();
    let is_dev = is_dev_mode();

    // Send to the Worker in the background; it dedupes into GitHub issues.
    std::thread::spawn(move || {
        let sys_info = system_info_block(&version);
        let payload = build_auto_report(&message, &stack_str, &ctx, &sys_info, &version, is_dev, scrub_for_report);
        match post_report(&payload) {
            Ok(o) => log::info!("[BugReporter] Report {} (issue #{})", o.action, o.issue_number.unwrap_or(0)),
            Err(e) => log::warn!("[BugReporter] Failed: {e}"),
        }
    });
}

#[derive(Serialize)]
pub struct BugReportResult {
    success: bool,
    number: Option<u32>,
    url: Option<String>,
    error: Option<String>,
}

#[tauri::command]
pub async fn report_bug(
    app: tauri::AppHandle,
    expected: Option<String>,
    actual: Option<String>,
    steps: Option<String>,
) -> BugReportResult {
    // Off-load the work: system_info_block spawns several subprocesses (pip,
    // sw_vers, ioreg, sysctl) before the POST. Running it on the command's
    // worker thread blocks the renderer's submit button for several seconds —
    // spawn_blocking keeps the IPC pool responsive.
    let version = app.package_info().version.to_string();
    tokio::task::spawn_blocking(move || {
        let title_text = actual.as_deref().or(expected.as_deref()).unwrap_or("Bug report");
        let title = truncate_chars(
            &format!("[User Report] {}", scrub_for_report(&truncate_chars(title_text, 80)).replace(['\r', '\n'], " ")),
            REPORT_TITLE_MAX_CHARS,
        );
        let sys_info = system_info_block(&version);
        let body = scrub_for_report(&format!(
            "## User Bug Report\n\n### Expected behaviour\n{}\n\n### Actual behaviour\n{}\n\n{}\n### System\n{sys_info}\n\n---\n*Submitted via in-app bug reporter.*",
            expected.as_deref().unwrap_or("_Not provided_"),
            actual.as_deref().unwrap_or("_Not provided_"),
            steps.map(|s| format!("### Steps to reproduce\n{s}\n\n")).unwrap_or_default(),
        ));
        let payload = serde_json::json!({
            "kind": "user",
            "title": title,
            "body": truncate_bytes(&body, REPORT_BODY_MAX_BYTES),
            "app_version": version,
            "os": macos_version_short(),
        });

        match post_report(&payload) {
            Ok(o) => {
                let number = o.issue_number.map(|n| n as u32);
                log::info!("[BugReporter] User report created: #{}", number.unwrap_or(0));
                BugReportResult { success: true, number, url: o.url, error: None }
            }
            Err(e) => BugReportResult { success: false, number: None, url: None, error: Some(e) },
        }
    })
    .await
    .unwrap_or_else(|e| BugReportResult {
        success: false,
        number: None,
        url: None,
        error: Some(format!("Background task panicked: {e}")),
    })
}

// ---- Notifications ----

#[derive(Serialize)]
pub struct NotificationResult {
    shown: bool,
}

#[tauri::command]
pub fn show_notification(
    app: tauri::AppHandle,
    title: String,
    body: String,
    action_label: Option<String>,
) -> NotificationResult {
    use tauri::Emitter;
    use tauri_plugin_notification::NotificationExt;

    let mut builder = app.notification().builder().title(&title).body(&body);

    if let Some(ref label) = action_label {
        builder = builder.action_type_id(label);
    }

    let result = builder.show();

    // Show window when any notification is interacted with
    // (Tauri notification plugin doesn't have per-notification callbacks,
    // so we handle this globally via the plugin's event system)
    if let Some(label) = action_label {
        let app_clone = app.clone();
        std::thread::spawn(move || {
            // Small delay to let the notification appear
            std::thread::sleep(std::time::Duration::from_millis(100));
            // Emit notification-action event for the frontend, using the
            // action label as payload so the frontend can dispatch accordingly.
            let _ = app_clone.emit("notification-action", label);
        });
    }

    NotificationResult {
        shown: result.is_ok(),
    }
}

// ---- Select Directory ----

#[tauri::command]
pub async fn select_directory(app: tauri::AppHandle) -> Option<String> {
    use tauri_plugin_dialog::DialogExt;
    let (tx, rx) = tokio::sync::oneshot::channel::<Option<String>>();
    app.dialog().file().pick_folder(move |path| {
        let _ = tx.send(path.map(|p| p.to_string()));
    });
    rx.await.unwrap_or(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SALT: &str = "test-salt";

    #[test]
    fn version_compare() {
        assert!(is_newer_version("2.3.1", "2.3.0"));
        assert!(is_newer_version("v2.4.0", "2.3.9"));
        assert!(is_newer_version("10.0.0", "9.9.9"));
        assert!(is_newer_version("2.3.0.1", "2.3.0"));
        assert!(!is_newer_version("2.3.0", "2.3.0"));
        assert!(!is_newer_version("2.2.9", "2.3.0"));
        // Pre-release / build suffixes are ignored, never "newer".
        assert!(!is_newer_version("2.3.0-beta", "2.3.0"));
        assert!(!is_newer_version("2.3.0-beta.2", "2.3.0-beta.1"));
        assert!(!is_newer_version("2.3.0+build5", "2.3.0"));
        assert!(is_newer_version("2.4.0-rc1", "2.3.0"));
        assert!(!is_newer_version("", "2.3.0"));
        assert!(!is_newer_version("garbage", "2.3.0"));
        assert_eq!(parse_version("v2.3.0-beta"), vec![2, 3, 0]);
    }

    #[test]
    fn asset_name_and_checksum_checks() {
        assert!(is_safe_asset_name("Plaud.V2_2.3.0_aarch64.dmg"));
        assert!(!is_safe_asset_name("../evil.dmg"));
        assert!(!is_safe_asset_name("a/b.dmg"));
        assert!(!is_safe_asset_name(".hidden.dmg"));
        assert!(!is_safe_asset_name("update.zip"));
        assert!(is_hex_sha256(&"a".repeat(64)));
        assert!(!is_hex_sha256(&"g".repeat(64)));
        assert!(!is_hex_sha256("abc"));
        assert!(is_translocated("/private/var/folders/x/AppTranslocation/ABC/d/Plaud V2.app"));
        assert!(!is_translocated("/Applications/Plaud V2.app"));
    }

    #[test]
    fn install_script_has_constants_filled_in() {
        let s = install_script();
        assert!(s.contains("EXPECTED_BUNDLE_ID=\"com.plaud.v2\""));
        assert!(s.contains("EXPECTED_TEAM=\"4SN93PWF7Z\""));
        assert!(!s.contains("__BUNDLE_ID__") && !s.contains("__TEAM_ID__"));
    }

    #[test]
    fn cached_download_check() {
        let dir = std::env::temp_dir().join(format!("plaud-v2-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("x.dmg");
        std::fs::write(&f, b"abc").unwrap();
        let sha = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad".to_string();
        let asset = |size, sha256: Option<String>| LatestAsset { name: "x.dmg".into(), size, sha256 };
        assert!(cached_download_matches(&f, &asset(3, None)));
        assert!(cached_download_matches(&f, &asset(3, Some(sha.clone()))));
        assert!(!cached_download_matches(&f, &asset(3, Some("0".repeat(64)))));
        assert!(!cached_download_matches(&f, &asset(4, Some(sha))));
        assert!(!cached_download_matches(&dir.join("missing.dmg"), &asset(3, None)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn truncation_is_char_safe() {
        let s = "é".repeat(100); // 2 bytes each
        assert_eq!(truncate_chars(&s, 80).chars().count(), 81); // 80 + ellipsis
        assert_eq!(truncate_chars("short", 80), "short");
        assert_eq!(truncate_chars("ab🎙cd", 3), "ab🎙…");
        assert_eq!(truncate_bytes(&s, 3), "é");
        assert_eq!(truncate_bytes("abc", 10), "abc");
        assert_eq!(truncate_bytes("日本語", 4), "日");
    }

    #[test]
    fn tail_lines_keeps_the_end() {
        let tb: String = (1..=40).map(|i| format!("line {i}\n")).collect();
        let t = tail_lines(&tb, 15);
        assert!(t.starts_with("… (25 earlier lines omitted)\nline 26"), "{t}");
        assert!(t.ends_with("line 40"));
        assert_eq!(tail_lines("a\nb", 15), "a\nb");
    }

    #[test]
    fn scrubs_paths() {
        let s = scrub_report_text(
            "File \"/Users/ollie/projects/x.py\", in /Users/someone-else/Library and '/Users/bob' and /Users/Shared/x",
            "/Users/ollie",
            SALT,
        );
        assert_eq!(s, "File \"~/projects/x.py\", in /Users/~/Library and '/Users/~' and /Users/Shared/x");
        assert_eq!(scrub_report_text("/var/home/me/a", "/var/home/me", SALT), "~/a");
    }

    #[test]
    fn scrubs_serials_and_uuids() {
        let msg = "API error: /device/connect [SN: 8820B50256947317] — HTTP 500 (device 8820B50256947317)";
        let s = scrub_report_text(msg, "/Users/ollie", SALT);
        assert!(!s.contains("8820B50256947317"), "{s}");
        let tag = id_tag(SALT, "8820B50256947317");
        assert_eq!(tag.len(), 8);
        assert!(s.contains(&format!("[SN: {tag}]")), "{s}");
        assert!(s.contains(&format!("sn-{tag}")), "{s}");
        assert_ne!(id_tag("other", "8820B50256947317"), tag);

        let ble = "Device with address 3F2504E0-4F89-11D3-9A0C-0305E82C3301 not found; char 00002bb1-0000-1000-8000-00805f9b34fb";
        let s = scrub_report_text(ble, "/Users/ollie", SALT);
        assert!(!s.contains("3F2504E0-4F89"), "{s}");
        assert!(s.contains("uuid-"));
        assert!(s.contains("00002bb1-0000-1000-8000-00805f9b34fb"));

        // Not serials: wrong length, part of a longer word, no digits.
        let keep = "hash abcdef0123456789abcd x123456789012345678 ABCDEFABCDEFABCD";
        assert_eq!(scrub_report_text(keep, "/x", SALT), keep);
        assert_eq!(scrub_report_text("café — ✓", "/x", SALT), "café — ✓");
    }

    #[test]
    fn fingerprint_is_stable_hex() {
        let a = report_fingerprint("API error: /device/sync — timeout after 30s (attempt 2)");
        let b = report_fingerprint("API error: /device/sync — timeout after 45s (attempt 7)");
        let c = report_fingerprint("API error: /device/scan — Bluetooth off");
        assert_eq!(a, b);
        assert_ne!(a, c);
        assert_eq!(a.len(), 16);
        assert!(a.chars().all(|ch| ch.is_ascii_hexdigit() && !ch.is_ascii_uppercase()));
        // Pinned: changing the normalisation would split existing GitHub issues.
        assert_eq!(report_fingerprint("x"), sha256::hex_digest(b"x")[..16]);
    }

    #[test]
    fn auto_report_payload() {
        let long_msg = format!("boom in /Users/ollie/a.py [SN: 8820B50256947317] {}", "é".repeat(3000));
        let stack: String = (1..=50).map(|i| format!("frame {i}\n")).collect();
        let p = build_auto_report(
            &long_msg,
            &stack,
            "api:/device/sync",
            "- **Python path:** `/Users/ollie/.venv/bin/python`",
            "2.3.0",
            false,
            |s| scrub_report_text(s, "/Users/ollie", SALT),
        );
        let title = p["title"].as_str().unwrap();
        let body = p["body"].as_str().unwrap();
        assert!(title.starts_with("[Auto] boom in ~/a.py"), "{title}");
        assert!(title.chars().count() <= 200);
        assert!(body.len() <= 20 * 1024);
        assert!(!body.contains("/Users/ollie"));
        assert!(!body.contains("8820B50256947317"));
        assert!(body.contains("frame 50") && !body.contains("frame 35\n"));
        assert_eq!(p["kind"], "auto");
        assert_eq!(p["app_version"], "2.3.0");
        let fp = p["fingerprint"].as_str().unwrap();
        assert!(fp.len() <= 64 && fp.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn crash_report_setting() {
        assert!(crash_reports_enabled_in(None));
        assert!(crash_reports_enabled_in(Some("not json")));
        assert!(crash_reports_enabled_in(Some("{}")));
        assert!(crash_reports_enabled_in(Some(r#"{"crash_reports_enabled": true}"#)));
        assert!(!crash_reports_enabled_in(Some(r#"{"crash_reports_enabled": false}"#)));
    }

    /// Hits the deployed Worker: `cargo test --lib -- --ignored live_worker`.
    #[test]
    #[ignore]
    fn live_worker_latest_and_download_redirect() {
        let r = fetch_latest_release().expect("latest");
        assert!(!parse_version(&r.version).is_empty());
        assert!(is_safe_asset_name(&r.asset.name));
        assert!(r.asset.size > 1_000_000);
        let resp = reqwest::blocking::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .unwrap()
            .get(&r.download_url)
            .send()
            .unwrap();
        assert_eq!(resp.status().as_u16(), 302);
        let loc = resp.headers().get("location").unwrap().to_str().unwrap();
        assert!(loc.starts_with("https://") && loc.contains("githubusercontent.com"), "{loc}");

        // Full download path: .part → size/sha check → rename. Use a wrong
        // checksum first (must fail and leave nothing behind), then the right one.
        let dir = std::env::temp_dir().join(format!("plaud-v2-live-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let dest = dir.join(&r.asset.name);
        let mut bad = r.clone();
        bad.asset.sha256 = Some("0".repeat(64));
        assert!(download_update(&bad, &dest, |_| {}).is_err());
        assert!(!dest.exists());
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 0, "no .part left behind");
        download_update(&r, &dest, |_| {}).expect("download");
        assert_eq!(dest.metadata().unwrap().len(), r.asset.size);
        let digest = file_sha256(&dest).unwrap();
        let shasum = std::process::Command::new("/usr/bin/shasum").args(["-a", "256"]).arg(&dest).output().unwrap();
        assert!(String::from_utf8_lossy(&shasum.stdout).starts_with(&digest), "pure-Rust SHA-256 disagrees with shasum");
        let mut good = r.clone();
        good.asset.sha256 = Some(digest);
        assert!(cached_download_matches(&dest, &good.asset));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn worker_base_is_https() {
        assert!(WORKER_BASE.starts_with("https://plaud-v2-updates."));
        assert!(!WORKER_BASE.ends_with('/'));
    }
}
