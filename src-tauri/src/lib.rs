mod commands;
mod python;

use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconBuilder,
    Emitter, Listener, Manager,
};

/// Set the macOS dock icon based on system theme.
///
/// # Panics
/// Panics if called from a thread other than the main thread.  Callers that
/// are not guaranteed to be on the main thread (e.g. Tauri IPC handlers) must
/// dispatch via `app.run_on_main_thread()` before calling this function.
#[cfg(target_os = "macos")]
pub fn set_dock_icon_for_theme(is_dark: bool) {
    use objc2::{AnyThread, MainThreadMarker};
    use objc2_app_kit::{NSApplication, NSImage};
    use objc2_foundation::NSData;

    let icon_bytes: &[u8] = if is_dark {
        include_bytes!("../icons/dock-dark.png")
    } else {
        include_bytes!("../icons/dock-light.png")
    };

    let data = NSData::with_bytes(icon_bytes);
    let image = NSImage::initWithData(NSImage::alloc(), &data).unwrap();
    let mtm = MainThreadMarker::new()
        .expect("set_dock_icon_for_theme must be called on the main thread");
    let app = NSApplication::sharedApplication(mtm);
    unsafe { app.setApplicationIconImage(Some(&image)) };
}

#[cfg(target_os = "macos")]
pub fn is_system_dark_mode() -> bool {
    let output = std::process::Command::new("defaults")
        .args(["read", "-g", "AppleInterfaceStyle"])
        .output();
    match output {
        Ok(o) => String::from_utf8_lossy(&o.stdout)
            .trim()
            .eq_ignore_ascii_case("dark"),
        Err(_) => false,
    }
}

/// Returns the current user's home directory, falling back to `/tmp` when the
/// home directory cannot be determined (e.g. sandboxed environments).
pub fn home_dir() -> std::path::PathBuf {
    dirs::home_dir().unwrap_or_else(|| std::path::PathBuf::from("/tmp"))
}

/// Simple prefs file for Tauri-level settings (dock visibility, etc.)
fn prefs_path() -> std::path::PathBuf {
    home_dir()
        .join("Library/Application Support/com.plaud.v2")
        .join("tauri-prefs.json")
}

fn load_prefs() -> serde_json::Value {
    std::fs::read_to_string(prefs_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(serde_json::json!({}))
}

fn save_prefs(prefs: &serde_json::Value) {
    let path = prefs_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = std::fs::write(path, serde_json::to_string_pretty(prefs).unwrap_or_default());
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Catch Rust panics and report to GitHub Issues
    std::panic::set_hook(Box::new(|info| {
        let message = format!("{info}");
        let backtrace = std::backtrace::Backtrace::force_capture().to_string();
        log::error!("[Rust Panic] {message}\n{backtrace}");
        // Fire-and-forget report
        std::thread::spawn(move || {
            commands::report_error(
                format!("[PANIC] {message}"),
                Some(backtrace),
                Some("rust-panic".to_string()),
            );
            // Give the HTTP request time to complete
            std::thread::sleep(std::time::Duration::from_secs(2));
        });
    }));
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_decorum::init())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let window = app.get_webview_window("main")
                .expect("Main window must exist — check tauri.conf.json windows config");

            // Cache app version for bug reports (report_error doesn't have AppHandle)
            commands::set_app_version(app.package_info().version.to_string());

            // macOS: traffic lights + themed dock icon
            #[cfg(target_os = "macos")]
            {
                use tauri_plugin_decorum::WebviewWindowExt;
                window.create_overlay_titlebar().unwrap();
                window.set_traffic_lights_inset(16.0, 30.0).unwrap();

                set_dock_icon_for_theme(is_system_dark_mode());

                // Restore dock visibility preference
                let prefs = load_prefs();
                if prefs.get("dockVisible").and_then(|v| v.as_bool()) == Some(false) {
                    use objc2::MainThreadMarker;
                    use objc2_app_kit::{NSApplication, NSApplicationActivationPolicy};
                    unsafe {
                        let mtm = MainThreadMarker::new_unchecked();
                        let ns_app = NSApplication::sharedApplication(mtm);
                        ns_app.setActivationPolicy(NSApplicationActivationPolicy::Accessory);
                    }
                }

                // Watch for system theme changes via file modification
                // macOS writes theme pref to ~/Library/Preferences/.GlobalPreferences.plist
                // Watching this file is cheaper than spawning `defaults read` every 5s
                let app_handle = app.handle().clone();
                std::thread::spawn(move || {
                    let pref_path = crate::home_dir()
                        .join("Library/Preferences/.GlobalPreferences.plist");
                    let mut was_dark = is_system_dark_mode();
                    let mut last_mtime = std::fs::metadata(&pref_path)
                        .and_then(|m| m.modified())
                        .ok();
                    loop {
                        std::thread::sleep(std::time::Duration::from_secs(3));
                        let current_mtime = std::fs::metadata(&pref_path)
                            .and_then(|m| m.modified())
                            .ok();
                        // Only check theme if the pref file was modified
                        if current_mtime != last_mtime {
                            last_mtime = current_mtime;
                            let is_dark = is_system_dark_mode();
                            if is_dark != was_dark {
                                was_dark = is_dark;
                                // Dispatch to main thread — AppKit requires it.
                                let _ = app_handle.run_on_main_thread(move || {
                                    set_dock_icon_for_theme(is_dark);
                                });
                                // Renderer caches the resolved theme at startup; tell it to
                                // re-resolve so `auto` follows the system flip.
                                let _ = app_handle.emit("system-theme-changed", is_dark);
                            }
                        }
                    }
                });
            }

            // System tray
            let show = MenuItemBuilder::with_id("show", "Show Plaud V2").build(app)?;
            let sync = MenuItemBuilder::with_id("sync", "Sync Device").build(app)?;
            let quit = MenuItemBuilder::with_id("quit", "Quit").build(app)?;
            let menu = MenuBuilder::new(app)
                .item(&show)
                .separator()
                .item(&sync)
                .separator()
                .item(&quit)
                .build()?;

            let tray_icon = {
                let icon_bytes = include_bytes!("../icons/tray.png");
                tauri::image::Image::from_bytes(icon_bytes)
                    .unwrap_or_else(|_| app.default_window_icon().unwrap().clone())
            };

            let _tray = TrayIconBuilder::new()
                .icon(tray_icon)
                .icon_as_template(true)
                .tooltip("Plaud V2")
                .menu(&menu)
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                    "sync" => {
                        // Route through the frontend so a tray sync behaves exactly
                        // like clicking Sync in-app (progress UI, transcription,
                        // recordings reload). Poking /device/sync over a raw socket
                        // bypassed all of that and left the UI unaware.
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                            let _ = w.emit("tray-sync", ());
                        }
                    }
                    "quit" => {
                        python::stop_python_server();
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let tauri::tray::TrayIconEvent::Click { .. } = event {
                        if let Some(w) = tray.app_handle().get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                })
                .build(app)?;

            // Deep link: parse OAuth callbacks (plaud-v2://callback?auth_code=...)
            let app_handle_dl = app.handle().clone();
            app.listen("deep-link://new-url", move |event: tauri::Event| {
                let url_str = event.payload();
                if let Some(url_str) = url_str.strip_prefix('"').and_then(|s: &str| s.strip_suffix('"')) {
                    if let Ok(parsed) = url::Url::parse(url_str) {
                        let auth_code = parsed
                            .query_pairs()
                            .find(|(k, _)| k == "auth_code" || k == "code")
                            .map(|(_, v)| v.to_string());
                        if let Some(code) = auth_code {
                            if let Some(w) = app_handle_dl.get_webview_window("main") {
                                let _ = w.emit("oauth-callback", &code);
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                    }
                }
            });

            // System wake detection — poll for sleep/wake transitions
            let app_handle_wake = app.handle().clone();
            std::thread::spawn(move || {
                // Use `pmset -g log` approach: check system uptime discontinuities
                let mut last_instant = std::time::Instant::now();
                loop {
                    std::thread::sleep(std::time::Duration::from_secs(5));
                    let now = std::time::Instant::now();
                    let elapsed = now.duration_since(last_instant);
                    // If more than 15 seconds passed during a 5-second sleep, system was sleeping
                    if elapsed.as_secs() > 15 {
                        log::info!("[PowerMonitor] System wake detected (gap: {}s)", elapsed.as_secs());
                        if let Some(w) = app_handle_wake.get_webview_window("main") {
                            let _ = w.emit("system-wake", ());
                        }
                    }
                    last_instant = now;
                }
            });

            // Start the Python Flask backend
            let app_handle = app.handle().clone();
            if let Err(e) = python::start_python_server(&app_handle) {
                log::error!("Failed to start Python server: {e}");
                let err = python::get_server().lock().unwrap().startup_error.clone();
                if let Some(e) = err {
                    let _ = window.emit("startup-error", e);
                }
            }

            // Periodic update checks — 5s after launch, then every 4 hours
            let app_handle_upd = app.handle().clone();
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_secs(5));
                commands::check_for_updates(app_handle_upd.clone());
                loop {
                    std::thread::sleep(std::time::Duration::from_secs(4 * 60 * 60));
                    commands::check_for_updates(app_handle_upd.clone());
                }
            });

            let _ = window.show();
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_api_info,
            commands::get_theme,
            commands::cycle_theme,
            commands::get_app_version,
            commands::open_external,
            commands::report_error,
            commands::report_bug,
            commands::set_backend_health,
            commands::open_diagnostics_log,
            commands::get_login_item,
            commands::set_login_item,
            commands::get_dock_visible,
            commands::set_dock_visible,
            commands::check_for_updates,
            commands::install_update,
            commands::show_notification,
            commands::select_directory,
        ])
        .on_window_event(|window, event| {
            match event {
                tauri::WindowEvent::CloseRequested { api, .. } => {
                    #[cfg(target_os = "macos")]
                    {
                        let _ = window.hide();
                        api.prevent_close();
                    }
                }
                _ => {}
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Plaud V2")
        .run(|app, event| {
            match event {
                // macOS: dock icon click reshows window
                #[cfg(target_os = "macos")]
                tauri::RunEvent::Reopen {
                    has_visible_windows,
                    ..
                } => {
                    if !has_visible_windows {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                }
                // Clean shutdown: disconnect BLE + kill Python
                tauri::RunEvent::ExitRequested { .. } => {
                    python::stop_python_server();
                }
                _ => {}
            }
        });
}
