let sessionActionBusy = false;
let workbenchReady = false;
function syncSessionControls() {
  const el = id => document.getElementById(id);
  const transitioning = sessionActionBusy || isConnecting || Boolean(stopPromise);
  el('meeting-start').disabled = !workbenchReady || Boolean(activeMeetingId) || transitioning || isGenerating;
  el('btn-start').disabled = !activeMeetingId || transitioning;
  el('btn-start').textContent = stopPromise ? '正在收齐最后一句…' : isConnecting ? '正在准备识别…' : isListening ? '暂停' : '继续';
  el('meeting-finish').disabled = !activeMeetingId || transitioning;
  el('meeting-title').disabled = Boolean(activeMeetingId) || transitioning;
  el('meeting-audio').disabled = Boolean(activeMeetingId) || transitioning;
  el('capture-device').disabled = isListening || transitioning;
  el('capture-language').disabled = isListening || transitioning;
  el('meeting-live').disabled = !activeMeetingId;
  // The API deliberately rejects a full summary while the source session is
  // active. Live summary remains available through its separate checkbox.
  el('meeting-summary').disabled = !workbenchReady || !el('meeting-select').value
    || Boolean(activeMeetingId) || transitioning || isGenerating;
}

(() => {
  const el = name => document.getElementById(`meeting-${name}`);
  const field = id => document.getElementById(id);
  let busy = false;
  let lastSource = '';
  let recordView = 'summary';
  let refreshGeneration = 0;
  const request = async (path, options = {}) => responseJson(await apiFetch(path, options));
  const status = text => { el('status').textContent = text; };
  function selected() {
    if (!el('select').value) throw Error('请先选择一场记录');
    return el('select').value;
  }
  function sourceNote(record, kind) {
    const meta = record[`${kind}_source`];
    field('summary-source').textContent = meta?.source_at ? `总结依据：截至 ${new Date(meta.source_at).toLocaleString()} 的全部原文` : '';
  }
  function formatRecord(record, kind) {
    if (kind === 'original') return record.transcript || '暂无原始记录';
    if (kind === 'bilingual') return (record.translations || []).map(row =>
      `[${row.source_at ? new Date(row.source_at).toLocaleTimeString() : ''}]\n原文：${row.original}\n译文：${row.translation}`).join('\n\n') || '暂无译文；翻译完成后可刷新查看';
    if (kind === 'answers') return (record.answers || []).map(row =>
      `[问题时间：${row.source_at ? new Date(row.source_at).toLocaleTimeString() : ''}]\n问题：${row.question}\n答案：${row.answer}`).join('\n\n') || '本场尚无问答';
    return record.summary || record['live-summary'] || '尚未生成总结';
  }
  async function showRecord() {
    const generation = ++refreshGeneration;
    const id = el('select').value;
    if (!id) { el('content').textContent = '暂无场次记录'; return; }
    const record = await request(`/api/meetings/${id}`);
    if (generation !== refreshGeneration || el('select').value !== id) return;
    el('content').textContent = formatRecord(record, recordView);
    field('summary-source').textContent = '';
    if (recordView === 'summary') sourceNote(record, record.summary ? 'summary' : 'live-summary');
    el('tracks').replaceChildren();
    for (const track of record.tracks) {
      const button = document.createElement('button');
      button.textContent = `下载录音 ${track.file}`;
      button.disabled = id === activeMeetingId;
      button.addEventListener('click', () => download(id, track.file).catch(error => status(error.message)));
      el('tracks').append(button);
    }
  }
  async function refresh(preferred) {
    const previous = preferred || el('select').value;
    const data = await request('/api/meetings');
    activeMeetingId = data.active_id;
    el('select').replaceChildren();
    for (const meeting of data.meetings) {
      const option = document.createElement('option');
      option.value = meeting.id;
      option.textContent = `${meeting.title} · ${new Date(meeting.started_at).toLocaleString()}${meeting.id === activeMeetingId ? '（进行中）' : ''}`;
      el('select').append(option);
    }
    if (data.meetings.some(row => row.id === previous)) el('select').value = previous;
    syncSessionControls();
    await showRecord();
  }
  function saveBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  async function download(id, filename) {
    const response = await apiFetch(`/api/meetings/${id}/files/${filename}`);
    if (!response.ok) throw Error('文件尚未生成，请结束记录或完成总结后重试');
    saveBlob(await response.blob(), `${id}-${filename}`);
  }
  async function downloadText(kind) {
    const id = selected();
    const record = await request(`/api/meetings/${id}`);
    saveBlob(new Blob([formatRecord(record, kind)], {type: 'text/plain;charset=utf-8'}), `${id}-${kind}.txt`);
  }
  async function summarize(id, live = false) {
    if (busy) return;
    busy = true;
    el('summary').disabled = true;
    status(live ? '正在更新实时总结，录音继续…' : '正在读取全部原始记录生成完整总结…');
    try {
      const result = await request(`/api/meetings/${id}/summary?live=${live}`, {method: 'POST'});
      if (el('select').value === id && recordView === 'summary') {
        el('content').textContent = result.summary;
        field('summary-source').textContent = `总结依据：截至 ${result.source_at ? new Date(result.source_at).toLocaleString() : '本次请求'} 的全部原文`;
      }
      status(`${live ? '实时' : '完整'}总结已保存，处理 ${result.source_characters} 字符原文。`);
      return true;
    } finally { busy = false; syncSessionControls(); }
  }
  async function saveCaptureSettings() {
    await request('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
      voice_input_device: Number(field('capture-device').value), voice_language: field('capture-language').value,
    })});
    field('capture-description').textContent = `${field('capture-device').selectedOptions[0]?.textContent} · ${field('capture-language').selectedOptions[0]?.textContent}`;
  }
  function action(name, fn, session = false) {
    el(name).addEventListener('click', async () => {
      if (session && sessionActionBusy) return;
      if (session) sessionActionBusy = true;
      el(name).disabled = true;
      syncSessionControls();
      try { await fn(); } catch (error) { status(error.message); }
      finally { if (session) sessionActionBusy = false; el(name).disabled = false; syncSessionControls(); }
    });
  }
  action('start', async () => {
    if (activeMeetingId || isGenerating) return;
    await saveCaptureSettings();
    const record = await request('/api/meetings', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: el('title').value, record_audio: el('audio').checked})});
    activeMeetingId = currentMeetingId = record.id;
    lastSource = ''; transcriptChunks = []; meetingTranscript = []; meetingRecords = [];
    conversationHistory = []; currentQuestion = ''; lastAnswer = ''; qaIndex = 0;
    cancelTranslationPreview();
    pendingSentence = null; selectedTranscript = ''; selectedSourceAt = null; draftSourceAt = null;
    answerMeetingId = null; translationGeneration += 1; translationQueue = [];
    field('question-draft').value = ''; elements.transcriptContainer.replaceChildren(); clearCurrentAnswer();
    await startListening();
    await refresh(record.id);
    status('已建立本场记录，原始文字自动保存；可按需开启实时总结或回答辅助。');
  }, true);
  field('btn-start').addEventListener('click', async () => {
    if (!activeMeetingId || sessionActionBusy) return;
    sessionActionBusy = true; syncSessionControls();
    try {
      if (isListening || isConnecting) {
        await stopListening(false); updateStatus('paused', '已暂停，录音和转录暂停');
      } else {
        await saveCaptureSettings(); await startListening();
      }
    } catch (error) { status(error.message); }
    finally { sessionActionBusy = false; syncSessionControls(); }
  });
  action('finish', async () => {
    if (!activeMeetingId) return;
    status('正在收齐最后一段转录并保存录音…');
    await stopListening(false);
    finalizePendingSentence();
    const record = await request('/api/meetings/finish', {method: 'POST'});
    activeMeetingId = null; el('live').checked = false;
    await refresh(record.id);
    updateStatus('', '本场已结束并保存');
    status('本场已保存。译文和问答完成后仍归入本场；可点击“生成完整总结”。');
  }, true);
  action('refresh', () => refresh());
  action('summary', () => { recordView = 'summary'; return summarize(selected()); });
  for (const kind of ['original', 'bilingual', 'answers']) action(kind, async () => { recordView = kind; await showRecord(); });
  action('download', () => download(selected(), 'transcript.txt'));
  action('save-summary', () => download(selected(), 'summary.txt'));
  action('save-bilingual', () => downloadText('bilingual'));
  action('save-answers', () => downloadText('answers'));
  el('select').addEventListener('change', () => { recordView = 'summary'; showRecord().catch(error => status(error.message)); });
  for (const id of ['capture-device', 'capture-language']) field(id).addEventListener('change', () => saveCaptureSettings().catch(error => status(error.message)));
  for (const id of ['translation-target', 'answer-language']) {
    const value = localStorage.getItem(`workbench.${id}`);
    if ([...field(id).options].some(option => option.value === value)) field(id).value = value;
    field(id).addEventListener('change', () => localStorage.setItem(`workbench.${id}`, field(id).value));
  }
  setInterval(async () => {
    if (!activeMeetingId || !el('live').checked || busy) return;
    const id = activeMeetingId;
    try {
      const record = await request(`/api/meetings/${id}`);
      if (!record.transcript || record.transcript === lastSource) return;
      if (await summarize(id, true)) lastSource = record.transcript;
    } catch (error) { status(`实时总结失败：${error.message}；原始记录继续保存`); }
  }, 60000);
  window.workbenchInitialization = (async () => {
    const [settings, audio] = await Promise.all([request('/api/settings'), request('/api/audio/devices')]);
    for (const device of audio.devices || []) {
      const option = document.createElement('option'); option.value = String(device.index); option.textContent = device.name; field('capture-device').append(option);
    }
    field('capture-device').value = String(settings.voice_input_device ?? -1);
    if (!field('capture-device').value) field('capture-device').value = '-1';
    if (![...field('capture-language').options].some(option => option.value === settings.voice_language)) {
      const option = document.createElement('option'); option.value = settings.voice_language || 'auto'; option.textContent = settings.voice_language || '自动检测'; field('capture-language').append(option);
    }
    field('capture-language').value = settings.voice_language || 'auto';
    field('capture-description').textContent = `${field('capture-device').selectedOptions[0]?.textContent} · ${field('capture-language').selectedOptions[0]?.textContent}`;
    await refresh();
    if (activeMeetingId) {
      currentMeetingId = activeMeetingId;
      const record = await request(`/api/meetings/${activeMeetingId}`);
      el('title').value = record.title; el('audio').checked = record.record_audio;
      transcriptChunks = (record.entries || []).map(row => ({text: row.text, source_at: row.created_at}));
      for (const row of transcriptChunks) {
        const rendered = addTranscript(row.text, true);
        rendered.translationElement.textContent = '已恢复原始记录，译文可在本场中英对照中查看';
      }
      status('已恢复进行中的场次。点击“继续”恢复监听；原文、译文和问答可按场次查看。');
    }
    workbenchReady = true; syncSessionControls();
  })().catch(error => status(`工作台初始化失败：${error.message}，请重新打开窗口`));
})();
