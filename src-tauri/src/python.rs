use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{AppHandle, Emitter, Manager};

// Minimum Python version required
const MIN_PYTHON_VERSION: (u32, u32) = (3, 10);

/// Global state for the Python server process
pub struct PythonServer {
    pub process: Option<Child>,
    pub port: u16,
    pub url: String,
    pub python_path: String,
    pub python_version: String,
    pub startup_error: Option<StartupError>,
}

#[derive(Clone, serde::Serialize)]
pub struct StartupError {
    pub code: String,
    pub title: String,
    pub friendly: String,
    pub hint: String,
    pub detail: String,
}

static PYTHON_SERVER: std::sync::OnceLock<Mutex<PythonServer>> = std::sync::OnceLock::new();

pub fn get_server() -> &'static Mutex<PythonServer> {
    PYTHON_SERVER.get_or_init(|| {
        Mutex::new(PythonServer {
            process: None,
            port: 5123,
            url: "http://127.0.0.1:5123".to_string(),
            python_path: String::new(),
            python_version: String::new(),
            startup_error: None,
        })
    })
}

fn find_free_port(start: u16) -> u16 {
    for port in start..start + 100 {
        if TcpListener::bind(("127.0.0.1", port)).is_ok() {
            return port;
        }
    }
    start
}

const PYTHON_CANDIDATES: &[&str] = &[
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
    "python3",
];

fn home_dir() -> Option<PathBuf> {
    std::env::var("HOME").ok().map(PathBuf::from)
}

fn bundled_python_dir() -> PathBuf {
    home_dir()
        .unwrap_or_default()
        .join("Library/Application Support/com.plaud.v2/python")
}

/// Find a working Python 3 binary, returns (path, version_string)
fn find_python(full_path: &str) -> Option<(String, String)> {
    let tauri_bundled = bundled_python_dir().join("bin/python3");
    let mut candidates: Vec<String> = vec![];
    if tauri_bundled.exists() {
        candidates.push(tauri_bundled.to_string_lossy().to_string());
    }
    candidates.extend(PYTHON_CANDIDATES.iter().map(|s| s.to_string()));

    for path in &candidates {
        match Command::new(path)
            .arg("--version")
            .env("PATH", full_path)
            .output()
        {
            Ok(output) if output.status.success() => {
                let version = String::from_utf8_lossy(&output.stdout).trim().to_string();
                let version = if version.is_empty() {
                    String::from_utf8_lossy(&output.stderr).trim().to_string()
                } else {
                    version
                };
                if version.contains("Python 3.") {
                    return Some((path.clone(), version));
                }
            }
            _ => continue,
        }
    }
    None
}

/// Parse "Python 3.12.9" -> (3, 12, 9)
fn parse_python_version(s: &str) -> Option<(u32, u32, u32)> {
    let re_part = s.trim().split(' ').last()?;
    let parts: Vec<&str> = re_part.split('.').collect();
    if parts.len() >= 2 {
        Some((
            parts[0].parse().ok()?,
            parts[1].parse().ok()?,
            parts.get(2).and_then(|s| s.parse().ok()).unwrap_or(0),
        ))
    } else {
        None
    }
}

fn is_python_too_old(version: &str) -> bool {
    match parse_python_version(version) {
        Some((major, minor, _)) => {
            major < MIN_PYTHON_VERSION.0
                || (major == MIN_PYTHON_VERSION.0 && minor < MIN_PYTHON_VERSION.1)
        }
        None => true,
    }
}

/// Strategy 1: Install Python via Homebrew
fn try_homebrew_install(full_path: &str) -> bool {
    if Command::new("which")
        .arg("brew")
        .env("PATH", full_path)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
    {
        log::info!("[Python] Attempting install via Homebrew...");
        Command::new("brew")
            .arg("install")
            .arg("python3")
            .env("PATH", full_path)
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    } else {
        false
    }
}

/// Strategy 2: Download standalone Python from Astral
fn try_download_standalone() -> bool {
    let arch = if cfg!(target_arch = "aarch64") {
        "aarch64"
    } else {
        "x86_64"
    };
    let tag = "20250317";
    let py_ver = "3.12.9";
    let filename = format!("cpython-{py_ver}+{tag}-{arch}-apple-darwin-install_only.tar.gz");
    let url = format!(
        "https://github.com/astral-sh/python-build-standalone/releases/download/{tag}/{filename}"
    );

    log::info!("[Python] Downloading standalone Python: {filename}");

    let tmp_dir = std::env::temp_dir();
    let tar_path = tmp_dir.join(&filename);

    // Download with curl
    let dl = Command::new("curl")
        .args(["-fSL", "--retry", "2", "-o"])
        .arg(&tar_path)
        .arg(&url)
        .output();

    if dl.is_err() || !dl.unwrap().status.success() {
        log::error!("[Python] Download failed");
        return false;
    }

    // Extract
    let dest = bundled_python_dir();
    let _ = std::fs::create_dir_all(&dest);
    let extract = Command::new("tar")
        .args(["-xzf"])
        .arg(&tar_path)
        .args(["--strip-components=1", "-C"])
        .arg(&dest)
        .output();

    let _ = std::fs::remove_file(&tar_path);

    if extract.is_err() || !extract.unwrap().status.success() {
        log::error!("[Python] Extract failed");
        return false;
    }

    // Verify
    let bin = dest.join("bin/python3");
    Command::new(&bin)
        .arg("--version")
        .output()
        .map(|o| {
            if o.status.success() {
                log::info!(
                    "[Python] Standalone installed: {}",
                    String::from_utf8_lossy(&o.stdout).trim()
                );
                true
            } else {
                false
            }
        })
        .unwrap_or(false)
}

fn ensure_python_deps(python_path: &str, full_path: &str) -> Result<(), String> {
    let deps = ["flask", "requests", "bleak", "cryptography"];
    let mut missing = vec![];

    for dep in &deps {
        let result = Command::new(python_path)
            .args(["-c", &format!("import {dep}")])
            .env("PATH", full_path)
            .output();
        if result.is_err() || !result.unwrap().status.success() {
            missing.push(*dep);
        }
    }

    if missing.is_empty() {
        return Ok(());
    }

    log::info!("Installing missing Python deps: {:?}", missing);

    // Try with --break-system-packages first (pip 23.1+)
    let pip_args: Vec<&str> = ["-m", "pip", "install", "--break-system-packages", "--user", "-q"]
        .iter()
        .copied()
        .chain(missing.iter().copied())
        .collect();

    if let Ok(output) = Command::new(python_path)
        .args(&pip_args)
        .env("PATH", full_path)
        .output()
    {
        if output.status.success() {
            return Ok(());
        }
    }

    // Fallback without --break-system-packages
    let pip_args: Vec<&str> = ["-m", "pip", "install", "--user", "-q"]
        .iter()
        .copied()
        .chain(missing.iter().copied())
        .collect();

    match Command::new(python_path)
        .args(&pip_args)
        .env("PATH", full_path)
        .output()
    {
        Ok(output) if output.status.success() => Ok(()),
        Ok(output) => Err(format!(
            "pip install failed: {}",
            String::from_utf8_lossy(&output.stderr)
        )),
        Err(e) => Err(format!("pip install error: {e}")),
    }
}

fn wait_for_server(url: &str, max_attempts: u32) -> bool {
    let addr = url
        .trim_start_matches("http://")
        .trim_start_matches("https://");
    for _ in 0..max_attempts {
        if let Ok(mut stream) = std::net::TcpStream::connect_timeout(
            &addr
                .parse()
                .unwrap_or_else(|_| std::net::SocketAddr::from(([127, 0, 0, 1], 5123))),
            Duration::from_secs(1),
        ) {
            let request = format!("GET /health HTTP/1.0\r\nHost: {addr}\r\n\r\n");
            if stream.write_all(request.as_bytes()).is_ok() {
                let mut response = String::new();
                let _ = stream.read_to_string(&mut response);
                if response.contains("200") && response.contains("ok") {
                    return true;
                }
            }
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

/// Send BLE disconnect request to the Python backend (fire-and-forget)
pub fn send_ble_disconnect() {
    let server = get_server().lock().unwrap();
    let port = server.port;
    drop(server);
    if let Ok(mut s) = std::net::TcpStream::connect_timeout(
        &format!("127.0.0.1:{port}")
            .parse()
            .unwrap_or_else(|_| std::net::SocketAddr::from(([127, 0, 0, 1], 5123))),
        Duration::from_secs(2),
    ) {
        let _ = write!(
            s,
            "POST /device/disconnect HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n"
        );
    }
}

fn set_startup_error(code: &str, title: &str, friendly: &str, hint: &str, detail: &str) {
    let err = StartupError {
        code: code.to_string(),
        title: title.to_string(),
        friendly: friendly.to_string(),
        hint: hint.to_string(),
        detail: detail.to_string(),
    };
    if let Ok(mut server) = get_server().lock() {
        server.startup_error = Some(err);
    }
}

fn emit_startup_status(app: &AppHandle, msg: &str) {
    log::info!("[Startup] {msg}");
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.emit("startup-status", msg);
    }
}

/// Build PATH with common Python locations + bundled bin dir
fn build_path(app: &AppHandle) -> String {
    let resource_path = app.path().resource_dir().unwrap_or_default();
    let bundled_bin = resource_path.join("bin");

    // App support bin/ — where FFmpeg gets downloaded on first use
    let app_support_bin = home_dir()
        .unwrap_or_default()
        .join("Library/Application Support/com.plaud.v2/bin");

    let extra_paths = vec![
        app_support_bin.to_string_lossy().to_string(),
        bundled_bin.to_string_lossy().to_string(),
        "/opt/homebrew/bin".to_string(),
        "/usr/local/bin".to_string(),
        "/usr/bin".to_string(),
        "/bin".to_string(),
    ];

    let system_path = std::env::var("PATH").unwrap_or_default();
    let mut all_paths: Vec<String> = extra_paths;
    for p in system_path.split(':') {
        if !all_paths.contains(&p.to_string()) {
            all_paths.push(p.to_string());
        }
    }
    all_paths.join(":")
}

/// Kill any orphaned Python server from a previous crash (SIGKILL leaves zombies)
fn kill_orphan_python_servers() {
    // Look for Python processes listening on our port range
    if let Ok(output) = Command::new("lsof")
        .args(["-ti", "tcp:5123-5223", "-sTCP:LISTEN"])
        .output()
    {
        let pids = String::from_utf8_lossy(&output.stdout);
        for pid_str in pids.trim().lines() {
            if let Ok(pid) = pid_str.trim().parse::<i32>() {
                // Check if it's a Python process (not something else on that port)
                if let Ok(ps) = Command::new("ps")
                    .args(["-p", &pid.to_string(), "-o", "comm="])
                    .output()
                {
                    let comm = String::from_utf8_lossy(&ps.stdout);
                    if comm.to_lowercase().contains("python") {
                        log::warn!("[Cleanup] Killing orphan Python server (pid: {pid})");
                        #[cfg(unix)]
                        unsafe {
                            libc::kill(pid, libc::SIGTERM);
                        }
                    }
                }
            }
        }
    }
}

/// Start the Python Flask API server
pub fn start_python_server(app: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    // Clean up any orphaned Python servers from a previous crash
    kill_orphan_python_servers();
    let full_path = build_path(app);
    let app_handle = app.clone();

    // Find Python
    emit_startup_status(app, "Looking for Python...");
    let mut python = find_python(&full_path);

    // Check version
    if let Some((_, ref ver)) = python {
        if is_python_too_old(ver) {
            let reason = format!("{ver} is too old (need {}.{}+)", MIN_PYTHON_VERSION.0, MIN_PYTHON_VERSION.1);
            emit_startup_status(app, &format!("Python {reason} — installing automatically..."));
            python = None; // Force re-install
        }
    }

    // Auto-install if missing or too old
    if python.is_none() {
        emit_startup_status(app, "Trying Homebrew install...");
        if try_homebrew_install(&full_path) {
            python = find_python(&full_path);
            if python.as_ref().map(|(_, v)| is_python_too_old(v)).unwrap_or(true) {
                python = None;
            }
        }
        if python.is_none() {
            emit_startup_status(app, "Downloading Python 3.12...");
            if try_download_standalone() {
                python = find_python(&full_path);
            }
        }
        if python.is_none() {
            let detail = "No suitable Python 3 found. Auto-install failed.";
            set_startup_error(
                "PYTHON_NOT_FOUND",
                "Python 3 not found",
                "Plaud V2 needs Python 3 to run.",
                "Install Python 3 from python.org or run: brew install python3",
                detail,
            );
            return Err(detail.into());
        }
    }

    let (python_path, python_version) = python.unwrap();
    emit_startup_status(app, &format!("Found {python_version}"));

    // Store Python info
    {
        let mut server = get_server().lock().unwrap();
        server.python_path = python_path.clone();
        server.python_version = python_version.clone();
    }

    // Install missing deps
    emit_startup_status(app, "Checking dependencies...");
    if let Err(e) = ensure_python_deps(&python_path, &full_path) {
        log::error!("Failed to install Python deps: {e}");
        set_startup_error(
            "DEPS_FAILED",
            "Missing dependencies",
            "Some required Python packages couldn't be installed.",
            "Try running: pip3 install flask requests bleak cryptography",
            &e,
        );
    }

    // Find free port
    let port = find_free_port(5123);
    let url = format!("http://127.0.0.1:{port}");

    // Locate api_server.py
    emit_startup_status(app, "Starting server...");
    let server_script = find_server_script(app);
    let cwd = server_script
        .parent()
        .and_then(|p| p.parent())
        .unwrap_or(server_script.parent().unwrap())
        .to_path_buf();

    log::info!("Python script: {}", server_script.display());
    log::info!("CWD: {}", cwd.display());
    log::info!("Port: {port}");

    // Spawn with stdout/stderr capture
    let mut child = Command::new(&python_path)
        .arg(&server_script)
        .current_dir(&cwd)
        .env("PATH", &full_path)
        .env("PYTHONUNBUFFERED", "1")
        .env("PLAUD_API_PORT", port.to_string())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| {
            set_startup_error(
                "SPAWN_FAILED",
                "Couldn't start Python",
                "The system couldn't launch the Python process.",
                "Check that Python 3 is installed and accessible.",
                &e.to_string(),
            );
            format!("Failed to spawn Python: {e}")
        })?;

    // Capture stdout in a background thread
    if let Some(stdout) = child.stdout.take() {
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                if let Ok(line) = line {
                    eprintln!("[Python] {line}");
                }
            }
        });
    }

    // Capture stderr in a background thread
    if let Some(stderr) = child.stderr.take() {
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines() {
                if let Ok(line) = line {
                    eprintln!("[Python] {line}");
                }
            }
        });
    }

    // Store in global state
    {
        let mut server = get_server().lock().unwrap();
        server.process = Some(child);
        server.port = port;
        server.url = url.clone();
    }

    // Wait for server + monitor for crashes in background
    let url_clone = url.clone();
    std::thread::spawn(move || {
        if wait_for_server(&url_clone, 60) {
            log::info!("Python server is ready at {url_clone}");
        } else {
            log::error!("Python server failed to start within 30 seconds");
            set_startup_error(
                "SERVER_TIMEOUT",
                "Server not responding",
                "The Python backend started but never became ready.",
                "Try restarting the app.",
                "Health check timed out after 30s",
            );
            if let Some(w) = app_handle.get_webview_window("main") {
                let err = get_server().lock().unwrap().startup_error.clone();
                if let Some(e) = err {
                    let _ = w.emit("startup-error", e);
                }
            }
        }

        // Monitor for crashes — check every 5 seconds, auto-restart up to 3 times
        let mut restart_count = 0;
        const MAX_RESTARTS: u32 = 3;
        loop {
            std::thread::sleep(Duration::from_secs(5));
            let mut server = get_server().lock().unwrap();
            if let Some(ref mut child) = server.process {
                match child.try_wait() {
                    Ok(Some(status)) => {
                        let code = status.code().unwrap_or(-1);
                        server.process = None;
                        drop(server);

                        if code != 0 {
                            log::error!("[Python] Server crashed with exit code {code}");
                            restart_count += 1;

                            if restart_count <= MAX_RESTARTS {
                                log::info!("[Python] Auto-restarting (attempt {restart_count}/{MAX_RESTARTS})...");
                                // Re-spawn the Python server
                                if let Err(e) = restart_python_server(&app_handle) {
                                    log::error!("[Python] Restart failed: {e}");
                                    set_startup_error(
                                        "SERVER_CRASH",
                                        "Server crashed",
                                        "The Python backend crashed and could not be restarted.",
                                        "Try restarting the app.",
                                        &format!("Exit code {code}, restart failed: {e}"),
                                    );
                                    if let Some(w) = app_handle.get_webview_window("main") {
                                        let err = get_server().lock().unwrap().startup_error.clone();
                                        if let Some(e) = err {
                                            let _ = w.emit("startup-error", e);
                                        }
                                    }
                                    break;
                                }
                                // Continue monitoring the new process
                                continue;
                            } else {
                                log::error!("[Python] Max restarts ({MAX_RESTARTS}) exceeded");
                                set_startup_error(
                                    "SERVER_CRASH",
                                    "Server keeps crashing",
                                    "The Python backend crashed repeatedly.",
                                    "Try restarting the app. If this keeps happening, check Python installation.",
                                    &format!("Exit code {code}, {restart_count} restarts attempted"),
                                );
                                if let Some(w) = app_handle.get_webview_window("main") {
                                    let err = get_server().lock().unwrap().startup_error.clone();
                                    if let Some(e) = err {
                                        let _ = w.emit("startup-error", e);
                                    }
                                }
                                break;
                            }
                        } else {
                            // Clean exit (code 0) — don't restart
                            break;
                        }
                    }
                    Ok(None) => {} // Still running
                    Err(_) => break,
                }
            } else {
                break;
            }
        }
    });

    Ok(())
}

fn find_server_script(app: &AppHandle) -> PathBuf {
    // Check bundled Resources FIRST — the CARGO_MANIFEST_DIR dev path is baked at
    // compile time and will still exist on the dev machine even in production builds.
    // We must prefer the bundled version so _is_packaged resolves correctly.
    if let Ok(resource_dir) = app.path().resource_dir() {
        let bundled = resource_dir.join("app").join("api_server.py");
        if bundled.exists() {
            return bundled;
        }
    }

    // Fallback: dev mode (running from cargo tauri dev)
    let dev_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("app")
        .join("api_server.py");
    dev_path
}

/// Re-spawn the Python server after a crash (reuses existing config)
fn restart_python_server(app: &AppHandle) -> Result<(), String> {
    let server = get_server().lock().unwrap();
    let python_path = server.python_path.clone();
    let port = server.port;
    drop(server);

    if python_path.is_empty() {
        return Err("No Python path cached".to_string());
    }

    let full_path = build_path(app);
    let server_script = find_server_script(app);
    let cwd = server_script
        .parent()
        .and_then(|p| p.parent())
        .unwrap_or(server_script.parent().unwrap())
        .to_path_buf();

    let mut child = Command::new(&python_path)
        .arg(&server_script)
        .current_dir(&cwd)
        .env("PATH", &full_path)
        .env("PYTHONUNBUFFERED", "1")
        .env("PLAUD_API_PORT", port.to_string())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Spawn failed: {e}"))?;

    // Capture output
    if let Some(stdout) = child.stdout.take() {
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines().map_while(Result::ok) {
                eprintln!("[Python] {line}");
            }
        });
    }
    if let Some(stderr) = child.stderr.take() {
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().map_while(Result::ok) {
                eprintln!("[Python] {line}");
            }
        });
    }

    {
        let mut server = get_server().lock().unwrap();
        server.process = Some(child);
        server.startup_error = None;
    }

    // Wait for health check
    let url = format!("http://127.0.0.1:{port}");
    if !wait_for_server(&url, 30) {
        return Err("Server did not become ready after restart".to_string());
    }

    log::info!("[Python] Server restarted successfully on port {port}");
    Ok(())
}

/// Gracefully stop the Python server — disconnect BLE, SIGTERM, then SIGKILL
pub fn stop_python_server() {
    // Disconnect BLE before killing Python — give it 2s to complete the BLE teardown
    send_ble_disconnect();
    std::thread::sleep(Duration::from_secs(2));

    if let Some(server_lock) = PYTHON_SERVER.get() {
        if let Ok(mut server) = server_lock.lock() {
            if let Some(ref mut child) = server.process {
                let pid = child.id();
                log::info!("Stopping Python server (pid: {pid})...");

                // Send SIGTERM first (graceful shutdown)
                #[cfg(unix)]
                {
                    unsafe {
                        libc::kill(pid as i32, libc::SIGTERM);
                    }
                }

                // Wait up to 3 seconds for graceful exit
                let start = std::time::Instant::now();
                while start.elapsed() < Duration::from_secs(3) {
                    if child.try_wait().ok().flatten().is_some() {
                        log::info!("Python server exited gracefully");
                        server.process = None;
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }

                // Force kill if still running
                log::warn!("Python server did not exit gracefully, sending SIGKILL");
                let _ = child.kill();
                let _ = child.wait();
            }
            server.process = None;
        }
    }
}
