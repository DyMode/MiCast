import type { State } from "../state";
import { escapeHtml } from "./receivers-shared";

export function renderToast(state: State["toast"]): string {
  return `
    <div class="toast ${state.visible ? "visible" : ""}" role="status" aria-live="polite">
      ${escapeHtml(state.message)}
    </div>
  `;
}
