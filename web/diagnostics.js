'use strict';
async function runDiagnostics() {
  const button = document.getElementById('run-diagnostics');
  const container = document.getElementById('diagnostic-results');
  button.disabled = true;
  container.textContent = '正在检查设备、配置与连接…';
  try {
    const result = await api('diagnostics');
    container.replaceChildren();
    for (const check of result.checks) {
      const row = document.createElement('div'); row.className = 'diagnostic-row';
      const title = document.createElement('strong'); title.textContent = `${check.status === 'ok' ? '✓' : '○'} ${check.name}`;
      const detail = document.createElement('p'); detail.textContent = check.detail;
      row.append(title, detail);
      if (check.action) { const advice = document.createElement('p'); advice.className = 'muted'; advice.textContent = check.action; row.append(advice); }
      container.append(row);
    }
    const note = document.createElement('p'); note.className = 'help'; note.textContent = result.note; container.append(note);
  } catch (error) { container.textContent = error.message || '检查未完成，请确认电脑后台正在运行。'; }
  finally { button.disabled = false; }
}
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('run-diagnostics')?.addEventListener('click', runDiagnostics);
});
