import { appUrl } from '../paths';
import { store } from '../state';
import { escapeHtml } from './receivers-shared';

export const supportEmail = 'support@dymode.com';
const channels = [
  { key: 'wechat', name: '微信赞赏', file: 'wechat-appreciation.jpg' },
  { key: 'alipay', name: '支付宝', file: 'alipay-support.jpg' },
];

export function openSupportDialog(kind: 'donate' | 'contact'): () => void {
  const previous = document.activeElement;
  const dialog = document.createElement('dialog');
  dialog.className = 'support-dialog';
  dialog.setAttribute('aria-labelledby', 'support-title');
  const deployment = store.get().fullConfig?.deployment || '未知';
  const subject = encodeURIComponent('MiCast 使用反馈');
  const body = encodeURIComponent(`你好，DyMode 支持：\n\n部署方式：${deployment}\n应用版本：\n问题描述：\n\n发生步骤：\n\n预期结果：\n\n请按需附上截图或脱敏诊断报告，发送前遮挡私人信息。请勿发送密码或米家登录凭证。`);
  dialog.innerHTML = `
    <header class="support-header"><h2 id="support-title">${kind === 'donate' ? '支持开发' : '联系支持'}</h2><button class="button plain" data-support-close aria-label="关闭">关闭</button></header>
    ${kind === 'donate' ? `
      <div class="support-channels">${channels.map(channel => {
        const image = escapeHtml(appUrl(`assets/support/${channel.file}`));
        return `<figure class="support-qr"><figcaption>${channel.name}</figcaption><div class="support-qr-window is-${channel.key}"><img src="${image}" alt="${channel.name}收款二维码"></div><a class="button secondary" href="${image}" download="MiCast-${channel.name}.jpg">保存收款图</a></figure>`;
      }).join('')}</div>
      <p class="support-hint">自愿支持开发，不影响功能使用。不构成购买或订阅，不附带专属服务或更新承诺；误付请联系 ${supportEmail}。</p>
    ` : `
      <div class="support-contact"><a class="support-email" href="mailto:${supportEmail}">${supportEmail}</a><div class="support-actions"><a class="button secondary" href="mailto:${supportEmail}?subject=${subject}&amp;body=${body}">打开邮件应用</a><button class="button secondary" data-copy-support>复制邮箱</button></div></div>
      <a class="support-github" href="https://github.com/DyMode/MiCast/releases" target="_blank" rel="noopener noreferrer">GitHub 发布页 ↗</a>
      <p class="support-hint" role="status" aria-live="polite" data-support-status></p>
    `}`;
  const remove = () => {
    dialog.remove();
    if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
  };
  dialog.addEventListener('close', remove, { once: true });
  dialog.querySelector('[data-support-close]')?.addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => {
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
  });
  dialog.querySelector('[data-copy-support]')?.addEventListener('click', async () => {
    const status = dialog.querySelector<HTMLElement>('[data-support-status]');
    try {
      await navigator.clipboard.writeText(supportEmail);
      if (status) status.textContent = '邮箱已复制';
    } catch {
      if (status) status.textContent = '请长按或选中上方邮箱地址复制。';
    }
  });
  document.body.appendChild(dialog);
  dialog.showModal();
  return () => { if (dialog.open) dialog.close(); else remove(); };
}
