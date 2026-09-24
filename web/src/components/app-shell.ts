import type { Section, Theme } from "../state";
import { brandMark, icon, type IconName } from "../icons";

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

      <div class="fnos-toolbar">
        <button class="icon-button fnos-theme-toggle" id="fnos-theme-toggle" aria-label="切换主题" title="${themeLabel(theme)}">
          ${themeIcon(theme)}
        </button>
      </div>

      <div class="app-body">
        <nav class="sidebar" role="tablist" aria-label="主导航">
          ${navItems
            .map(
              (item) => `
                <button class="nav-item ${item.id === activeSection ? "active" : ""}"
                        data-section="${item.id}" role="tab" aria-selected="${item.id === activeSection}">
                  <span class="nav-icon">${icon(item.icon)}</span>
                  ${item.label}
                </button>
              `
            )
            .join("")}
        </nav>
        <main class="main-content page-view">${content}</main>
      </div>

      <nav class="tab-bar" role="tablist" aria-label="底部导航">
        ${navItems
          .map(
            (item) => `
              <button class="tab-item ${item.id === activeSection ? "active" : ""}"
                      data-section="${item.id}" role="tab" aria-selected="${item.id === activeSection}">
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
  container.querySelectorAll("#theme-toggle, #fnos-theme-toggle").forEach((btn) => btn.addEventListener("click", onToggle));
}

export function updateThemeToggle(container: ParentNode, theme: Theme) {
  const label = themeLabel(theme);
  container.querySelectorAll<HTMLButtonElement>("#theme-toggle, #fnos-theme-toggle").forEach((btn) => {
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
    meta.content = dark ? "rgb(14, 14, 16)" : "rgb(245, 245, 247)";
  }
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
