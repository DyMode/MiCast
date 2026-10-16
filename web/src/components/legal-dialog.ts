import documents from '../legal-documents.json';
import { escapeHtml } from './receivers-shared';

export function openLegalDialog(kind: 'agreement' | 'privacy'): () => void {
  const previous = document.activeElement;
  const doc = documents[kind];
  const dialog = document.createElement('dialog');
  dialog.className = 'support-dialog legal-dialog';
  dialog.setAttribute('aria-labelledby', 'legal-title');
  dialog.innerHTML = `<header class="support-header"><h2 id="legal-title">${escapeHtml(doc.title)}</h2><button class="button plain" type="button" data-legal-close aria-label="关闭">关闭</button></header>
    <p class="legal-version">版本与更新日期：${escapeHtml(documents.version)}</p>
    <article>${doc.sections.map(section => `<section><h3>${escapeHtml(section.title)}</h3>${section.paragraphs.map(p => `<p>${escapeHtml(p)}</p>`).join('')}</section>`).join('')}</article>`;
  const remove = () => {
    dialog.remove();
    if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
  };
  dialog.addEventListener('close', remove, { once: true });
  dialog.querySelector('[data-legal-close]')?.addEventListener('click', () => dialog.close());
  document.body.appendChild(dialog);
  dialog.showModal();
  return () => { if (dialog.open) dialog.close(); else remove(); };
}
