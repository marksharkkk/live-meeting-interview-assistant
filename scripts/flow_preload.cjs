// Test-only, deterministic service for the actual workbench page.
window.__flow = {translations: [], previewTranslations: [], sockets: [], stopCount: 0, answers: [], emptyAnswer: false, sessions: [], active: null, timers: [], summaries: 0};
const f = window.__flow;
window.setInterval = callback => { f.timers.push(callback); return f.timers.length; };
window.electronAPI = {
  getApiConfig: async () => ({baseUrl: 'http://127.0.0.1:8000', token: 'flow-test'}),
  onPrivacyModeChanged() {}, setPrivacyMode() {}, setWorkspaceView() {}, maximizeTeleprompter() {}, minimizeTeleprompter() {}, closeTeleprompter() {},
  exportMeetingRecord: async () => ({canceled: true}),
};
const response = (payload, status = 200) => ({ok: status < 300, status, json: async () => payload, blob: async () => new Blob()});
window.fetch = async (url, options = {}) => {
  const path = new URL(url).pathname;
  const body = JSON.parse(options.body || '{}');
  if (path === '/api/settings') return response({voice_input_device: -1, voice_language: 'auto'});
  if (path === '/api/audio/devices') return response({devices: []});
  if (path === '/api/meetings') {
    if (options.method === 'POST') {
      const record = {id: String(f.sessions.length + 1).padStart(32, '0'), title: body.title || 'Test', started_at: new Date().toISOString(), record_audio: body.record_audio,
        tracks: [], transcript: '', entries: [], translations: [], answers: []};
      f.sessions.push(record); f.active = record.id;
      return response(record);
    }
    return response({meetings: f.sessions, active_id: f.active});
  }
  if (path === '/api/meetings/finish') {
    const record = f.sessions.find(row => row.id === f.active); f.active = null; return response(record);
  }
  if (path.endsWith('/summary')) {
    f.summaries++;
    return response({summary: 'Summary', source_at: new Date().toISOString(), source_characters: 100});
  }
  if (path.startsWith('/api/meetings/')) return response(f.sessions.find(row => row.id === path.split('/')[3]));
  if (path === '/api/translate') {
    if (body.persist === false) f.previewTranslations.push(body);
    else f.translations.push(body);
    const record = f.sessions.find(row => row.id === body.meeting_id);
    if (body.persist !== false) {
      record?.translations.push({original: body.text, translation: '译文', source_at: body.source_at});
    }
    return response({translation: `译文：${body.text}`});
  }
  if (path === '/api/answer/stream') {
    f.answers.push(body);
    return {ok: true, status: 200, body: new ReadableStream({start(controller) {
      setTimeout(() => {
        f.sockets.at(-1).emit({type: 'transcript', text: 'New speech while AI is generating.'});
        if (!f.emptyAnswer) {
          controller.enqueue(new TextEncoder().encode('data: {"type":"token","content":"This is the visible answer."}\n\n'));
          f.sessions.find(row => row.id === body.meeting_id)?.answers.push({...body, answer: 'This is the visible answer.'});
        }
        controller.enqueue(new TextEncoder().encode('data: {"type":"done"}\n\n')); controller.close();
      }, 100);
    }})};
  }
  return response({});
};
class FlowWebSocket {
  static OPEN = 1;
  constructor() {
    this.readyState = 0; f.sockets.push(this);
    setTimeout(() => {
      this.readyState = 1; this.onopen?.();
      this.emit({type: 'status', message: 'Loading speech model'});
      setTimeout(() => {
        this.emit({type: 'status', message: 'Listening'});
        this.emit({type: 'transcript', text: 'This is'});
        setTimeout(() => {
          this.emit({type: 'transcript', text: 'a complete sentence.'});
          this.emit({type: 'transcript-boundary', end_of_utterance: true});
        }, 500);
      }, 100);
    }, 0);
  }
  emit(message) {
    if (this.readyState !== 1) return;
    if (message.type === 'transcript') {
      message.source_at = new Date().toISOString();
      const record = f.sessions.find(row => row.id === f.active);
      if (record) { record.transcript += message.text; record.entries.push({text: message.text, created_at: message.source_at}); }
    }
    this.onmessage?.({data: JSON.stringify(message)});
  }
  send(value) {
    if (JSON.parse(value).type !== 'stop') return;
    f.stopCount++;
    setTimeout(() => {
      this.emit({type: 'transcript', text: 'These are the final words.'});
      this.emit({type: 'stopped'}); this.readyState = 3; this.onclose?.();
    }, 5);
  }
}
window.WebSocket = FlowWebSocket;
