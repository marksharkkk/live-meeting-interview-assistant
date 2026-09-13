const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../main.js'), 'utf8');
const start = source.indexOf('function applyPrivacyMode(');
const end = source.indexOf('\nfunction createTeleprompterWindow', start);
function fakeWindow(minimized = false) {
  return {
    visible: !minimized, minimized, skip: false,
    isDestroyed: () => false,
    isVisible() { return this.visible; },
    isMinimized() { return this.minimized; },
    setContentProtection(value) { this.protected = value; },
    setSkipTaskbar(value) { this.skip = value; },
    hide() { this.visible = false; },
    show() { this.visible = true; },
    minimize() { this.minimized = true; },
    restore() { this.minimized = false; },
    webContents: { send() {} },
  };
}
const settings = fakeWindow();
const prompt = fakeWindow(true);
const context = vm.createContext({mainWindow: settings, teleprompterWindow: prompt,
  isPrivacyMode: false, privacyWindowStates: new Map()});
vm.runInContext(source.slice(start, end), context);
context.applyPrivacyMode(true);
for (const window of [settings, prompt]) {
  assert.equal(window.visible, false);
  assert.equal(window.skip, true);
}
context.applyPrivacyMode(true); // Repeated Escape must not overwrite saved state.
context.applyPrivacyMode(false);
assert.equal(settings.visible, true);
assert.equal(prompt.minimized, true);
assert.equal(prompt.skip, false);
assert.equal(settings.skip, false);
context.teleprompterWindow = null; // Privacy must also work with only settings open.
context.applyPrivacyMode(true);
assert.equal(settings.visible, false);
context.applyPrivacyMode(false);
assert.equal(settings.visible, true);
process.stdout.write('Window privacy checks passed\n');
