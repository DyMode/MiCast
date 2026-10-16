const key = 'micast-reduced-motion';
const system = window.matchMedia('(prefers-reduced-motion: reduce)');
let preference: boolean | null = null;
try { const saved = localStorage.getItem(key); if (saved === 'true' || saved === 'false') preference = saved === 'true'; } catch {}
export function reducedMotion(): boolean { return preference ?? system.matches; }
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
system.addEventListener('change', applyMotion);
window.addEventListener('storage', event => {
  if (event.key !== key && event.key !== null) return;
  preference = event.newValue === 'true' ? true : event.newValue === 'false' ? false : null;
  applyMotion();
});
applyMotion();
