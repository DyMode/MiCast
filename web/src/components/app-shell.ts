import type { Section, Theme } from "../state";
import { brandMark, icon, type IconName } from "../icons";
import { moveLens } from '../ui/spring';

interface NavItem {
  id: Section;
  label: string;
  icon: IconName;
}

const navItems: NavItem[] = [
  { id: "receivers", label: "播放", icon: "antenna" },
  { id: "devices", label: "音箱", icon: "speaker" },
  { id: "topology", label: "链路", icon: "topology" },
  { id: "settings", label: "设置", icon: "settings" },
  { id: "debug", label: "诊断", icon: "terminal" },
];

export function renderAppShell(
  content: string,
  activeSection: Section,
  appName: string,
  theme: Theme
): string {
  return `
    <div class="app-shell">
      <header class="app-header">
        <div class="app-brand">
          <div class="app-logo">${brandMark()}</div>
          <h1 class="app-title" id="app-title">${escapeHtml(appName)}</h1>
        </div>
        <div class="app-header-actions">
          <button class="icon-button" id="theme-toggle" aria-label="切换主题" title="${themeLabel(theme)}">
            ${themeIcon(theme)}
          </button>
        </div>
      </header>
      <aside class="connection-notice" data-connection-notice hidden role="status">
        <span>连接暂时中断，显示最近数据 · <time></time></span>
        <button type="button" class="button plain" data-connection-retry>重新连接</button>
      </aside>

      <div class="app-body">
        <div class="app-lane">
          <nav class="sidebar" aria-label="主导航">
            <span class="nav-lens" aria-hidden="true"></span>
            ${navItems
              .map(
                (item) => `
                  <button class="nav-item ${item.id === activeSection ? "active" : ""}"
                          data-section="${item.id}" ${item.id === activeSection ? 'aria-current="page"' : ''}>
                    <span class="nav-icon">${icon(item.icon)}</span>
                    ${item.label}
                  </button>
                `
              )
              .join("")}
          </nav>
          <div id="playback-slot"></div>
        </div>
        <main class="main-content page-view">${content}</main>
      </div>

      <nav class="tab-bar" aria-label="底部导航">
        <span class="tab-lens" aria-hidden="true"></span>
        ${navItems
          .map(
            (item) => `
              <button class="tab-item ${item.id === activeSection ? "active" : ""}"
                      data-section="${item.id}" ${item.id === activeSection ? 'aria-current="page"' : ''}>
                <span class="tab-icon">${icon(item.icon)}</span>
                <span>${item.label}</span>
              </button>
            `
          )
          .join("")}
      </nav>
    </div>
  `;
}

export function bindNavigation(
  container: HTMLElement,
  onSelect: (section: Section) => void
) {
  container.querySelectorAll("[data-section]").forEach((el) => {
    el.addEventListener("click", () => {
      const section = (el as HTMLElement).dataset.section as Section | undefined;
      if (section) onSelect(section);
    });
  });
}

export function bindThemeToggle(
  container: HTMLElement,
  onToggle: () => void
) {
  container.querySelectorAll("#theme-toggle").forEach((btn) => btn.addEventListener("click", onToggle));
}

/**
 * Liquid Glass navigation interactions, shared shape on both rails:
 * - Tap: the rail "liquifies" (scales up, brightens, deep refraction) and the
 *   glass lens glides to the target, growing while it travels and settling
 *   back to size on arrival.
 * - Mobile tab bar: touch down glides the lens to the pressed item; dragging
 *   a finger along the bar scrubs the lens 1:1 (refracting what it passes);
 *   release selects the tab under the finger. The bar itself never moves.
 * - Desktop sidebar: the lens glides vertically to the hovered item; click
 *   or keyboard selects.
 * The shell mounts once, so listeners bind once at startup; MutationObservers
 * keep the lenses parked under the active item when selection changes without
 * a click (keyboard, programmatic).
 */

export function bindTabBarDrag(container: HTMLElement) {
  const controller = new AbortController();
  const cleanups: (() => void)[] = [];
  for (const [surfaceSelector, lensSelector, itemSelector, axis] of [
    ['.tab-bar', '.tab-lens', '.tab-item', 'x'],
    ['.sidebar', '.nav-lens', '.nav-item', 'y'],
  ] as const) {
    const surface = container.querySelector<HTMLElement>(surfaceSelector);
    const lens = surface?.querySelector<HTMLElement>(lensSelector);
    if (!surface || !lens) continue;
    let gesture: { id: number; start: number; origin: number; moved: boolean } | null = null;
    let suppressClick = false;
    const coord = (e: PointerEvent) => axis === 'x' ? e.clientX : e.clientY;
    const origin = () => parseFloat(lens.style.getPropertyValue(`--lens-${axis}`)) || 0;
    const sync = (direct = false) => {
      if (gesture || controller.signal.aborted) return;
      const active = surface.querySelector<HTMLElement>(`${itemSelector}.active`);
      if (!active || !surface.getClientRects().length) return;
      if (axis === 'y') {
        lens.style.height = `${active.offsetHeight}px`;
        moveLens(lens, axis, active.offsetTop, direct);
      } else {
        lens.style.top = `${active.offsetTop + (active.offsetHeight - lens.offsetHeight) / 2}px`;
        moveLens(lens, axis, active.offsetLeft + (active.offsetWidth - lens.offsetWidth) / 2, direct);
      }
    };
    const listen = (target: EventTarget, type: string, handler: EventListener, capture = false) =>
      target.addEventListener(type, handler, { signal: controller.signal, capture });
    const clear = () => {
      const id = gesture?.id;
      gesture = null;
      surface.classList.remove('scrubbing', 'held', 'liquid');
      lens.classList.remove('held', 'traveling');
      if (id !== undefined && surface.hasPointerCapture(id)) surface.releasePointerCapture(id);
    };
    const cancel = () => { suppressClick = Boolean(gesture?.moved); clear(); sync(); };
    listen(surface, 'click', ((e: MouseEvent) => {
      if (suppressClick && e.isTrusted) { e.preventDefault(); e.stopImmediatePropagation(); }
    }) as EventListener, true);
    listen(surface, 'pointerdown', ((e: PointerEvent) => {
      if (gesture || (e.pointerType === 'mouse' && e.button !== 0)) return;
      const item = (e.target as HTMLElement).closest(itemSelector);
      if (!item) return;
      suppressClick = false;
      // Freeze any settling spring at its presentation position; never jump on press.
      moveLens(lens, axis, origin(), true);
      gesture = { id: e.pointerId, start: coord(e), origin: origin(), moved: false };
      lens.classList.add('held');
    }) as EventListener);
    listen(surface, 'pointermove', ((e: PointerEvent) => {
      if (!gesture) {
        const r = lens.getBoundingClientRect();
        lens.classList.toggle('held', e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom);
        return;
      }
      if (e.pointerId !== gesture.id) return;
      const delta = coord(e) - gesture.start;
      if (!gesture.moved && Math.abs(delta) < 5) return;
      if (!gesture.moved) { gesture.moved = true; surface.setPointerCapture(e.pointerId); surface.classList.add('scrubbing'); }
      const size = axis === 'x' ? surface.clientWidth : surface.clientHeight;
      const lensSize = axis === 'x' ? lens.offsetWidth : lens.offsetHeight;
      moveLens(lens, axis, Math.max(6, Math.min(size - lensSize - 6, gesture.origin + delta)), true);
    }) as EventListener);
    listen(surface, 'pointerup', ((e: PointerEvent) => {
      if (!gesture || e.pointerId !== gesture.id) return;
      const moved = gesture.moved;
      const center = origin() + (axis === 'x' ? lens.offsetWidth : lens.offsetHeight) / 2;
      const items = [...surface.querySelectorAll<HTMLElement>(itemSelector)];
      const target = items.reduce((best, item) => {
        const distance = (el: HTMLElement) => Math.abs(center - (axis === 'x' ? el.offsetLeft + el.offsetWidth / 2 : el.offsetTop + el.offsetHeight / 2));
        return distance(item) < distance(best) ? item : best;
      }, items[0]);
      suppressClick = moved;
      clear();
      if (moved) target?.click();
      sync();
    }) as EventListener);
    listen(surface, 'pointercancel', cancel);
    listen(surface, 'lostpointercapture', ((e: PointerEvent) => { if (e.target === surface && gesture) cancel(); }) as EventListener);
    listen(surface, 'pointerleave', (() => { if (!gesture) lens.classList.remove('held'); }) as EventListener);
    listen(window, 'blur', cancel);
    listen(window, 'resize', (() => { if (gesture) cancel(); sync(true); }) as EventListener);
    // Observe selection only. Lens feedback must never trigger automatic settling.
    const observer = new MutationObserver(() => sync());
    for (const item of surface.querySelectorAll(itemSelector)) observer.observe(item, { attributes: true, attributeFilter: ['aria-current'] });
    const resize = new ResizeObserver(() => { if (!gesture) sync(true); });
    resize.observe(surface);
    requestAnimationFrame(() => sync(true));
    document.fonts?.ready.then(() => sync(true));
    cleanups.push(() => { observer.disconnect(); resize.disconnect(); clear(); });
  }
  return () => { controller.abort(); cleanups.forEach(cleanup => cleanup()); };
}

export function updateThemeToggle(container: ParentNode, theme: Theme) {
  const label = themeLabel(theme);
  container.querySelectorAll<HTMLButtonElement>("#theme-toggle").forEach((btn) => {
    btn.innerHTML = themeIcon(theme);
    btn.setAttribute("aria-label", `切换主题，当前：${label}`);
    btn.title = label;
  });
}

export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "dark") {
    root.setAttribute("data-theme", "dark");
  } else if (theme === "light") {
    root.setAttribute("data-theme", "light");
  } else {
    root.removeAttribute("data-theme");
  }
  const meta = document.querySelector('meta[name="theme-color"]') as HTMLMetaElement | null;
  if (meta) {
    const dark = theme === "dark" || (theme === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    meta.content = dark ? "rgb(8, 10, 16)" : "rgb(243, 239, 233)";
  }
}

/**
 * The 外观 control shared by the setup flow and the settings page. Segments
 * carry `data-theme`, so a section only has to bind `[data-theme]` clicks to
 * `store.setUi({ theme })` + `applyTheme`; state lives in `ui.theme`, which the
 * store persists.
 */
export function renderThemeControl(theme: Theme): string {
  const options: Array<{ value: Theme; label: string }> = [
    { value: "auto", label: "跟随系统" },
    { value: "light", label: "浅色" },
    { value: "dark", label: "深色" },
  ];
  return `<div class="segmented-control" role="group" aria-label="外观">
    ${options
      .map(
        (item) => `<button class="segment ${item.value === theme ? "active" : ""}" type="button"
              data-theme="${item.value}" aria-pressed="${item.value === theme}">${item.label}</button>`
      )
      .join("")}
  </div>`;
}

function themeIcon(theme: Theme): string {
  switch (theme) {
    case "light":
      return icon("sun");
    case "dark":
      return icon("moon");
    default:
      return icon("appearance");
  }
}

function themeLabel(theme: Theme): string {
  switch (theme) {
    case "light":
      return "浅色模式";
    case "dark":
      return "深色模式";
    default:
      return "跟随系统";
  }
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
