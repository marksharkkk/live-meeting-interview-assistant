// Render the actual page without a backend or recording, using a test-only preload.
const {app, BrowserWindow} = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
app.whenReady().then(async () => {
  const output = fs.mkdtempSync(path.join(os.tmpdir(), 'meeting-layout-'));
  // Windows GUI processes can outlive the launching terminal's output pipe.
  // Persist diagnostics instead of writing to stdout/stderr (EPIPE).
  const log = value => fs.appendFileSync(path.join(output, 'results.log'), value + '\n');
  const win = new BrowserWindow({show: false, width: 1120, height: 780,
    webPreferences: {preload: path.join(__dirname, 'layout_preload.cjs'), contextIsolation: false, sandbox: false}});
  win.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL) =>
    log(JSON.stringify({event: 'did-fail-load', errorCode, errorDescription, validatedURL})));
  win.webContents.on('render-process-gone', (_event, details) =>
    log(JSON.stringify({event: 'render-process-gone', details})));
  win.webContents.on('console-message', (_event, level, message, line, sourceId) =>
    log(JSON.stringify({event: 'console', level, message, line, sourceId})));
  try {
    await win.loadFile(path.join(__dirname, '../renderer/teleprompter.html'));
    for (const [width, height, mode] of [[1120,780,'meeting'], [640,480,'meeting'], [1120,780,'interview'], [640,480,'focus']]) {
      win.setContentSize(width, height);
      await win.webContents.executeJavaScript(`
        setWorkspaceView(${JSON.stringify(mode === 'interview' ? 'interview' : 'meeting')});
        if (${JSON.stringify(mode)} === 'focus') setTranscriptFocused(true);
        new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      `);
      const result = await win.webContents.executeJavaScript(`(() => {
        const buttons = [...document.querySelectorAll('button')].filter(e => e.getClientRects().length);
        return {width: innerWidth, height: innerHeight, clippedButtons: buttons.filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.id),
          horizontalOverflow: document.querySelector('.workspace-panels').scrollWidth > document.querySelector('.workspace-panels').clientWidth + 1,
          transcriptHeight: document.querySelector('.transcript-section').getBoundingClientRect().height,
          paneOverlap: (() => {
            const meeting = document.querySelector('.meeting-panel');
            if (!meeting.getClientRects().length) return false;
            const a = meeting.getBoundingClientRect();
            const b = document.querySelector('.transcript-section').getBoundingClientRect();
            return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
          })()};
      })()`);
      log(JSON.stringify({mode, ...result}));
      if (result.clippedButtons.length || result.horizontalOverflow || result.paneOverlap || result.transcriptHeight < 150) throw Error('Layout overflow');
      const image = await win.webContents.capturePage();
      const filename = path.join(output, `${mode}-${width}.png`);
      fs.writeFileSync(filename, image.toPNG());
      log(filename);
    }
    log('PASS');
  } catch (error) { log(String(error.stack || error)); process.exitCode = 1; }
  finally { win.destroy(); app.quit(); }
});
