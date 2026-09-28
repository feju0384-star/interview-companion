'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const email = '2806391703@qq.com';
  const plans = {
    setup: {name: '安装连通', price: 99, scope: '1 台 Windows 电脑 + 1 部手机，预约 60 分钟；3 天内同问题邮件跟进 1 次。'},
    practice: {name: '练习上手', price: 199, scope: '1 台 Windows 电脑 + 1 部手机，预约共 90 分钟；7 天内 1 次 15 分钟答疑。'},
  };
  const planInput = byId('service-plan');
  const draft = byId('inquiry-draft');
  const status = byId('inquiry-status');
  function updateDraft() {
    const plan = plans[planInput.value] || plans.setup;
    const subject = `听答预约咨询｜${plan.name} ¥${plan.price}`;
    const body = [
      '你好，我想咨询听答的安装 / 上手服务。', '',
      `服务：${plan.name} · ¥${plan.price} / 次（试卖价格）`,
      `范围：${plan.scope}`,
      `电脑系统：${byId('computer-system').value}`,
      `当前问题：${byId('inquiry-problem').value.trim() || '待补充'}`,
      `方便的时间：${byId('inquiry-time').value.trim() || '请邮件协商'}`, '',
      '我了解以上是人工服务费，模型账户、API 和语音识别调用费用另付。',
      '请先确认兼容性、服务范围、排期、付款及取消 / 未完成时的处理方式。',
      '这是一封咨询邮件，尚未确认订单或付款。',
    ].join('\n');
    draft.value = `收件人：${email}\n主题：${subject}\n\n${body}`;
    byId('email-inquiry').href = `mailto:${email}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    status.textContent = '';
  }
  for (const id of ['service-plan', 'computer-system', 'inquiry-problem', 'inquiry-time']) {
    byId(id).addEventListener('input', updateDraft);
    byId(id).addEventListener('change', updateDraft);
  }
  document.querySelectorAll('.plan-link').forEach(link => {
    link.addEventListener('click', () => {
      if (plans[link.dataset.plan]) planInput.value = link.dataset.plan;
      updateDraft();
    });
  });
  byId('copy-inquiry').addEventListener('click', async () => {
    // Keep a visible manual-copy path for HTTP LAN pages and denied clipboard access.
    try {
      if (!navigator.clipboard) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(draft.value);
      status.textContent = '已复制咨询内容。请在你的邮箱中粘贴并发送；此页面不会自动发送。';
    } catch {
      draft.closest('details').open = true;
      draft.focus();
      draft.select();
      status.textContent = '草稿已展开并选中，请手动复制，再发送到上方邮箱。';
    }
  });
  updateDraft();
  byId('inquiry-builder').hidden = false;
})();
