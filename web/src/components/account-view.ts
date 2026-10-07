import { SurfaceScope } from "../ui/lifecycle";
import type { State } from "../state";
import { api } from "../api";
import { store } from "../state";
import { renderDeviceProofs, bindDeviceProofs } from "./device-capabilities";

let accountScope: SurfaceScope | null = null;
export function disposeAccountView() {
  accountScope?.dispose();
  accountScope = null;
}

function mijiaIcon(): string {
  return '<img class="mijia-brand-icon" src="assets/brands/mijia-app.png" alt="">';
}

function escHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

export function renderAccountView(state: State): string {
  return `<button class="button plain back-link" data-account-back>‹ 返回设置</button>${renderAccountBody(state)}`;
}

function renderAccountBody(state: State): string {
  const { xiaomi } = state;
  const connectionError = state.deviceLoadError;
  if (xiaomi.logged_in) {
    if (connectionError) {
      const credentialsInvalid = /HTTP 401|登录已失效|重新连接/.test(connectionError);
      return `
        <div class="page-heading">
          <h2 class="page-title">服务</h2>
          <p>连接音箱品牌服务，自动同步可用设备。</p>
        </div>
        <div class="group-header">需要处理</div>
        <div class="group">
          <div class="cell provider-row">
            <div class="cell-icon device-brand mijia">${mijiaIcon()}</div>
            <div class="cell-content">
              <span class="cell-title">米家</span>
              <span class="cell-subtitle">${credentialsInvalid ? "登录已失效，设备配置已保留" : "连接异常，设备配置已保留"}</span>
            </div>
          </div>
          <div class="cell provider-actions">
            ${credentialsInvalid
              ? `<button class="button primary" id="btn-qr-login">重新登录</button>
                 <button class="button plain" id="btn-other-login">登录其他账号</button>`
              : `<button class="button primary" id="btn-retry-provider">重试连接</button>
                 <button class="button plain" id="btn-other-login">重新登录</button>`}
          </div>
        </div>
        ${credentialsInvalid ? `<p class="section-note">重新登录同一账号会恢复原配置；登录其他账号后，旧账号的设备、入口和分组将被清理。</p>` : ""}
      `;
    }
    return `
      <div class="page-heading">
        <h2 class="page-title">服务</h2>
        <p>连接音箱品牌服务，自动同步可用设备。</p>
      </div>

      <div class="group-header">登录凭据已保存</div>
      <div class="group">
        <div class="cell provider-row">
          <div class="cell-icon device-brand mijia">${mijiaIcon()}</div>
          <div class="cell-content">
            <span class="cell-title">米家</span>
            <span class="cell-subtitle">登录凭据已保存${state.devices.length ? ` · ${state.devices.length} 台音箱` : ""}</span>
          </div>
          <button class="button plain danger-text provider-disconnect" id="btn-logout">退出米家账号</button>
        </div>
        <div class="cell settings-input-cell">
          <div class="cell-content">
            <span class="cell-title">登录失效通知</span>
            <span class="cell-subtitle">小米登录失效时发送提醒；留空关闭</span>
          </div>
          <input type="url" class="input" id="notify-webhook" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…" value="${escHtml(state.fullConfig?.notify_webhook_url ?? "")}" aria-label="通知 Webhook 地址">
          <div class="settings-save-row"><span class="caption" id="notify-webhook-status" aria-live="polite"></span></div>
        </div>
      </div>
      <p class="section-note">音频通过局域网传输，获取音箱列表和下发播放命令仍需访问小米云端。退出账号会删除本机保存的登录凭据，音箱列表将不再显示，也无法发起新的播放；音箱配置仍会保留，重新登录同一账号并成功获取设备后恢复。</p>

      <div class="group-header">指令验证</div>
      <p class="group-header-hint">协议声明不代表实际出声；验证记录随账号保存，切换账号后重新记录。</p>
      ${renderDeviceProofs()}
    `;
  }

  return `
    <div class="page-heading">
      <h2 class="page-title">服务</h2>
      <p>连接音箱品牌服务，自动同步可用设备。</p>
    </div>

    <div class="group-header">可连接</div>
    <div class="group">
      <div class="cell clickable" id="btn-qr-login">
        <div class="cell-icon device-brand mijia">${mijiaIcon()}</div>
        <div class="cell-content">
          <span class="cell-title">米家</span>
          <span class="cell-subtitle">使用米家 App 扫码连接</span>
        </div>
        <span class="caption">›</span>
      </div>
    </div>

    <p class="section-note">未登录时不显示米家音箱列表。退出账号不会删除已保存的音箱配置，重新登录同一账号并成功获取设备后恢复。</p>

    <button class="button plain advanced-login-toggle" id="btn-cookie-login">使用凭据连接</button>

    <div id="cookie-form" style="display: none;">
      <div class="group-header">连接凭据</div>
      <div class="group">
        <div class="cell" style="flex-direction: column; align-items: stretch; gap: var(--space-md); padding: var(--space-lg);">
          <input type="text" id="cookie-user-id" placeholder="userId" class="input">
          <input type="text" id="cookie-pass-token" placeholder="passToken" class="input">
          <button class="button primary full" id="btn-cookie-submit">连接</button>
          <button class="button plain" id="btn-cookie-cancel">取消</button>
        </div>
      </div>
    </div>
  `;
}

export function bindAccountView(
  container: HTMLElement,
  handlers: {
    onBack: () => void;
    onLogout: () => void;
    onQRLogin: () => void;
    onRetry: () => void;
    onCookieLogin: (userId: string, passToken: string) => void;
  }
) {
  container.querySelector("[data-account-back]")?.addEventListener("click", handlers.onBack);
  container.querySelector("#btn-logout")?.addEventListener("click", handlers.onLogout);
  container.querySelector("#btn-qr-login")?.addEventListener("click", handlers.onQRLogin);
  container.querySelector("#btn-other-login")?.addEventListener("click", handlers.onQRLogin);
  container.querySelector("#btn-retry-provider")?.addEventListener("click", handlers.onRetry);
  disposeAccountView();
  const scope = accountScope = new SurfaceScope();
  bindDeviceProofs(container);

  const webhookInput = container.querySelector<HTMLInputElement>("#notify-webhook");
  const webhookStatus = container.querySelector<HTMLElement>("#notify-webhook-status");
  webhookInput?.addEventListener("input", () => {
    scope.debounce("webhook", async () => {
      const current = store.beginRead("notify-webhook", true);
      const url = webhookInput.value.trim();
      try {
        await api.setNotifyWebhook(url);
        if (!scope.active || !current()) return;
        const config = store.get().fullConfig;
        if (config) store.set({ fullConfig: { ...config, notify_webhook_url: url } });
        if (webhookStatus) webhookStatus.textContent = url ? "已保存" : "已关闭";
        store.showToast(url ? "通知地址已保存" : "登录失效通知已关闭");
      } catch (e) {
        if (!scope.active || !current()) return;
        if (webhookStatus) webhookStatus.textContent = "";
        store.showToast(`保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
      }
    }, 600);
  });

  const cookieBtn = container.querySelector("#btn-cookie-login");
  const cookieForm = container.querySelector("#cookie-form");
  cookieBtn?.addEventListener("click", () => {
    if (cookieForm) {
      (cookieForm as HTMLElement).style.display = "block";
    }
  });

  container.querySelector("#btn-cookie-cancel")?.addEventListener("click", () => {
    if (cookieForm) {
      (cookieForm as HTMLElement).style.display = "none";
    }
  });

  container.querySelector("#btn-cookie-submit")?.addEventListener("click", () => {
    const userId = (container.querySelector("#cookie-user-id") as HTMLInputElement)?.value;
    const passToken = (container.querySelector("#cookie-pass-token") as HTMLInputElement)?.value;
    if (!userId || !passToken) {
      // handled by caller
      return;
    }
    handlers.onCookieLogin(userId, passToken);
  });
}
