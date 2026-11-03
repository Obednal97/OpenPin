use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Flipped to `true` only once the backend monitor sees `/health` respond
/// (however long that takes). Reset to `false` whenever the backend is stopped
/// or crashes. `get_api_info` / `get_startup_status` read it.
pub static SERVER_READY: AtomicBool = AtomicBool::new(false);

/// Set once the app starts quitting (`begin_shutdown`): no backend may be
/// spawned after that, and one spawned concurrently is stopped right away.
static SHUTTING_DOWN: AtomicBool = AtomicBool::new(false);
/// True from just before a backend spawn until the child is registered in
/// `PythonServer::process`, where `stop_python_server` can find it.
static SPAWNING: AtomicBool = AtomicBool::new(false);
/// Error text when a start is abandoned because the app is quitting.
pub const QUITTING: &str = "The app is quitting";

use tauri::{AppHandle, Emitter, Manager};

/// The only Python minor the backend can run on: app/requirements/core.txt is
/// resolved for CPython 3.12 (cp312 wheels, hashes, --only-binary), so pip can
/// never satisfy it on another minor.
const REQUIRED_PYTHON: (u32, u32) = (3, 12);
/// CPU architecture the lockfile's wheels are built for.
const SUPPORTED_ARCH: &str = "aarch64";

/// Global state for the Python server process
pub struct PythonServer {
    pub process: Option<Child>,
    pub port: u16,
    pub url: String,
    pub python_path: String,
    pub python_version: String,
    /// "managed" (app-downloaded CPython 3.12) or "fallback" (Homebrew/system).
    /// Either way `python_path` is the app venv's interpreter.
    pub runtime: String,
    pub startup_error: Option<StartupError>,
    /// Per-launch secret the Python backend requires in the `X-Plaud-Token`
    /// header (passed to it as `PLAUD_API_TOKEN`). Generated once per app
    /// launch and reused across crash restarts so the webview's cached copy
    /// stays valid. Never log it.
    pub api_token: String,
    /// Bumped each time a backend process is registered. A monitor thread
    /// exits once the process it watches has been replaced (Retry).
    pub generation: u64,
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
            runtime: String::new(),
            startup_error: None,
            api_token: generate_api_token(),
            generation: 0,
        })
    })
}

/// HTTP header the Python backend checks the per-launch token in.
pub const API_TOKEN_HEADER: &str = "X-Plaud-Token";

/// 32 random bytes from the OS CSPRNG, hex-encoded (64 chars).
fn generate_api_token() -> String {
    let mut bytes = [0u8; 32];
    let filled = std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .is_ok();
    if !filled {
        // /dev/urandom always exists on macOS/Linux; this is belt and braces.
        // std's RandomState keys are seeded from the OS RNG, so hashing with
        // fresh instances still yields unpredictable bytes.
        log::warn!("[Security] /dev/urandom unavailable, using RandomState fallback for API token");
        use std::hash::{BuildHasher, Hasher};
        for chunk in bytes.chunks_mut(8) {
            let mut h = std::collections::hash_map::RandomState::new().build_hasher();
            h.write_u128(
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_nanos())
                    .unwrap_or(0),
            );
            let v = h.finish().to_le_bytes();
            chunk.copy_from_slice(&v[..chunk.len()]);
        }
    }
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn find_free_port(start: u16) -> Result<u16, String> {
    for port in start..start + 100 {
        if TcpListener::bind(("127.0.0.1", port)).is_ok() {
            return Ok(port);
        }
    }
    Err(format!(
        "NO_FREE_PORT: all ports in {start}–{end} are in use",
        end = start + 99
    ))
}

// ---------------------------------------------------------------------------
// App-managed Python runtime
// ---------------------------------------------------------------------------
//
// The backend runs in a venv at ~/Library/Application Support/com.plaud.v2/venv
// (com.plaud.v2.dev/venv for debug builds, see `paths`) built on a pinned
// CPython 3.12 from python-build-standalone, which the app downloads (SHA-256
// checked against the hashes below) into com.plaud.v2/python, shared by both.
// Core packages come from app/requirements/core.txt (exact pins + hashes),
// embedded in this binary. The venv records the lockfile's SHA-256, so an app
// update that changes the lockfile re-syncs the venv on the next launch.
//
// Homebrew/system Python is only a fallback for when the download fails, and
// even then the packages go into the app's venv, never --user site-packages
// (a Homebrew upgrade used to empty those and features silently disappeared).

/// python-build-standalone release and CPython version of the managed runtime.
const STANDALONE_TAG: &str = "20261026";
const STANDALONE_PY_VERSION: &str = "3.12.16";
/// SHA-256 of `cpython-3.12.16+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz`
/// (GitHub release asset digest; the release's SHA256SUMS lists the same).
/// Pinned here because newer releases no longer publish per-file .sha256 files,
/// and a hash fetched from the same release would only catch corruption anyway.
/// Apple Silicon only: core.txt has no x86_64 wheels, so an Intel runtime
/// couldn't install them anyway.
const STANDALONE_SHA256_AARCH64: &str =
    "353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e";

/// Lockfiles (scripts/lock-python-deps.sh). Embedded so the packaged app doesn't
/// depend on them being listed in tauri.conf.json's bundle resources; written to
/// Application Support at launch and passed to Python in PLAUD_REQUIREMENTS_DIR
/// (app/utils/system.py ensure_installed reads the extras from there).
const CORE_LOCK: &str = include_str!("../../app/requirements/core.txt");
const LOCKFILES: &[(&str, &str)] = &[
    ("core.txt", CORE_LOCK),
    (
        "whisper.txt",
        include_str!("../../app/requirements/whisper.txt"),
    ),
    (
        "diarize.txt",
        include_str!("../../app/requirements/diarize.txt"),
    ),
    (
        "local-summary.txt",
        include_str!("../../app/requirements/local-summary.txt"),
    ),
];

/// Modules the backend can't start without. Checked after every sync, and on
/// each launch (about 0.12 s warm) so a damaged venv gets repaired. anthropic is
/// only located, not imported: importing it (pydantic) would add ~0.35 s per launch.
const CORE_IMPORT_CHECK: &str = "import flask, requests, bleak, cryptography, websockets, \
                                 objc, CoreBluetooth, CoreWLAN, Foundation, importlib.util; \
                                 assert importlib.util.find_spec('anthropic'), 'anthropic missing'";

/// Written into the venv after a successful sync.
const VENV_STAMP_FILE: &str = ".plaud-runtime";

/// Fallback interpreters, in order, when the managed runtime can't be downloaded.
/// Only a CPython 3.12 is accepted (see `REQUIRED_PYTHON`); the unversioned
/// names are probed in case they happen to be 3.12. /usr/bin/python3 is left
/// out: it's 3.9, and without the Command Line Tools probing it pops up an
/// install dialog.
const PYTHON_CANDIDATES: &[&str] = &[
    "/opt/homebrew/bin/python3.12",
    "/opt/homebrew/opt/python@3.12/bin/python3.12",
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12",
    "/usr/local/bin/python3.12",
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
];

use crate::paths::app_support_dir;

/// Managed runtime, shared by dev and release builds (a venv notices a swapped
/// runtime through its stamp and rebuilds).
fn bundled_python_dir() -> PathBuf {
    crate::paths::shared_support_dir().join("python")
}

fn venv_dir() -> PathBuf {
    app_support_dir().join("venv")
}

fn requirements_dir() -> PathBuf {
    app_support_dir().join("requirements")
}

/// Which interpreter the venv is built on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RuntimeKind {
    /// The pinned python-build-standalone CPython the app downloads.
    Managed,
    /// Homebrew/system Python, used only when the download failed.
    Fallback,
}

impl RuntimeKind {
    pub fn as_str(self) -> &'static str {
        match self {
            RuntimeKind::Managed => "managed",
            RuntimeKind::Fallback => "fallback",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct BasePython {
    path: String,
    /// `python3 --version` output, e.g. "Python 3.12.16".
    version: String,
    kind: RuntimeKind,
}

struct StandaloneAsset {
    filename: String,
    url: String,
    sha256: &'static str,
}

/// Download details of the pinned standalone build for a CPU architecture
/// (`std::env::consts::ARCH`). None for anything but Apple Silicon.
fn standalone_asset(arch: &str) -> Option<StandaloneAsset> {
    if arch != SUPPORTED_ARCH {
        return None;
    }
    let filename = format!(
        "cpython-{STANDALONE_PY_VERSION}+{STANDALONE_TAG}-{arch}-apple-darwin-install_only_stripped.tar.gz"
    );
    let url = format!(
        "https://github.com/astral-sh/python-build-standalone/releases/download/{STANDALONE_TAG}/{filename}"
    );
    Some(StandaloneAsset {
        filename,
        url,
        sha256: STANDALONE_SHA256_AARCH64,
    })
}

/// Run `<path> --version`; returns (path, "Python 3.x.y") if it's a Python 3.
fn probe_python(path: &str, full_path: &str) -> Option<(String, String)> {
    let output = Command::new(path)
        .arg("--version")
        .env("PATH", full_path)
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let mut version = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if version.is_empty() {
        version = String::from_utf8_lossy(&output.stderr).trim().to_string();
    }
    version
        .contains("Python 3.")
        .then(|| (path.to_string(), version))
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

/// True for any CPython 3.12.x ("Python 3.12.9"): the only minor core.txt's
/// cp312 wheels install on.
fn is_supported_python(version: &str) -> bool {
    minor_version(version) == Some(REQUIRED_PYTHON)
}

/// Runtime selection order: the managed standalone build (if present and 3.12),
/// else the first fallback candidate that is 3.12.
fn choose_base_python(
    managed: Option<(String, String)>,
    fallbacks: Vec<(String, String)>,
) -> Option<BasePython> {
    if let Some((path, version)) = managed {
        if is_supported_python(&version) {
            return Some(BasePython {
                path,
                version,
                kind: RuntimeKind::Managed,
            });
        }
    }
    fallbacks
        .into_iter()
        .find(|(_, version)| is_supported_python(version))
        .map(|(path, version)| BasePython {
            path,
            version,
            kind: RuntimeKind::Fallback,
        })
}

/// SHA-256 as lowercase hex, for the downloaded tarball and the lockfile stamp.
use crate::commands::sha256::hex_digest as sha256_hex;

/// What a venv was last synced from.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VenvStamp {
    lock_sha256: String,
    base_python: String,
    base_version: String,
}

impl VenvStamp {
    fn render(&self) -> String {
        format!(
            "lock_sha256={}\nbase_python={}\nbase_version={}\n",
            self.lock_sha256, self.base_python, self.base_version
        )
    }

    fn parse(s: &str) -> Option<VenvStamp> {
        let mut lock_sha256 = None;
        let mut base_python = None;
        let mut base_version = None;
        for line in s.lines() {
            match line.split_once('=') {
                Some(("lock_sha256", v)) => lock_sha256 = Some(v.trim().to_string()),
                Some(("base_python", v)) => base_python = Some(v.trim().to_string()),
                Some(("base_version", v)) => base_version = Some(v.trim().to_string()),
                _ => {}
            }
        }
        Some(VenvStamp {
            lock_sha256: lock_sha256.filter(|s| !s.is_empty())?,
            base_python: base_python.filter(|s| !s.is_empty())?,
            base_version: base_version.filter(|s| !s.is_empty())?,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum VenvAction {
    /// Venv matches the base interpreter and the bundled lockfile.
    UpToDate,
    /// Same interpreter, different lockfile (app update): pip install -r core.txt.
    Sync,
    /// Missing/broken venv, or a different base interpreter: recreate + install.
    Rebuild,
}

fn minor_version(version: &str) -> Option<(u32, u32)> {
    parse_python_version(version).map(|(major, minor, _)| (major, minor))
}

/// Decide what the venv needs. Patch updates of the managed build (same path,
/// same 3.12) keep the venv: its symlinks point at the replaced interpreter.
fn plan_venv(
    stamp: Option<&VenvStamp>,
    venv_python_runs: bool,
    base: &BasePython,
    lock_sha256: &str,
) -> VenvAction {
    let Some(stamp) = stamp else {
        return VenvAction::Rebuild;
    };
    if !venv_python_runs
        || stamp.base_python != base.path
        || minor_version(&stamp.base_version).is_none()
        || minor_version(&stamp.base_version) != minor_version(&base.version)
    {
        return VenvAction::Rebuild;
    }
    if stamp.lock_sha256 != lock_sha256 {
        return VenvAction::Sync;
    }
    VenvAction::UpToDate
}

/// Last `max_lines` lines of a command's combined output, for error details.
fn output_tail(output: &std::process::Output, max_lines: usize) -> String {
    let text = format!(
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let lines: Vec<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
    lines[lines.len().saturating_sub(max_lines)..].join("\n")
}

/// Download the pinned standalone CPython, check its SHA-256, and swap it into
/// `bundled_python_dir()` (replacing an older managed build, if any).
fn try_download_standalone() -> Result<(), String> {
    let asset = standalone_asset(std::env::consts::ARCH)
        .ok_or_else(|| format!("no Python build for {}", std::env::consts::ARCH))?;
    log::info!("[Python] Downloading standalone Python: {}", asset.filename);

    // Next to python/ so the final rename stays on one volume.
    let work = crate::paths::shared_support_dir().join("python-download");
    let _ = std::fs::remove_dir_all(&work);
    std::fs::create_dir_all(&work).map_err(|e| format!("create {}: {e}", work.display()))?;
    let result = (|| {
        let tar_path = work.join(&asset.filename);
        let dl = Command::new("curl")
            .args([
                "-fsSL",
                "--retry",
                "2",
                "--connect-timeout",
                "15",
                "--max-time",
                "900",
                "-o",
            ])
            .arg(&tar_path)
            .arg(&asset.url)
            .output()
            .map_err(|e| format!("curl: {e}"))?;
        if !dl.status.success() {
            return Err(format!("download failed: {}", output_tail(&dl, 5)));
        }

        let data = std::fs::read(&tar_path).map_err(|e| format!("read download: {e}"))?;
        let actual = sha256_hex(&data);
        drop(data);
        if actual != asset.sha256 {
            return Err(format!(
                "checksum mismatch: expected {}, got {actual}. Discarding download.",
                asset.sha256
            ));
        }
        log::info!("[Python] Checksum verified ({})", &actual[..16]);

        let staged = work.join("python");
        std::fs::create_dir_all(&staged).map_err(|e| format!("create staging dir: {e}"))?;
        let extract = Command::new("tar")
            .arg("-xzf")
            .arg(&tar_path)
            .args(["--strip-components=1", "-C"])
            .arg(&staged)
            .output()
            .map_err(|e| format!("tar: {e}"))?;
        if !extract.status.success() {
            return Err(format!("extract failed: {}", output_tail(&extract, 5)));
        }
        let staged_bin = staged.join("bin/python3");
        let expected = format!("Python {STANDALONE_PY_VERSION}");
        match probe_python(&staged_bin.to_string_lossy(), "/usr/bin:/bin") {
            Some((_, v)) if v == expected => {}
            other => {
                return Err(format!(
                    "extracted interpreter reports {:?}, expected {expected}",
                    other.map(|(_, v)| v)
                ))
            }
        }

        // Swap in. The venv's symlinks point into this path, so it's replaced
        // in place rather than versioned.
        let dest = bundled_python_dir();
        let old = work.join("python.old");
        if dest.exists() {
            std::fs::rename(&dest, &old).map_err(|e| format!("move old runtime aside: {e}"))?;
        }
        if let Err(e) = std::fs::rename(&staged, &dest) {
            if old.exists() {
                let _ = std::fs::rename(&old, &dest);
            }
            return Err(format!("install runtime: {e}"));
        }
        log::info!(
            "[Python] Standalone installed: {expected} at {}",
            dest.display()
        );
        Ok(())
    })();
    let _ = std::fs::remove_dir_all(&work);
    result
}

/// Pick the interpreter to build the venv on: the managed build (downloading
/// or updating it first if needed), falling back to a Homebrew/python.org 3.12.
/// The bool is true when the managed runtime was just downloaded (its binaries
/// are new to Gatekeeper/XProtect, so the first start is slow).
fn ensure_base_python(status: &dyn Fn(&str), full_path: &str) -> Option<(BasePython, bool)> {
    let managed_bin = bundled_python_dir().join("bin/python3");
    let pinned = format!("Python {STANDALONE_PY_VERSION}");
    let mut managed = probe_python(&managed_bin.to_string_lossy(), full_path);
    let mut downloaded = false;

    if managed.as_ref().map(|(_, v)| v != &pinned).unwrap_or(true) {
        status(&format!("Downloading Python {STANDALONE_PY_VERSION}..."));
        match try_download_standalone() {
            Ok(()) => {
                downloaded = true;
                managed = probe_python(&managed_bin.to_string_lossy(), full_path);
            }
            Err(e) => {
                log::error!("[Python] Managed runtime download failed: {e}");
                if let Some((_, ref v)) = managed {
                    log::warn!("[Python] Keeping the existing managed runtime ({v})");
                }
            }
        }
    }

    let fallbacks = if managed.as_ref().is_some_and(|(_, v)| is_supported_python(v)) {
        vec![]
    } else {
        PYTHON_CANDIDATES
            .iter()
            .filter_map(|p| probe_python(p, full_path))
            .collect()
    };
    let base = choose_base_python(managed, fallbacks)?;
    match base.kind {
        RuntimeKind::Managed => {
            log::info!(
                "[Python] Runtime: managed {} at {}",
                base.version,
                base.path
            )
        }
        RuntimeKind::Fallback => {
            log::warn!(
                "[Python] !!! Runtime: FALLBACK {} at {}. The managed Python {STANDALONE_PY_VERSION} \
                 couldn't be downloaded. !!!",
                base.version,
                base.path
            );
            status(&format!(
                "Couldn't download Python {STANDALONE_PY_VERSION}, using {}",
                base.version
            ));
        }
    }
    Some((base, downloaded))
}

/// Write the embedded lockfiles to Application Support (only when changed).
fn write_lockfiles() -> Result<PathBuf, String> {
    let dir = requirements_dir();
    std::fs::create_dir_all(&dir).map_err(|e| format!("create {}: {e}", dir.display()))?;
    for (name, content) in LOCKFILES {
        let path = dir.join(name);
        if std::fs::read_to_string(&path).ok().as_deref() == Some(*content) {
            continue;
        }
        let tmp = dir.join(format!(".{name}.tmp"));
        std::fs::write(&tmp, content)
            .and_then(|_| std::fs::rename(&tmp, &path))
            .map_err(|e| format!("write {}: {e}", path.display()))?;
    }
    Ok(dir)
}

/// Where Python keeps bytecode caches (PYTHONPYCACHEPREFIX): never inside the
/// signed app bundle.
pub(crate) fn pycache_dir() -> PathBuf {
    crate::paths::app_support_dir().join("pycache")
}

/// A command running a venv/base interpreter with a clean Python environment.
fn python_command(python: &Path, full_path: &str) -> Command {
    let mut cmd = Command::new(python);
    cmd.env("PATH", full_path)
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONPYCACHEPREFIX", pycache_dir())
        .env("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .env_remove("PIP_USER")
        .env_remove("PIP_REQUIRE_HASHES");
    cmd
}

fn core_imports_ok(venv_python: &Path, full_path: &str) -> Result<(), String> {
    let out = python_command(venv_python, full_path)
        .args(["-c", CORE_IMPORT_CHECK])
        .output()
        .map_err(|e| format!("run {}: {e}", venv_python.display()))?;
    if out.status.success() {
        Ok(())
    } else {
        Err(output_tail(&out, 15))
    }
}

/// The app venv, ready to run the backend.
#[derive(Debug)]
struct ReadyVenv {
    /// The venv's interpreter.
    python: String,
    /// Its `--version` output.
    version: String,
    /// True when packages were just installed (rebuild or re-sync): the first
    /// start then runs freshly written binaries, which XProtect scans.
    fresh: bool,
}

/// Make sure the app venv exists on `base` and matches the bundled core.txt.
fn ensure_venv(
    status: &dyn Fn(&str),
    base: &BasePython,
    full_path: &str,
) -> Result<ReadyVenv, String> {
    let req_dir = write_lockfiles()?;
    let lock_sha = sha256_hex(CORE_LOCK.as_bytes());
    let venv = venv_dir();
    let venv_python = venv.join("bin/python3");
    let stamp_path = venv.join(VENV_STAMP_FILE);
    let stamp = std::fs::read_to_string(&stamp_path)
        .ok()
        .and_then(|s| VenvStamp::parse(&s));
    let venv_probe = probe_python(&venv_python.to_string_lossy(), full_path);

    let mut action = plan_venv(stamp.as_ref(), venv_probe.is_some(), base, &lock_sha);
    log::info!("[Python] venv {}: {action:?}", venv.display());
    if action == VenvAction::UpToDate {
        match core_imports_ok(&venv_python, full_path) {
            Ok(()) => {
                let (python, version) = venv_probe.expect("probed above");
                return Ok(ReadyVenv {
                    python,
                    version,
                    fresh: false,
                });
            }
            Err(e) => {
                log::warn!("[Python] venv is up to date but core imports fail, re-syncing:\n{e}");
                action = VenvAction::Sync;
            }
        }
    }

    if action == VenvAction::Rebuild {
        status("Setting up Python environment...");
        let _ = std::fs::remove_dir_all(&venv);
        let out = python_command(Path::new(&base.path), full_path)
            .args(["-m", "venv"])
            .arg(&venv)
            .output()
            .map_err(|e| format!("run {} -m venv: {e}", base.path))?;
        if !out.status.success() {
            return Err(format!(
                "Couldn't create the Python environment with {}:\n{}",
                base.path,
                output_tail(&out, 15)
            ));
        }
        status("Installing dependencies (first launch can take a minute)...");
    } else {
        status("Updating dependencies...");
    }

    let started = std::time::Instant::now();
    let out = python_command(&venv_python, full_path)
        .args([
            "-m",
            "pip",
            "install",
            "--no-input",
            "--require-hashes",
            "--only-binary",
            ":all:",
            "-r",
        ])
        .arg(req_dir.join("core.txt"))
        .output()
        .map_err(|e| format!("run pip: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "pip install -r core.txt failed (Python {}):\n{}",
            base.version,
            output_tail(&out, 20)
        ));
    }
    log::info!(
        "[Python] Core dependencies synced in {:.1}s",
        started.elapsed().as_secs_f32()
    );
    core_imports_ok(&venv_python, full_path)
        .map_err(|e| format!("Dependencies installed but don't import:\n{e}"))?;

    let new_stamp = VenvStamp {
        lock_sha256: lock_sha,
        base_python: base.path.clone(),
        base_version: base.version.clone(),
    };
    let tmp = venv.join(format!("{VENV_STAMP_FILE}.tmp"));
    if let Err(e) =
        std::fs::write(&tmp, new_stamp.render()).and_then(|_| std::fs::rename(&tmp, &stamp_path))
    {
        // Not fatal: the next launch just re-syncs (a no-op for pip).
        log::warn!("[Python] Couldn't write venv stamp: {e}");
    }
    let (python, version) = probe_python(&venv_python.to_string_lossy(), full_path)
        .ok_or_else(|| format!("{} doesn't run after setup", venv_python.display()))?;
    Ok(ReadyVenv {
        python,
        version,
        fresh: true,
    })
}

/// How long the backend gets to answer `/health` before the splash shows
/// SERVER_TIMEOUT. The monitor keeps polling after that, so a late start still
/// becomes ready and clears the error by itself.
const READY_BUDGET: Duration = Duration::from_secs(30);
/// Budget when the runtime was just downloaded or the venv just (re)synced: the
/// first exec of freshly written binaries waits on XProtect, and the first
/// imports of new packages write their .pyc files.
const READY_BUDGET_FRESH: Duration = Duration::from_secs(120);
/// /health poll interval while not ready; crash-check interval once ready.
const POLL_NOT_READY: Duration = Duration::from_millis(500);
const POLL_READY: Duration = Duration::from_secs(5);

fn ready_budget(fresh: bool) -> Duration {
    if fresh {
        READY_BUDGET_FRESH
    } else {
        READY_BUDGET
    }
}

/// True for an HTTP 200 response whose body says "ok" (`{"status": "ok"}`).
fn is_health_response_ok(response: &str) -> bool {
    let status_200 = response.lines().next().is_some_and(|line| {
        line.starts_with("HTTP/") && line.split_whitespace().nth(1) == Some("200")
    });
    status_200 && response.contains("ok")
}

/// One `GET /health` against the backend's current URL. The URL is re-read each
/// time so it follows PLAUD_PORT_BOUND if Python had to walk past an occupied
/// port. Bounded: 1 s to connect, 2 s to answer.
fn health_ok() -> bool {
    let addr_str = {
        let server = get_server().lock().unwrap_or_else(|e| e.into_inner());
        server
            .url
            .trim_start_matches("http://")
            .trim_start_matches("https://")
            .to_string()
    };
    let Ok(addr) = addr_str.parse::<std::net::SocketAddr>() else {
        return false;
    };
    let Ok(mut stream) = std::net::TcpStream::connect_timeout(&addr, Duration::from_secs(1)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
    let request = format!("GET /health HTTP/1.0\r\nHost: {addr_str}\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    let _ = stream.read_to_string(&mut response);
    is_health_response_ok(&response)
}

/// The backend answered /health: mark it ready and drop any startup error (a
/// SERVER_TIMEOUT from a slow first start, or a crash it recovered from), so
/// `get_startup_status` and `get_api_info` both report ready.
fn mark_server_ready() {
    {
        let mut server = get_server().lock().unwrap_or_else(|e| e.into_inner());
        if let Some(err) = server.startup_error.take() {
            log::info!("[Python] Backend is healthy; clearing startup error {}", err.code);
        }
    }
    SERVER_READY.store(true, Ordering::Release);
}

/// For Retry: true (and marked ready) if the backend process this app started
/// is alive and answering /health right now, so it must not be restarted.
pub fn confirm_ready_now() -> bool {
    if SERVER_READY.load(Ordering::Acquire) {
        return true;
    }
    let alive = {
        let mut server = get_server().lock().unwrap_or_else(|e| e.into_inner());
        match server.process.as_mut() {
            Some(child) => matches!(child.try_wait(), Ok(None)),
            None => false,
        }
    };
    if alive && health_ok() {
        mark_server_ready();
        return true;
    }
    false
}

/// Called when the app starts quitting, before it looks for a backend to stop:
/// stops new spawns and waits (briefly) for one in flight to finish. That spawn
/// either registered its process before seeing the flag (so
/// `stop_python_server` finds it) or saw the flag and stopped the process
/// itself (see `spawn_and_register`). Bounded at 3 s: a self-stop takes at most
/// ~1 s of SIGTERM grace plus SIGKILL.
pub fn begin_shutdown() {
    SHUTTING_DOWN.store(true, Ordering::SeqCst);
    let deadline = Instant::now() + Duration::from_secs(3);
    while SPAWNING.load(Ordering::SeqCst) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// Parse a stdout line for the `PLAUD_PORT_BOUND: <port>` marker emitted by
/// api_server.py just before serve_forever. Returns the port if matched.
fn parse_port_bound(line: &str) -> Option<u16> {
    line.strip_prefix("PLAUD_PORT_BOUND: ")
        .and_then(|rest| rest.trim().parse::<u16>().ok())
}

/// Send BLE disconnect request to the Python backend (fire-and-forget)
/// Ask the backend to disconnect the NotePin and wait (up to
/// BLE_DISCONNECT_WAIT) for its answer: /device/disconnect only answers once
/// the teardown is done, and at once when nothing is connected. (It used to
/// fire and forget, and the caller slept 2 s every time.)
pub fn send_ble_disconnect() {
    let server = get_server().lock().unwrap();
    let port = server.port;
    let token = server.api_token.clone();
    drop(server);
    if let Ok(mut s) = std::net::TcpStream::connect_timeout(
        &format!("127.0.0.1:{port}")
            .parse()
            .unwrap_or_else(|_| std::net::SocketAddr::from(([127, 0, 0, 1], 5123))),
        Duration::from_secs(2),
    ) {
        if write!(
            s,
            "POST /device/disconnect HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n{API_TOKEN_HEADER}: {token}\r\nContent-Length: 0\r\n\r\n"
        )
        .is_ok()
        {
            let _ = s.set_read_timeout(Some(BLE_DISCONNECT_WAIT));
            let mut sink = Vec::new();
            let _ = std::io::Read::read_to_end(&mut s, &mut sink);
        }
    }
}

/// Longest wait for the backend's answer to /device/disconnect on quit.
const BLE_DISCONNECT_WAIT: Duration = Duration::from_millis(2500);

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

    // App support bin/ — where FFmpeg gets downloaded on first use (shared by
    // dev and release builds; app/utils/system.py hard-codes it)
    let app_support_bin = crate::paths::shared_support_dir().join("bin");

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

fn pid_file_path() -> std::path::PathBuf {
    app_support_dir().join("server.pid")
}

fn write_pid_file(pid: u32) {
    let path = pid_file_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = std::fs::write(&path, pid.to_string());
}

fn delete_pid_file() {
    let _ = std::fs::remove_file(pid_file_path());
}

/// Kill any orphaned Python server from a previous run using the PID file.
///
/// Only kills the exact PID that was written at spawn time, and only after
/// confirming the process is still alive and is a Python process — avoiding
/// false-positive kills of unrelated tools (Jupyter, ML servers, etc.).
fn kill_orphan_python_servers() {
    let path = pid_file_path();
    let pid_str = match std::fs::read_to_string(&path) {
        Ok(s) => s.trim().to_string(),
        Err(_) => return,
    };
    let pid: u32 = match pid_str.parse() {
        Ok(p) => p,
        Err(_) => {
            delete_pid_file();
            return;
        }
    };

    // Check the process is still alive (signal 0 = existence check, no signal sent)
    #[cfg(unix)]
    let is_alive = unsafe { libc::kill(pid as libc::pid_t, 0) == 0 };
    #[cfg(not(unix))]
    let is_alive = false;

    if !is_alive {
        delete_pid_file();
        return;
    }

    // Confirm it is a Python process before killing
    let is_python = Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "comm="])
        .output()
        .ok()
        .and_then(|out| String::from_utf8(out.stdout).ok())
        .map(|s| s.to_lowercase().contains("python"))
        .unwrap_or(false);

    if is_python {
        log::info!("[Python] Killing orphan from previous run: PID {pid}");
        #[cfg(unix)]
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGKILL);
        }
    }
    delete_pid_file();
}

fn emit_startup_error(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let err = get_server()
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .startup_error
            .clone();
        if let Some(e) = err {
            let _ = w.emit("startup-error", e);
        }
    }
}

/// Start the Python Flask API server
pub fn start_python_server(app: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    // Clean up any orphaned Python servers from a previous crash
    kill_orphan_python_servers();

    if std::env::consts::ARCH != SUPPORTED_ARCH {
        let detail = format!(
            "This copy of Open Pin runs as {}; its Python packages are built for Apple Silicon (arm64) only.",
            std::env::consts::ARCH
        );
        log::error!("[Python] {detail}");
        set_startup_error(
            "UNSUPPORTED_MAC",
            "Intel Macs aren't supported",
            "This build of Open Pin needs a Mac with Apple Silicon (M1 or later); Intel Macs aren't supported.",
            "On an Apple Silicon Mac, make sure Open Pin isn't set to open using Rosetta \
             (Finder › Get Info).",
            &detail,
        );
        return Err(detail.into());
    }

    let full_path = build_path(app);
    let status = |msg: &str| emit_startup_status(app, msg);
    // Find (or download) the base interpreter
    emit_startup_status(app, "Looking for Python...");
    let Some((base, downloaded)) = ensure_base_python(&status, &full_path) else {
        let detail = format!(
            "Couldn't download Python {STANDALONE_PY_VERSION}, and no Python {}.{} was found at: {}",
            REQUIRED_PYTHON.0,
            REQUIRED_PYTHON.1,
            PYTHON_CANDIDATES.join(", ")
        );
        log::error!("[Python] {detail}");
        set_startup_error(
            "PYTHON_NOT_FOUND",
            "Python 3.12 not available",
            "Open Pin needs Python 3.12; couldn't download it.",
            "Check your internet connection and click Retry. Or install Python 3.12 \
             (python.org, or `brew install python@3.12`) and click Retry.",
            &detail,
        );
        return Err(detail.into());
    };
    emit_startup_status(app, &format!("Found {}", base.version));
    get_server().lock().unwrap().runtime = base.kind.as_str().to_string();

    // App venv with the pinned core packages
    emit_startup_status(app, "Checking dependencies...");
    let venv = match ensure_venv(&status, &base, &full_path) {
        Ok(v) => v,
        Err(e) => {
            log::error!(
                "[Python] Dependency setup failed on {} {} ({}): {e}",
                base.kind.as_str(),
                base.version,
                base.path
            );
            let hint = match base.kind {
                RuntimeKind::Managed => "Check your internet connection and click Retry. If it keeps \
                     failing, delete ~/Library/Application Support/com.plaud.v2/venv and click Retry."
                    .to_string(),
                RuntimeKind::Fallback => format!(
                    "Open Pin needs Python 3.12; couldn't download it, so it used {} at {}. \
                     Check your internet connection and click Retry.",
                    base.version, base.path
                ),
            };
            set_startup_error(
                "DEPS_FAILED",
                "Missing dependencies",
                "Open Pin couldn't install the Python packages it needs.",
                &hint,
                &e,
            );
            return Err(e.into());
        }
    };

    // Store Python info (the venv interpreter, which restarts reuse)
    let api_token = {
        let mut server = get_server().lock().unwrap();
        server.python_path = venv.python.clone();
        server.python_version = venv.version.clone();
        server.api_token.clone()
    };

    // Find free port
    let port = find_free_port(5123).map_err(|e| {
        set_startup_error(
            "NO_FREE_PORT",
            "No available port",
            "All ports in the range 5123–5222 are already in use.",
            "Close other applications that may be using these ports, then click Retry.",
            &e,
        );
        e
    })?;
    // Set before the spawn: the stdout reader updates it from PLAUD_PORT_BOUND.
    {
        let mut server = get_server().lock().unwrap();
        server.port = port;
        server.url = format!("http://127.0.0.1:{port}");
    }

    emit_startup_status(app, "Starting server...");
    let generation = match spawn_and_register(app, &venv.python, port, &api_token) {
        Ok(g) => g,
        Err(SpawnError::Quitting) => return Err(QUITTING.into()),
        Err(SpawnError::Io(e)) => {
            set_startup_error(
                "SPAWN_FAILED",
                "Couldn't start Python",
                "The system couldn't launch the Python process.",
                "Click Retry. If it keeps failing, delete \
                 ~/Library/Application Support/com.plaud.v2/venv and click Retry.",
                &e.to_string(),
            );
            return Err(format!("Failed to spawn Python: {e}").into());
        }
    };

    // Wait for /health, then watch for crashes, in the background.
    let budget = ready_budget(downloaded || venv.fresh);
    log::info!(
        "[Python] Backend spawned; allowing {}s for /health{}",
        budget.as_secs(),
        if downloaded || venv.fresh {
            " (fresh runtime/venv)"
        } else {
            ""
        }
    );
    let app_handle = app.clone();
    std::thread::Builder::new()
        .name("backend-monitor".into())
        .spawn(move || monitor_backend(app_handle, generation, budget))?;
    Ok(())
}

/// Wait for the backend to answer /health, then watch it for crashes and
/// restart it (up to 3 times). Runs until the backend stops, is replaced by a
/// newer one (Retry), or can't be restarted.
///
/// Missing the `budget` sets SERVER_TIMEOUT so the splash can say so, but
/// polling continues: if /health answers later, the server is marked ready and
/// the error cleared.
fn monitor_backend(app: AppHandle, mut generation: u64, budget: Duration) {
    const MAX_RESTARTS: u32 = 3;
    let mut restart_count = 0;
    let mut waiting_since = Instant::now();
    let mut timeout_reported = false;

    enum LoopAction {
        StillRunning,
        CleanExit,
        Crashed(i32),
        NoProcess,
        Superseded,
        WaitError(String),
    }

    loop {
        // Inspect the process under the lock, then release it before probing
        // or sleeping (stop_python_server needs it on quit).
        let action = {
            let mut server = get_server().lock().unwrap_or_else(|e| e.into_inner());
            if server.generation != generation {
                LoopAction::Superseded
            } else if let Some(ref mut child) = server.process {
                match child.try_wait() {
                    Ok(Some(status)) => {
                        let code = status.code().unwrap_or(-1);
                        server.process = None;
                        if code != 0 {
                            LoopAction::Crashed(code)
                        } else {
                            LoopAction::CleanExit
                        }
                    }
                    Ok(None) => LoopAction::StillRunning,
                    Err(e) => LoopAction::WaitError(e.to_string()),
                }
            } else {
                LoopAction::NoProcess
            }
        };

        match action {
            LoopAction::StillRunning => {
                if !SERVER_READY.load(Ordering::Acquire) {
                    let waited = waiting_since.elapsed();
                    if health_ok() {
                        mark_server_ready();
                        if timeout_reported {
                            log::warn!(
                                "[Python] Backend became ready after {:.0}s, past the {}s budget",
                                waited.as_secs_f32(),
                                budget.as_secs()
                            );
                        } else {
                            log::info!(
                                "[Python] Backend ready after {:.1}s",
                                waited.as_secs_f32()
                            );
                        }
                    } else if !timeout_reported && waited >= budget {
                        timeout_reported = true;
                        log::error!(
                            "[Python] No answer from /health within {}s; still watching",
                            budget.as_secs()
                        );
                        set_startup_error(
                            "SERVER_TIMEOUT",
                            "Server not responding",
                            "The Python backend started but hasn't answered yet.",
                            "Open Pin keeps waiting and carries on by itself if it comes up. \
                             Retry restarts it.",
                            &format!("No answer from /health within {}s", budget.as_secs()),
                        );
                        emit_startup_error(&app);
                    }
                }
            }
            LoopAction::CleanExit | LoopAction::NoProcess => break,
            LoopAction::Superseded => {
                log::info!("[Python] Backend replaced; monitor for generation {generation} exits");
                break;
            }
            LoopAction::WaitError(err) => {
                // Silent break here would leave the user with a stale "ready"
                // status forever. Log it and surface to the renderer so the
                // user knows the watchdog has stopped monitoring.
                log::error!("[Python] Watchdog try_wait failed: {err} — stopping crash monitor");
                SERVER_READY.store(false, Ordering::Release);
                set_startup_error(
                    "WATCHDOG_DIED",
                    "Process monitor stopped",
                    "Open Pin's crash watchdog could not poll the backend process.",
                    "Restart the app to recover.",
                    &format!("try_wait error: {err}"),
                );
                emit_startup_error(&app);
                break;
            }
            LoopAction::Crashed(code) => {
                log::error!("[Python] Server crashed with exit code {code}");
                // Mark not-ready immediately so get_api_info returns "starting"
                // during the restart window rather than stale "ready".
                SERVER_READY.store(false, Ordering::Release);
                delete_pid_file();
                if SHUTTING_DOWN.load(Ordering::SeqCst) {
                    break;
                }
                restart_count += 1;
                if restart_count > MAX_RESTARTS {
                    log::error!("[Python] Max restarts ({MAX_RESTARTS}) exceeded");
                    set_startup_error(
                        "SERVER_CRASH",
                        "Server keeps crashing",
                        "The Python backend crashed repeatedly.",
                        "Click Retry. If this keeps happening, open the diagnostics log.",
                        &format!("Exit code {code}, {restart_count} restarts attempted"),
                    );
                    emit_startup_error(&app);
                    break;
                }
                log::info!("[Python] Auto-restarting (attempt {restart_count}/{MAX_RESTARTS})...");
                match restart_python_server(&app) {
                    Ok(g) => {
                        generation = g;
                        waiting_since = Instant::now();
                        timeout_reported = false;
                        // Start watching the fresh child immediately.
                        continue;
                    }
                    Err(e) => {
                        if SHUTTING_DOWN.load(Ordering::SeqCst) {
                            break;
                        }
                        log::error!("[Python] Restart failed: {e}");
                        set_startup_error(
                            "SERVER_CRASH",
                            "Server crashed",
                            "The Python backend crashed and could not be restarted.",
                            "Click Retry.",
                            &format!("Exit code {code}, restart failed: {e}"),
                        );
                        emit_startup_error(&app);
                        break;
                    }
                }
            }
        }

        std::thread::sleep(if SERVER_READY.load(Ordering::Acquire) {
            POLL_READY
        } else {
            POLL_NOT_READY
        });
    }
}

enum SpawnError {
    /// The app started quitting; nothing is left running.
    Quitting,
    Io(std::io::Error),
}

/// Spawn api_server.py on the port already stored in the server state and
/// register it as the current backend (PID file, output readers, global
/// state). Returns the new process generation.
///
/// Quitting can race with this. Shutdown sets SHUTTING_DOWN, then waits for
/// SPAWNING to clear before looking for a process to stop; this function checks
/// SHUTTING_DOWN before spawning and again after registering. So either
/// shutdown finds the registered process, or this function stops it itself.
fn spawn_and_register(
    app: &AppHandle,
    python_path: &str,
    port: u16,
    api_token: &str,
) -> Result<u64, SpawnError> {
    SPAWNING.store(true, Ordering::SeqCst);
    let result = (|| {
        if SHUTTING_DOWN.load(Ordering::SeqCst) {
            return Err(SpawnError::Quitting);
        }
        let full_path = build_path(app);
        let server_script = find_server_script(app);
        let cwd = script_cwd(&server_script);
        log::info!(
            "[Python] Spawning {} (cwd {}, port {port})",
            server_script.display(),
            cwd.display()
        );
        let mut child = spawn_backend(
            python_path,
            &server_script,
            &cwd,
            &full_path,
            port,
            api_token,
        )
        .map_err(SpawnError::Io)?;
        // Record PID so the next launch can clean up if we crash before stop_python_server runs
        write_pid_file(child.id());
        attach_output_readers(&mut child);
        let mut server = get_server().lock().unwrap_or_else(|e| e.into_inner());
        server.process = Some(child);
        server.generation += 1;
        Ok(server.generation)
    })();
    // SPAWNING stays set until a self-stop below has finished, so shutdown
    // (begin_shutdown waits for it) doesn't exit the app halfway through it.
    let result = result.and_then(|generation| {
        if SHUTTING_DOWN.load(Ordering::SeqCst) {
            log::info!("[Python] Quit requested during startup; stopping the backend just spawned");
            terminate_backend(Duration::from_secs(1));
            Err(SpawnError::Quitting)
        } else {
            Ok(generation)
        }
    });
    SPAWNING.store(false, Ordering::SeqCst);
    result
}

/// Forward the backend's stdout/stderr to the log (target `python`, so it lands
/// in app.log next to the shell's own messages). stdout is also watched for the
/// `PLAUD_PORT_BOUND` marker so we follow Python if it had to walk past an
/// occupied port (closes the find_free_port TOCTOU window).
fn attach_output_readers(child: &mut Child) {
    if let Some(stdout) = child.stdout.take() {
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines().map_while(Result::ok) {
                if let Some(actual_port) = parse_port_bound(&line) {
                    let mut server = get_server().lock().unwrap_or_else(|e| e.into_inner());
                    server.port = actual_port;
                    server.url = format!("http://127.0.0.1:{actual_port}");
                }
                log::info!(target: "python", "{line}");
            }
        });
    }
    if let Some(stderr) = child.stderr.take() {
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().map_while(Result::ok) {
                log::info!(target: "python", "{line}");
            }
        });
    }
}

/// Spawn api_server.py with the venv interpreter (first launch and restarts).
fn spawn_backend(
    python_path: &str,
    server_script: &Path,
    cwd: &Path,
    full_path: &str,
    port: u16,
    api_token: &str,
) -> std::io::Result<Child> {
    let runtime = get_server().lock().unwrap().runtime.clone();
    Command::new(python_path)
        .arg(server_script)
        .current_dir(cwd)
        .env("PATH", full_path)
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONNOUSERSITE", "1")
        // Bytecode caches go outside the app bundle: .pyc files written into
        // Contents/Resources break the bundle's code signature after the first
        // launch (codesign --verify / spctl fail on the installed app).
        .env("PYTHONPYCACHEPREFIX", pycache_dir())
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .env("PLAUD_API_PORT", port.to_string())
        .env("PLAUD_API_TOKEN", api_token)
        // Lockfiles for ensure_installed(extra=...), and which runtime this is.
        .env("PLAUD_REQUIREMENTS_DIR", requirements_dir())
        .env("PLAUD_PYTHON_RUNTIME", runtime)
        // /health reports it (the plaud-v2 package itself isn't installed in the venv).
        .env("PLAUD_APP_VERSION", env!("CARGO_PKG_VERSION"))
        // Always explicit, so the backend and the Rust side (the bug
        // report's wifi_sync.log) agree: PLAUD_DATA_DIR if the shell got one, else
        // "Plaud V2 (Dev)" for debug builds and "Plaud V2" for release.
        .env(crate::paths::DATA_DIR_ENV, crate::paths::data_dir())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
}

/// Return the working directory to use when spawning the Python server.
///
/// Tries two levels up from the script path (the repo/bundle root), falling
/// back to one level up, and ultimately to `"."` if the script has no parent
/// at all.  This avoids the nested `.unwrap()` that would panic when the path
/// has no parent component.
fn script_cwd(script: &Path) -> PathBuf {
    script
        .parent()
        .and_then(|p| p.parent())
        .or_else(|| script.parent())
        .unwrap_or(Path::new("."))
        .to_path_buf()
}

fn find_server_script(app: &AppHandle) -> PathBuf {
    // Check bundled Resources FIRST — the CARGO_MANIFEST_DIR dev path is baked at
    // compile time and will still exist on the dev machine even in production builds.
    // Debug builds find a copy here too (tauri copies the resources into
    // target/debug/), so dev runs that copy, not the repo's app/. Which data
    // folder it uses doesn't depend on this: spawn_backend passes PLAUD_DATA_DIR.
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

/// Re-spawn the Python server after a crash (reuses the cached interpreter,
/// port and token). Returns the new generation; the caller's monitor loop waits
/// for /health.
fn restart_python_server(app: &AppHandle) -> Result<u64, String> {
    let (python_path, port, api_token) = {
        let server = get_server().lock().unwrap_or_else(|e| e.into_inner());
        // Same token as the first launch: the webview cached it from get_api_info.
        (
            server.python_path.clone(),
            server.port,
            server.api_token.clone(),
        )
    };
    if python_path.is_empty() {
        return Err("No Python path cached".to_string());
    }
    let generation = spawn_and_register(app, &python_path, port, &api_token).map_err(|e| match e {
        SpawnError::Quitting => QUITTING.to_string(),
        SpawnError::Io(e) => format!("Spawn failed: {e}"),
    })?;
    get_server()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .startup_error = None;
    log::info!("[Python] Backend respawned on port {port}; waiting for /health");
    Ok(generation)
}

/// SIGTERM the registered backend, SIGKILL it if it hasn't exited after
/// `grace`, and forget it (PID file included). The server lock is released
/// while waiting.
fn terminate_backend(grace: Duration) {
    let Some(server_lock) = PYTHON_SERVER.get() else {
        return;
    };
    let child = server_lock
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .process
        .take();
    SERVER_READY.store(false, Ordering::Release);
    if let Some(mut child) = child {
        let pid = child.id();
        log::info!("[Python] Stopping backend (pid {pid})...");
        #[cfg(unix)]
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGTERM);
        }
        let start = Instant::now();
        let mut exited = false;
        while start.elapsed() < grace {
            if child.try_wait().ok().flatten().is_some() {
                exited = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        if exited {
            log::info!("[Python] Backend exited gracefully");
        } else {
            log::warn!("[Python] Backend did not exit within {grace:?}, sending SIGKILL");
            let _ = child.kill();
            let _ = child.wait();
        }
    }
    delete_pid_file();
}

/// Gracefully stop the Python server — disconnect BLE, SIGTERM, then SIGKILL
pub fn stop_python_server() {
    // Disconnect BLE before killing Python; send_ble_disconnect waits for the
    // teardown (instant when nothing is connected). Only a backend that got
    // as far as answering /health can hold a BLE connection.
    if SERVER_READY.load(Ordering::Acquire) {
        send_ble_disconnect();
    }
    terminate_backend(Duration::from_secs(3));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(v: &str) -> String {
        v.to_string()
    }

    #[test]
    fn sha256_matches_shasum_on_the_core_lockfile() {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../app/requirements/core.txt");
        let out = Command::new("shasum")
            .args(["-a", "256"])
            .arg(&path)
            .output()
            .unwrap();
        let expected = String::from_utf8_lossy(&out.stdout)
            .split_whitespace()
            .next()
            .unwrap()
            .to_string();
        assert_eq!(sha256_hex(CORE_LOCK.as_bytes()), expected);
    }

    #[test]
    fn standalone_asset_is_pinned_312_stripped() {
        let a = standalone_asset("aarch64").expect("Apple Silicon asset");
        assert_eq!(
            a.filename,
            "cpython-3.12.16+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz"
        );
        assert!(a.url.starts_with(
            "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"
        ));
        assert!(a.url.ends_with(&a.filename));
        assert_eq!(a.sha256, STANDALONE_SHA256_AARCH64);
        assert_eq!(a.sha256.len(), 64);
        assert!(a
            .sha256
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
        // core.txt has arm64 wheels only: no Intel runtime to download.
        assert!(standalone_asset("x86_64").is_none());
        assert_eq!(
            parse_python_version(STANDALONE_PY_VERSION).map(|v| (v.0, v.1)),
            Some((3, 12))
        );
    }

    #[test]
    fn managed_runtime_wins_over_fallbacks() {
        let base = choose_base_python(
            Some((s("/app/python/bin/python3"), s("Python 3.12.16"))),
            vec![(s("/opt/homebrew/bin/python3"), s("Python 3.12.1"))],
        )
        .unwrap();
        assert_eq!(base.kind, RuntimeKind::Managed);
        assert_eq!(base.path, "/app/python/bin/python3");
    }

    #[test]
    fn older_managed_build_is_still_used() {
        // A 3.12.9 build from an older app version, when the update download failed.
        let base = choose_base_python(Some((s("/m/python3"), s("Python 3.12.9"))), vec![]).unwrap();
        assert_eq!(base.kind, RuntimeKind::Managed);
    }

    #[test]
    fn fallback_accepts_only_312() {
        // core.txt is cp312 wheels with hashes: any other minor can't install it.
        let fallbacks = vec![
            (s("/opt/homebrew/bin/python3"), s("Python 3.15.0")),
            (s("/usr/bin/python3"), s("Python 3.9.6")),
            (s("/usr/local/bin/python3.13"), s("Python 3.13.2")),
            (s("/usr/local/bin/python3.11"), s("Python 3.11.9")),
            (s("/usr/local/bin/python3.10"), s("Python 3.10.14")),
            (s("/opt/homebrew/bin/python3.12"), s("Python 3.12.8")),
        ];
        let base = choose_base_python(None, fallbacks.clone()).unwrap();
        assert_eq!(base.kind, RuntimeKind::Fallback);
        assert_eq!(base.path, "/opt/homebrew/bin/python3.12");

        // No 3.12 among them: nothing usable.
        assert_eq!(choose_base_python(None, fallbacks[..5].to_vec()), None);
        assert_eq!(choose_base_python(None, vec![]), None);
        // Any 3.12 patch is fine.
        assert!(is_supported_python("Python 3.12.0"));
        assert!(is_supported_python("Python 3.12.16"));
        assert!(!is_supported_python("Python 3.13.0"));
        assert!(!is_supported_python("Python 3.1.2"));
        assert!(!is_supported_python("garbage"));
    }

    #[test]
    fn fallback_candidates_are_never_the_clt_shim() {
        assert!(!PYTHON_CANDIDATES.contains(&"/usr/bin/python3"));
        assert!(!PYTHON_CANDIDATES.contains(&"python3"));
        assert_eq!(PYTHON_CANDIDATES[0], "/opt/homebrew/bin/python3.12");
    }

    #[test]
    fn non_312_managed_falls_back() {
        let base = choose_base_python(
            Some((s("/m/python3"), s("Python 3.13.1"))),
            vec![
                (s("/opt/homebrew/bin/python3.11"), s("Python 3.11.4")),
                (s("/usr/local/bin/python3.12"), s("Python 3.12.4")),
            ],
        )
        .unwrap();
        assert_eq!(base.kind, RuntimeKind::Fallback);
        assert_eq!(base.path, "/usr/local/bin/python3.12");
    }

    #[test]
    fn health_response_parsing() {
        let ok = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\": \"ok\"}";
        assert!(is_health_response_ok(ok));
        assert!(is_health_response_ok(
            "HTTP/1.0 200 OK\r\n\r\n{\"status\":\"ok\",\"version\":\"2.3.0\"}"
        ));
        // 401/500 bodies can contain "ok" too (e.g. "token"): status must be 200.
        assert!(!is_health_response_ok(
            "HTTP/1.1 401 UNAUTHORIZED\r\n\r\n{\"error\": \"bad token\"}"
        ));
        assert!(!is_health_response_ok("HTTP/1.1 500 INTERNAL SERVER ERROR\r\n\r\nok"));
        assert!(!is_health_response_ok(""));
        // A body that merely mentions 200 isn't a 200 status line.
        assert!(!is_health_response_ok("garbage 200 ok"));
    }

    #[test]
    fn ready_budget_is_longer_after_a_fresh_install() {
        assert_eq!(ready_budget(false), Duration::from_secs(30));
        assert!(ready_budget(true) >= Duration::from_secs(120));
    }

    #[test]
    fn fallback_ties_keep_candidate_order() {
        let base = choose_base_python(
            None,
            vec![(s("/a"), s("Python 3.12.1")), (s("/b"), s("Python 3.12.9"))],
        )
        .unwrap();
        assert_eq!(base.path, "/a");
    }

    #[test]
    fn stamp_round_trips_and_rejects_partial() {
        let stamp = VenvStamp {
            lock_sha256: s("abc123"),
            base_python: s("/Users/x/Library/Application Support/com.plaud.v2/python/bin/python3"),
            base_version: s("Python 3.12.16"),
        };
        assert_eq!(VenvStamp::parse(&stamp.render()), Some(stamp));
        assert_eq!(VenvStamp::parse("lock_sha256=abc\nbase_python=/p\n"), None);
        assert_eq!(
            VenvStamp::parse("lock_sha256=\nbase_python=/p\nbase_version=Python 3.12.1\n"),
            None
        );
        assert_eq!(VenvStamp::parse(""), None);
    }

    fn managed(path: &str, version: &str) -> BasePython {
        BasePython {
            path: s(path),
            version: s(version),
            kind: RuntimeKind::Managed,
        }
    }

    #[test]
    fn venv_plan() {
        let base = managed("/m/bin/python3", "Python 3.12.16");
        let stamp = VenvStamp {
            lock_sha256: s("lock1"),
            base_python: s("/m/bin/python3"),
            base_version: s("Python 3.12.16"),
        };
        assert_eq!(
            plan_venv(Some(&stamp), true, &base, "lock1"),
            VenvAction::UpToDate
        );
        // App update changed core.txt
        assert_eq!(
            plan_venv(Some(&stamp), true, &base, "lock2"),
            VenvAction::Sync
        );
        // No venv / never finished a sync / venv interpreter broken
        assert_eq!(plan_venv(None, true, &base, "lock1"), VenvAction::Rebuild);
        assert_eq!(
            plan_venv(Some(&stamp), false, &base, "lock1"),
            VenvAction::Rebuild
        );
        // Managed patch update in place (3.12.9 -> 3.12.16): keep the venv
        let old = VenvStamp {
            base_version: s("Python 3.12.9"),
            ..stamp.clone()
        };
        assert_eq!(
            plan_venv(Some(&old), true, &base, "lock1"),
            VenvAction::UpToDate
        );
        // Built on the fallback last time, managed now available
        let from_brew = VenvStamp {
            base_python: s("/opt/homebrew/bin/python3.12"),
            ..stamp.clone()
        };
        assert_eq!(
            plan_venv(Some(&from_brew), true, &base, "lock1"),
            VenvAction::Rebuild
        );
        // Same path, new minor (Homebrew python3 3.14 -> 3.15)
        let brew = BasePython {
            path: s("/opt/homebrew/bin/python3"),
            version: s("Python 3.15.0"),
            kind: RuntimeKind::Fallback,
        };
        let brew_stamp = VenvStamp {
            base_python: s("/opt/homebrew/bin/python3"),
            base_version: s("Python 3.14.2"),
            lock_sha256: s("lock1"),
        };
        assert_eq!(
            plan_venv(Some(&brew_stamp), true, &brew, "lock1"),
            VenvAction::Rebuild
        );
    }

    #[test]
    fn embedded_lockfiles_are_pinned() {
        assert_eq!(LOCKFILES[0], ("core.txt", CORE_LOCK));
        for (name, content) in LOCKFILES {
            let pins: Vec<&str> = content
                .lines()
                .filter(|l| !l.starts_with(' ') && !l.starts_with('#') && !l.trim().is_empty())
                .collect();
            assert!(!pins.is_empty(), "{name} is empty");
            for line in pins {
                assert!(line.contains("=="), "{name}: unpinned line {line}");
            }
        }
        for pkg in [
            "flask==",
            "bleak==",
            "cryptography==",
            "websockets==",
            "pyobjc-framework-corewlan==",
            "anthropic==2.3.0",
        ] {
            assert!(CORE_LOCK.contains(pkg), "core.txt lacks {pkg}");
        }
    }

    /// End-to-end exercise of the real download + venv + pip path, against a
    /// scratch HOME (never the user's Application Support). Network, ~30 s:
    ///
    /// PLAUD_RUNTIME_EXERCISE_HOME=/tmp/scratch cargo test --lib \
    ///     python::tests::exercise_runtime_setup -- --ignored --nocapture
    #[test]
    #[ignore]
    fn exercise_runtime_setup() {
        let Ok(home) = std::env::var("PLAUD_RUNTIME_EXERCISE_HOME") else {
            eprintln!("PLAUD_RUNTIME_EXERCISE_HOME not set; skipping");
            return;
        };
        let real_home = dirs::home_dir().unwrap();
        assert!(
            !home.is_empty() && Path::new(&home) != real_home,
            "use a scratch HOME"
        );
        std::env::set_var("HOME", &home);
        assert!(app_support_dir().starts_with(&home));

        let full_path = "/usr/bin:/bin:/usr/sbin:/sbin";
        let status = |m: &str| eprintln!("[status] {m}");
        let time = |label: &str, t: std::time::Instant| {
            eprintln!("[time] {label}: {:.1}s", t.elapsed().as_secs_f32())
        };

        let t = std::time::Instant::now();
        let (base, downloaded) = ensure_base_python(&status, full_path).expect("base python");
        time("base python (download + verify + extract)", t);
        assert!(downloaded);
        assert_eq!(base.kind, RuntimeKind::Managed);
        assert_eq!(base.version, format!("Python {STANDALONE_PY_VERSION}"));

        let t = std::time::Instant::now();
        let venv = ensure_venv(&status, &base, full_path).expect("venv");
        time("venv create + pip install core.txt", t);
        assert!(venv.python.starts_with(&home), "{}", venv.python);
        assert_eq!(venv.version, base.version);
        assert!(venv.fresh);

        let t = std::time::Instant::now();
        assert!(!ensure_venv(&status, &base, full_path).expect("warm venv").fresh);
        time("warm launch (stamp + import check)", t);

        // Simulate an app update that changed core.txt.
        let stamp_path = venv_dir().join(VENV_STAMP_FILE);
        let stamp = std::fs::read_to_string(&stamp_path).unwrap();
        std::fs::write(
            &stamp_path,
            stamp.replace("lock_sha256=", "lock_sha256=old"),
        )
        .unwrap();
        let t = std::time::Instant::now();
        assert!(ensure_venv(&status, &base, full_path).expect("re-sync").fresh);
        time("re-sync after lockfile change", t);
        let stamp = VenvStamp::parse(&std::fs::read_to_string(&stamp_path).unwrap()).unwrap();
        assert_eq!(stamp.lock_sha256, sha256_hex(CORE_LOCK.as_bytes()));

        // Second launch with the managed build already there: no download.
        let t = std::time::Instant::now();
        let (again, downloaded) = ensure_base_python(&status, full_path).unwrap();
        time("base python, already installed", t);
        assert_eq!(again, base);
        assert!(!downloaded);

        for d in [bundled_python_dir(), venv_dir()] {
            let out = Command::new("du").arg("-sh").arg(&d).output().unwrap();
            eprintln!("[size] {}", String::from_utf8_lossy(&out.stdout).trim());
        }
    }
}
