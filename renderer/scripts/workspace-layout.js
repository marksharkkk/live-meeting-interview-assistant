function setWorkspaceView(view) {
  const answer = view === 'interview';
  document.getElementById('summary-panel').hidden = answer;
  document.getElementById('answer-panel').hidden = !answer;
  for (const name of ['meeting', 'interview']) {
    const selected = (name === 'interview') === answer;
    const button = document.getElementById(`view-${name}`);
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', String(selected));
  }
  window.electronAPI.setWorkspaceView('meeting');
}
document.getElementById('view-meeting').addEventListener('click', () => setWorkspaceView('meeting'));
document.getElementById('view-interview').addEventListener('click', () => setWorkspaceView('interview'));
document.getElementById('toggle-ai').addEventListener('click', () => setTranscriptFocused(!isTranscriptFocused));
document.getElementById('btn-maximize').addEventListener('click', () => window.electronAPI.maximizeTeleprompter());
