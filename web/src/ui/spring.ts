/** Critically damped navigation motion; retargeting keeps presentation and
 * velocity, while direct manipulation tracks the pointer without latency. */
type Axis = 'x' | 'y';
interface Spring { value: number; target: number; velocity: number; time: number; raf: number; }
const springs = new WeakMap<HTMLElement, Spring>();
export function moveLens(element: HTMLElement, axis: Axis, target: number, direct = false) {
  let spring = springs.get(element);
  const now = performance.now();
  const paint = (value: number) => element.style.setProperty(`--lens-${axis}`, `${value.toFixed(2)}px`);
  if (!spring) {
    spring = { value: target, target, velocity: 0, time: now, raf: 0 };
    springs.set(element, spring);
    paint(target);
    return;
  }
  spring.target = target;
  if (direct || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    cancelAnimationFrame(spring.raf);
    spring.raf = 0;
    spring.velocity = direct ? Math.max(-1200, Math.min(1200, (target - spring.value) / Math.max(.008, (now - spring.time)/1000))) : 0;
    spring.value = target; spring.time = now; paint(target); return;
  }
  if (spring.raf) return;
  spring.time = now;
  const frame = (time: number) => {
    if (!element.isConnected) { spring!.raf = 0; return; }
    const dt = Math.min(.032, Math.max(.001, (time - spring!.time)/1000));
    spring!.time = time;
    // Analytic critically damped spring, stable even after a dropped frame.
    const response = 18;
    const displacement = spring!.value - spring!.target;
    const term = spring!.velocity + response * displacement;
    const decay = Math.exp(-response * dt);
    spring!.value = spring!.target + (displacement + term * dt) * decay;
    spring!.velocity = (spring!.velocity - response * term * dt) * decay;
    paint(spring!.value);
    if (Math.abs(spring!.value - spring!.target) < .1 && Math.abs(spring!.velocity) < .5) {
      spring!.value = spring!.target; spring!.velocity = 0; spring!.raf = 0; paint(spring!.target);
    } else spring!.raf = requestAnimationFrame(frame);
  };
  spring.raf = requestAnimationFrame(frame);
}
