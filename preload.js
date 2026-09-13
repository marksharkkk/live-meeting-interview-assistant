const { contextBridge, ipcRenderer } = require('electron');

function subscribe(channel, callback) {
  const listener = (_event, value) => callback(value);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld('electronAPI', {
  getApiConfig: () => ipcRenderer.invoke('get-api-config'),
  showTeleprompter: () => ipcRenderer.send('show-teleprompter'),
  closeTeleprompter: () => ipcRenderer.send('close-teleprompter'),
  minimizeTeleprompter: () => ipcRenderer.send('minimize-teleprompter'),
  maximizeTeleprompter: () => ipcRenderer.send('maximize-teleprompter'),
  setPrivacyMode: (enabled) => ipcRenderer.send('set-privacy-mode', Boolean(enabled)),
  setWorkspaceView: (view) => ipcRenderer.send('set-workspace-view', view),
  exportMeetingRecord: (payload) => ipcRenderer.invoke('export-meeting-record', payload),
  onPrivacyModeChanged: (callback) => subscribe('privacy-mode-changed', callback),
});
