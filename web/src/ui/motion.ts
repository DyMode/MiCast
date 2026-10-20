const key = 'micast-reduced-motion';
let preference: boolean | null = null;
try { const saved = localStorage.getItem(key); if (saved === 'true' || saved === 'false') preference = saved === 'true'; } catch {}
// Simplified visuals are the default; users can opt out in settings or at the
// end of onboarding.
export function reducedMotion(): boolean { return preference ?? true; }
function applyMotion() {
  document.documentElement.classList.toggle('reduce-motion', reducedMotion());
  if (reducedMotion()) document.getAnimations?.().forEach(animation => { try { animation.finish(); } catch { animation.cancel(); } });
  window.dispatchEvent(new Event('micast:motion-change'));
}
export function setReducedMotion(enabled: boolean) {
  preference = enabled;
  try { localStorage.setItem(key, String(enabled)); } catch {}
  applyMotion();
}
window.addEventListener('storage', event => {
  if (event.key !== key && event.key !== null) return;
  preference = event.newValue === 'true' ? true : event.newValue === 'false' ? false : null;
  applyMotion();
});
applyMotion();
