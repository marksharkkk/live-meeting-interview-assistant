const apiConfigPromise = window.electronAPI.getApiConfig();

const elements = {
  apiKey: document.getElementById('api-key'),
  apiBase: document.getElementById('api-base'),
  model: document.getElementById('model'),
  saveApiSettings: document.getElementById('save-api-settings'),
  testApi: document.getElementById('test-api'),
  kbUpload: document.getElementById('kb-upload'),
  uploadKb: document.getElementById('upload-kb'),
  kbText: document.getElementById('kb-text'),
  addKbText: document.getElementById('add-kb-text'),
  clearKb: document.getElementById('clear-kb'),
  refreshKb: document.getElementById('refresh-kb'),
  cleanupKb: document.getElementById('cleanup-kb'),
  reloadKb: document.getElementById('reload-kb'),
  kbStatus: document.getElementById('kb-status'),
  kbFileList: document.getElementById('kb-file-list'),
  voiceDevice: document.getElementById('voice-device'),
  voiceLanguage: document.getElementById('voice-language'),
  saveVoiceSettings: document.getElementById('save-voice-settings'),
  showTeleprompter: document.getElementById('show-teleprompter'),
  notification: document.getElementById('notification'),
};

let apiKeyConfigured = false;
let notificationTimer = null;

async function apiFetch(path, options = {}) {
  const config = await apiConfigPromise;
  const headers = new Headers(options.headers || {});
  headers.set('X-Meeting-Assistant-Token', config.token);
  return fetch(`${config.baseUrl}${path}`, { ...options, headers });
}

async function responseJson(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || data.error || `请求失败（${response.status}）`);
  }
  return data;
}

async function runWithButtonDisabled(button, action) {
  if (button.disabled) return;
  button.disabled = true;
  try {
    await action();
  } finally {
    button.disabled = false;
  }
}

function showNotification(message, isError = false) {
  clearTimeout(notificationTimer);
  elements.notification.textContent = message;
  elements.notification.classList.remove('hidden', 'error');
  if (isError) elements.notification.classList.add('error');
  notificationTimer = setTimeout(() => elements.notification.classList.add('hidden'), 4500);
}

document.querySelectorAll('.preset-btn').forEach((button) => {
  button.addEventListener('click', () => {
    elements.apiBase.value = button.dataset.base;
    elements.model.value = button.dataset.model;
    document.querySelectorAll('.preset-btn').forEach((item) => item.classList.remove('active'));
    button.classList.add('active');
  });
});

async function loadSettings() {
  try {
    const settings = await responseJson(await apiFetch('/api/settings'));
    apiKeyConfigured = settings.api_key_configured;
    elements.apiKey.value = '';
    elements.apiKey.placeholder = apiKeyConfigured
      ? '已配置；留空表示不修改'
      : '输入您的 API Key';
    elements.apiBase.value = settings.openai_api_base || '';
    elements.model.value = settings.openai_model || '';
    elements.voiceLanguage.value = settings.voice_language || 'zh-CN';
    await loadAudioDevices(settings.voice_input_device ?? -1);
  } catch (error) {
    showNotification(`设置加载失败：${error.message}`, true);
  }
}

async function loadAudioDevices(selectedIndex = -1) {
  const data = await responseJson(await apiFetch('/api/audio/devices'));
  elements.voiceDevice.replaceChildren();
  const automatic = document.createElement('option');
  automatic.value = '-1';
  automatic.textContent = '自动选择';
  elements.voiceDevice.appendChild(automatic);
  (data.devices || []).forEach((device) => {
    const option = document.createElement('option');
    option.value = String(device.index);
    option.textContent = device.name;
    elements.voiceDevice.appendChild(option);
  });
  elements.voiceDevice.value = String(selectedIndex ?? -1);
  if (elements.voiceDevice.value !== String(selectedIndex ?? -1)) {
    elements.voiceDevice.value = '-1';
  }
}

elements.saveApiSettings.addEventListener('click', () => runWithButtonDisabled(
  elements.saveApiSettings,
  async () => {
    const apiKey = elements.apiKey.value.trim();
    if (!apiKey && !apiKeyConfigured) {
      showNotification('请先输入 API Key', true);
      return;
    }
    const payload = {
      openai_api_base: elements.apiBase.value.trim(),
      openai_model: elements.model.value.trim(),
    };
    if (apiKey) payload.openai_api_key = apiKey;

    try {
      await responseJson(await apiFetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }));
      apiKeyConfigured = true;
      elements.apiKey.value = '';
      elements.apiKey.placeholder = '已配置；留空表示不修改';
      showNotification('API 设置已保存，重启后仍会保留');
    } catch (error) {
      showNotification(`保存失败：${error.message}`, true);
    }
  },
));

elements.testApi.addEventListener('click', () => runWithButtonDisabled(
  elements.testApi,
  async () => {
    try {
      await responseJson(await apiFetch('/api/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: 'Reply with the single word OK.',
          use_knowledge_base: false,
        }),
      }));
      showNotification('API 连接成功');
    } catch (error) {
      showNotification(`API 测试失败：${error.message}`, true);
    }
  },
));

function formatSize(bytes) {
  if (!bytes) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function createFileItem(file) {
  const item = document.createElement('div');
  item.className = 'kb-file-item';

  const info = document.createElement('div');
  info.className = 'kb-file-info';

  const name = document.createElement('span');
  name.className = 'kb-file-name';
  name.textContent = file.name;

  const size = document.createElement('span');
  size.className = 'kb-file-size';
  size.textContent = formatSize(file.size);

  const badge = document.createElement('span');
  badge.className = `kb-file-badge ${file.loaded ? 'loaded' : 'unloaded'}`;
  badge.textContent = file.loaded ? '✅ 已加载' : '⚠️ 未加载';

  const deleteButton = document.createElement('button');
  deleteButton.className = 'kb-delete-btn';
  deleteButton.type = 'button';
  deleteButton.title = '删除';
  deleteButton.textContent = '🗑️';
  deleteButton.addEventListener('click', () => deleteKnowledgeFile(file.name, deleteButton));

  info.append(name, size, badge);
  item.append(info, deleteButton);
  return item;
}

async function loadKnowledgeBase() {
  try {
    const data = await responseJson(await apiFetch('/api/knowledge'));
    elements.kbStatus.replaceChildren();
    const count = document.createElement('strong');
    count.textContent = String(data.document_count);
    elements.kbStatus.append(count, ' 个文本块已加载');

    elements.kbFileList.replaceChildren();
    if (!data.files?.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-hint';
      empty.textContent = '暂无上传文件';
      elements.kbFileList.appendChild(empty);
      return;
    }
    data.files.forEach((file) => elements.kbFileList.appendChild(createFileItem(file)));
  } catch (error) {
    elements.kbStatus.textContent = '加载失败';
    showNotification(`知识库加载失败：${error.message}`, true);
  }
}

async function deleteKnowledgeFile(filename, button) {
  if (!confirm(`确定删除“${filename}”吗？`)) return;
  await runWithButtonDisabled(button, async () => {
    try {
      await responseJson(await apiFetch(`/api/knowledge/files/${encodeURIComponent(filename)}`, {
        method: 'DELETE',
      }));
      showNotification(`已删除 ${filename}`);
      await loadKnowledgeBase();
    } catch (error) {
      showNotification(`删除失败：${error.message}`, true);
    }
  });
}

elements.uploadKb.addEventListener('click', () => runWithButtonDisabled(
  elements.uploadKb,
  async () => {
    const files = [...elements.kbUpload.files];
    if (!files.length) {
      showNotification('请先选择文件', true);
      return;
    }
    if (files.some((file) => file.size > 25 * 1024 * 1024)) {
      showNotification('单个文件不能超过 25 MB', true);
      return;
    }

    const formData = new FormData();
    files.forEach((file) => formData.append('files', file));
    try {
      const result = await responseJson(await apiFetch('/api/knowledge/files', {
        method: 'POST',
        body: formData,
      }));
      const failures = result.files?.filter((file) => file.status === 'failed') || [];
      showNotification(failures.length ? `${result.message}；${failures.length} 个文件失败` : result.message, Boolean(failures.length));
      elements.kbUpload.value = '';
      await loadKnowledgeBase();
    } catch (error) {
      showNotification(`上传失败：${error.message}`, true);
    }
  },
));

elements.addKbText.addEventListener('click', () => runWithButtonDisabled(
  elements.addKbText,
  async () => {
    const text = elements.kbText.value.trim();
    if (!text) {
      showNotification('请输入文本内容', true);
      return;
    }
    try {
      await responseJson(await apiFetch('/api/knowledge/text', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      }));
      elements.kbText.value = '';
      showNotification('文本已添加到知识库');
      await loadKnowledgeBase();
    } catch (error) {
      showNotification(`添加失败：${error.message}`, true);
    }
  },
));

elements.clearKb.addEventListener('click', () => runWithButtonDisabled(
  elements.clearKb,
  async () => {
    if (!confirm('确定清空知识库及全部上传文件吗？此操作不可撤销。')) return;
    try {
      await responseJson(await apiFetch('/api/knowledge', { method: 'DELETE' }));
      showNotification('知识库及上传文件已清空');
      await loadKnowledgeBase();
    } catch (error) {
      showNotification(`清空失败：${error.message}`, true);
    }
  },
));

elements.cleanupKb.addEventListener('click', () => runWithButtonDisabled(
  elements.cleanupKb,
  async () => {
    try {
      const result = await responseJson(await apiFetch('/api/knowledge/cleanup', { method: 'POST' }));
      showNotification(result.message);
      await loadKnowledgeBase();
    } catch (error) {
      showNotification(`清理失败：${error.message}`, true);
    }
  },
));

elements.reloadKb.addEventListener('click', () => runWithButtonDisabled(
  elements.reloadKb,
  async () => {
    try {
      const result = await responseJson(await apiFetch('/api/knowledge/reload', { method: 'POST' }));
      showNotification(result.message);
      await loadKnowledgeBase();
    } catch (error) {
      showNotification(`重新加载失败：${error.message}`, true);
    }
  },
));

elements.saveVoiceSettings.addEventListener('click', () => runWithButtonDisabled(
  elements.saveVoiceSettings,
  async () => {
    try {
      await responseJson(await apiFetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          voice_language: elements.voiceLanguage.value,
          voice_input_device: Number(elements.voiceDevice.value),
        }),
      }));
      showNotification('语音设置已保存');
    } catch (error) {
      showNotification(`保存失败：${error.message}`, true);
    }
  },
));

elements.refreshKb.addEventListener('click', loadKnowledgeBase);
elements.showTeleprompter.addEventListener('click', () => window.electronAPI.showTeleprompter());

Promise.all([loadSettings(), loadKnowledgeBase()]).catch((error) => {
  showNotification(`初始化失败：${error.message}`, true);
});
