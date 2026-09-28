'use strict';
let practiceSelection = '', practiceLoading = false, microphonesLoaded = false;
const practiceDrafts = new Map();
function practiceItem() { return items.find(item => item.id === practiceSelection); }
function practiceDraft(item) {
  if (!item) return null;
  let draft = practiceDrafts.get(item.id);
  const saved = item.candidate_answer || '';
  if (!draft || (!draft.dirty && draft.base !== saved)) {
    draft = {text:saved, base:saved, dirty:false}; practiceDrafts.set(item.id, draft);
  }
  return draft;
}
function renderPractice() {
  const session = state.practice || {};
  const eligible = items.filter(item => !item.demo && item.source !== 'screen' && item.status !== 'collecting');
  for (const key of practiceDrafts.keys()) if (!items.some(item => item.id === key)) practiceDrafts.delete(key);
  if (session.item_id) practiceSelection = session.item_id;
  if (!eligible.some(item => item.id === practiceSelection)) practiceSelection = eligible.at(-1)?.id || '';
  const select = $('practice-question');
  const signature = JSON.stringify(eligible.map(item => [item.id, item.question]));
  if (select.dataset.signature !== signature) {
    select.replaceChildren(...eligible.map(item => {
      const option = element('option', '', item.question.slice(0, 90)); option.value = item.id; return option;
    })); select.dataset.signature = signature;
  }
  select.value = practiceSelection;
  const item = practiceItem(), draft = practiceDraft(item);
  const busy = !!(session.recording || session.processing || session.reviewing);
  const blocked = !connected || practiceLoading;
  $('practice-empty').hidden = !!item;
  $('practice-question-text').textContent = item?.question || '';
  $('practice-add-question').disabled = blocked || busy;
  select.disabled = blocked || busy || !item;
  $('practice-microphone').disabled = blocked || busy;
  $('practice-refresh').disabled = blocked || busy;
  $('practice-record').hidden = !!session.recording;
  $('practice-record').disabled = blocked || busy || !item || state.screen_monitoring || state.screen_hotkey || state.screen_capturing;
  $('practice-finish').hidden = !session.recording;
  $('practice-finish').disabled = blocked;
  $('practice-answer').disabled = blocked || busy || !item;
  if ($('practice-answer').value !== (draft?.text || '')) $('practice-answer').value = draft?.text || '';
  $('practice-save').disabled = blocked || busy || !item;
  $('practice-review').disabled = blocked || busy || !draft?.text.trim();
  $('practice-review').textContent = session.reviewing ? '正在生成复盘…' : item?.review ? '重新生成复盘 →' : '生成复盘 →';
  $('practice-dirty').textContent = draft?.dirty ? '文字有修改；点击保存，或直接生成复盘并保存。' : item?.candidate_answer ? '文字已保存在本次会话' : '';
  $('practice-notice').textContent = item?.candidate_error || session.notice || '麦克风默认关闭，由你手动开始和结束。';
  const level = session.level || 0, db = level > 0 ? 20 * Math.log10(level) : -90;
  $('practice-level').value = Math.max(0, Math.min(100, (db + 70) / 70 * 100));
  $('practice-level-text').textContent = session.recording ? `${session.device} · ${level > 0 ? db.toFixed(0) + ' dBFS' : '静音'}` : '点击录音后显示电平';
  const review = item?.review;
  $('practice-review-text').textContent = review?.text || (review?.status === 'generating' ? '正在分析你的实际回答…' : '完成回答后，查看说清楚的点、遗漏、改进示例和下一轮追问。');
  $('practice-review-status').textContent = review ? ({generating:'正在生成', done:'复盘完成', error:'生成未完成', cancelled:'已停止，保留部分内容'}[review.status] + (draft?.dirty ? ' · 当前文字有修改，需重新生成' : '')) : '仅根据本题、你的回答和已填写的岗位资料分析';
  $('practice-review-error').hidden = !review?.error; $('practice-review-error').textContent = review?.error || '';
  $('practice-export').disabled = blocked || !review?.text || review.status === 'generating';
}
async function practiceAction(operation) {
  if (practiceLoading) return;
  practiceLoading = true; renderPractice();
  await action(null, async () => { try { await operation(); } finally { practiceLoading = false; } });
}
async function refreshMicrophones() {
  try {
    const {devices: sources} = await api('microphones');
    if (!active) return;
    const select = $('practice-microphone'), previous = select.selectedOptions[0]?.dataset.name;
    const defaultOption = element('option', '', '系统默认输入设备'); defaultOption.value = '';
    select.replaceChildren(defaultOption);
    sources.forEach(source => {
      const option = element('option', '', source.name + (source.default ? '（默认）' : ''));
      option.value = String(source.id); option.dataset.name = source.name; select.append(option);
    });
    const selected = [...select.options].find(option => option.dataset.name === previous);
    if (selected) select.value = selected.value;
    microphonesLoaded = true;
    $('practice-device-notice').textContent = sources.length ? `找到 ${sources.length} 个输入设备；采集发生在电脑，手机无需麦克风权限。` : '未找到输入设备，请连接电脑麦克风并检查系统权限。';
  } catch (error) { $('practice-device-notice').textContent = error.message; }
}
async function savePracticeDraft() {
  const item = practiceItem(), draft = practiceDraft(item);
  if (!item || !draft) throw new Error('请先选择问题');
  await api('practice/answer', {item_id:item.id, answer:draft.text});
  draft.base = draft.text.trim(); draft.text = draft.base; draft.dirty = false;
}
function setupPractice() {
  $('practice-add-question').onclick = () => practiceAction(async () => {
    const question = $('practice-new-question').value.trim();
    if (!question) throw new Error('请输入练习问题');
    const result = await api('practice/question', {question});
    practiceSelection = result.id; $('practice-new-question').value = '';
  });
  $('practice-question').onchange = () => { practiceSelection = $('practice-question').value; renderPractice(); };
  $('practice-answer').oninput = () => {
    const draft = practiceDraft(practiceItem()); if (!draft) return;
    draft.text = $('practice-answer').value; draft.dirty = draft.text !== draft.base; renderPractice();
  };
  $('practice-refresh').onclick = () => practiceAction(refreshMicrophones);
  $('practice-record').onclick = () => practiceAction(async () => {
    if (practiceDraft(practiceItem())?.dirty) await savePracticeDraft();
    const select = $('practice-microphone');
    await api('practice/start', {item_id:practiceSelection, device_id:select.value ? Number(select.value) : null,
                                device_name:select.selectedOptions[0]?.dataset.name || null});
  });
  $('practice-finish').onclick = () => practiceAction(() => api('practice/finish', {}));
  $('practice-save').onclick = () => practiceAction(async () => { await savePracticeDraft(); toast('实际回答已保存'); });
  $('practice-review').onclick = () => practiceAction(async () => {
    const item = practiceItem(), draft = practiceDraft(item);
    await api('practice/review', {item_id:item.id, answer:draft.text});
    draft.base = draft.text.trim(); draft.text = draft.base; draft.dirty = false;
  });
  $('practice-export').onclick = () => {
    const item = practiceItem(), review = item?.review; if (!review?.text) return;
    const text = `# 面试练习复盘\n\n生成时间：${review.at}\n状态：${review.status}\n\n## 问题\n\n${review.question}\n\n## 实际回答（生成时版本）\n\n${review.answer}\n\n## 复盘建议\n\n${review.text}\n`;
    const url = URL.createObjectURL(new Blob([text], {type:'text/markdown;charset=utf-8'}));
    const link = element('a'); link.href = url; link.download = `听答-复盘-${item.id}.md`;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
}
