/** Focus ownership for custom sheets and overlays, including DOM replacement. */
const FOCUSABLE = 'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]';
let current: HTMLElement | null = null;
let returnFocus: HTMLElement | null = null;
let closeModal: (() => void) | null = null;
const background = new Map<HTMLElement, boolean>();

export function syncModal(dialog: HTMLElement | null, onClose?: () => void) {
  if (!dialog) {
    if (!current) return;
    for (const [element, inert] of background) element.inert = inert;
    background.clear();
    current = null;
    closeModal = null;
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    returnFocus = null;
    return;
  }
  if (current === dialog) { closeModal = onClose ?? null; return; }
  if (!current) returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  for (const [element, inert] of background) element.inert = inert;
  background.clear();
  current = dialog;
  closeModal = onClose ?? null;
  dialog.tabIndex = -1;
  // Inert only branches outside the modal's ancestry.
  for (let branch: HTMLElement = dialog; branch.parentElement; branch = branch.parentElement) {
    for (const sibling of branch.parentElement.children) {
      if (!(sibling instanceof HTMLElement) || sibling === branch || sibling.contains(dialog)) continue;
      if (!background.has(sibling)) background.set(sibling, sibling.inert);
      sibling.inert = true;
    }
    if (branch.parentElement === document.body) break;
  }
  if (!dialog.contains(document.activeElement)) {
    const focus = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE)].find(el => el.getClientRects().length);
    (focus ?? dialog).focus({ preventScroll: true });
  }
}

document.addEventListener('keydown', event => {
  if (!current?.isConnected) return;
  if (event.key === 'Escape' && closeModal) {
    event.preventDefault(); event.stopImmediatePropagation(); closeModal(); return;
  }
  if (event.key !== 'Tab') return;
  const items = [...current.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(el => el.getClientRects().length && !el.closest('[hidden]'));
  if (!items.length) { event.preventDefault(); current.focus(); return; }
  const index = items.indexOf(document.activeElement as HTMLElement);
  if (index < 0 || (event.shiftKey && index === 0) || (!event.shiftKey && index === items.length - 1)) {
    event.preventDefault(); (event.shiftKey ? items[items.length - 1] : items[0]).focus();
  }
}, true);
