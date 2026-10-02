/** Resources belong to a mounted surface, never to detached DOM. */
export class SurfaceScope {
  private cleanups: Array<() => void> = [];
  private pending = new Map<string, { timer: number; work: () => void }>();
  active = true;

  own(cleanup: () => void) { this.cleanups.push(cleanup); }

  listen(target: EventTarget, event: string, listener: EventListener) {
    target.addEventListener(event, listener);
    this.own(() => target.removeEventListener(event, listener));
  }

  interval(work: () => void, milliseconds: number) {
    const timer = window.setInterval(() => { if (this.active) work(); }, milliseconds);
    this.own(() => clearInterval(timer));
    return timer;
  }

  controller() {
    const controller = new AbortController();
    this.own(() => controller.abort());
    return controller;
  }

  debounce(key: string, work: () => void, delay: number) {
    const previous = this.pending.get(key);
    if (previous) clearTimeout(previous.timer);
    const timer = window.setTimeout(() => {
      this.pending.delete(key);
      if (this.active) work();
    }, delay);
    this.pending.set(key, { timer, work });
  }

  dispose(flush = false) {
    if (!this.active) return;
    this.active = false;
    for (const { timer, work } of this.pending.values()) {
      clearTimeout(timer);
      if (flush) work();
    }
    this.pending.clear();
    for (const cleanup of this.cleanups.splice(0)) cleanup();
  }
}
