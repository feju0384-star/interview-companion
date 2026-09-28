'use strict';
let screenMonitors = [], screenRegion = null, screenPreviewImage = null;
let screenLoading = false, screenDrag = null, screenLoaded = false;

function screenRequest() {
  if (!$('screen-monitor').value) throw new Error('请先刷新并选择电脑显示器');
  const values = ['x', 'y', 'width', 'height'].map(key => Number($(`screen-${key}`).value) / 100);
  const [x, y, width, height] = values;
  if (!values.every(Number.isFinite) || x < 0 || y < 0 || width <= 0 || height <= 0 || x + width > 1.000001 || y + height > 1.000001) {
    throw new Error('框选坐标无效，请检查位置、宽度和高度');
  }
  return {monitor_id: $('screen-monitor').value, region: screenRegion ? {x, y, width, height} : null,
          backend: $('screen-backend').value, style: $('screen-style').value,
          instruction: $('screen-instruction').value.trim()};
}

function renderScreenState() {
  const armed = !!state.screen_hotkey;
  const monitoring = !!state.screen_monitoring;
  const blocked = !connected || screenLoading || state.screen_capturing || state.practice?.recording || state.practice?.processing || state.practice?.reviewing;
  $('screen-options').disabled = blocked || armed || monitoring;
  $('screen-ask').disabled = blocked || state.listening || !!state.context?.compacting || armed || monitoring;
  $('screen-hotkey').disabled = blocked || monitoring || (!armed && (state.listening || !!state.context?.compacting));
  $('screen-monitor-toggle').disabled = blocked || (!monitoring && (state.listening || !!state.context?.compacting));
  $('screen-monitor-toggle').textContent = monitoring ? '停止自动监测' : '开始自动监测';
  $('screen-monitor-toggle').classList.toggle('danger', monitoring);
  $('screen-monitor-toggle').classList.toggle('primary', !monitoring);
  $('screen-hotkey').textContent = armed ? '关闭快捷键' : '启用快捷键';
  $('screen-ask').textContent = state.screen_capturing ? '正在截图…' : '单次截图解题 ↑';
  $('screen-status').textContent = state.listening ? '请先停止声音监听，再使用屏幕解题。' :
    monitoring ? `后台监测中 · 每 ${state.screen_monitor_interval} 秒检查 · 已提交 ${state.screen_monitor_count || 0} 次。${state.screen_notice || ''}` :
    armed ? `快捷键已启用：Ctrl + Alt + S。${state.screen_notice || ''}` :
    state.screen_notice || '自动监测尚未开启。';
}

async function screenAction(button, operation) {
  if (screenLoading) return;
  screenLoading = true; renderScreenState();
  await action(button, async () => {
    try { await operation(); } finally { screenLoading = false; }
  });
}

async function refreshScreens() {
  const result = await api('screen/monitors');
  const old = $('screen-monitor').value;
  screenMonitors = result.monitors;
  $('screen-monitor').replaceChildren(...screenMonitors.map((monitor, i) => {
    const option = document.createElement('option'); option.value = monitor.id;
    option.textContent = `屏幕 ${i + 1}${monitor.primary ? '（主屏）' : ''} · ${monitor.width} × ${monitor.height}`;
    return option;
  }));
  if (screenMonitors.some(monitor => monitor.id === old)) $('screen-monitor').value = old;
  else resetScreenRegion();
  screenLoaded = true;
}

function resetScreenRegion() {
  screenRegion = null; screenPreviewImage = null; screenDrag = null;
  $('screen-preview-wrap').hidden = true;
  const canvas = $('screen-canvas'); canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
  syncScreenRegion();
}

function syncScreenRegion() {
  if (screenRegion) {
    const rounded = Object.fromEntries(Object.entries(screenRegion).map(([key, value]) => [key, Math.round(value * 1000) / 1000]));
    rounded.width = Math.min(rounded.width, 1 - rounded.x);
    rounded.height = Math.min(rounded.height, 1 - rounded.y);
    screenRegion = rounded;
  }
  const region = screenRegion || {x: 0, y: 0, width: 1, height: 1};
  for (const key of ['x', 'y', 'width', 'height']) $(`screen-${key}`).value = String(Math.round(region[key] * 1000) / 10);
  $('screen-region-label').textContent = screenRegion ? `范围：框选区域 · 宽 ${Math.round(region.width * 100)}% × 高 ${Math.round(region.height * 100)}%` : '范围：整块屏幕';
  drawScreenPreview();
}

function drawScreenPreview() {
  if (!screenPreviewImage) return;
  const canvas = $('screen-canvas'), ctx = canvas.getContext('2d');
  ctx.drawImage(screenPreviewImage, 0, 0, canvas.width, canvas.height);
  if (screenRegion && Object.values(screenRegion).every(Number.isFinite) && screenRegion.width > 0 && screenRegion.height > 0) {
    const {x, y, width, height} = screenRegion;
    ctx.fillStyle = '#153d3566'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(screenPreviewImage, x * canvas.width, y * canvas.height, width * canvas.width, height * canvas.height,
                  x * canvas.width, y * canvas.height, width * canvas.width, height * canvas.height);
    ctx.strokeStyle = '#b8e870'; ctx.lineWidth = 3;
    ctx.strokeRect(x * canvas.width, y * canvas.height, width * canvas.width, height * canvas.height);
  }
}

function setupScreen() {
  $('screen-refresh').onclick = event => screenAction(event.currentTarget, refreshScreens);
  $('screen-monitor').onchange = resetScreenRegion;
  $('screen-full').onclick = () => { screenRegion = null; syncScreenRegion(); };
  $('screen-preview').onclick = event => screenAction(event.currentTarget, async () => {
    const request = screenRequest();
    const result = await api('screen/preview', {...request, region: null});
    if (!active || !token) return;
    const preview = new Image(); preview.src = result.image; await preview.decode();
    if (!active || !token) return;
    screenPreviewImage = preview;
    $('screen-canvas').width = preview.width; $('screen-canvas').height = preview.height;
    $('screen-preview-wrap').hidden = false; drawScreenPreview();
    toast('预览已更新；可拖动框选题目');
  });
  $('screen-ask').onclick = event => screenAction(event.currentTarget, async () => {
    await api('screen/ask', screenRequest());
    selectPane('answers'); $('answers').scrollIntoView({behavior: 'smooth', block: 'start'});
  });
  $('screen-hotkey').onclick = event => screenAction(event.currentTarget, async () => {
    const enabled = !state.screen_hotkey;
    const result = await api('screen/hotkey', {enabled, request: enabled ? screenRequest() : null});
    state.screen_hotkey = result.enabled;
    toast(result.enabled ? '已启用 Ctrl + Alt + S；在电脑按下即可解题' : '快捷键已关闭');
  });
  $('screen-monitor-toggle').onclick = event => screenAction(event.currentTarget, async () => {
    const enabled = !state.screen_monitoring;
    const options = enabled ? {request: screenRequest(), interval_seconds: Number($('screen-interval').value), sensitivity: $('screen-sensitivity').value} : null;
    const result = await api('screen/monitor', {enabled, options});
    state.screen_monitoring = result.enabled;
    toast(result.enabled ? '后台自动监测已开启，可切到回答页查看答案' : '自动监测与本次自动回答已停止');
    if (result.enabled) {
      selectPane('answers');
      $('answers').scrollIntoView({behavior: 'smooth', block: 'start'});
    }
  });
  const canvas = $('screen-canvas');
  function position(event) {
    const rect = canvas.getBoundingClientRect();
    return {x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
            y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))};
  }
  canvas.onpointerdown = event => {
    if (state.screen_hotkey || state.screen_monitoring || screenLoading || !connected || !screenPreviewImage || screenDrag) return;
    screenDrag = {...position(event), pointerId: event.pointerId, previous: screenRegion};
    canvas.setPointerCapture(event.pointerId);
  };
  canvas.onpointermove = event => {
    if (!screenDrag || screenDrag.pointerId !== event.pointerId) return;
    const point = position(event);
    screenRegion = {x: Math.min(point.x, screenDrag.x), y: Math.min(point.y, screenDrag.y),
                    width: Math.abs(point.x - screenDrag.x), height: Math.abs(point.y - screenDrag.y)};
    drawScreenPreview();
  };
  canvas.onpointerup = event => {
    if (!screenDrag || screenDrag.pointerId !== event.pointerId) return;
    if (!screenRegion || screenRegion.width < .01 || screenRegion.height < .01) screenRegion = screenDrag.previous;
    screenDrag = null; syncScreenRegion();
  };
  canvas.onpointercancel = () => {
    if (screenDrag) screenRegion = screenDrag.previous;
    screenDrag = null; syncScreenRegion();
  };
  for (const key of ['x', 'y', 'width', 'height']) $(`screen-${key}`).oninput = () => {
    screenRegion = Object.fromEntries(['x', 'y', 'width', 'height'].map(name => [name, Number($(`screen-${name}`).value) / 100]));
    $('screen-region-label').textContent = '范围：自定义坐标';
    drawScreenPreview();
  };
}
