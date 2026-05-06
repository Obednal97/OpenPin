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
    let (resolved, preference) = {
        let mut pref = theme_pref().lock().unwrap();
        *pref = match pref.as_str() {
            "auto" => "light".to_string(),
            "light" => "dark".to_string(),
            "dark" => "auto".to_string(),
            _ => "auto".to_string(),
        };
        let resolved = resolve_theme(&pref);
        (resolved, pref.clone())
    };

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
    let pref = theme_pref().lock().unwrap();
    let resolved = resolve_theme(&pref);
    ThemeInfo {
        resolved,
        preference: pref.clone(),
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

const UPDATE_REPO: &str = "Obednal97/Plaud-V2";

/// Obfuscated GitHub PAT — XOR'd with a key so `strings` can't extract it.
/// The token is read-only for repo contents + read/write for issues.
fn update_token() -> String {
    const KEY: u8 = 0x5A;
    const ENCODED: &[u8] = &[/* token removed from history */];
    ENCODED.iter().map(|b| (b ^ KEY) as char).collect()
}

// Pre-compute on first use
fn get_update_token() -> &'static str {
    static TOKEN: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    TOKEN.get_or_init(update_token)
}

/// Cached pending update info
static PENDING_UPDATE: std::sync::OnceLock<std::sync::Mutex<Option<PendingUpdate>>> =
    std::sync::OnceLock::new();

#[allow(dead_code)]
struct PendingUpdate {
    version: String,
    zip_path: String,
}

fn pending_update() -> &'static std::sync::Mutex<Option<PendingUpdate>> {
    PENDING_UPDATE.get_or_init(|| std::sync::Mutex::new(None))
}

fn updates_dir() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    std::path::PathBuf::from(home)
        .join("Library/Application Support/com.plaud.v2/updates")
}

#[tauri::command]
pub fn check_for_updates(app: tauri::AppHandle) {
    use tauri::Emitter;
    let current = app.package_info().version.to_string();
    std::thread::spawn(move || {
        let emit = |status: &str, info: serde_json::Value| {
            let mut payload = info;
            payload["status"] = serde_json::json!(status);
            let _ = app.emit("update-status", &payload);
        };

        match github_get(&format!("/repos/{UPDATE_REPO}/releases/latest")) {
            Ok(release) => {
                let latest = release["tag_name"]
                    .as_str()
                    .unwrap_or("")
                    .trim_start_matches('v')
                    .to_string();
                if latest.is_empty() || latest == current {
                    log::info!("[Updater] App is up to date ({current})");
                    emit("up-to-date", serde_json::json!({}));
                    return;
                }

                let parse = |v: &str| -> Vec<u32> {
                    v.split('.').filter_map(|s| s.parse().ok()).collect()
                };
                let c = parse(&current);
                let l = parse(&latest);
                if l <= c {
                    log::info!("[Updater] App is up to date ({current})");
                    emit("up-to-date", serde_json::json!({}));
                    return;
                }

                log::info!("[Updater] Update available: v{latest}");
                emit("available", serde_json::json!({ "version": latest }));

                // Find the DMG release asset
                let update_asset = release["assets"]
                    .as_array()
                    .and_then(|a| {
                        a.iter()
                            .find(|a| {
                                a["name"]
                                    .as_str()
                                    .map(|n| n.ends_with(".dmg") && !n.contains("blockmap"))
                                    .unwrap_or(false)
                            })
                    });

                let asset = match update_asset {
                    Some(a) => a,
                    None => {
                        log::error!("[Updater] No update asset found in release");
                        emit("error", serde_json::json!({ "message": "No update package found" }));
                        return;
                    }
                };

                let asset_url = asset["url"].as_str().unwrap_or("").to_string();
                let asset_name = asset["name"].as_str().unwrap_or("update.zip").to_string();
                let asset_size = asset["size"].as_u64().unwrap_or(0);

                // Cache directory
                let cache_dir = updates_dir();
                let _ = std::fs::create_dir_all(&cache_dir);
                let zip_path = cache_dir.join(&asset_name);

                // Skip download if already cached with matching size
                if zip_path.exists() {
                    if let Ok(meta) = zip_path.metadata() {
                        if meta.len() == asset_size {
                            log::info!("[Updater] Update already downloaded");
                            *pending_update().lock().unwrap() = Some(PendingUpdate {
                                version: latest.clone(),
                                zip_path: zip_path.to_string_lossy().to_string(),
                            });
                            emit("ready", serde_json::json!({ "version": latest }));
                            return;
                        }
                    }
                }

                // Download the ZIP with progress
                log::info!("[Updater] Downloading {asset_name}...");
                match download_github_asset(&asset_url, &zip_path, |percent| {
                    emit("downloading", serde_json::json!({ "percent": percent }));
                }) {
                    Ok(()) => {
                        log::info!("[Updater] Update downloaded: v{latest}");
                        *pending_update().lock().unwrap() = Some(PendingUpdate {
                            version: latest.clone(),
                            zip_path: zip_path.to_string_lossy().to_string(),
                        });
                        emit("ready", serde_json::json!({ "version": latest }));
                    }
                    Err(e) => {
                        log::error!("[Updater] Download failed: {e}");
                        emit("error", serde_json::json!({ "message": e }));
                    }
                }
            }
            Err(e) => {
                log::error!("[Updater] Check failed: {e}");
                emit("error", serde_json::json!({ "message": e }));
            }
        }
    });
}

/// Download a GitHub release asset with progress callback
fn download_github_asset(
    asset_url: &str,
    dest: &std::path::Path,
    on_progress: impl Fn(u32),
) -> Result<(), String> {
    let client = reqwest::blocking::Client::new();
    let mut resp = client
        .get(asset_url)
        .header("User-Agent", "Plaud-V2-Updater")
        .header("Accept", "application/octet-stream")
        .header("Authorization", format!("Bearer {}", get_update_token()))
        .send()
        .map_err(|e| format!("Download request failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!("Download failed: HTTP {}", resp.status()));
    }

    let total = resp.content_length().unwrap_or(0);
    let mut file =
        std::fs::File::create(dest).map_err(|e| format!("Failed to create file: {e}"))?;
    let mut downloaded: u64 = 0;

    use std::io::{Read, Write};
    let mut buf = [0u8; 65536];
    loop {
        let n = resp
            .read(&mut buf)
            .map_err(|e| format!("Read error: {e}"))?;
        if n == 0 {
            break;
        }
        file.write_all(&buf[..n])
            .map_err(|e| format!("Write error: {e}"))?;
        downloaded += n as u64;
        if total > 0 {
            on_progress(((downloaded * 100) / total) as u32);
        }
    }
    Ok(())
}

#[derive(Serialize)]
pub struct UpdateResult {
    installing: bool,
}

#[tauri::command]
pub fn install_update(app: tauri::AppHandle) -> UpdateResult {
    let pending = pending_update().lock().unwrap().take();
    let pending = match pending {
        Some(p) => p,
        None => {
            log::warn!("[Updater] No pending update to install");
            return UpdateResult { installing: false };
        }
    };

    let zip_path = pending.zip_path;

    // Find the .app bundle path
    // In production: /Applications/Plaud V2.app/Contents/MacOS/plaud-v2
    // We need the .app directory (3 levels up from the binary)
    let exe_path = std::env::current_exe().unwrap_or_default();
    let app_path = exe_path
        .parent() // MacOS/
        .and_then(|p| p.parent()) // Contents/
        .and_then(|p| p.parent()) // Plaud V2.app/
        .unwrap_or(&exe_path)
        .to_string_lossy()
        .to_string();

    let is_dmg = zip_path.ends_with(".dmg");
    log::info!("[Updater] Installing from {zip_path} (dmg={is_dmg}) to {app_path}");

    // Spawn detached bash script that handles both DMG and ZIP formats.
    // zip_path and app_path are passed as positional arguments ($1, $2) to
    // avoid shell injection: format!()-interpolating untrusted values (e.g.
    // the GitHub release asset name) into the script body would allow a
    // maliciously crafted asset name containing $(), backticks, or quotes to
    // break out of the variable assignments and execute arbitrary commands.
    let script_template = r#"
zip_path="$1"
app_path="$2"
IS_DMG="$3"

sleep 1
rm -rf /tmp/plaud-v2-update

if [ "$IS_DMG" = "true" ]; then
  # DMG: mount, copy .app, unmount
  # Use sed to extract full /Volumes/... path (handles spaces in volume names)
  MOUNT_POINT=$(hdiutil attach -nobrowse -noverify -noautoopen "$zip_path" 2>/dev/null | grep "/Volumes/" | sed 's/.*\(\/Volumes\/.*\)/\1/' | tail -1)
  if [ -z "$MOUNT_POINT" ]; then
    MOUNT_POINT=$(hdiutil attach -nobrowse -noverify -noautoopen "$zip_path" 2>/dev/null | tail -1 | sed 's/.*\(\/Volumes\/.*\)/\1/')
  fi
  NEW_APP=$(find "$MOUNT_POINT" -maxdepth 1 -name "*.app" 2>/dev/null | head -1)
else
  # ZIP: extract with ditto
  ditto -xk "$zip_path" "/tmp/plaud-v2-update"
  NEW_APP=$(find /tmp/plaud-v2-update -maxdepth 1 -name "*.app" | head -1)
fi

if [ -n "$NEW_APP" ] && [ -d "$NEW_APP/Contents/MacOS" ]; then
  # Verify code signature before installing
  SIGCHECK=$(codesign --verify --deep --strict "$NEW_APP" 2>&1)
  if [ $? -ne 0 ]; then
    echo "Update signature invalid: $SIGCHECK" >&2
    [ "$IS_DMG" = "true" ] && hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null
    rm -rf /tmp/plaud-v2-update
    open "$app_path"
    exit 1
  fi
  # Safe swap: rename old, move new, only then delete old
  mv "$app_path" "${app_path}.old" 2>/dev/null
  cp -R "$NEW_APP" "$app_path"
  if [ -d "${app_path}/Contents/MacOS" ]; then
    rm -rf "${app_path}.old"
    xattr -dr com.apple.quarantine "$app_path"
    [ "$IS_DMG" = "true" ] && hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null
    rm -rf /tmp/plaud-v2-update
    open "$app_path"
  else
    # Swap failed — restore old app
    rm -rf "$app_path"
    mv "${app_path}.old" "$app_path" 2>/dev/null
    [ "$IS_DMG" = "true" ] && hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null
    open "$app_path"
  fi
else
  # Extraction/mount failed — just relaunch existing app
  [ "$IS_DMG" = "true" ] && hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null
  rm -rf /tmp/plaud-v2-update
  open "$app_path"
fi
"#;

    // Spawn detached so it survives app.exit(0). Rust's default spawn keeps
    // the child in the parent's session; when the Tauri process terminates,
    // macOS can reap the still-attached bash before it finishes the swap +
    // `open`. pre_exec runs between fork and exec — calling setsid() there
    // makes the child a new session leader so it outlives the parent.
    //
    // bash -c '<script>' '<$0>' '<$1>' '<$2>' '<$3>'
    // The first arg after the script is $0 (script name placeholder); actual
    // positional params start at $1. Values are never part of the script body.
    let mut cmd = std::process::Command::new("bash");
    cmd.args(["-c", script_template, "install-update"])
        .arg(&zip_path)
        .arg(&app_path)
        .arg(is_dmg.to_string())
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
    let _ = cmd.spawn();

    // Quit the app so the script can replace it
    crate::python::stop_python_server();
    app.exit(0);

    UpdateResult { installing: true }
}

// ---- GitHub API helpers ----

fn github_client() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .unwrap_or_else(|_| reqwest::blocking::Client::new())
}

fn github_get(path: &str) -> Result<serde_json::Value, String> {
    let resp = github_client()
        .get(format!("https://api.github.com{path}"))
        .header("User-Agent", "Plaud-V2-Tauri")
        .header("Accept", "application/vnd.github+json")
        .header("Authorization", format!("Bearer {}", get_update_token()))
        .send()
        .map_err(|e| format!("HTTP error: {e}"))?;
    resp.json::<serde_json::Value>()
        .map_err(|e| format!("JSON parse error: {e}"))
}

fn github_post(path: &str, body: &serde_json::Value) -> Result<(u16, serde_json::Value), String> {
    let resp = github_client()
        .post(format!("https://api.github.com{path}"))
        .header("User-Agent", "Plaud-V2-BugReporter")
        .header("Accept", "application/vnd.github+json")
        .header("Authorization", format!("Bearer {}", get_update_token()))
        .header("Content-Type", "application/json")
        .json(body)
        .send()
        .map_err(|e| format!("HTTP error: {e}"))?;
    let status = resp.status().as_u16();
    let data = resp
        .json::<serde_json::Value>()
        .unwrap_or(serde_json::Value::Null);
    Ok((status, data))
}

// ---- System Info for Bug Reports ----

/// Anonymous device ID — SHA-256 of hardware UUID, first 8 chars
/// Stable across builds: same machine always produces the same ID
fn device_id() -> String {
    static DEVICE_ID: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    DEVICE_ID
        .get_or_init(|| {
            let output = std::process::Command::new("ioreg")
                .args(["-rd1", "-c", "IOPlatformExpertDevice"])
                .output();
            if let Ok(o) = output {
                let text = String::from_utf8_lossy(&o.stdout);
                if let Some(cap) = text.split("IOPlatformUUID").nth(1) {
                    if let Some(uuid) = cap.split('"').nth(1) {
                        // SHA-256 hash via shasum(1)
                        use std::io::Write;
                        let hash_output = std::process::Command::new("shasum")
                            .args(["-a", "256"])
                            .stdin(std::process::Stdio::piped())
                            .stdout(std::process::Stdio::piped())
                            .spawn()
                            .and_then(|mut child| {
                                let mut stdin = child.stdin.take().ok_or_else(|| {
                                    std::io::Error::new(
                                        std::io::ErrorKind::Other,
                                        "shasum stdin unavailable",
                                    )
                                })?;
                                stdin.write_all(uuid.as_bytes())?;
                                drop(stdin);
                                child.wait_with_output()
                            });
                        if let Ok(out) = hash_output {
                            let hex = String::from_utf8_lossy(&out.stdout);
                            return hex.trim().chars().take(8).collect();
                        }
                    }
                }
            }
            "unknown".to_string()
        })
        .clone()
}

/// Cached app version (set once during startup so report_error can use it without AppHandle)
static APP_VERSION: std::sync::OnceLock<String> = std::sync::OnceLock::new();

pub fn set_app_version(version: String) {
    let _ = APP_VERSION.set(version);
}

fn app_version() -> String {
    APP_VERSION.get().cloned().unwrap_or_else(|| "unknown".to_string())
}

/// Whether we're running in dev mode (not packaged)
fn is_dev_mode() -> bool {
    // In dev, the binary is in src-tauri/target/debug/
    let exe = std::env::current_exe().unwrap_or_default();
    exe.to_string_lossy().contains("target/debug")
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
    dirs::home_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("/tmp"))
        .join("Library/Application Support/com.plaud.v2/diagnostics.log")
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
    // BLE UUIDs and long hex strings → HEX, digits → N
    let mut result = String::new();
    let mut hex_run = String::new();
    for c in message.chars() {
        if c.is_ascii_hexdigit() {
            hex_run.push(c);
        } else {
            if hex_run.len() >= 8 {
                result.push_str("HEX");
            } else {
                result.push_str(&hex_run);
            }
            hex_run.clear();
            // Replace digits with N
            if c.is_ascii_digit() {
                result.push('N');
            } else {
                result.push(c);
            }
        }
    }
    if hex_run.len() >= 8 {
        result.push_str("HEX");
    } else {
        result.push_str(&hex_run);
    }
    result.chars().take(100).collect()
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

    // Existing dedupe (per session)
    {
        let mut set = reported_errors().lock().unwrap();
        if set.contains(&fingerprint) {
            return;
        }
        set.insert(fingerprint.clone());
    }

    let version = app_version();
    let is_dev = is_dev_mode();

    // Post to GitHub in background
    std::thread::spawn(move || {
        let sys_info = system_info_block(&version);

        // Search for existing issue
        let query = format!("repo:{UPDATE_REPO} is:issue label:auto-bug \"{}\"", &fingerprint[..60.min(fingerprint.len())]);
        if let Ok(search) = github_get(&format!("/search/issues?q={}&per_page=5", urlencoding(&query))) {
            let items = search["items"].as_array();
            let open_issue = items
                .and_then(|arr| arr.iter().find(|i| i["state"].as_str() == Some("open")));

            if let Some(issue) = open_issue {
                if let Some(number) = issue["number"].as_u64() {
                    // Include full system info in GitHub issue comments
                    let dev_tag = if is_dev { " (DEV)" } else { "" };
                    let comment = serde_json::json!({
                        "body": format!(
                            "### Another occurrence{dev_tag}\n\n**System**\n{sys_info}\n\n**Context:** `{ctx}`\n```\n{message}\n```"
                        )
                    });
                    let _ = github_post(&format!("/repos/{UPDATE_REPO}/issues/{number}/comments"), &comment);
                    log::info!("[BugReporter] Commented on existing issue #{number}");
                    return;
                }
            }
        }

        // Create new issue
        let dev_tag = if is_dev { "[DEV] " } else { "" };
        let title = format!("[Auto] {dev_tag}{}", &message[..80.min(message.len())]);
        let body = format!(
            "## Auto-generated bug report\n\n### System\n{sys_info}\n\n### Error\n**Context:** `{ctx}`\n```\n{message}\n```\n\n{}\n\n---\n*Auto-reported by Plaud V2 (Tauri build)*",
            if stack_str.is_empty() { String::new() } else { format!("### Stack trace\n```\n{}\n```", &stack_str[..2000.min(stack_str.len())]) }
        );
        let issue = serde_json::json!({
            "title": title,
            "body": body,
            "labels": ["auto-bug"]
        });
        match github_post(&format!("/repos/{UPDATE_REPO}/issues"), &issue) {
            Ok((201, data)) => {
                log::info!("[BugReporter] Created issue #{}", data["number"]);
            }
            Ok((status, _)) => log::warn!("[BugReporter] GitHub returned {status}"),
            Err(e) => log::error!("[BugReporter] Failed: {e}"),
        }
    });
}

fn urlencoding(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                String::from(b as char)
            }
            _ => format!("%{b:02X}"),
        })
        .collect()
}

#[derive(Serialize)]
pub struct BugReportResult {
    success: bool,
    number: Option<u32>,
    url: Option<String>,
    error: Option<String>,
}

#[tauri::command]
pub fn report_bug(
    app: tauri::AppHandle,
    expected: Option<String>,
    actual: Option<String>,
    steps: Option<String>,
) -> BugReportResult {
    let version = app.package_info().version.to_string();
    let title_text = actual.as_deref().or(expected.as_deref()).unwrap_or("Bug report");
    let title = format!("[User Report] {}", &title_text[..80.min(title_text.len())]);
    let sys_info = system_info_block(&version);
    let body = format!(
        "## User Bug Report\n\n### Expected behaviour\n{}\n\n### Actual behaviour\n{}\n\n{}\n### System\n{sys_info}\n\n---\n*Submitted via in-app bug reporter.*",
        expected.as_deref().unwrap_or("_Not provided_"),
        actual.as_deref().unwrap_or("_Not provided_"),
        steps.map(|s| format!("### Steps to reproduce\n{s}\n\n")).unwrap_or_default(),
    );

    let issue = serde_json::json!({
        "title": title,
        "body": body,
        "labels": ["user-report"]
    });

    match github_post(&format!("/repos/{UPDATE_REPO}/issues"), &issue) {
        Ok((201, data)) => {
            let number = data["number"].as_u64().map(|n| n as u32);
            let url = data["html_url"].as_str().map(String::from);
            log::info!("[BugReporter] User report created: #{}", number.unwrap_or(0));
            BugReportResult {
                success: true,
                number,
                url,
                error: None,
            }
        }
        Ok((status, _)) => BugReportResult {
            success: false,
            number: None,
            url: None,
            error: Some(format!("GitHub returned {status}")),
        },
        Err(e) => BugReportResult {
            success: false,
            number: None,
            url: None,
            error: Some(e),
        },
    }
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
