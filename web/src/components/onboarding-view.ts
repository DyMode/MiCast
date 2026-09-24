import type { State, Theme } from "../state";
import { brandIcon, brandMark } from "../icons";
import { renderThemeControl } from "./app-shell";

function logo(): string {
  return `<span class="setup-logo">${brandMark()}</span><strong>MiCast</strong>`;
}

function qrPanel(state: State): string {
  const labels = {
    idle: "准备二维码",
    waiting: "等待扫码",
    scanned: "已扫码，请在手机上确认",
    confirmed: "米家已连接",
    expired: "二维码已过期",
  };
  return `<div class="setup-qr" aria-live="polite">
    ${state.qr.qrUrl
      ? `<img src="api/xiaomi/login/qr/image?url=${encodeURIComponent(state.qr.qrUrl)}" alt="米家连接二维码">`
      : `<div class="setup-qr-placeholder"><span class="setup-progress"></span></div>`}
    <div><strong>${labels[state.qr.state]}</strong><p>打开米家 App，扫描二维码后确认登录。</p></div>
  </div>`;
}

export function renderOnboarding(state: State): string {
  const step = state.access?.access_configured ? state.onboardingStep : "access";
  const supportsAirPlay2 = state.fullConfig?.airplay2_available ?? false;
  // Step order: 管理访问 → 连接米家 → 播放入口 → (AirPlay 2) → complete
  const orderedSteps = ["access", "xiaomi", "receivers", ...(supportsAirPlay2 ? ["airplay2"] : [])];
  const stepIndex = (key: string) => orderedSteps.indexOf(key);
  const currentIndex = stepIndex(step);
  const stepState = (key: string) => (stepIndex(key) < currentIndex || step === "complete" ? "done" : step === key ? "active" : "");
  const stepNumber = (key: string) => (stepState(key) === "done" ? "✓" : String(stepIndex(key) + 1));
  // Surface prerequisites right in the step list: a step you cannot finish yet
  // is labelled, not silently absent (esp. on the fnOS single-instance build
  // where AirPlay 2 is the headline step).
  const xiaomiReady = state.xiaomi.logged_in || state.qr.state === "confirmed";
  // 回退目标沿可见链路；完成页无需回退，扫码确认自动前进时也收起“上一步”。
  const backTargets: Record<string, string> = {
    xiaomi: "access",
    receivers: "xiaomi",
    airplay2: xiaomiReady ? "receivers" : "xiaomi",
  };
  const showBack = step in backTargets && !(step === "xiaomi" && state.qr.state === "confirmed");
  const backButton = showBack
    ? `<button class="button plain" type="button" data-setup-back="${backTargets[step]}">上一步</button>`
    : "";
  const targetOptions = state.devices.map((item) => `<option value="speaker:${escapeHtml(item.did)}">${escapeHtml(item.alias || item.name)}</option>`).join("");
  const addableSpeakers = state.devices.filter(
    (item) => !state.fullConfig?.receivers.some((r) => r.target_type === "speaker" && r.target_id === item.did)
  );
  return `<main class="setup-page">
    <header class="setup-brand">${logo()}</header>
    <div class="setup-shell">
      <nav class="setup-steps" aria-label="部署进度">
        <div class="setup-step ${stepState("access")}"><span>${stepNumber("access")}</span><div><strong>管理访问</strong><small>设置进入 MiCast 的方式</small></div></div>
        <div class="setup-step ${stepState("xiaomi")}"><span>${stepNumber("xiaomi")}</span><div><strong>连接米家</strong><small>同步并控制你的音箱</small></div></div>
        <div class="setup-step ${stepState("receivers")}"><span>${stepNumber("receivers")}</span><div><strong>播放入口</strong><small>一键添加全部音箱${stepIndex("receivers") > currentIndex && !xiaomiReady ? ` <em class="step-tag">需先连接米家</em>` : ""}</small></div></div>
        ${supportsAirPlay2 ? `<div class="setup-step ${stepState("airplay2")}"><span>${stepNumber("airplay2")}</span><div><strong>AirPlay 2 <em class="feature-badge">实验性</em></strong><small>按需开启独立入口${stepIndex("airplay2") > currentIndex && !xiaomiReady ? ` <em class="step-tag">需先连接米家</em>` : ""}</small></div></div>` : ""}
      </nav>
      <section class="setup-content">
        ${step === "access" ? `<div class="setup-copy"><h1>管理访问</h1><p>选择谁可以打开和管理这台 MiCast。</p></div>
          <form class="setup-form" data-setup-access>
            <label class="choice-row selected"><input type="radio" name="access_mode" value="protected" checked><span><strong>使用账号密码</strong><small>限制谁可以管理 MiCast</small></span></label>
            <div class="setup-credentials" data-setup-credentials>
              <label>用户名<input class="input" name="username" autocomplete="username" maxlength="64" value="${escapeHtml(state.access?.username || "admin")}" required></label>
              <label>密码<input class="input" name="password" type="password" autocomplete="new-password" minlength="6" required></label>
              <label>确认密码<input class="input" name="password_confirm" type="password" autocomplete="new-password" minlength="6" required></label>
            </div>
            <label class="choice-row warning-choice"><input type="radio" name="access_mode" value="open"><span><strong>不设置管理账号</strong><small>局域网内无需登录</small></span></label>
            <p class="form-error" data-setup-error aria-live="polite" hidden></p>
            <div class="setup-actions"><button class="button primary" type="submit">下一步</button></div>
          </form>` : step === "xiaomi" ? `<div class="setup-copy"><h1>连接米家</h1><p>连接后会自动发现账号中的小爱音箱。</p></div>
          ${state.xiaomi.logged_in || state.qr.state === "confirmed"
            ? `<div class="setup-success"><span>✓</span><div><strong>米家已连接</strong><p>${state.qr.state === "confirmed" ? "正在进入下一步…" : "音箱将在进入应用后自动同步。"}</p></div></div>`
            : state.qr.open ? qrPanel(state) : `<div class="setup-provider"><img src="assets/brands/mijia-app.png" alt=""><div><strong>米家</strong><p>使用米家 App 扫码连接</p></div><button class="button primary" type="button" data-setup-xiaomi>开始连接</button></div>`}
          ${state.qr.state === "confirmed" ? "" : `<div class="setup-actions split">${backButton}${state.xiaomi.logged_in
            ? `<button class="button primary" type="button" data-setup-skip>继续</button>`
            : `<button class="button plain" type="button" data-setup-skip>暂时跳过</button>`}${state.qr.state === "expired" ? `<button class="button primary" type="button" data-setup-xiaomi>刷新二维码</button>` : ""}</div>`}`
          : step === "receivers" ? `<div class="setup-copy"><h1>播放入口</h1><p>把音箱添加为 AirPlay 播放入口，即可在手机 AirPlay 列表选择。</p></div>
          ${addableSpeakers.length
            ? `<div class="setup-provider"><span class="cell-icon device-brand xiaomi">${brandIcon("xiaomi")}</span><div><strong>发现 ${addableSpeakers.length} 台音箱</strong><p>${escapeHtml(addableSpeakers.slice(0, 4).map((item) => item.alias || item.name).join("、"))}${addableSpeakers.length > 4 ? ` 等` : ""}</p></div></div>`
            : state.deviceLoadError
              ? `<div class="setup-provider"><span class="cell-icon">⚠</span><div><strong>音箱列表加载失败</strong><p>${escapeHtml(state.deviceLoadError)}</p></div><button class="button primary" type="button" data-receivers-refresh>重新扫描</button></div>`
              : `<div class="setup-success"><span>✓</span><div><strong>${state.devices.length ? "音箱都已有播放入口" : "暂未发现音箱"}</strong><p>之后可以在「播放」页随时添加。</p></div></div>${state.devices.length ? "" : `<div class="setup-actions"><button class="button plain" type="button" data-receivers-refresh>重新扫描</button></div>`}`}
          <div class="setup-actions split">${backButton}<button class="button plain" type="button" data-receivers-skip>${addableSpeakers.length ? "暂不添加" : "继续"}</button>${addableSpeakers.length ? `<button class="button primary" type="button" data-receivers-add-all>一键添加 ${addableSpeakers.length} 台音箱</button>` : ""}</div>`
          : step === "airplay2" ? `<div class="setup-copy"><h1>AirPlay 2 <span class="feature-badge">实验性</span></h1><p>开启后会增加一个独立播放入口，并占用少量系统资源。${xiaomiReady ? "" : "选择播放目标需要先连接米家。"}</p></div>
          <form class="setup-form" data-setup-airplay2>
            <label class="choice-row selected"><input type="radio" name="airplay2_enabled" value="false" checked><span><strong>暂不开启</strong><small>之后可随时在设置中开启</small></span></label>
            <label class="choice-row"><input type="radio" name="airplay2_enabled" value="true"><span><strong>开启 AirPlay 2</strong><small>创建一个独立的 AirPlay 2 播放入口</small></span></label>
            ${targetOptions ? `<div class="setup-credentials setup-target" data-airplay2-target hidden><label>播放目标<select class="input" name="target">${targetOptions}</select></label><p class="caption">AirPlay 2 会将声音播放到这台音箱，之后可在设置中更改。</p></div>` : ""}
            <div class="setup-actions split">${backButton}<button class="button primary" type="submit">继续</button></div>
          </form>`
          : `<div class="setup-complete"><span>✓</span><h1>已经准备好了</h1><p>之后可随时在设置中调整。</p>
          <div class="setup-appearance"><strong>外观</strong>${renderThemeControl(state.ui.theme)}</div>
          <div class="setup-actions"><button class="button primary" type="button" data-setup-enter>开始使用</button></div></div>`}
      </section>
    </div>
  </main>`;
}

export function bindOnboarding(container: HTMLElement, handlers: {
  onAccess: (payload: { auth_enabled: boolean; username: string; password: string; password_confirm: string }) => Promise<void>;
  onXiaomi: () => void;
  onReview: () => Promise<void>;
  onBack: (step: string) => void;
  onAddReceivers: () => Promise<void>;
  onSkipReceivers: () => void;
  onRefreshReceivers: () => Promise<void>;
  onAirPlay2: (enabled: boolean, target: string | null) => Promise<void>;
  onTheme: (theme: Theme) => void;
  onComplete: () => Promise<void>;
}) {
  const form = container.querySelector<HTMLFormElement>("[data-setup-access]");
  const syncMode = () => {
    const protectedMode = form?.querySelector<HTMLInputElement>('input[name="access_mode"]:checked')?.value === "protected";
    container.querySelectorAll<HTMLElement>(".choice-row").forEach((row) => row.classList.toggle("selected", (row.querySelector("input") as HTMLInputElement)?.checked));
    const fields = container.querySelector<HTMLElement>("[data-setup-credentials]");
    if (fields) fields.hidden = !protectedMode;
    fields?.querySelectorAll<HTMLInputElement>("input").forEach((input) => input.required = protectedMode);
  };
  form?.querySelectorAll<HTMLInputElement>('input[name="access_mode"]').forEach((input) => input.addEventListener("change", syncMode));
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const authEnabled = data.get("access_mode") === "protected";
    const button = form.querySelector<HTMLButtonElement>('button[type="submit"]');
    const error = form.querySelector<HTMLElement>("[data-setup-error]");
    if (button) { button.disabled = true; button.textContent = "正在保存…"; }
    if (error) error.hidden = true;
    await handlers.onAccess({
      auth_enabled: authEnabled,
      username: String(data.get("username") || "admin"),
      password: authEnabled ? String(data.get("password") || "") : "",
      password_confirm: authEnabled ? String(data.get("password_confirm") || "") : "",
    }).catch((reason) => {
      if (button) { button.disabled = false; button.textContent = "下一步"; }
      if (error) { error.hidden = false; error.textContent = reason instanceof Error ? readableError(reason) : "无法保存设置"; }
    });
  });
  container.querySelectorAll<HTMLElement>("[data-setup-xiaomi]").forEach((button) => button.addEventListener("click", handlers.onXiaomi));
  container.querySelectorAll<HTMLElement>("[data-setup-back]").forEach((button) =>
    button.addEventListener("click", () => handlers.onBack(button.dataset.setupBack!))
  );
  const reviewButton = container.querySelector<HTMLButtonElement>("[data-setup-skip]");
  reviewButton?.addEventListener("click", async () => {
    reviewButton.disabled = true;
    try { await handlers.onReview(); }
    finally { reviewButton.disabled = false; }
  });
  const refreshButton = container.querySelector<HTMLButtonElement>("[data-receivers-refresh]");
  refreshButton?.addEventListener("click", async () => {
    refreshButton.disabled = true;
    refreshButton.textContent = "正在扫描…";
    try { await handlers.onRefreshReceivers(); }
    finally { refreshButton.disabled = false; refreshButton.textContent = "重新扫描"; }
  });
  container.querySelector<HTMLElement>("[data-receivers-skip]")?.addEventListener("click", () => handlers.onSkipReceivers());
  const addAllButton = container.querySelector<HTMLButtonElement>("[data-receivers-add-all]");
  addAllButton?.addEventListener("click", async () => {
    addAllButton.disabled = true;
    addAllButton.textContent = "正在添加…";
    // 添加进行中，收起操作行里的其他入口，避免跳过或回退与添加结果打架。
    const siblings = container.querySelectorAll<HTMLElement>("[data-setup-back], [data-receivers-skip]");
    siblings.forEach((el) => (el.hidden = true));
    try { await handlers.onAddReceivers(); }
    catch {
      addAllButton.disabled = false;
      addAllButton.textContent = "一键添加全部音箱";
      siblings.forEach((el) => (el.hidden = false));
    }
  });
  const airplay2Form = container.querySelector<HTMLFormElement>("[data-setup-airplay2]");
  const syncAirPlay2 = () => {
    const enabled = airplay2Form?.querySelector<HTMLInputElement>('input[name="airplay2_enabled"]:checked')?.value === "true";
    const target = airplay2Form?.querySelector<HTMLElement>("[data-airplay2-target]");
    if (target) target.hidden = !enabled;
    airplay2Form?.querySelectorAll<HTMLElement>(".choice-row").forEach((row) => row.classList.toggle("selected", (row.querySelector("input") as HTMLInputElement)?.checked));
  };
  airplay2Form?.querySelectorAll<HTMLInputElement>('input[name="airplay2_enabled"]').forEach((input) => input.addEventListener("change", syncAirPlay2));
  airplay2Form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(airplay2Form);
    const enabled = data.get("airplay2_enabled") === "true";
    const button = airplay2Form.querySelector<HTMLButtonElement>('button[type="submit"]');
    if (button) { button.disabled = true; button.textContent = "正在保存…"; }
    try { await handlers.onAirPlay2(enabled, enabled ? String(data.get("target") || "") || null : null); }
    catch { if (button) { button.disabled = false; button.textContent = "继续"; } }
  });
  container.querySelector<HTMLElement>("[data-setup-enter]")?.addEventListener("click", handlers.onComplete);
  container.querySelectorAll<HTMLElement>("[data-theme]").forEach((el) =>
    el.addEventListener("click", () => handlers.onTheme(el.dataset.theme as Theme))
  );
}

export function renderAccessLogin(access: NonNullable<State["access"]>): string {
  return `<main class="login-page"><form class="login-panel" data-access-login>
    <div class="login-brand">${logo()}</div><div><h1>欢迎回来</h1><p>登录后继续管理播放和音箱。</p></div>
    <label>用户名<input class="input" name="username" autocomplete="username" value="${escapeHtml(access.username)}" required autofocus></label>
    <label>密码<input class="input" name="password" type="password" autocomplete="current-password" required></label>
    <p class="form-error" data-login-error hidden></p><button class="button primary full" type="submit">登录</button>
  </form></main>`;
}

export function bindAccessLogin(container: HTMLElement, onLogin: (username: string, password: string) => Promise<void>) {
  const form = container.querySelector<HTMLFormElement>("[data-access-login]");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const button = form.querySelector<HTMLButtonElement>('button[type="submit"]');
    const error = form.querySelector<HTMLElement>("[data-login-error]");
    if (button) { button.disabled = true; button.textContent = "正在登录…"; }
    if (error) error.hidden = true;
    try { await onLogin(String(data.get("username")), String(data.get("password"))); }
    catch (reason) {
      if (button) { button.disabled = false; button.textContent = "登录"; }
      if (error) { error.hidden = false; error.textContent = reason instanceof Error ? readableError(reason) : "登录失败"; }
    }
  });
}

export function renderXiaomiRecovery(state: State): string {
  if (state.xiaomi.status !== "expired" || state.recoveryDismissed || state.qr.open) return "";
  return `<div class="sheet-overlay open" data-recovery-close></div><section class="sheet open recovery-sheet" role="dialog" aria-modal="true" aria-labelledby="recovery-title">
    <button class="sheet-close" type="button" data-recovery-close aria-label="稍后处理">×</button>
    <img class="recovery-provider-icon" src="assets/brands/mijia-app.png" alt="">
    <h2 id="recovery-title">重新连接米家</h2><p>登录已失效。你的音箱、组合和播放设置都已保留。</p>
    <button class="button primary full" type="button" data-recovery-login>重新连接米家</button>
    <button class="button plain full" type="button" data-recovery-close>稍后处理</button>
  </section>`;
}

function readableError(error: Error): string {
  const match = error.message.match(/"detail"\s*:\s*"([^"]+)"/);
  return match?.[1] || error.message;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char]!));
}
