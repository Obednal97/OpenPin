const { app, BrowserWindow, ipcMain, shell, nativeTheme, Tray, Menu } = require('electron');
const path = require('path');
const { spawn, execSync } = require('child_process');
const http = require('http');

let mainWindow;
let pythonProcess;
let tray = null;
const API_PORT = 5123;
const API_URL = `http://127.0.0.1:${API_PORT}`;
let themePreference = 'auto'; // 'auto' | 'light' | 'dark'

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

// Find python3 binary
function findPython() {
  const candidates = ['python3', '/usr/bin/python3', '/usr/local/bin/python3', '/opt/homebrew/bin/python3'];
  for (const p of candidates) {
    try {
      execSync(`${p} --version`, { stdio: 'pipe' });
      return p;
    } catch {}
  }
  return 'python3';
}

// Start the Python API server
function startPythonServer() {
  const pythonPath = findPython();
  const serverScript = path.join(__dirname, 'api_server.py');

  console.log(`Starting Python API server: ${pythonPath} ${serverScript}`);

  pythonProcess = spawn(pythonPath, [serverScript], {
    cwd: path.join(__dirname, '..'),
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
  });

  pythonProcess.stdout.on('data', (data) => {
    console.log(`[Python] ${data.toString().trim()}`);
  });

  pythonProcess.stderr.on('data', (data) => {
    console.error(`[Python] ${data.toString().trim()}`);
  });

  pythonProcess.on('close', (code) => {
    console.log(`Python server exited with code ${code}`);
    pythonProcess = null;
  });

  pythonProcess.on('error', (err) => {
    console.error('Failed to start Python server:', err);
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
    trafficLightPosition: { x: 16, y: 16 },
    roundedCorners: true,
    visualEffectState: 'active',
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
  startPythonServer();

  try {
    await waitForServer();
  } catch (err) {
    console.error(err.message);
    // Still create the window — the UI will show an error state
  }

  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
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

ipcMain.handle('get-dock-visible', () => {
  return { visible: !app.dock || app.dock.isVisible() };
});

ipcMain.handle('set-dock-visible', (event, visible) => {
  if (app.dock) {
    if (visible) app.dock.show();
    else app.dock.hide();
  }
  return { visible };
});

// IPC handlers for opening external links
ipcMain.handle('open-external', (event, url) => {
  shell.openExternal(url);
});
