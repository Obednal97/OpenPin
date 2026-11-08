//! Backend startup, startup status, shutdown and the panic hook.
//!
//! * Startup: `start_python_server` (python.rs) can take minutes on a first
//!   launch (finding or installing Python, pip installs), so it runs on a
//!   background thread after the window is shown. Its progress messages are
//!   kept here, and the page polls `get_startup_status` for them.
//! * Shutdown: stopping the backend (BLE disconnect + SIGTERM/SIGKILL) takes
//!   2-5 s. It runs once, off the main thread where possible, and the app exits
//!   when it's done.
//! * Panics: the panic hook records them in the app log and the local
//!   diagnostics log (nothing is sent anywhere).

use std::panic::AssertUnwindSafe;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};

use crate::{commands, python};

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

/// True while a `start_python_server` call is in progress.
static STARTUP_RUNNING: AtomicBool = AtomicBool::new(false);
/// Number of startup attempts this launch (1 = first launch, more = Retry).
static STARTUP_ATTEMPT: AtomicU32 = AtomicU32::new(0);

struct StartupState {
    /// Latest progress message.
    message: String,
    started_at: Option<Instant>,
    /// Result of the last finished `start_python_server` call.
    outcome: Option<Result<(), String>>,
}

static STARTUP_STATE: Mutex<StartupState> = Mutex::new(StartupState {
    message: String::new(),
    started_at: None,
    outcome: None,
});

fn startup_state() -> std::sync::MutexGuard<'static, StartupState> {
    STARTUP_STATE.lock().unwrap_or_else(|e| e.into_inner())
}

/// Latest human-readable progress message from python.rs ("Looking for Python...").
pub(crate) fn set_startup_message(msg: &str) {
    startup_state().message = msg.to_string();
}

/// What `get_startup_status` returns to the page.
#[derive(Clone, Serialize)]
pub struct StartupStatus {
    /// "starting" | "ready" | "error"
    pub phase: &'static str,
    /// Latest progress message, e.g. "Checking dependencies...".
    pub message: String,
    /// Time since the current startup attempt began.
    pub elapsed_ms: u64,
    /// 1 on a normal launch; higher after Retry.
    pub attempt: u32,
    /// Set only when `phase` is "error".
    pub error: Option<python::StartupError>,
}

/// Pure decision behind `current_status`, kept separate so it can be tested.
fn decide_phase(
    ready: bool,
    running: bool,
    outcome: Option<&Result<(), String>>,
    error_code: Option<&str>,
) -> &'static str {
    if ready {
        return "ready";
    }
    if running {
        return "starting";
    }
    match outcome {
        Some(Err(_)) => "error",
        // Server spawned; python.rs is waiting for /health. An error set after
        // the spawn (SERVER_TIMEOUT, SERVER_CRASH...) shows the failure screen.
        // SERVER_TIMEOUT isn't final: python.rs keeps polling, and a late
        // answer sets SERVER_READY and clears the error ("ready" above).
        Some(Ok(())) => match error_code {
            Some(_) => "error",
            None => "starting",
        },
        // The startup thread hasn't been spawned yet.
        None => "starting",
    }
}

pub fn current_status() -> StartupStatus {
    let ready = python::SERVER_READY.load(Ordering::Acquire);
    let running = STARTUP_RUNNING.load(Ordering::Acquire);
    let server_error = python::get_server()
        .lock()
        .map(|s| s.startup_error.clone())
        .unwrap_or_else(|e| e.into_inner().startup_error.clone());
    let st = startup_state();
    let phase = decide_phase(
        ready,
        running,
        st.outcome.as_ref(),
        server_error.as_ref().map(|e| e.code.as_str()),
    );
    let error = if phase == "error" {
        server_error.or_else(|| match &st.outcome {
            Some(Err(detail)) => Some(python::StartupError {
                code: "STARTUP_FAILED".into(),
                title: "Couldn't start the backend".into(),
                friendly: "Open Pin's local server couldn't be started.".into(),
                hint: "Try again. If this keeps happening, open the diagnostics log.".into(),
                detail: detail.clone(),
            }),
            _ => None,
        })
    } else {
        None
    };
    let message = if ready {
        "Ready".to_string()
    } else if st.message.is_empty() {
        "Starting…".to_string()
    } else {
        st.message.clone()
    };
    StartupStatus {
        phase,
        message,
        elapsed_ms: st
            .started_at
            .map(|t| t.elapsed().as_millis() as u64)
            .unwrap_or(0),
        attempt: STARTUP_ATTEMPT.load(Ordering::Acquire),
        error,
    }
}

/// Run `start_python_server` on a background thread. Returns false if a start
/// is already running or the app is quitting.
///
/// With `restart`, a backend process left over from a failed attempt is
/// stopped and the old error cleared first (used by Retry).
pub fn spawn_backend_startup(app: &AppHandle, restart: bool) -> bool {
    if SHUTDOWN_STARTED.load(Ordering::Acquire) {
        return false;
    }
    if STARTUP_RUNNING
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return false;
    }
    STARTUP_ATTEMPT.fetch_add(1, Ordering::AcqRel);
    {
        let mut st = startup_state();
        st.message = "Starting…".into();
        st.started_at = Some(Instant::now());
        st.outcome = None;
    }

    let app = app.clone();
    let spawned = std::thread::Builder::new()
        .name("backend-startup".into())
        .spawn(move || {
            if restart {
                if backend_process_running() {
                    // It may have come up since the page last looked (a slow
                    // first start past SERVER_TIMEOUT): never kill a healthy one.
                    if python::confirm_ready_now() {
                        log::info!("[Startup] Retry: backend is already healthy, not restarting");
                        startup_state().outcome = Some(Ok(()));
                        STARTUP_RUNNING.store(false, Ordering::Release);
                        return;
                    }
                    set_startup_message("Stopping the previous backend…");
                    python::stop_python_server();
                }
                if let Ok(mut server) = python::get_server().lock() {
                    server.startup_error = None;
                }
            }

            // A panic in here would otherwise leave STARTUP_RUNNING stuck at
            // true and the page waiting until its ceiling.
            let outcome = std::panic::catch_unwind(AssertUnwindSafe(|| {
                python::start_python_server(&app).map_err(|e| e.to_string())
            }))
            .unwrap_or_else(|_| Err("Backend startup panicked".to_string()));

            if let Err(e) = &outcome {
                log::error!("Failed to start Python server: {e}");
                eprintln!("[Startup] Failed to start Python server: {e}");
                let err = python::get_server()
                    .lock()
                    .ok()
                    .and_then(|s| s.startup_error.clone());
                if let Some(err) = err {
                    let _ = app.emit("startup-error", err);
                }
            }
            startup_state().outcome = Some(outcome);
            STARTUP_RUNNING.store(false, Ordering::Release);

            // Belt and braces: python.rs stops a backend spawned after quit
            // began (spawn_and_register), but if one is still registered here
            // while quitting, stop it rather than orphan it.
            if SHUTDOWN_STARTED.load(Ordering::Acquire) && backend_process_running() {
                python::stop_python_server();
            }
        });

    if let Err(e) = spawned {
        log::error!("Couldn't spawn the backend startup thread: {e}");
        startup_state().outcome = Some(Err(format!("Couldn't spawn startup thread: {e}")));
        STARTUP_RUNNING.store(false, Ordering::Release);
        return false;
    }
    true
}

fn backend_process_running() -> bool {
    python::get_server()
        .lock()
        .map(|s| s.process.is_some())
        .unwrap_or(false)
}

/// Startup progress for the page's splash (health-gate.js).
#[tauri::command]
pub fn get_startup_status() -> StartupStatus {
    current_status()
}

/// Retry after a failed startup: stop any half-started backend and start again.
/// No-op when the backend is ready (checked against /health right now, since a
/// slow start can come up after SERVER_TIMEOUT) or a start is already running.
#[tauri::command]
pub fn retry_startup(app: AppHandle) -> StartupStatus {
    if !python::confirm_ready_now() {
        spawn_backend_startup(&app, true);
    }
    current_status()
}

// ---------------------------------------------------------------------------
// Shutdown
// ---------------------------------------------------------------------------

static SHUTDOWN_STARTED: AtomicBool = AtomicBool::new(false);
static SHUTDOWN_DONE: AtomicBool = AtomicBool::new(false);
/// How long a second caller waits for the first one's shutdown to finish.
/// stop_python_server takes at most ~5 s (2 s BLE disconnect + 3 s SIGTERM grace).
const SHUTDOWN_WAIT: Duration = Duration::from_secs(8);

pub fn shutdown_done() -> bool {
    SHUTDOWN_DONE.load(Ordering::Acquire)
}

/// Stop the backend exactly once. Blocking. Later callers wait for the first
/// one to finish instead of running the BLE disconnect + kill sequence again.
pub fn shutdown_backend() {
    if SHUTDOWN_STARTED.swap(true, Ordering::AcqRel) {
        let deadline = Instant::now() + SHUTDOWN_WAIT;
        while !SHUTDOWN_DONE.load(Ordering::Acquire) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(50));
        }
        return;
    }
    // Block new spawns and let one in flight register, so a backend spawned
    // mid-startup is found here (or stopped by the spawner) instead of being
    // orphaned when the app exits.
    python::begin_shutdown();
    // Nothing to stop if the backend never started or was already stopped
    // (install_update stops it itself before calling app.exit).
    if backend_process_running() {
        python::stop_python_server();
    } else {
        log::info!("[Shutdown] No backend process to stop");
    }
    SHUTDOWN_DONE.store(true, Ordering::Release);
}

/// Quit without blocking the main thread: hide the window, stop the backend
/// on a background thread, then exit with `code`.
pub fn quit_app(app: &AppHandle, code: i32) {
    if shutdown_done() {
        app.exit(code);
        return;
    }
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.hide();
    }
    let worker_app = app.clone();
    let spawned = std::thread::Builder::new()
        .name("shutdown".into())
        .spawn(move || {
            shutdown_backend();
            worker_app.exit(code);
        });
    if spawned.is_err() {
        // Couldn't get a thread: do it here rather than not at all.
        shutdown_backend();
        app.exit(code);
    }
}

/// How long after a termination signal the event loop gets to exit by itself
/// (once the backend is stopped) before the process exits directly.
const SIGNAL_EXIT_GRACE: Duration = Duration::from_secs(3);

/// Stop the backend on SIGTERM/SIGINT, then exit. Without this the default
/// action ends the process at once and the Python backend is orphaned (the next
/// launch's PID-file sweep only catches it later). signal-hook's iterator
/// receives signals on its own thread, so nothing runs in signal context. A
/// second signal exits immediately.
pub fn handle_termination_signals(app: &AppHandle) {
    use signal_hook::consts::{SIGINT, SIGTERM};
    let mut signals = match signal_hook::iterator::Signals::new([SIGTERM, SIGINT]) {
        Ok(s) => s,
        Err(e) => {
            log::warn!("[Shutdown] Couldn't install SIGTERM/SIGINT handlers: {e}");
            return;
        }
    };
    let app = app.clone();
    let spawned = std::thread::Builder::new()
        .name("signals".into())
        .spawn(move || {
            let mut first = true;
            for sig in signals.forever() {
                if !first {
                    log::warn!("[Shutdown] Second signal {sig}; exiting now");
                    std::process::exit(128 + sig);
                }
                first = false;
                log::info!("[Shutdown] Signal {sig}: stopping the backend, then exiting");
                let app = app.clone();
                let _ = std::thread::Builder::new()
                    .name("signal-shutdown".into())
                    .spawn(move || {
                        shutdown_backend();
                        app.exit(0);
                        std::thread::sleep(SIGNAL_EXIT_GRACE);
                        log::warn!("[Shutdown] Event loop didn't exit; exiting directly");
                        log::logger().flush();
                        std::process::exit(128 + sig);
                    });
            }
        });
    if let Err(e) = spawned {
        log::warn!("[Shutdown] Couldn't start the signal thread: {e}");
    }
}

// ---------------------------------------------------------------------------
// Panic hook
// ---------------------------------------------------------------------------

/// Set while a panic is being recorded, so a burst of follow-on panics
/// (e.g. threads hitting a poisoned mutex) only logs.
static PANIC_REPORT_IN_FLIGHT: AtomicBool = AtomicBool::new(false);

/// Record Rust panics in the app log and the local diagnostics log.
///
/// Nothing is sent anywhere: a panic shows up in an exported bug report
/// (Report a bug › Export bug report). The record is written directly, with no
/// extra thread, because on the main thread a panic aborts the process when the
/// hook returns.
pub fn install_panic_hook() {
    std::panic::set_hook(Box::new(|info| {
        let message = format!("{info}");
        let backtrace = std::backtrace::Backtrace::force_capture().to_string();
        let thread = std::thread::current();
        let thread_name = thread.name().unwrap_or("unnamed").to_string();
        log::error!("[Rust Panic] ({thread_name}) {message}\n{backtrace}");
        eprintln!("[Rust Panic] ({thread_name}) {message}\n{backtrace}");

        if PANIC_REPORT_IN_FLIGHT.swap(true, Ordering::AcqRel) {
            return;
        }
        commands::report_error(format!("[PANIC] {message}"), Some(backtrace), Some("rust-panic".to_string()));
        PANIC_REPORT_IN_FLIGHT.store(false, Ordering::Release);
    }));
}

#[cfg(test)]
mod tests {
    use super::decide_phase;

    #[test]
    fn ready_wins() {
        assert_eq!(decide_phase(true, false, Some(&Err("x".into())), Some("SERVER_CRASH")), "ready");
    }

    #[test]
    fn running_is_starting_even_with_stale_error() {
        assert_eq!(decide_phase(false, true, None, Some("SERVER_TIMEOUT")), "starting");
    }

    #[test]
    fn failed_start_is_error() {
        assert_eq!(decide_phase(false, false, Some(&Err("no python".into())), Some("PYTHON_NOT_FOUND")), "error");
        assert_eq!(decide_phase(false, false, Some(&Err("x".into())), None), "error");
        // python.rs returns Err for a failed pip install: a real error.
        assert_eq!(decide_phase(false, false, Some(&Err("pip".into())), Some("DEPS_FAILED")), "error");
    }

    #[test]
    fn spawned_and_waiting_is_starting() {
        assert_eq!(decide_phase(false, false, Some(&Ok(())), None), "starting");
    }

    #[test]
    fn error_after_spawn_is_error() {
        assert_eq!(decide_phase(false, false, Some(&Ok(())), Some("SERVER_TIMEOUT")), "error");
        assert_eq!(decide_phase(false, false, Some(&Ok(())), Some("SERVER_CRASH")), "error");
    }

    #[test]
    fn late_ready_after_timeout_is_ready() {
        // python.rs keeps polling after SERVER_TIMEOUT; once /health answers
        // SERVER_READY is set (and the error cleared), and ready wins even if
        // the page reads the status between the two.
        assert_eq!(decide_phase(true, false, Some(&Ok(())), Some("SERVER_TIMEOUT")), "ready");
        assert_eq!(decide_phase(true, false, Some(&Ok(())), None), "ready");
    }

    #[test]
    fn not_started_yet_is_starting() {
        assert_eq!(decide_phase(false, false, None, None), "starting");
    }
}
