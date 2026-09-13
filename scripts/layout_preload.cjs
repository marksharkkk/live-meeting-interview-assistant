window.electronAPI = {
  getApiConfig: async () => ({baseUrl: 'http://127.0.0.1:1', token: 'layout-test'}),
  onPrivacyModeChanged: () => {},
  setPrivacyMode: () => {},
  setWorkspaceView: () => {},
  maximizeTeleprompter: () => {},
  minimizeTeleprompter: () => {},
  closeTeleprompter: () => {},
  exportMeetingRecord: async () => ({canceled: true}),
};
