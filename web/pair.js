'use strict';
const $ = id => document.getElementById(id);
let pairing = {}, socket, timer, toastTimer, active = true;
async function api(path, data) {
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 15000);
  try {
    const response = await fetch(`/api/${path}`, {method:data === undefined ? 'GET' : 'POST',
      headers:data === undefined ? {} : {'Content-Type':'application/json'},
      body:data === undefined ? undefined : JSON.stringify(data), signal:abort.signal});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '连接电脑后台失败');
    return result;
  } finally { clearTimeout(timeout); }
}
function toast(message) {
  clearTimeout(toastTimer); $('toast').textContent = message; $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3500);
}
function showPairing(info) {
  const previous = $('network').value;
  pairing = info; $('network').replaceChildren();
  info.urls.forEach((url, index) => {
    const option = document.createElement('option'); option.value = String(index);
    option.textContent = new URL(url).host; $('network').append(option);
  });
  if (!info.urls.length) {
    const option = document.createElement('option'); option.value = '-1';
    option.textContent = '未找到局域网地址'; $('network').append(option);
  }
  if ([...$('network').options].some(option => option.value === previous)) $('network').value = previous;
  $('qr').src = `/api/qr?index=${$('network').value}&v=${Date.now()}`;
}
function showState(state) {
  const count = state.controller_count || 0;
  $('controller-status').textContent = count ? `${count} 个手机控制台已连接` : '等待手机连接';
  $('controller-dot').classList.toggle('live', count > 0);
  $('capture-state').textContent = state.listening ? '电脑正在监听' : state.screen_monitoring ? '屏幕自动监测中' : state.screen_capturing ? '正在截图' : state.screen_hotkey ? '屏幕快捷键已启用' : '后台已就绪';
  if (state.practice?.recording) $('capture-state').textContent = '电脑麦克风录音中';
  else if (state.practice?.processing) $('capture-state').textContent = '正在转写实际回答';
  else if (state.practice?.reviewing) $('capture-state').textContent = '正在生成复盘';
}
async function boot() {
  clearTimeout(timer);
  try {
    const info = await api('bootstrap'); showPairing(info.pairing); showState(info.snapshot.state);
    $('notice').hidden = true;
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
    socket = ws;
    ws.onopen = () => ws.send(JSON.stringify({token:info.token}));
    ws.onmessage = message => {
      const event = JSON.parse(message.data);
      if (event.state) showState(event.state);
      if (event.type === 'snapshot') { $('connection').textContent = '后台已连接'; $('connection').className = 'connection online'; }
    };
    ws.onclose = () => {
      $('connection').textContent = '重新连接中'; $('connection').className = 'connection error';
      if (active) timer = setTimeout(boot, 2000);
    };
    ws.onerror = () => ws.close();
  } catch {
    $('notice').textContent = '电脑后台未连接，正在重试。若已退出，请重新双击“启动听答.cmd”。';
    $('notice').hidden = false;
    if (active) timer = setTimeout(boot, 3000);
  }
}
$('network').onchange = () => { $('qr').src = `/api/qr?index=${$('network').value}&v=${Date.now()}`; };
$('copy-link').onclick = async () => {
  try {
    await navigator.clipboard.writeText(pairing.urls[Number($('network').value)] || pairing.local_url);
    toast('已复制手机控制链接');
  } catch { toast('复制失败，请直接用手机扫描二维码'); }
};
$('preview-phone').onclick = () => window.open(pairing.local_url, '_blank', 'noopener');
$('rotate-link').onclick = async event => {
  const button = event.currentTarget; button.disabled = true;
  try { showPairing(await api('pairing/rotate', {})); toast('已停止采集、关闭快捷键并撤销旧手机权限，请重新扫码'); }
  catch (error) { toast(error.message); }
  finally { button.disabled = false; }
};
window.addEventListener('pagehide', () => { active = false; clearTimeout(timer); socket?.close(); });
window.addEventListener('pageshow', event => { if (event.persisted) { active = true; boot(); } });
boot();
