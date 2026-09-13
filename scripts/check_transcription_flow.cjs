const {app, BrowserWindow} = require('electron');
const fs = require('fs'); const os = require('os'); const path = require('path');
app.whenReady().then(async () => {
  const output = fs.mkdtempSync(path.join(os.tmpdir(), 'meeting-flow-'));
  const log = value => fs.appendFileSync(path.join(output, 'results.log'), value + '\n');
  const win = new BrowserWindow({show: false, width: 1120, height: 780,
    webPreferences: {preload: path.join(__dirname, 'flow_preload.cjs'), contextIsolation: false, sandbox: false}});
  const run = code => win.webContents.executeJavaScript(code);
  const waitFor = async condition => {
    for (let i = 0; i < 80; i++) {
      if (await run(condition)) return;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw Error(`Timed out: ${condition}`);
  };
  const check = async (condition, message) => { if (!(await run(condition))) throw Error(message); log(message + ': PASS'); };
  try {
    await win.loadFile(path.join(__dirname, '../renderer/teleprompter.html'));
    await run('window.workbenchInitialization');
    await run("document.getElementById('meeting-start').click()");
    await waitFor('isListening && __flow.translations.length > 0');
    await check("__flow.sessions.length === 1 && __flow.previewTranslations.length > 0 && __flow.translations[0].text === 'This is a complete sentence.'", 'Preview is replaceable and only the complete sentence is saved as translation');
    await run("Array.from({length: 9}, (_, i) => `Continuous speech block ${i} contains an important idea without a pause`).forEach(text => __flow.sockets[0].emit({type: 'transcript', text})); __flow.sockets[0].emit({type: 'transcript-boundary', end_of_utterance: true})");
    await waitFor("__flow.translations.slice(1).map(row => row.text).join(' ').includes('block 8')");
    await check("__flow.translations.slice(1).length >= 3 && __flow.translations.slice(1).every(row => row.text.length <= MAX_TRANSLATION_CHARS) && __flow.translations.slice(1).map(row => row.text).join(' ').includes('block 0') && __flow.sessions[0].transcript.includes('block 8')", 'Continuous speech is translated in bounded sequential parts without dropping original words');
    await run("__flow.sockets[0].emit({type: 'transcript', text: Array.from({length: 20}, (_, i) => `very long recognition part ${i}`).join(' ')}); __flow.sockets[0].emit({type: 'transcript-boundary', end_of_utterance: true})");
    await waitFor("__flow.translations.map(row => row.text).join(' ').includes('part 19')");
    await check("__flow.translations.every(row => row.text.length <= MAX_TRANSLATION_CHARS)", 'One long recognition chunk is split before translation');
    await run("setWorkspaceView('interview'); document.getElementById('question-draft').value = 'What is your experience?'; setTranscriptFocused(true); generateAnswer()");
    await check("isListening && __flow.stopCount === 0 && transcriptChunks.some(row => row.text.includes('New speech'))", 'AI receives new speech without stopping recording');
    await check("isTranscriptFocused && elements.currentAnswer.textContent === 'This is the visible answer.'", 'Answer remains available without interrupting fullscreen subtitles');
    await check("__flow.answers[0].question === 'What is your experience?' && __flow.answers[0].meeting_id === currentMeetingId && __flow.answers[0].answer_language === 'auto'", 'Answer uses fixed question, session and language');
    await run("setTranscriptFocused(false); setWorkspaceView('interview')");
    await check("document.querySelector('.current-answer-section').getClientRects().length > 0", 'Answer can be revealed');
    await run("document.getElementById('btn-clear').click()");
    await check("isListening && __flow.stopCount === 0 && transcriptChunks.length > 0 && elements.currentAnswer.textContent.length > 0", 'Clearing display preserves originals, answer and recording');
    await run("document.getElementById('meeting-live').checked = true; __flow.timers[0]()");
    await check('__flow.summaries === 1 && isListening', 'Live summary leaves audio running');
    await run("document.getElementById('btn-start').click()");
    await waitFor('!isListening && !stopPromise && !sessionActionBusy');
    await check('__flow.stopCount === 1 && activeMeetingId !== null', 'Pause retains active session');
    await run("document.getElementById('btn-start').click()");
    await waitFor('isListening && __flow.sockets.length === 2');
    await check('__flow.sessions.length === 1 && elements.currentAnswer.textContent.length > 0', 'Resume keeps the same session and answer');
    await run("document.getElementById('meeting-finish').click()");
    await waitFor('!activeMeetingId && !sessionActionBusy');
    await check("__flow.sessions[0].transcript.includes('These are the final words.')", 'Finish includes the final audio chunk');
    await run("document.getElementById('meeting-start').click()");
    await waitFor('isListening && __flow.sessions.length === 2');
    await check("conversationHistory.length === 0 && __flow.sessions[0].answers.length === 1 && __flow.sessions[1].answers.length === 0", 'New session does not inherit previous questions');
    await run("__flow.emptyAnswer = true; document.getElementById('question-draft').value = 'Another question'; generateAnswer()");
    await check("elements.currentAnswer.textContent.includes('模型没有返回答案正文') && isListening", 'Empty AI output reports failure without stopping recording');
    await run("document.getElementById('meeting-finish').click()");
    await waitFor('!activeMeetingId && !sessionActionBusy');
    log('PASS');
  } catch (error) { log(String(error.stack || error)); process.exitCode = 1; }
  finally { win.destroy(); app.quit(); }
});
