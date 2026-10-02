/** Connection health is independent of the last usable device snapshot. */
let lastConfirmed = Date.now();
let failed = false;

export function bindConnectivity(retry: () => Promise<void>) {
  const update = () => {
    const stale = failed || Date.now() - lastConfirmed > 25000;
    const notice = document.querySelector<HTMLElement>('[data-connection-notice]');
    if (notice) notice.hidden = !stale;
    document.documentElement.classList.toggle('connection-degraded', stale && Boolean(notice));
    const time = notice?.querySelector('time');
    if (time) time.textContent = new Date(lastConfirmed).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };
  window.addEventListener('micast:connection', event => {
    failed = !(event as CustomEvent<{ ok: boolean }>).detail.ok;
    if (!failed) lastConfirmed = Date.now();
    update();
  });
  document.addEventListener('click', async event => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-connection-retry]');
    if (!button || button.disabled) return;
    button.disabled = true;
    try { await retry(); }
    catch { failed = true; } // Keep the retry surface visible after another failure.
    finally { button.disabled = false; update(); }
  });
  window.setInterval(() => { if (!document.hidden) update(); }, 5000);
}
