const { app, BrowserWindow, ipcMain, shell, nativeTheme, Tray, Menu, powerMonitor, Notification } = require('electron');
const path = require('path');
const { spawn, execSync } = require('child_process');
const http = require('http');
const https = require('https');
const fs = require('fs');

let mainWindow;
let pythonProcess;
let tray = null;
let API_PORT = 5123;
let API_URL = `http://127.0.0.1:${API_PORT}`;
let themePreference = 'auto'; // 'auto' | 'light' | 'dark'
let _startupError = null; // Sent to renderer via IPC

// ---- Custom Auto-Updater (no code signing required) ----
const UPDATE_REPO = 'Obednal97/Plaud-V2';
const UPDATE_TOKEN = 'github_pat_REDACTED';
let _pendingUpdate = null; // { version, zipUrl, zipPath }

function sendUpdateStatus(status, info = {}) {
  if (mainWindow && mainWindow.webContents) {
    mainWindow.webContents.send('update-status', { status, ...info });
  }
}

function githubApi(urlPath) {
  return new Promise((resolve, reject) => {
    const opts = {
      hostname: 'api.github.com',
      path: urlPath,
      headers: {
        'User-Agent': 'Plaud-V2-Updater',
        Accept: 'application/vnd.github+json',
        Authorization: `Bearer ${UPDATE_TOKEN}`,
      },
    };
    https.get(opts, (res) => {
      if (res.statusCode === 302 || res.statusCode === 301) {
        // Follow redirect
        https.get(res.headers.location, (r2) => {
          let body = '';
          r2.on('data', (c) => (body += c));
          r2.on('end', () => resolve(JSON.parse(body)));
        }).on('error', reject);
        return;
      }
      let body = '';
      res.on('data', (c) => (body += c));
      res.on('end', () => {
        try { resolve(JSON.parse(body)); }
        catch { reject(new Error(`Bad response: ${body.slice(0, 200)}`)); }
      });
    }).on('error', reject);
  });
}

function downloadFile(url, destPath, onProgress) {
  return new Promise((resolve, reject) => {
    const doGet = (getUrl) => {
      const mod = getUrl.startsWith('https') ? https : http;
      mod.get(getUrl, { headers: {
        'User-Agent': 'Plaud-V2-Updater',
        Authorization: `Bearer ${UPDATE_TOKEN}`,
        Accept: 'application/octet-stream',
      }}, (res) => {
        if (res.statusCode === 302 || res.statusCode === 301) {
          doGet(res.headers.location);
          return;
        }
        if (res.statusCode !== 200) {
          reject(new Error(`Download failed: HTTP ${res.statusCode}`));
          return;
        }
        const total = parseInt(res.headers['content-length'], 10) || 0;
        let downloaded = 0;
        const file = fs.createWriteStream(destPath);
        res.on('data', (chunk) => {
          downloaded += chunk.length;
          if (total && onProgress) onProgress(Math.round((downloaded / total) * 100));
        });
        res.pipe(file);
        file.on('finish', () => file.close(resolve));
        file.on('error', reject);
      }).on('error', reject);
    };
    doGet(url);
  });
}

async function checkForUpdates() {
  console.log('[Updater] Checking for updates...');
  try {
    const release = await githubApi(`/repos/${UPDATE_REPO}/releases/latest`);
    const latest = release.tag_name.replace(/^v/, '');
    const current = app.getVersion();

    if (latest === current) {
      console.log('[Updater] App is up to date');
      sendUpdateStatus('up-to-date');
      return;
    }

    // Compare versions (simple semver)
    const parse = (v) => v.split('.').map(Number);
    const [cMaj, cMin, cPat] = parse(current);
    const [lMaj, lMin, lPat] = parse(latest);
    if (lMaj < cMaj || (lMaj === cMaj && lMin < cMin) || (lMaj === cMaj && lMin === cMin && lPat <= cPat)) {
      console.log('[Updater] App is up to date');
      sendUpdateStatus('up-to-date');
      return;
    }

    console.log(`[Updater] Update available: v${latest}`);
    sendUpdateStatus('available', { version: latest });

    // Find the ZIP asset
    const zipAsset = release.assets.find((a) => a.name.endsWith('-mac.zip'));
    if (!zipAsset) {
      console.error('[Updater] No ZIP asset found in release');
      sendUpdateStatus('error', { message: 'No update package found' });
      return;
    }

    // Download to cache dir
    const cacheDir = path.join(app.getPath('userData'), 'updates');
    fs.mkdirSync(cacheDir, { recursive: true });
    const zipPath = path.join(cacheDir, zipAsset.name);

    // Skip download if already cached
    if (fs.existsSync(zipPath) && fs.statSync(zipPath).size === zipAsset.size) {
      console.log('[Updater] Update already downloaded');
      _pendingUpdate = { version: latest, zipPath };
      sendUpdateStatus('ready', { version: latest });
      return;
    }

    await downloadFile(zipAsset.url, zipPath, (percent) => {
      sendUpdateStatus('downloading', { percent });
    });

    console.log(`[Updater] Update downloaded: v${latest}`);
    _pendingUpdate = { version: latest, zipPath };
    sendUpdateStatus('ready', { version: latest });
  } catch (err) {
    console.error('[Updater] Error:', err.message);
    sendUpdateStatus('error', { message: err.message });
  }
}

function installUpdate() {
  if (!_pendingUpdate) return;
  const { zipPath } = _pendingUpdate;
  const appPath = path.dirname(path.dirname(path.dirname(app.getAppPath()))); // .app bundle
  console.log(`[Updater] Installing from ${zipPath} to ${appPath}`);

  // Shell script: wait for quit, extract, safe-swap, ad-hoc sign, relaunch
  const script = `
    sleep 1
    rm -rf /tmp/plaud-v2-update
    ditto -xk "${zipPath}" "/tmp/plaud-v2-update"
    NEW_APP=$(find /tmp/plaud-v2-update -maxdepth 1 -name "*.app" | head -1)
    if [ -n "$NEW_APP" ] && [ -d "$NEW_APP/Contents/MacOS" ]; then
      # Safe swap: rename old, move new, only then delete old
      mv "${appPath}" "${appPath}.old" 2>/dev/null
      mv "$NEW_APP" "${appPath}"
      if [ -d "${appPath}/Contents/MacOS" ]; then
        rm -rf "${appPath}.old"
        rm -rf /tmp/plaud-v2-update
        xattr -dr com.apple.quarantine "${appPath}"
        codesign --force --deep --sign - "${appPath}" 2>/dev/null
        open "${appPath}"
      else
        # Move failed — restore old app
        rm -rf "${appPath}"
        mv "${appPath}.old" "${appPath}" 2>/dev/null
        open "${appPath}"
      fi
    else
      # Extraction failed — just relaunch existing app
      open "${appPath}"
    fi
  `;

  spawn('bash', ['-c', script], { detached: true, stdio: 'ignore' }).unref();
  app.quit();
}

// ---- Auto Bug Reporter ----
const _reportedErrors = new Set(); // Deduplicate within a session
const os = require('os');

function githubApiPost(urlPath, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const opts = {
      hostname: 'api.github.com',
      path: urlPath,
      method: 'POST',
      headers: {
        'User-Agent': 'Plaud-V2-BugReporter',
        Accept: 'application/vnd.github+json',
        Authorization: `Bearer ${UPDATE_TOKEN}`,
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data),
      },
    };
    const req = https.request(opts, (res) => {
      let resBody = '';
      res.on('data', (c) => (resBody += c));
      res.on('end', () => {
        try { resolve({ status: res.statusCode, data: JSON.parse(resBody) }); }
        catch { resolve({ status: res.statusCode, data: resBody }); }
      });
    });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

function errorFingerprint(error) {
  // Create a short fingerprint from the error for deduplication
  const msg = String(error.message || error).replace(/[0-9]+/g, 'N').slice(0, 100);
  return msg;
}

function getMacOSName() {
  // Map Darwin kernel versions to macOS names
  const major = parseInt(os.release().split('.')[0], 10);
  const names = { 24: 'Sequoia 15', 25: 'Tahoe 26', 23: 'Sonoma 14', 22: 'Ventura 13', 21: 'Monterey 12' };
  return names[major] || `Darwin ${os.release()}`;
}

const _appStartTime = Date.now();

async function reportError(error, context = 'main') {
  try {
    const fingerprint = errorFingerprint(error);
    if (_reportedErrors.has(fingerprint)) return;
    _reportedErrors.add(fingerprint);

    const version = app.getVersion();
    const errorMsg = String(error.message || error);
    const stack = error.stack || '';

    // Gather system info
    const uptimeMin = Math.round((Date.now() - _appStartTime) / 60000);
    const memMB = Math.round(process.memoryUsage().rss / 1024 / 1024);
    const pythonRunning = pythonProcess && !pythonProcess.killed;
    const electronVersion = process.versions.electron;

    // Search for existing issues with this error
    const query = encodeURIComponent(`repo:${UPDATE_REPO} is:issue label:auto-bug "${fingerprint.slice(0, 60)}"`);
    const search = await githubApi(`/search/issues?q=${query}&per_page=5`);

    const systemInfo = [
      `- **Version:** ${version}`,
      `- **Electron:** ${electronVersion}`,
      `- **macOS:** ${getMacOSName()} (${os.release()})`,
      `- **Arch:** ${os.arch()}`,
      `- **Memory:** ${memMB} MB`,
      `- **Uptime:** ${uptimeMin} min`,
      `- **Python server:** ${pythonRunning ? 'running' : 'not running'}`,
      `- **Context:** ${context}`,
    ].join('\n');

    const openIssue = (search.items || []).find((i) => i.state === 'open');
    if (openIssue) {
      // Comment on existing open issue
      await githubApiPost(`/repos/${UPDATE_REPO}/issues/${openIssue.number}/comments`, {
        body: `Another occurrence reported:\n\n${systemInfo}\n\n\`\`\`\n${errorMsg}\n\`\`\``,
      });
      console.log(`[BugReporter] Commented on existing issue #${openIssue.number}`);
    } else {
      // Create new issue
      const title = `[Auto] ${errorMsg.slice(0, 80)}`;
      const body = [
        '## Auto-generated bug report',
        '',
        '### System',
        systemInfo,
        '',
        '### Error',
        '```',
        errorMsg,
        '```',
        '',
        stack ? `### Stack trace\n\`\`\`\n${stack.slice(0, 2000)}\n\`\`\`` : '',
        '',
        '---',
        '*This issue was created automatically by the Plaud V2 error reporter.*',
      ].join('\n');

      const result = await githubApiPost(`/repos/${UPDATE_REPO}/issues`, {
        title,
        body,
        labels: ['auto-bug'],
      });
      if (result.status === 201) {
        console.log(`[BugReporter] Created issue #${result.data.number}: ${title}`);
      } else {
        console.log(`[BugReporter] Failed to create issue: ${result.status}`);
      }
    }
  } catch (err) {
    // Never let the reporter crash the app
    console.error('[BugReporter] Failed to report:', err.message);
  }
}

// Catch uncaught exceptions in main process
process.on('uncaughtException', (err) => {
  console.error('[Main] Uncaught exception:', err);
  reportError(err, 'main-uncaught');
});

process.on('unhandledRejection', (reason) => {
  console.error('[Main] Unhandled rejection:', reason);
  reportError(reason instanceof Error ? reason : new Error(String(reason)), 'main-unhandled');
});

function getResolvedTheme() {
  if (themePreference === 'auto') {
    return nativeTheme.shouldUseDarkColors ? 'dark' : 'light';
  }
  return themePreference;
}

function applyTheme() {
  if (mainWindow && mainWindow.webContents) {
    const theme = getResolvedTheme();
    mainWindow.webContents.executeJavaScript(
      `document.documentElement.setAttribute('data-theme', '${theme}');` +
        `window._currentTheme = '${theme}'; window._themePreference = '${themePreference}';`,
    );
  }
}

// Register custom URL scheme for OAuth callback
if (process.defaultApp) {
  // Dev mode: register with full path
  app.setAsDefaultProtocolClient('plaud-v2', process.execPath, [path.resolve(process.argv[1])]);
} else {
  app.setAsDefaultProtocolClient('plaud-v2');
}

// Ensure PATH includes common Python install locations
// macOS apps launched from Finder/Dock get a minimal PATH that misses Homebrew etc.
const EXTRA_PATHS = ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin'];
const systemPath = process.env.PATH || '';
const fullPath = [...new Set([...EXTRA_PATHS, ...systemPath.split(':')])].join(':');

// Find a free port starting from the preferred one
function findFreePort(startPort) {
  return new Promise((resolve) => {
    const net = require('net');
    const server = net.createServer();
    server.listen(startPort, '127.0.0.1', () => {
      server.close(() => resolve(startPort));
    });
    server.on('error', () => {
      // Port in use — try the next one
      resolve(findFreePort(startPort + 1));
    });
  });
}

// Startup error codes — friendly messages for users, raw details go to GitHub
const STARTUP_ERRORS = {
  PYTHON_NOT_FOUND: {
    code: 'PYTHON_NOT_FOUND',
    title: 'Python 3 not found',
    friendly: 'Plaud V2 needs Python 3 to run. We tried installing it automatically via Homebrew and by downloading a standalone build, but neither worked.',
    hint: 'Install Python 3 manually from python.org or run: brew install python3\nThen restart Plaud V2.',
  },
  DEPS_FAILED: {
    code: 'DEPS_FAILED',
    title: 'Missing dependencies',
    friendly: 'Some required Python packages couldn\'t be installed automatically.',
    hint: 'Try running in Terminal: pip3 install flask requests bleak cryptography',
  },
  SERVER_CRASH: {
    code: 'SERVER_CRASH',
    title: 'Server crashed',
    friendly: 'The Python backend exited unexpectedly.',
    hint: 'Try restarting the app. If this keeps happening, check that Python 3 and pip are working.',
  },
  SERVER_TIMEOUT: {
    code: 'SERVER_TIMEOUT',
    title: 'Server not responding',
    friendly: 'The Python backend started but never became ready.',
    hint: 'Try restarting the app. Another instance may be running.',
  },
  SPAWN_FAILED: {
    code: 'SPAWN_FAILED',
    title: 'Couldn\'t start Python',
    friendly: 'The system couldn\'t launch the Python process.',
    hint: 'Check that Python 3 is installed and accessible.',
  },
};

function setStartupError(errorKey, rawDetail) {
  const info = STARTUP_ERRORS[errorKey] || { code: errorKey, title: 'Unknown error', friendly: rawDetail, hint: '' };
  _startupError = { ...info, detail: rawDetail };
  console.error(`[Startup] ${info.code}: ${rawDetail}`);
  // Report raw detail to GitHub (user never sees this)
  reportError(new Error(`[${info.code}] ${rawDetail}`), `startup-${info.code.toLowerCase()}`);
}

// Bundled standalone Python lives here when auto-downloaded
const BUNDLED_PYTHON_DIR = path.join(app.getPath('userData'), 'python');

// Find python3 binary — returns { path, version } or null
function findPython() {
  // Check bundled standalone Python first (from a previous auto-install)
  const bundled = path.join(BUNDLED_PYTHON_DIR, 'bin', 'python3');
  const candidates = [
    bundled,
    '/opt/homebrew/bin/python3',
    '/usr/local/bin/python3',
    '/usr/bin/python3',
    'python3',
  ];
  for (const p of candidates) {
    try {
      const version = execSync(`${p} --version`, {
        stdio: ['pipe', 'pipe', 'pipe'],
        env: { ...process.env, PATH: fullPath },
      }).toString().trim();
      return { path: p, version };
    } catch {}
  }
  return null;
}

// Strategy 1: Install Python 3 via Homebrew (if available)
function tryHomebrewInstall() {
  try {
    execSync('which brew', { stdio: 'pipe', env: { ...process.env, PATH: fullPath } });
  } catch {
    console.log('[Python] Homebrew not available, skipping');
    return false;
  }
  console.log('[Python] Attempting install via Homebrew...');
  try {
    execSync('brew install python3', {
      stdio: 'pipe',
      env: { ...process.env, PATH: fullPath },
      timeout: 300000,
    });
    return true;
  } catch {
    return false;
  }
}

// Strategy 2: Download standalone Python to app data dir (no admin, no Homebrew)
// Uses python-build-standalone from Astral (https://github.com/astral-sh/python-build-standalone)
function tryDownloadStandalonePython() {
  const arch = process.arch === 'arm64' ? 'aarch64' : 'x86_64';
  const tag = '20250317';
  const pyVer = '3.12.9';
  const filename = `cpython-${pyVer}+${tag}-${arch}-apple-darwin-install_only.tar.gz`;
  const url = `https://github.com/astral-sh/python-build-standalone/releases/download/${tag}/${filename}`;

  console.log(`[Python] Downloading standalone Python: ${filename}`);

  const tarPath = path.join(app.getPath('temp'), filename);
  try {
    // Download using curl (always available on macOS)
    execSync(`curl -fSL --retry 2 -o "${tarPath}" "${url}"`, {
      stdio: 'pipe',
      timeout: 300000,
    });
  } catch (err) {
    console.error(`[Python] Download failed: ${err.message}`);
    return false;
  }

  // Extract to app data dir
  try {
    fs.mkdirSync(BUNDLED_PYTHON_DIR, { recursive: true });
    // The tarball extracts to a "python/" directory — strip one level
    execSync(`tar -xzf "${tarPath}" --strip-components=1 -C "${BUNDLED_PYTHON_DIR}"`, {
      stdio: 'pipe',
      timeout: 60000,
    });
  } catch (err) {
    console.error(`[Python] Extract failed: ${err.message}`);
    return false;
  }

  // Clean up tarball
  try { fs.unlinkSync(tarPath); } catch {}

  // Verify it works
  const bin = path.join(BUNDLED_PYTHON_DIR, 'bin', 'python3');
  try {
    const version = execSync(`"${bin}" --version`, { stdio: 'pipe' }).toString().trim();
    console.log(`[Python] Standalone Python installed: ${version}`);
    return true;
  } catch {
    console.error('[Python] Standalone Python binary not working after extract');
    return false;
  }
}

// Check pip works and install missing dependencies
function ensurePythonDeps(pythonPath) {
  const deps = ['flask', 'requests', 'bleak', 'cryptography'];
  const missing = [];
  for (const dep of deps) {
    try {
      execSync(`${pythonPath} -c "import ${dep}"`, {
        stdio: 'pipe',
        env: { ...process.env, PATH: fullPath },
      });
    } catch {
      missing.push(dep);
    }
  }
  if (missing.length === 0) return null;

  console.log(`[Python] Installing missing dependencies: ${missing.join(', ')}`);
  try {
    execSync(
      `${pythonPath} -m pip install --break-system-packages -q ${missing.join(' ')}`,
      { stdio: 'pipe', env: { ...process.env, PATH: fullPath }, timeout: 120000 },
    );
    return null;
  } catch (err) {
    return `pip install failed for: ${missing.join(', ')}. stderr: ${(err.stderr || '').toString().slice(0, 500)}`;
  }
}

// Start the Python API server
function startPythonServer() {
  let python = findPython();

  // Python not found — try to install it automatically
  if (!python) {
    console.log('[Python] Python 3 not found, attempting auto-install...');
    // Strategy 1: Homebrew (fast if available)
    if (tryHomebrewInstall()) {
      python = findPython();
    }
    // Strategy 2: Download standalone Python (works everywhere, no admin needed)
    if (!python) {
      if (tryDownloadStandalonePython()) {
        python = findPython();
      }
    }
    if (!python) {
      setStartupError('PYTHON_NOT_FOUND', 'No python3 binary found. Homebrew install failed or unavailable. Standalone Python download failed. Arch: ' + process.arch);
      return;
    }
    console.log('[Python] Auto-install succeeded');
  }

  const pythonPath = python.path;
  console.log(`[Python] Binary: ${pythonPath} (${python.version})`);

  // Install missing deps before starting
  const depError = ensurePythonDeps(pythonPath);
  if (depError) {
    setStartupError('DEPS_FAILED', depError);
    return;
  }

  const serverScript = path.join(__dirname, 'api_server.py');
  const cwd = path.join(__dirname, '..');

  console.log(`[Python] Script: ${serverScript}`);
  console.log(`[Python] CWD: ${cwd}`);
  console.log(`[Python] Port: ${API_PORT}`);

  const _stderrBuf = [];
  pythonProcess = spawn(pythonPath, [serverScript], {
    cwd,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PATH: fullPath, PYTHONUNBUFFERED: '1', PLAUD_API_PORT: String(API_PORT) },
  });

  pythonProcess.stdout.on('data', (data) => {
    console.log(`[Python] ${data.toString().trim()}`);
  });

  pythonProcess.stderr.on('data', (data) => {
    const line = data.toString().trim();
    console.error(`[Python] ${line}`);
    _stderrBuf.push(line);
  });

  pythonProcess.on('close', (code) => {
    console.log(`Python server exited with code ${code}`);
    if (code !== 0 && code !== null && !_startupError) {
      const stderr = _stderrBuf.slice(-10).join('\n');
      setStartupError('SERVER_CRASH', `Exit code ${code}. Last stderr:\n${stderr}`);
      if (mainWindow && mainWindow.webContents) {
        mainWindow.webContents.send('startup-error', _startupError);
      }
    }
    pythonProcess = null;
  });

  pythonProcess.on('error', (err) => {
    console.error('Failed to start Python server:', err);
    setStartupError('SPAWN_FAILED', err.message);
  });
}

// Poll until the API server responds
function waitForServer(maxAttempts = 30) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const check = () => {
      attempts++;
      const req = http.get(`${API_URL}/health`, (res) => {
        let body = '';
        res.on('data', (chunk) => (body += chunk));
        res.on('end', () => {
          try {
            const data = JSON.parse(body);
            if (data.status === 'ok') {
              console.log('Python API server is ready');
              resolve();
              return;
            }
          } catch {}
          retry();
        });
      });
      req.on('error', retry);
      req.setTimeout(1000, () => {
        req.destroy();
        retry();
      });
    };
    const retry = () => {
      if (attempts >= maxAttempts) {
        reject(new Error('Python API server did not start'));
      } else {
        setTimeout(check, 500);
      }
    };
    check();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    transparent: false,
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 16, y: 18 },
    roundedCorners: true,
    visualEffectState: 'active',
    icon: path.join(__dirname, 'icon.icns'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
  });

  mainWindow.loadFile(path.join(__dirname, 'index.html'));

  // Clean disconnect BLE before page reload (Cmd+R)
  mainWindow.webContents.on('will-navigate', () => {
    try {
      const req = http.request(`${API_URL}/device/disconnect`, { method: 'POST', timeout: 2000 });
      req.on('error', () => {});
      req.end();
    } catch {}
  });

  // Theme — bind events
  nativeTheme.on('updated', () => {
    if (themePreference === 'auto') applyTheme();
  });
  mainWindow.webContents.on('did-finish-load', applyTheme);

  mainWindow.once('ready-to-show', () => {
    applyTheme();
    mainWindow.show();
  });

  // macOS: hide window on close instead of destroying it, so tray can reopen it
  let _forceQuit = false;
  app.on('before-quit', () => { _forceQuit = true; });
  mainWindow.on('close', (e) => {
    if (!_forceQuit) {
      e.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Menu bar tray icon — always shown so app is accessible even without dock icon
  const trayIcon = path.join(__dirname, 'trayTemplate.png');
  tray = new Tray(trayIcon);
  tray.setToolTip('Plaud V2');
  const trayMenu = Menu.buildFromTemplate([
    {
      label: 'Show Plaud V2',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        }
      },
    },
    { type: 'separator' },
    {
      label: 'Sync Device',
      click: () => {
        try {
          const req = http.request(`${API_URL}/device/sync`, { method: 'POST', timeout: 5000 });
          req.on('error', () => {});
          req.end();
        } catch {}
      },
    },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]);
  tray.setContextMenu(trayMenu);
  tray.on('click', () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

// Lifecycle
app.whenReady().then(async () => {
  // Set dock icon (needed for dev mode — packaged app uses .icns from build config)
  if (app.dock) {
    app.dock.setIcon(path.join(__dirname, 'icon_512.png'));
    // Restore dock visibility preference
    const prefs = loadElectronPrefs();
    if (prefs.dockVisible === false) {
      app.dock.hide();
    }
  }

  // Find a free port (in case 5123 is already in use)
  API_PORT = await findFreePort(5123);
  API_URL = `http://127.0.0.1:${API_PORT}`;
  console.log(`[Startup] Using port ${API_PORT}`);

  startPythonServer();

  if (!_startupError) {
    try {
      await waitForServer();
    } catch (err) {
      if (!_startupError) {
        setStartupError('SERVER_TIMEOUT', err.message);
      }
    }
  }

  createWindow();

  // Check for updates after a short delay (don't block startup)
  setTimeout(() => checkForUpdates(), 5000);

  // Re-check every 4 hours
  setInterval(() => checkForUpdates(), 4 * 60 * 60 * 1000);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });

  // Detect system wake from sleep — BLE connections silently die during sleep
  powerMonitor.on('resume', () => {
    console.log('[PowerMonitor] System resumed from sleep — notifying renderer');
    if (mainWindow && mainWindow.webContents) {
      mainWindow.webContents.send('system-wake');
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  // Gracefully disconnect BLE before quitting so device doesn't stay in "connected" state
  if (pythonProcess) {
    try {
      console.log('Disconnecting BLE device...');
      const req = http.request(`${API_URL}/device/disconnect`, { method: 'POST', timeout: 2000 });
      req.on('error', () => {});
      req.end();
    } catch {}
    console.log('Stopping Python API server...');
    pythonProcess.kill('SIGTERM');
    setTimeout(() => {
      if (pythonProcess) pythonProcess.kill('SIGKILL');
    }, 2000);
  }
});

// Handle OAuth callback via custom URL scheme (plaud-v2://callback?auth_code=...)
function handleProtocolUrl(url) {
  if (!url) return;
  try {
    const parsed = new URL(url);
    const authCode = parsed.searchParams.get('auth_code') || parsed.searchParams.get('code');
    if (authCode && mainWindow) {
      // Send the auth code to the renderer
      mainWindow.webContents.send('oauth-callback', authCode);
      // Also focus the window
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  } catch {}
}

// macOS: open-url event
app.on('open-url', (event, url) => {
  event.preventDefault();
  handleProtocolUrl(url);
});

// Windows/Linux: second-instance event
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', (event, commandLine) => {
    // Protocol URL is the last argument
    const url = commandLine.find((arg) => arg.startsWith('plaud-v2://'));
    handleProtocolUrl(url);
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

// IPC: API connection info (dynamic port)
ipcMain.handle('get-api-info', () => {
  return { port: API_PORT, url: API_URL, error: _startupError };
});

// IPC: theme cycling
ipcMain.handle('cycle-theme', () => {
  const cycle = { auto: 'light', light: 'dark', dark: 'auto' };
  themePreference = cycle[themePreference] || 'auto';
  applyTheme();
  return { preference: themePreference, resolved: getResolvedTheme() };
});

ipcMain.handle('get-theme', () => {
  return { preference: themePreference, resolved: getResolvedTheme() };
});

// IPC: system settings
ipcMain.handle('get-login-item', () => {
  const settings = app.getLoginItemSettings();
  return { enabled: settings.openAtLogin };
});

ipcMain.handle('set-login-item', (event, enabled) => {
  app.setLoginItemSettings({ openAtLogin: enabled });
  return { enabled };
});

// Persist Electron-level preferences to a JSON file in userData (survives updates)
const _prefsPath = path.join(app.getPath('userData'), 'electron-prefs.json');
function loadElectronPrefs() {
  try { return JSON.parse(fs.readFileSync(_prefsPath, 'utf8')); } catch { return {}; }
}
function saveElectronPrefs(prefs) {
  fs.writeFileSync(_prefsPath, JSON.stringify(prefs, null, 2));
}

ipcMain.handle('get-dock-visible', () => {
  return { visible: !app.dock || app.dock.isVisible() };
});

ipcMain.handle('set-dock-visible', async (event, visible) => {
  if (app.dock) {
    if (visible) {
      app.dock.show();
    } else {
      app.dock.hide();
      // dock.hide() deactivates the app on macOS — re-focus the window
      if (mainWindow) {
        mainWindow.show();
        mainWindow.focus();
      }
    }
  }
  const prefs = loadElectronPrefs();
  prefs.dockVisible = visible;
  saveElectronPrefs(prefs);
  return { visible };
});

// IPC handlers for opening external links
ipcMain.handle('open-external', (event, url) => {
  shell.openExternal(url);
});

// IPC: auto-updater
ipcMain.handle('install-update', () => {
  installUpdate();
  return { installing: true };
});

ipcMain.handle('check-for-updates', () => {
  checkForUpdates();
});

ipcMain.handle('get-app-version', () => {
  return app.getVersion();
});

// IPC: bug reporter (from renderer)
ipcMain.handle('report-error', (event, { message, stack, context }) => {
  const err = new Error(message);
  err.stack = stack || '';
  reportError(err, context || 'renderer');
});

// IPC: manual bug report (from renderer)
ipcMain.handle('report-bug', async (event, { expected, actual, steps }) => {
  try {
    const version = app.getVersion();
    const uptimeMin = Math.round((Date.now() - _appStartTime) / 60000);
    const memMB = Math.round(process.memoryUsage().rss / 1024 / 1024);
    const pythonRunning = pythonProcess && !pythonProcess.killed;
    const electronVersion = process.versions.electron;

    const systemInfo = [
      `- **Version:** ${version}`,
      `- **Electron:** ${electronVersion}`,
      `- **macOS:** ${getMacOSName()} (${os.release()})`,
      `- **Arch:** ${os.arch()}`,
      `- **Memory:** ${memMB} MB`,
      `- **Uptime:** ${uptimeMin} min`,
      `- **Python server:** ${pythonRunning ? 'running' : 'not running'}`,
    ].join('\n');

    const titleText = (actual || expected || 'Bug report').slice(0, 80);
    const title = `[User Report] ${titleText}`;
    const body = [
      '## User Bug Report',
      '',
      '### Expected behaviour',
      expected || '_Not provided_',
      '',
      '### Actual behaviour',
      actual || '_Not provided_',
      '',
      steps ? `### Steps to reproduce\n${steps}\n` : '',
      '### System',
      systemInfo,
      '',
      '---',
      '*This issue was submitted by a user via the in-app bug reporter.*',
    ].filter(Boolean).join('\n');

    const result = await githubApiPost(`/repos/${UPDATE_REPO}/issues`, {
      title,
      body,
      labels: ['user-report'],
    });

    if (result.status === 201) {
      console.log(`[BugReporter] User report created: #${result.data.number}`);
      return { success: true, number: result.data.number, url: result.data.html_url };
    }
    console.log(`[BugReporter] Failed to create user report: ${result.status}`);
    return { success: false, error: `GitHub returned ${result.status}` };
  } catch (err) {
    console.error('[BugReporter] Failed to submit user report:', err.message);
    return { success: false, error: err.message };
  }
});

// IPC: system notification (for background WiFi sync prompts)
ipcMain.handle('show-notification', (event, { title, body, actionLabel }) => {
  if (!Notification.isSupported()) return { shown: false };
  const notif = new Notification({
    title,
    body,
    silent: false,
    actions: actionLabel ? [{ type: 'button', text: actionLabel }] : [],
  });
  notif.on('click', () => {
    // Bring window to front
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });
  if (actionLabel) {
    notif.on('action', () => {
      if (mainWindow && mainWindow.webContents) {
        mainWindow.webContents.send('notification-action', 'wifi-sync');
      }
    });
  }
  notif.show();
  return { shown: true };
});
