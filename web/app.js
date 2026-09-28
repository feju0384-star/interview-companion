'use strict';
const $ = (id) => document.getElementById(id);
const emptyTemplate = $('answers').firstElementChild.cloneNode(true);
let settings = {}, state = {}, items = [], transcripts = [];
let socket, retryTimer, pingTimer, lastPong = 0, reconnectAttempt = 0, connected = false;
let token = '', active = true, toastTimer, uiError = '', networkError = '';
let devices = [], deviceRefresh = null, deviceSignature = '', switchingSource = false;
let codexModels = [], codexModelsLoaded = false, codexModelsRequest = null;
let fontSize = Math.min(30, Math.max(14, Number(localStorage.getItem('answerFontSize')) || 18));
function applyFont() {
  // All dynamic presentation uses CSSOM on a local stylesheet, compatible with the CSP.
  const sheet = [...document.styleSheets].find((sheet) => sheet.href?.endsWith('/static/style.css'));
  if (sheet) {
    if (applyFont.ruleIndex !== undefined) sheet.deleteRule(applyFont.ruleIndex);
    applyFont.ruleIndex = sheet.insertRule(`:root { --answer-size: ${fontSize}px; }`, sheet.cssRules.length);
  }
  localStorage.setItem('answerFontSize', String(fontSize));
}
function toast(message) {
  clearTimeout(toastTimer); $('toast').textContent = message; $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4000);
}
function notice(message) { $('notice').textContent = message || ''; $('notice').hidden = !message; }
async function api(path, data) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(`/api/${path}`, {
      method: data === undefined ? 'GET' : 'POST',
      headers: {...(data === undefined ? {} : {'Content-Type': 'application/json'}), Authorization: `Bearer ${token}`},
      body: data === undefined ? undefined : JSON.stringify(data), signal: controller.signal,
    });
    const result = await response.json();
    if (!response.ok) { const error = new Error(result.detail || `请求失败（${response.status}）`); error.status = response.status; throw error; }
    return result;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('电脑服务响应超时，请检查后台程序是否运行');
    throw error;
  } finally { clearTimeout(timeout); }
}
function connectionStatus(status, text) {
  $('connection').className = `connection ${status}`; $('connection').textContent = text;
}
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function clockTime(value) {
  return new Date(value).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'});
}
function renderItem(item) {
  let card = document.getElementById(`answer-${item.id}`);
  if (!card) {
    if ($('answers').querySelector('.empty-state')) $('answers').replaceChildren();
    card = element('article', 'answer-card'); card.id = `answer-${item.id}`;
    const head = element('div', 'question-head');
    head.append(element('span', 'tag', item.demo ? '演示 · 固定示例' : '面试问题'), element('time', '', clockTime(item.at)));
    const title = element('h3', '', item.question);
    const body = element('div', 'answer-body');
    const error = element('div', 'answer-error'); error.hidden = true;
    const meta = element('div', 'answer-meta');
    meta.append(element('span', 'answer-status'));
    const copy = element('button', 'text-button', '复制'); copy.type = 'button';
    copy.addEventListener('click', () => copyText(card.querySelector('.answer-body').textContent));
    meta.append(copy); card.append(head, title, body, error, meta);
    if (!item.demo && item.source !== 'screen') {
      const practice = element('button', 'text-button', '练习这道题 →'); practice.type = 'button';
      practice.onclick = () => { practiceSelection = item.id; selectPane('practice'); renderPractice(); };
      card.append(practice);
    }
    $('answers').prepend(card);
  }
  const body = card.querySelector('.answer-body');
  body.textContent = item.answer || (item.status === 'ready' ? '这是一道练习题。请在“练习复盘”中先说出你的回答。' : item.status === 'collecting' ? '正在听完整问题…' : item.status === 'generating' ? '正在回答…' : '尚未收到回答');
  body.classList.toggle('generating', item.status === 'generating');
  const error = card.querySelector('.answer-error'); error.hidden = !item.error; error.textContent = item.error || '';
  const names = {ready:'等待你的回答', collecting:'等待说完 · 续句会自动合并', generating:'正在生成', done:'生成完成', cancelled:'已停止 · 保留部分内容', error:'生成未完成'};
  let meta = names[item.status] || item.status;
  card.querySelector('.question-head .tag').textContent = item.demo ? '演示 · 固定示例' : item.source === 'screen' ? '屏幕解题' : item.fragments > 1 ? `面试问题 · 已合并 ${item.fragments} 段` : '面试问题';
  card.querySelector('h3').textContent = item.question;
  if (item.first_token_ms !== null) meta += ` · 首字 ${(item.first_token_ms / 1000).toFixed(1)} 秒`;
  card.querySelector('.answer-status').textContent = meta;
  while ($('answers').children.length > 40) $('answers').lastElementChild.remove();
}
function renderAll() {
  $('answers').replaceChildren();
  if (!items.length) $('answers').append(emptyTemplate.cloneNode(true));
  items.forEach(renderItem); renderCount();
}
function renderCount() {
  $('answer-count').textContent = String(items.length).padStart(2, '0');
}
function renderTranscripts() {
  $('transcripts').replaceChildren();
  if (!transcripts.length) $('transcripts').append(element('p', 'muted small', '完成语音识别后显示。可点选一条内容，修改后重新回答。'));
  [...transcripts].reverse().forEach((entry) => {
    const button = element('button', 'transcript-entry');
    button.append(element('time', '', clockTime(entry.at)), document.createTextNode(entry.text));
    button.addEventListener('click', () => { selectPane('answers'); $('question').value = entry.text; $('question').focus(); });
    $('transcripts').append(button);
  });
}
function renderState() {
  const collecting = items.some((item) => item.status === 'collecting');
  const generating = collecting || items.some((item) => item.status === 'generating');
  const context = state.context || {};
  const compacting = !!context.compacting;
  const practice = state.practice || {};
  const practicing = practice.recording || practice.processing || practice.reviewing;
  $('asr-timing').textContent = state.asr_ms == null ? '识别耗时会在收到结果后显示' : `最近音频片段识别 ${(state.asr_ms / 1000).toFixed(1)} 秒（含上传）`;
  $('capture-dot').classList.toggle('live', !!state.listening);
  $('capture-title').textContent = state.listening ? (state.transcribing ? '正在识别问题' : '正在接收系统声音') : state.screen_capturing ? '正在截取电脑屏幕' : (collecting ? '正在听完整问题' : generating ? '正在生成回答' : state.screen_monitoring ? '正在后台监测题目' : state.screen_hotkey ? '屏幕快捷键已启用' : '电脑已就绪');
  {
    $('capture-state').textContent = state.listening ? '监听中' : state.screen_monitoring ? '自动监测中' : state.screen_hotkey ? '快捷键就绪' : generating ? '回答中' : '未开始';
    $('start').hidden = state.listening || generating || compacting || state.screen_hotkey || state.screen_capturing || state.screen_monitoring;
    $('start').disabled = !connected || compacting;
    $('stop').hidden = !state.listening && !generating && !compacting && !state.screen_hotkey && !state.screen_capturing && !state.screen_monitoring;
    $('stop').disabled = !connected;
    $('ask-button').disabled = !connected || compacting || state.screen_monitoring;
    $('ask-form').hidden = !!state.screen_monitoring;
    if ($('demo')) $('demo').disabled = !connected || state.screen_monitoring;
    const level = state.level || 0;
    const db = level > 0 ? 20 * Math.log10(level) : -90;
    $('level').value = Math.max(0, Math.min(100, (db + 70) / 70 * 100));
    const threshold = settings.energy_threshold || 0.008;
    $('sound-hint').textContent = state.listening ? `${level > 0 ? db.toFixed(0) + ' dBFS' : '静音'} · ${level >= threshold ? '已达到检测阈值' : '低于检测阈值'}` : '开始监听后显示采集电平';
    $('audio-level-detail').textContent = `检测阈值 ${(20 * Math.log10(threshold)).toFixed(0)} dBFS · 采集增益 ${settings.audio_gain || 1} 倍`;
    const info = state.device_info || {};
    $('active-device').textContent = state.listening ? `正在采集：${state.device || '等待设备'}${info.channels ? ' · ' + info.channels + ' 声道' : ''}${state.follow_default ? '（自动跟随默认输出）' : '（固定来源）'}` : '选择默认输出可自动跟随电脑切换；选择具体设备则固定采集该来源。';
    $('device-notice').textContent = state.audio_notice || '';
    $('device-notice').hidden = !state.audio_notice;
    renderDevices();
    $('draft').hidden = !state.draft; $('draft').textContent = state.draft ? `正在听写：${state.draft}` : '';
    const codexMode = context.backend === 'codex';
    $('compact-context').hidden = !codexMode;
    $('compact-context').disabled = !connected || !context.active || state.listening || generating || compacting || state.screen_monitoring;
    $('compact-context').textContent = compacting ? '压缩中…' : '压缩上下文';
    $('new-context').disabled = !connected;
    $('context-label').textContent = codexMode ? 'CODEX · 独立会话' : context.backend === 'claude' ? 'CLAUDE CODE · 近期上下文' : 'API · 上下文';
    $('context-summary').textContent = codexMode ? (context.lost ? '会话已断开，需要新建' : compacting ? '正在整理历史对话' : context.active ? `已完成 ${context.turns || 0} 轮 · ${context.model || 'Codex'}` : '首个问题将建立新会话') : '近期问答与问题原文';
    const tokenDetail = context.context_tokens != null ? `最近请求 ${Number(context.context_tokens).toLocaleString()} tokens${context.context_window ? ' / 模型窗口 ' + Number(context.context_window).toLocaleString() : ''}。` : '';
    $('context-detail').textContent = codexMode ? `${tokenDetail}已压缩 ${context.compactions || 0} 次。拟答稿不代表你实际说过的话。` : '携带简历、近期问答和最近问题原文；未完成回答的问题也保留话题线索。';
  }
  for (const id of ['open-settings', 'clear', 'refresh-devices']) $(id).disabled = !connected;
  $('device').disabled = !connected || switchingSource;
  $('phone-status').textContent = !connected ? '正在连接电脑…' : state.listening ? '电脑正在监听，回答会同步到这里。' : state.screen_monitoring ? `后台正在监测题目 · 已自动提交 ${state.screen_monitor_count || 0} 次` : state.screen_hotkey ? '电脑按 Ctrl + Alt + S 截图，答案同步到手机。' : '可以监听声音、屏幕解题，也可以手动提问。';
  notice(networkError || uiError || state.error || '');
  if (practicing) {
    $('capture-dot').classList.add('live');
    $('capture-title').textContent = practice.recording ? '正在录制你的回答' : practice.processing ? '正在转写你的回答' : '正在生成面试复盘';
    $('capture-state').textContent = practice.recording ? '麦克风开启' : '处理中';
    $('start').hidden = true; $('stop').hidden = false;
    $('compact-context').disabled = true;
  }
  $('stop').textContent = practicing ? '■ 停止全部' : '■ 停止';
  renderScreenState();
  renderPractice();
}
function receive(event) {
  if (event.type === 'snapshot') {
    state = event.state; items = event.items; transcripts = event.transcripts;
    renderAll(); renderTranscripts(); renderState();
  } else if (event.type === 'status') { state = event.state; renderState(); }
  else if (event.type === 'item') {
    const index = items.findIndex((item) => item.id === event.item.id);
    if (index >= 0) items[index] = event.item; else items.push(event.item);
    items = items.slice(-40); renderItem(event.item); renderCount(); renderState();
    if (index < 0 && event.item.source === 'screen' && state.screen_monitoring && !$('answers-pane').hidden) {
      document.getElementById(`answer-${event.item.id}`)?.scrollIntoView({behavior:'smooth', block:'start'});
    }
  } else if (event.type === 'transcript') {
    transcripts.push(event.transcript); transcripts = transcripts.slice(-60); renderTranscripts();
  } else if (event.type === 'revoked') {
    revoke('电脑已更换连接链接，请重新扫码连接。');
  } else if (event.type === 'pong') { lastPong = Date.now(); }
}
function revoke(message) {
  active = false; connected = false; clearInterval(pingTimer); clearTimeout(retryTimer);
  sessionStorage.removeItem('controllerToken'); token = ''; socket?.close();
  state = {}; settings = {}; $('settings-dialog').close(); $('settings-form').reset();
  resetScreenRegion(); screenLoaded = false;
  practiceDrafts.clear(); practiceSelection = ''; microphonesLoaded = false;
  $('practice-answer').value = ''; $('practice-review-text').textContent = '';
  $('controller-content').hidden = true; $('phone-actions').hidden = true; $('open-settings').disabled = true;
  items = []; transcripts = []; renderAll();
  connectionStatus('error', '连接已失效'); notice(message);
  $('pair-needed').hidden = false; $('phone-status').textContent = '请重新连接电脑';
}
function connect() {
  if (!active || !token) return;
  clearTimeout(retryTimer);
  if (socket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) return;
  connectionStatus('', reconnectAttempt ? '重连中' : '连接中');
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
  socket = ws;
  ws.onopen = () => {
    ws.send(JSON.stringify({token})); lastPong = Date.now();
    clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (Date.now() - lastPong > 45000) { ws.close(); return; }
      if (ws.readyState === WebSocket.OPEN) ws.send('ping');
    }, 15000);
  };
  ws.onmessage = (message) => {
    let event;
    try { event = JSON.parse(message.data); } catch { return; }
    if (event.type === 'snapshot') {
      connected = true; reconnectAttempt = 0; networkError = ''; connectionStatus('online', '已连接');
      $('pair-needed').hidden = true;
    }
    receive(event);
  };
  ws.onclose = async (event) => {
    clearInterval(pingTimer); connected = false;
    if (!active) return;
    renderState(); connectionStatus('error', '连接中断');
    if (event.code === 1008) { revoke('连接码无效或电脑已重启，请重新扫描电脑上的二维码。'); return; }
    networkError = '与电脑后台的连接已断开，正在自动重连。请确认后台程序仍在运行。';
    notice(networkError);
    retryTimer = setTimeout(connect, Math.min(1000 * 2 ** reconnectAttempt++, 10000));
  };
  ws.onerror = () => { ws.close(); };
}
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
    else {
      const input = element('textarea'); input.value = text; input.className = 'copy-buffer';
      document.body.append(input); input.select();
      const success = document.execCommand('copy'); input.remove();
      if (!success) throw new Error();
    }
    toast('已复制');
  } catch { toast('浏览器不允许复制，请长按文字手动复制'); }
}
function fillSettings(data) {
  settings = data;
  const form = $('settings-form');
  renderCodexModels(data.codex_model || '');
  for (const [name, value] of Object.entries(data)) {
    const field = form.elements.namedItem(name); if (!field) continue;
    if (field.type === 'checkbox') field.checked = value; else field.value = value;
  }
  for (const prefix of ['llm', 'asr']) {
    const field = form.elements.namedItem(`${prefix}_api_key`); field.value = '';
    field.placeholder = data[`${prefix}_key_saved`] ? '已保存 · 留空保留原密钥' : '粘贴此服务的 API Key';
  }
  $('key-status').textContent = `${data.answer_backend === 'codex' ? '回答使用 Codex 登录' : data.answer_backend === 'claude' ? '回答使用本机 Claude Code 配置' : '回答密钥' + (data.llm_key_saved ? '已保存' : '未填写')} · 识别密钥${data.asr_key_saved ? '已保存' : '未填写'}`;
  showBackendFields();
}
function showBackendFields() {
  for (const backend of ['api', 'codex', 'claude']) {
    const selected = $('answer-backend').value === backend;
    $(`${backend}-fields`).hidden = !selected;
    $(`${backend}-fields`).querySelectorAll('input,select').forEach(field => { field.disabled = !selected; });
  }
}
async function refreshDevices() {
  if (deviceRefresh) return deviceRefresh;
  deviceRefresh = (async () => {
    const result = await api('devices');
    if (!active) return;
    devices = result.devices;
    renderDevices();
    $('device-refresh-status').textContent = '设备列表已更新 · 每 3 秒自动刷新';
  })();
  try { await deviceRefresh; } finally { deviceRefresh = null; }
}
function renderDevices() {
  if (switchingSource) return;
  const signature = JSON.stringify([devices, state.listening, state.device, state.follow_default]);
  if (signature === deviceSignature) return;
  deviceSignature = signature;
  const select = $('device');
  const previousName = select.selectedOptions[0]?.dataset.name;
  const selectedName = state.listening ? (state.follow_default ? null : state.device) : previousName;
  const defaultDevice = devices.find(device => device.default);
  const defaultOption = element('option', '', `跟随系统默认输出${defaultDevice ? ' · ' + defaultDevice.name : '（暂无可用默认设备）'}`);
  defaultOption.value = ''; select.replaceChildren(defaultOption);
  devices.forEach((device) => {
    const option = element('option', '', `${device.name}${device.channels ? ' · ' + device.channels + ' 声道' : ''}${device.default ? '（系统默认）' : ''}`);
    option.value = String(device.id); option.dataset.name = device.name; select.append(option);
  });
  if (selectedName) {
    const selected = [...select.options].find(option => option.dataset.name === selectedName);
    if (selected) select.value = selected.value;
    else {
      const unavailable = element('option', '', `${selectedName}（已不可用，请重新选择）`);
      unavailable.value = 'unavailable'; unavailable.dataset.name = selectedName;
      select.append(unavailable); select.value = unavailable.value;
    }
  }
  if ($('answer-backend').value === 'codex' && $('settings-dialog').open) loadCodexModels();
}
function renderCodexModels(selected = $('codex-model').value) {
  const select = $('codex-model');
  const defaultOption = element('option', '', '沿用 Codex 默认设置');
  defaultOption.value = ''; select.replaceChildren(defaultOption);
  for (const model of codexModels) {
    const option = element('option', '', `${model.name}${model.is_default ? '（Codex 推荐）' : ''}`);
    option.value = model.id; select.append(option);
  }
  // Never erase a saved/custom model merely because discovery failed or omitted it.
  if (selected && !codexModels.some(model => model.id === selected)) {
    const saved = element('option', '', `${selected}（${codexModelsLoaded ? '列表未返回，保留原选择' : '已保存'}）`);
    saved.value = selected; select.append(saved);
  }
  select.value = selected;
}
async function loadCodexModels(force = false) {
  if (codexModelsRequest) return codexModelsRequest;
  if (codexModelsLoaded && !force) return;
  $('refresh-codex-models').disabled = true;
  $('codex-model-status').textContent = '正在读取可选模型…';
  codexModelsRequest = (async () => {
    try {
      const result = await api('codex/models');
      if (!active) return;
      if (!Array.isArray(result.models)) throw new Error('模型列表格式异常，请刷新重试');
      codexModels = result.models; codexModelsLoaded = true;
      renderCodexModels();
      $('codex-model-status').textContent = codexModels.length ? `已读取 ${codexModels.length} 个模型；选择后保存生效。` : '未返回可选模型；已保留原选择，可检查登录后刷新。';
    } catch (error) {
      $('codex-model-status').textContent = `${error.message}；已保留原选择。`;
    } finally {
      $('refresh-codex-models').disabled = false;
    }
  })();
  try { await codexModelsRequest; } finally { codexModelsRequest = null; }
}
function selectedSource() {
  if ($('device').value === 'unavailable') throw new Error('原声音来源已不可用，请重新选择');
  return {device_id: $('device').value ? Number($('device').value) : null,
          device_name: $('device').selectedOptions[0]?.dataset.name || null};
}
function autoRefreshDevices() {
  if (document.hidden || !active || !token || !connected) return;
  refreshDevices().catch(error => { $('device-refresh-status').textContent = `刷新失败：${error.message}`; });
}
async function action(button, operation) {
  uiError = '';
  if (button) button.disabled = true;
  try { await operation(); } catch (error) { uiError = error.message || '操作失败，请重试'; }
  finally { if (button) button.disabled = false; renderState(); }
}
function setupControls() {
  setupScreen();
  setupPractice();
  $('open-settings').onclick = () => {
    $('settings-error').hidden = true; $('settings-dialog').showModal();
    if ($('answer-backend').value === 'codex') loadCodexModels();
  };
  $('close-settings').onclick = () => $('settings-dialog').close();
  $('answer-backend').onchange = showBackendFields;
  $('refresh-codex-models').onclick = () => loadCodexModels(true);
  $('asr-provider').onchange = () => {
    const form = $('settings-form');
    const minimax = $('asr-provider').value === 'minimax';
    form.elements.asr_base_url.value = minimax ? 'https://api.minimaxi.com/v1' : 'https://api.siliconflow.cn/v1';
    form.elements.asr_model.value = minimax ? 'asr-1.0' : 'FunAudioLLM/SenseVoiceSmall';
    form.elements.asr_api_key.value = '';
    form.elements.asr_api_key.placeholder = '切换服务后请填写对应密钥';
  };
  $('check-codex').onclick = async (event) => {
    const button = event.currentTarget; button.disabled = true; $('codex-status').textContent = '正在检查…';
    try {
      const result = await api('codex/status');
      $('codex-status').textContent = result.logged_in ? `已连接 · ${result.auth_type === 'chatgpt' ? 'ChatGPT 登录' : 'CLI 已认证'}` : result.error || '已安装但未登录，请运行 codex login';
      if (result.logged_in) await loadCodexModels(true);
    } catch(error) { $('codex-status').textContent = error.message; }
    finally { button.disabled = false; }
  };
  $('check-claude').onclick = async (event) => {
    const button = event.currentTarget; button.disabled = true; $('claude-status').textContent = '正在检查…';
    try {
      const result = await api('claude/status');
      $('claude-status').textContent = result.configured ? `${result.version} · ${result.model || '默认模型'} · ${result.base_url} · 已检测到凭据（尚未验证联网）` : '已安装，请在电脑 Claude Code 中配置 API Key / 认证令牌';
    } catch (error) { $('claude-status').textContent = error.message; }
    finally { button.disabled = false; }
  };
  $('settings-form').onsubmit = async (event) => {
    event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('button[type="submit"]');
    const {llm_key_saved, asr_key_saved, ...persisted} = settings;
    const data = {...persisted, ...Object.fromEntries(new FormData(form))};
    for (const name of ['silence_seconds', 'energy_threshold', 'audio_gain', 'max_tokens', 'codex_timeout_seconds', 'claude_timeout_seconds', 'asr_chunk_seconds', 'question_merge_seconds']) data[name] = Number(data[name]);
    data.codex_fast = form.elements.codex_fast.checked;
    data.claude_direct = form.elements.claude_direct.checked;
    data.auto_answer = form.elements.auto_answer.checked; button.disabled = true;
    try { fillSettings(await api('settings', data)); $('settings-dialog').close(); toast('设置已保存'); }
    catch (error) { $('settings-error').textContent = error.message; $('settings-error').hidden = false; }
    finally { button.disabled = false; }
  };
  $('start').onclick = (event) => action(event.currentTarget, async () => {
    const missingAnswer = settings.answer_backend === 'api' && (!settings.llm_key_saved || !settings.llm_model);
    if (missingAnswer || !settings.asr_key_saved) {
      $('settings-dialog').showModal(); toast('先填写模型和语音识别的设置，即可开始监听'); return;
    }
    await api('start', selectedSource());
  });
  $('device').onchange = async () => {
    if (!state.listening) return;
    switchingSource = true;
    await action($('device'), async () => {
      try { await api('source', selectedSource()); toast('已切换声音来源'); }
      finally { switchingSource = false; deviceSignature = ''; }
    });
  };
  $('stop').onclick = (event) => action(event.currentTarget, () => api('stop', {}));
  $('refresh-devices').onclick = (event) => action(event.currentTarget, refreshDevices);
  $('clear').onclick = (event) => action(event.currentTarget, () => api('clear', {}));
  $('new-context').onclick = (event) => action(event.currentTarget, async () => { await api('clear', {}); toast('已结束旧会话，下个问题将使用新上下文'); });
  $('compact-context').onclick = (event) => action(event.currentTarget, () => api('context/compact', {}));
  $('ask-form').onsubmit = (event) => {
    event.preventDefault(); const question = $('question').value.trim();
    if (!question) { toast('请先输入问题'); return; }
    action($('ask-button'), async () => { await api('ask', {question}); $('question').value = ''; });
  };
  document.addEventListener('click', (event) => {
    const button = event.target.closest('#demo'); if (button) action(button, () => api('demo', {}));
  });
}
function bootPhone() {
  const fragment = location.hash.slice(1);
  if (fragment) { sessionStorage.setItem('controllerToken', fragment); history.replaceState(null, '', location.pathname); }
  token = fragment || sessionStorage.getItem('controllerToken') || '';
  if (token) loadController();
  else { $('pair-needed').hidden = false; connectionStatus('', '尚未连接'); $('phone-status').textContent = '扫描电脑二维码，即可开始'; }
  $('pair-form').onsubmit = (event) => {
    event.preventDefault();
    try {
      const url = new URL($('pair-input').value.trim());
      if (!['http:', 'https:'].includes(url.protocol) || url.pathname !== '/phone' || !/^[A-Za-z0-9_-]{24,100}$/.test(url.hash.slice(1))) throw new Error();
      if (url.origin === location.origin) {
        sessionStorage.setItem('controllerToken', url.hash.slice(1));
        location.reload();
      } else location.assign(url.href);
    } catch { toast('请粘贴电脑上复制的完整手机连接链接'); }
  };
  $('go-latest').onclick = () => { selectPane('answers'); $('answers').scrollIntoView({behavior:'smooth', block:'start'}); };
  $('answers-tab').onclick = () => selectPane('answers');
  $('controls-tab').onclick = () => selectPane('controls');
  $('screen-tab').onclick = () => selectPane('screen');
  $('practice-tab').onclick = () => selectPane('practice');
  const tabs = ['answers', 'practice', 'screen', 'controls'];
  for (const name of tabs) $(`${name}-tab`).onkeydown = event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs.at(-1) : tabs[(tabs.indexOf(name) + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length]; selectPane(next); $(`${next}-tab`).focus(); } };
}
$('font-down').onclick = () => { fontSize = Math.max(14, fontSize - 1); applyFont(); };
$('font-up').onclick = () => { fontSize = Math.min(30, fontSize + 1); applyFont(); };
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && active && token) {
    autoRefreshDevices();
    if (socket?.readyState === WebSocket.OPEN && Date.now() - lastPong > 45000) socket.close();
    else if (!socket || socket.readyState === WebSocket.CLOSED) connect();
  }
});
applyFont();
setupControls();
bootPhone();
setInterval(autoRefreshDevices, 3000);

function selectPane(name) {
  if (name === 'controls') autoRefreshDevices();
  if (name === 'screen' && !screenLoaded && !screenLoading) screenAction($('screen-refresh'), refreshScreens);
  if (name === 'practice' && !microphonesLoaded && !practiceLoading) practiceAction(refreshMicrophones);
  for (const pane of ['answers', 'practice', 'screen', 'controls']) {
    $(`${pane}-pane`).hidden = pane !== name;
    $(`${pane}-tab`).setAttribute('aria-selected', String(pane === name));
    $(`${pane}-tab`).tabIndex = pane === name ? 0 : -1;
  }
}
async function loadController() {
  if (!token || !active) return;
  clearTimeout(retryTimer);
  try {
    const info = await api('controller/bootstrap');
    fillSettings(info.settings); receive(info.snapshot);
    $('controller-content').hidden = false; $('phone-actions').hidden = false;
    $('pair-needed').hidden = true; networkError = ''; connect();
    refreshDevices().catch(error => { uiError = error.message; renderState(); });
  } catch (error) {
    if (error.status === 403) { revoke(error.message); return; }
    networkError = '暂时无法连接电脑，正在重试。请确认电脑后台已启动。';
    connectionStatus('error', '连接中断'); notice(networkError);
    retryTimer = setTimeout(loadController, 3000);
  }
}
