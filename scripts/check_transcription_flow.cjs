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
