/** A persistent, rolling receiver-lyric surface. Status ticks never rebuild it. */
class LyricViewport {
  private lines: string[] = [];
  constructor(private element: HTMLElement) {}

  update(next: string[]) {
    if (JSON.stringify(next) === JSON.stringify(this.lines)) return;
    const follow = !this.lines.length || this.element.scrollHeight - this.element.clientHeight - this.element.scrollTop < 24;
    let shared = Math.min(this.lines.length, next.length);
    while (shared && this.lines.slice(-shared).some((line, index) => line !== next[index])) shared--;
    const remove = this.lines.length - shared;
    const previousTop = this.element.scrollTop;
    let removedHeight = 0;
    for (let i = 0; i < remove; i++) {
      const node = this.element.firstElementChild as HTMLElement | null;
      if (!node) break;
      removedHeight += node.getBoundingClientRect().height + (parseFloat(getComputedStyle(this.element).rowGap) || 0);
      node.remove();
    }
    for (const line of next.slice(shared)) {
      const node = document.createElement('p');
      node.className = 'player-overlay-lyric is-past';
      node.textContent = line;
      this.element.append(node);
    }
    const children = [...this.element.children];
    children.forEach((node, index) => node.classList.toggle('is-current', index === children.length - 1));
    this.lines = [...next];
    if (removedHeight) this.element.scrollTop = Math.max(0, previousTop - removedHeight);
    if (follow) this.element.scrollTo({top: this.element.scrollHeight, behavior: 'instant'});
  }
}
const viewports = new WeakMap<HTMLElement, LyricViewport>();
export function updateLyricViewport(element: HTMLElement, lines: string[]) {
  let viewport = viewports.get(element);
  if (!viewport) { viewport = new LyricViewport(element); viewports.set(element, viewport); }
  viewport.update(lines.slice(-8));
}
