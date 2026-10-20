import documents from '../legal-documents.json';
import { escapeHtml } from './receivers-shared';

export function openLegalDialog(kind: 'agreement' | 'privacy', trigger?: HTMLElement): () => void {
  // Not every browser focuses a clicked button; take the trigger explicitly so
  // focus returns to the entry the user actually used.
  const previous = trigger ?? document.activeElement;
  // Settings polls can replace the trigger while the modal is open; remember
  // its kind so focus can return to the new button instead of body.
  const triggerKind = previous instanceof HTMLElement ? previous.closest<HTMLElement>('[data-legal]')?.dataset.legal : undefined;
  const doc = documents[kind];
  const dialog = document.createElement('dialog');
  dialog.className = 'support-dialog legal-dialog';
  dialog.setAttribute('aria-labelledby', 'legal-title');
  dialog.innerHTML = `<header class="support-header"><h2 id="legal-title">${escapeHtml(doc.title)}</h2><button class="button plain" type="button" data-legal-close aria-label="关闭">关闭</button></header>
    <p class="legal-version">版本与更新日期：${escapeHtml(documents.version)}</p>
    <article>${doc.sections.map(section => `<section><h3>${escapeHtml(section.title)}</h3>${section.paragraphs.map(p => `<p>${escapeHtml(p)}</p>`).join('')}</section>`).join('')}</article>`;
  const restoreFocus = () => {
    const target = previous instanceof HTMLElement && previous.isConnected
      ? previous
      : triggerKind
        ? document.querySelector<HTMLElement>(`[data-legal="${triggerKind}"]`)
        : null;
    target?.focus({ preventScroll: true });
  };
  const remove = () => {
    dialog.remove();
    restoreFocus();
    // Native dialog close restores focus to whatever was focused before
    // showModal(); when the trigger itself was not focused (e.g. a click on a
    // browser that does not focus buttons), that stale target would win.
    requestAnimationFrame(restoreFocus);
  };
  dialog.addEventListener('close', remove, { once: true });
  dialog.querySelector('[data-legal-close]')?.addEventListener('click', () => dialog.close());
  document.body.appendChild(dialog);
  dialog.showModal();
  return () => { if (dialog.open) dialog.close(); else remove(); };
}
