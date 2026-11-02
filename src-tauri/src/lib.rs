mod commands;
mod lifecycle;
mod logging;
mod paths;
mod power;
mod python;

use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconBuilder,
    Emitter, Manager,
};

pub use lifecycle::{quit_app, shutdown_backend};

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
    paths::app_support_dir().join("tauri-prefs.json")
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
    // ~/Library/Logs/Plaud V2/app.log ("Plaud V2 (Dev)" in debug builds, + stderr). First, so that
    // everything after it, panics included, is logged.
    logging::init();
    // Report Rust panics (see lifecycle::install_panic_hook for the ordering).
    lifecycle::install_panic_hook();

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
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

            // Keep the latest startup message for the page to poll: the first
            // ones are emitted before the page has loaded.
            lifecycle::track_startup_messages(app.handle());

            // SIGTERM/SIGINT (logout, `kill`, Ctrl-C in dev) would otherwise
            // end the process without stopping the Python backend.
            lifecycle::handle_termination_signals(app.handle());

            // macOS: themed dock icon. (Traffic lights: titleBarStyle Overlay +
            // trafficLightPosition in tauri.conf.json.)
            #[cfg(target_os = "macos")]
            {
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

                // Sleep/wake: NSWorkspace notifications (setup runs on the main thread)
                power::install_workspace_observers(app.handle());
            }

            // Wall-clock fallback for wake detection. De-duplicated with the
            // notifications, so one wake sends one `system-wake` event.
            power::spawn_fallback_detector(app.handle().clone());

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
                    // Hides the window, stops the backend off the main thread, then exits.
                    "quit" => lifecycle::quit_app(app, 0),
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

            // Show the window before the backend is up: the page's splash
            // (health-gate.js) polls get_startup_status and shows progress. A
            // first launch can spend minutes finding or installing Python.
            let _ = window.show();

            // Start the Python Flask backend on a background thread.
            lifecycle::spawn_backend_startup(app.handle(), false);

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

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_api_info,
            lifecycle::get_startup_status,
            lifecycle::retry_startup,
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
                // app.exit() (tray Quit, install_update): hold the exit, stop the
                // backend (BLE disconnect + kill Python) once, off the main
                // thread, then exit for real.
                tauri::RunEvent::ExitRequested { code, api, .. } => {
                    if !lifecycle::shutdown_done() {
                        api.prevent_exit();
                        lifecycle::quit_app(app, code.unwrap_or(0));
                    }
                }
                // Cmd+Q (NSApp terminate:) skips ExitRequested and the process
                // ends right after this event, so stop the backend here,
                // synchronously. No-op if it was already stopped.
                tauri::RunEvent::Exit => {
                    lifecycle::shutdown_backend();
                }
                _ => {}
            }
        });
}
