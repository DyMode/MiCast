import type { State } from "../state";
import { escapeHtml } from "./receivers-shared";

export function renderQRSheet(state: State): string {
  const qr = state.qr;
  if (!qr.open) return "";
  return `
    <div class="sheet-overlay ${qr.open ? "open" : ""}" data-close-qr></div>
    <div class="sheet ${qr.open ? "open" : ""}" role="dialog" aria-modal="true">
      <div class="sheet-handle"></div>
      <h2 class="title-2" style="text-align: center; margin-bottom: var(--space-xs);">连接米家</h2>
      <p class="caption" style="text-align: center; margin-bottom: var(--space-xl);">
        使用米家 App 扫描下方二维码
      </p>
      <div class="qr-card">
        ${
          qr.qrUrl
            ? `<img src="api/xiaomi/login/qr/image?url=${encodeURIComponent(qr.qrUrl)}" alt="米家连接二维码">`
            : `<div class="qr-placeholder">
                 ${
                   qr.state === "error"
                     ? `<span class="caption danger-text">${escapeHtml(qr.error || "未能获取二维码")}</span>`
                     : `<span class="caption">${
                         // Cloud calls for QR login can take ~20s before they
                         // fail; "加载中" alone left the user staring at a blank
                         // box with no idea whether anything was happening.
                         qr.state === "idle" ? "正在连接小米账号服务器…" : "正在获取二维码…"
                       }</span>`
                 }
               </div>`
        }
      </div>
      <div style="text-align: center; margin-bottom: var(--space-xl);">
        <span class="status-pill ${qr.state === "confirmed" ? "running" : qr.state === "expired" || qr.state === "error" ? "error" : ""}">
          ${stateLabel(qr.state)}
        </span>
      </div>
      ${cloudHint(state)}
      <div class="sheet-actions">
        ${qr.state === "error" ? `<button class="button primary full" type="button" data-qr-retry>重试</button>` : ""}
        <button class="button secondary full" data-close-qr>取消</button>
      </div>
    </div>
  `;
}

/** A scan cannot work when the device cannot reach Xiaomi's account servers,
 *  and the generic "check your network" advice is useless at this point. */
function cloudHint(state: State): string {
  if (state.xiaomi.status !== "unstable") return "";
  return `<p class="caption danger-text" style="text-align: center; margin-bottom: var(--space-md);">
    检测到这台设备连不上小米云端：扫码登录同样需要访问小米账号服务器，请先检查外网、DNS 或代理设置。
  </p>`;
}

function stateLabel(state: State["qr"]["state"]): string {
  const labels: Record<State["qr"]["state"], string> = {
    idle: "正在连接",
    waiting: "等待扫码",
    scanned: "已扫描，等待确认",
    confirmed: "登录成功",
    expired: "二维码已过期",
    error: "获取失败",
  };
  return labels[state];
}

export function bindQRSheet(container: HTMLElement, onClose: () => void, onRetry: () => void = () => {}) {
  container.querySelectorAll("[data-close-qr]").forEach((el) => {
    el.addEventListener("click", onClose);
  });
  container.querySelectorAll("[data-qr-retry]").forEach((el) => {
    el.addEventListener("click", onRetry);
  });
}
