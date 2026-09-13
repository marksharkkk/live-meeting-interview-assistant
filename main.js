const { app, BrowserWindow, ipcMain, globalShortcut, screen, dialog } = require('electron');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { spawn } = require('child_process');
const { pathToFileURL } = require('url');

// Some Windows installations cannot load Electron's out-of-process GPU
// component. The app is a lightweight text UI, so keep rendering in-process
// with the GPU disabled to avoid a startup crash before the first window.
if (process.platform === 'win32') {
  app.commandLine.appendSwitch('disable-gpu');
  app.commandLine.appendSwitch('in-process-gpu');
}

const API_BASE = 'http://127.0.0.1:8000';
const API_TOKEN = crypto.randomBytes(32).toString('hex');

let mainWindow = null;
let teleprompterWindow = null;
let backendProcess = null;
let isPrivacyMode = false;
let workspaceView = 'meeting';
let preparingExit = false;
let exitPrepared = false;
const privacyWindowStates = new Map();

const logDirectory = path.join(__dirname, 'logs');
const logFile = path.join(logDirectory, 'desktop.log');
function writeLog(message) {
  // GUI launches can lose their parent's stdout/stderr pipes on Windows.
  // Keep diagnostics on disk so logging cannot crash the main process with EPIPE.
  try {
    fs.mkdirSync(logDirectory, { recursive: true });
    if (fs.existsSync(logFile) && fs.statSync(logFile).size > 5 * 1024 * 1024) {
      fs.copyFileSync(logFile, path.join(logDirectory, 'desktop.previous.log'));
      fs.writeFileSync(logFile, '');
    }
    fs.appendFileSync(logFile, `[${new Date().toISOString()}] ${String(message)}\n`);
  } catch (_) {
    // A logging failure must not stop the meeting or trigger another pipe write.
  }
}

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

function attachRendererDiagnostics(window, name) {
  window.webContents.on('did-finish-load', () => writeLog(`${name} renderer loaded`));
  window.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL) => {
    writeLog(`${name} renderer load failed: ${errorCode} ${errorDescription} ${validatedURL}`);
  });
  window.webContents.on('render-process-gone', (_event, details) => {
    writeLog(`${name} renderer exited: ${JSON.stringify(details)}`);
  });
  window.webContents.on('console-message', (_event, level, message, line, sourceId) => {
    if (level >= 2) writeLog(`${name} console error: ${message} (${sourceId}:${line})`);
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

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'main.html')).catch((error) => {
    writeLog(`Main window load failed: ${error.stack || error.message}`);
  });
  restrictNavigation(mainWindow, 'renderer/main.html');
  attachRendererDiagnostics(mainWindow, 'Main');
  mainWindow.once('ready-to-show', () => {
    if (!mainWindow) return;
    if (isPrivacyMode) {
      privacyWindowStates.set(mainWindow, { visible: true, minimized: false });
      mainWindow.setSkipTaskbar(true);
      mainWindow.setContentProtection(true);
    } else mainWindow.show();
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  if (process.argv.includes('--dev')) mainWindow.webContents.openDevTools();
}

function applyPrivacyMode(enabled) {
  const next = Boolean(enabled);
  if (next === isPrivacyMode) return;
  isPrivacyMode = next;
  for (const window of [mainWindow, teleprompterWindow]) {
    if (!window || window.isDestroyed()) continue;
    window.setContentProtection(next);
    if (next) {
      privacyWindowStates.set(window, { visible: window.isVisible(), minimized: window.isMinimized() });
      window.setSkipTaskbar(true);
      window.hide();
    } else {
      const previous = privacyWindowStates.get(window);
      window.setSkipTaskbar(false);
      if (previous?.minimized) {
        window.show();
        window.minimize();
      } else if (previous?.visible) {
        if (window.isMinimized()) window.restore();
        window.show();
      }
    }
  }
  if (!next) privacyWindowStates.clear();
  teleprompterWindow?.webContents.send('privacy-mode-changed', next);
}

function createTeleprompterWindow() {
  workspaceView = 'meeting';
  const area = screen.getPrimaryDisplay().workArea;
  const width = Math.min(1120, area.width);
  const height = Math.min(780, area.height);

  teleprompterWindow = new BrowserWindow({
    width,
    height,
    minWidth: Math.min(640, area.width),
    minHeight: Math.min(480, area.height),
    x: area.x + Math.floor((area.width - width) / 2),
    y: area.y + Math.floor((area.height - height) / 2),
    frame: false,
    // A transparent window can be created successfully but render invisible
    // on Windows when Electron falls back to software rendering.
    transparent: false,
    alwaysOnTop: true,
    skipTaskbar: false,
    hasShadow: true,
    resizable: true,
    opacity: 1,
    webPreferences: secureWebPreferences(),
  });

  teleprompterWindow.loadFile(path.join(__dirname, 'renderer', 'teleprompter.html')).catch((error) => {
    writeLog(`Teleprompter load failed: ${error.stack || error.message}`);
    if (teleprompterWindow && !teleprompterWindow.isDestroyed()) teleprompterWindow.close();
  });
  restrictNavigation(teleprompterWindow, 'renderer/teleprompter.html');
  attachRendererDiagnostics(teleprompterWindow, 'Teleprompter');
  teleprompterWindow.once('ready-to-show', () => {
    if (!teleprompterWindow || teleprompterWindow.isDestroyed()) return;
    teleprompterWindow.show();
    teleprompterWindow.focus();
    writeLog('Teleprompter window shown and focused');
  });
  teleprompterWindow.setAlwaysOnTop(true, 'screen-saver');

  if (process.platform === 'win32') {
    teleprompterWindow.setHasShadow(false);
    teleprompterWindow.setBackgroundColor('#141428');
  }

  teleprompterWindow.on('closed', () => {
    privacyWindowStates.delete(teleprompterWindow);
    teleprompterWindow = null;
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

  const backendEnv = {
    ...process.env,
    MEETING_ASSISTANT_TOKEN: API_TOKEN,
  };
  // The main-window settings are persisted in backend/.env and are the
  // single source of truth. Do not let inherited shell variables silently
  // override a model or endpoint selected by the user in the UI.
  delete backendEnv.OPENAI_API_KEY;
  delete backendEnv.OPENAI_API_BASE;
  delete backendEnv.OPENAI_MODEL;

  backendProcess = spawn(pythonPath, ['-B', backendPath], {
    cwd: path.join(__dirname, 'backend'),
    stdio: 'pipe',
    windowsHide: true,
    env: backendEnv,
  });

  backendProcess.stdout.on('data', (data) => writeLog(`Backend: ${data}`));
  backendProcess.stderr.on('data', (data) => writeLog(`Backend: ${data}`));
  backendProcess.on('error', (error) => writeLog(`Backend start failed: ${error.message}`));
  backendProcess.on('exit', (code) => {
    writeLog(`Backend exited with code ${code}`);
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
    ['CommandOrControl+Shift+M', () => {
      if (!mainWindow || isPrivacyMode) return;
      if (!mainWindow.isMinimized()) {
        mainWindow.minimize();
      } else {
        mainWindow.restore();
        mainWindow.show();
        mainWindow.focus();
      }
    }],
    ['Control+H', () => applyPrivacyMode(!isPrivacyMode)],
    ['Escape', () => app.quit()],
  ];

  for (const [accelerator, handler] of shortcuts) {
    if (!globalShortcut.register(accelerator, handler)) {
      writeLog(`Could not register shortcut: ${accelerator}`);
    }
  }
}

const gotSingleInstanceLock = app.requestSingleInstanceLock();
writeLog(`Launch: single instance lock=${gotSingleInstanceLock}`);
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (isPrivacyMode) applyPrivacyMode(false);
    writeLog('Second launch: restoring settings window');
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    } else if (app.isReady() && backendProcess) {
      createMainWindow();
    }
  });

  app.whenReady().then(async () => {
    try {
      startBackendServer();
      const ready = await waitForBackend();
      if (!ready) throw new Error('The local service did not become ready within 30 seconds.');
      createMainWindow();
      writeLog('Startup complete: settings window created');
      registerShortcuts();
    } catch (error) {
      writeLog(`Startup failed: ${error.stack || error.message}`);
      dialog.showErrorBox('Meeting Assistant could not start', `${error.message}\n\n日志位置：${logFile}`);
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

app.on('before-quit', (event) => {
  if (exitPrepared || !backendProcess) return;
  event.preventDefault();
  if (preparingExit) return;
  preparingExit = true;
  writeLog('Preparing exit: saving recording and final transcript');
  const finish = () => {
    if (exitPrepared) return;
    exitPrepared = true;
    app.quit();
  };
  const request = http.request(`${API_BASE}/api/prepare-exit`, {
    method: 'POST', headers: { 'X-Meeting-Assistant-Token': API_TOKEN }, timeout: 30000,
  }, response => {
    response.resume();
    response.on('end', () => {
      writeLog(`Exit save response: ${response.statusCode}`);
      finish();
    });
    response.on('error', finish);
  });
  request.on('timeout', () => {
    writeLog('Exit save timed out; retained files on disk');
    request.destroy();
    finish();
  });
  request.on('error', error => { writeLog(`Exit save: ${error.message}`); finish(); });
  request.end();
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  if (backendProcess) {
    if (process.platform === 'win32') {
      // uv's Python launcher may own a second Python process: stop the full tree.
      const terminator = spawn('taskkill', ['/PID', String(backendProcess.pid), '/T', '/F'],
        {windowsHide: true, stdio: 'ignore'});
      terminator.on('error', () => backendProcess?.kill());
    } else backendProcess.kill();
  }
});

ipcMain.handle('get-api-config', (event) => {
  if (!isTrustedSender(event)) throw new Error('Untrusted renderer');
  return { baseUrl: API_BASE, token: API_TOKEN };
});

ipcMain.on('set-workspace-view', (event, view) => {
  if (!teleprompterWindow || event.sender !== teleprompterWindow.webContents) return;
  if (view !== 'meeting' && view !== 'interview') return;
  workspaceView = view;
  teleprompterWindow.setOpacity(view === 'meeting' ? 1 : 0.85);
});

ipcMain.on('show-teleprompter', (event) => {
  if (!isTrustedSender(event)) return;
  writeLog('Show teleprompter requested');
  if (isPrivacyMode) applyPrivacyMode(false);
  if (teleprompterWindow?.webContents.isCrashed()) {
    teleprompterWindow.destroy();
    teleprompterWindow = null;
  }
  if (!teleprompterWindow) createTeleprompterWindow();
  else {
    if (teleprompterWindow.isMinimized()) teleprompterWindow.restore();
    teleprompterWindow.show();
    teleprompterWindow.focus();
    writeLog('Teleprompter window restored and focused');
  }
});

ipcMain.on('close-teleprompter', (event) => {
  if (isTrustedSender(event)) teleprompterWindow?.close();
});

ipcMain.on('minimize-teleprompter', (event) => {
  if (isTrustedSender(event)) teleprompterWindow?.minimize();
});

ipcMain.on('maximize-teleprompter', (event) => {
  if (!isTrustedSender(event) || !teleprompterWindow) return;
  if (teleprompterWindow.isMaximized()) teleprompterWindow.unmaximize();
  else teleprompterWindow.maximize();
});

ipcMain.on('set-privacy-mode', (event, enabled) => {
  if (isTrustedSender(event) && typeof enabled === 'boolean') applyPrivacyMode(enabled);
});

ipcMain.handle('export-meeting-record', async (event, payload) => {
  if (!isTrustedSender(event)) throw new Error('Untrusted renderer');
  const content = typeof payload?.content === 'string' ? payload.content : '';
  if (!content || content.length > 500_000) throw new Error('Invalid meeting record');
  const defaultName = typeof payload?.filename === 'string' && payload.filename.trim()
    ? payload.filename.trim().replace(/[<>:"/\\|?*\x00-\x1F]/g, '_').slice(0, 120)
    : `meeting-record-${new Date().toISOString().slice(0, 10)}.txt`;
  const result = await dialog.showSaveDialog(mainWindow, {
    title: '导出会议记录',
    defaultPath: defaultName.endsWith('.txt') ? defaultName : `${defaultName}.txt`,
    filters: [{ name: '文本文件', extensions: ['txt'] }],
  });
  if (result.canceled || !result.filePath) return { canceled: true };
  await fs.promises.writeFile(result.filePath, content, 'utf8');
  return { canceled: false, filePath: result.filePath };
});
