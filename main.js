const { app, BrowserWindow, ipcMain, globalShortcut, screen, dialog } = require('electron');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { spawn } = require('child_process');
const { pathToFileURL } = require('url');

const API_BASE = 'http://127.0.0.1:8000';
const API_TOKEN = crypto.randomBytes(32).toString('hex');

let mainWindow = null;
let teleprompterWindow = null;
let backendProcess = null;
let isPrivacyMode = false;

function secureWebPreferences() {
  return {
    preload: path.join(__dirname, 'preload.js'),
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
  };
}

function isTrustedSender(event) {
  return [mainWindow, teleprompterWindow].some(
    (window) => window && event.sender === window.webContents,
  );
}

function restrictNavigation(window, relativeFile) {
  const allowedUrl = pathToFileURL(path.join(__dirname, relativeFile)).href;
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, targetUrl) => {
    if (targetUrl !== allowedUrl) event.preventDefault();
  });
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 900,
    height: 700,
    title: 'Meeting Assistant - Settings',
    show: false,
    webPreferences: secureWebPreferences(),
  });

  mainWindow.loadFile('renderer/main.html');
  restrictNavigation(mainWindow, 'renderer/main.html');
  mainWindow.once('ready-to-show', () => mainWindow?.show());
  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  if (process.argv.includes('--dev')) mainWindow.webContents.openDevTools();
}

function applyPrivacyMode(enabled) {
  isPrivacyMode = Boolean(enabled);
  if (!teleprompterWindow) return;

  teleprompterWindow.setContentProtection(isPrivacyMode);
  teleprompterWindow.setOpacity(isPrivacyMode ? 0.08 : 0.85);
  teleprompterWindow.setIgnoreMouseEvents(isPrivacyMode, { forward: true });
  teleprompterWindow.webContents.send('privacy-mode-changed', isPrivacyMode);
}

function createTeleprompterWindow() {
  const { width } = screen.getPrimaryDisplay().workAreaSize;

  teleprompterWindow = new BrowserWindow({
    width: 500,
    height: 300,
    x: width - 520,
    y: 20,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: true,
    resizable: true,
    opacity: 0.85,
    webPreferences: secureWebPreferences(),
  });

  teleprompterWindow.loadFile('renderer/teleprompter.html');
  restrictNavigation(teleprompterWindow, 'renderer/teleprompter.html');
  teleprompterWindow.setAlwaysOnTop(true, 'screen-saver');

  if (process.platform === 'win32') {
    teleprompterWindow.setHasShadow(false);
    teleprompterWindow.setBackgroundColor('#00000000');
  }

  teleprompterWindow.on('closed', () => {
    teleprompterWindow = null;
    isPrivacyMode = false;
  });
}

function startBackendServer() {
  const backendPath = path.join(__dirname, 'backend', 'main.py');
  const pythonPath = process.platform === 'win32'
    ? path.join(__dirname, '.venv', 'Scripts', 'python.exe')
    : path.join(__dirname, '.venv', 'bin', 'python');

  if (!fs.existsSync(pythonPath)) {
    throw new Error('Project environment is missing. Please run install.bat first.');
  }

  backendProcess = spawn(pythonPath, ['-B', backendPath], {
    cwd: path.join(__dirname, 'backend'),
    stdio: 'pipe',
    windowsHide: true,
    env: {
      ...process.env,
      MEETING_ASSISTANT_TOKEN: API_TOKEN,
    },
  });

  backendProcess.stdout.on('data', (data) => console.log(`Backend: ${data}`));
  backendProcess.stderr.on('data', (data) => console.error(`Backend: ${data}`));
  backendProcess.on('error', (error) => console.error(`Backend start failed: ${error.message}`));
  backendProcess.on('exit', (code) => {
    console.log(`Backend exited with code ${code}`);
    backendProcess = null;
  });
}

function checkBackendHealth() {
  return new Promise((resolve) => {
    const request = http.get(`${API_BASE}/api/health`, {
      headers: { 'X-Meeting-Assistant-Token': API_TOKEN },
      timeout: 1000,
    }, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on('timeout', () => {
      request.destroy();
      resolve(false);
    });
    request.on('error', () => resolve(false));
  });
}

async function waitForBackend(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!backendProcess) return false;
    if (await checkBackendHealth()) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

function registerShortcuts() {
  const shortcuts = [
    ['CommandOrControl+Shift+H', () => {
      if (!teleprompterWindow) return;
      teleprompterWindow.isVisible() ? teleprompterWindow.hide() : teleprompterWindow.show();
    }],
    ['CommandOrControl+Shift+M', () => {
      if (!mainWindow) return;
      if (mainWindow.isVisible()) {
        mainWindow.hide();
      } else {
        mainWindow.show();
        mainWindow.focus();
      }
    }],
    ['Control+H', () => applyPrivacyMode(!isPrivacyMode)],
    ['Escape', () => applyPrivacyMode(true)],
  ];

  for (const [accelerator, handler] of shortcuts) {
    if (!globalShortcut.register(accelerator, handler)) {
      console.warn(`Could not register shortcut: ${accelerator}`);
    }
  }
}

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    try {
      startBackendServer();
      const ready = await waitForBackend();
      if (!ready) throw new Error('The local service did not become ready within 30 seconds.');
      createMainWindow();
      registerShortcuts();
    } catch (error) {
      dialog.showErrorBox('Meeting Assistant could not start', error.message);
      app.quit();
      return;
    }

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
    });
  });
}

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  if (backendProcess) backendProcess.kill();
});

ipcMain.handle('get-api-config', (event) => {
  if (!isTrustedSender(event)) throw new Error('Untrusted renderer');
  return { baseUrl: API_BASE, token: API_TOKEN };
});

ipcMain.on('show-teleprompter', (event) => {
  if (!isTrustedSender(event)) return;
  if (!teleprompterWindow) createTeleprompterWindow();
  else teleprompterWindow.show();
});

ipcMain.on('close-teleprompter', (event) => {
  if (isTrustedSender(event)) teleprompterWindow?.close();
});

ipcMain.on('minimize-teleprompter', (event) => {
  if (isTrustedSender(event)) teleprompterWindow?.minimize();
});

ipcMain.on('set-privacy-mode', (event, enabled) => {
  if (isTrustedSender(event) && typeof enabled === 'boolean') applyPrivacyMode(enabled);
});
