let debugInteraction = false;
import { api } from "./api";
import "./volume-actions";
import { renderAccountView, bindAccountView } from "./components/account-view";
import {
  applyTheme,
  bindNavigation,
  bindTabBarDrag,
  bindThemeToggle,
  renderAppShell,
  updateThemeToggle,
} from "./components/app-shell";
import { updateDebugPanel, bindDebugPanel, bindStreamKicks, currentLogQuery, renderAudioPath, renderStatusOverview, renderDebugPanel, renderStreamRows, renderStreamSummary, renderTechnicalMetrics, updateLogChrome, updateRuntimeLog, type DebugState } from "./components/debug-panel";
import { bindDevicesView, renderDevicesView } from "./components/devices-view";
import { bindTuningView, disposeTuningView, renderTuningView } from "./components/tuning-view";
import { renderQRSheet, bindQRSheet } from "./components/qr-sheet";
import { bindReceiversView, renderReceiversView } from "./components/receivers-view";
import { bindSettingsView, disposeSettingsView, renderSettingsView } from "./components/settings-view";
import { bindAirPlay2View, renderAirPlay2View } from "./components/airplay2-view";
import { renderToast } from "./components/toast";
import { bindAccessLogin, bindOnboarding, renderAccessLogin, renderOnboarding, renderXiaomiRecovery } from "./components/onboarding-view";
import { syncPlayerChrome } from "./player/sync";
import { updateMediaSession } from "./media-session";
import { bindTopologyView, renderTopologyView } from "./components/topology-view";
import { store, type Section, type State, type Theme } from "./state";
import type { PlaybackState, Status } from "./api";
import { RealtimeConnection } from "./realtime";
import "./styles.css";
import { safeUserMessage } from "./errors";
import { bindRoutes } from './navigation';
import { bindConnectivity } from './connectivity';
import { syncModal } from './ui/modal';
import { escapeHtml } from './components/receivers-shared';
import { SurfaceScope } from './ui/lifecycle';
const applicationScope = new SurfaceScope();
applicationScope.listen(window, 'pagehide', ((event: PageTransitionEvent) => {
  if (!event.persisted) applicationScope.dispose();
}) as EventListener);

// fnOS presents MiCast inside its own titled window/sheet. Mark that context
// once, before the first render, so the web shell does not duplicate the host
// chrome while the standalone browser and desktop app keep their header.
document.documentElement.classList.toggle("is-embedded", window.self !== window.top);

let shellMounted = false;
// The topology view owns a canvas render loop and a shared-event listener; torn down
// whenever the main content is about to be replaced.
let topologyCleanup: (() => void) | null = null;
// Content scrolls inside .app-body; switching sections starts at the top.
let lastRenderedSection: Section | null = null;
let lastRenderedTuningDid: string | null = null;
let lastMainMarkup = '';
let bootError = '';
let shellCleanup: (() => void) | null = null;

function disposeMain() {
  topologyCleanup?.();
  topologyCleanup = null;
  disposeSettingsView();
  if (lastRenderedTuningDid) disposeTuningView();
  lastMainMarkup = '';
}

function render(state: State) {
  const app = document.getElementById("app");
  if (!app) return;

  if (!state.access) {
    shellCleanup?.(); shellCleanup = null;
    disposeMain();
    app.innerHTML = `<main class="boot-page"><div class="boot-content">${bootError
      ? `<h1 class="title-2">暂时无法连接 MiCast</h1><p role="alert">${escapeHtml(bootError)}</p><button class="button primary" data-boot-retry>重新连接</button>`
      : `<span class="setup-progress" aria-label="正在加载"></span><p>正在连接 MiCast…</p>`}</div></main>${renderToast(state.toast)}`;
    app.querySelector('[data-boot-retry]')?.addEventListener('click', () => { bootError = ''; void init(); });
    shellMounted = false;
    return;
  }
  if (!state.access.setup_complete) {
    shellCleanup?.(); shellCleanup = null;
    disposeMain();
    app.innerHTML = renderOnboarding(state) + renderToast(state.toast);
    bindOnboarding(app, {
      onAccess: async (payload) => {
        const current = store.beginRead('onboarding-access', true);
        const valid = () => applicationScope.active && current() && store.get().onboardingStep === 'access';
        try {
          // 退回第一步重设时，管理访问已经配置过，须走设置接口；首次 setup 会 409。
          if (store.get().access?.access_configured) await api.updateAccess(payload);
          else await api.setupAccess(payload);
          const [access, xiaomi] = await Promise.all([
            api.getAccessStatus(),
            api.getXiaomiStatus().catch(() => null),
          ]);
          if (!valid()) return;
          store.set({ access, ...(xiaomi ? { xiaomi } : {}), onboardingStep: "xiaomi" });
          render(store.get());
          // 已经连着米家（例如重跑引导）时不必再等人点“继续”。
          if (xiaomi?.logged_in) scheduleXiaomiAutoAdvance();
        } catch (error) {
          if (!valid()) return;
          store.showToast(`保存失败：${friendlyError(error)}`);
          throw error;
        }
      },
      onXiaomi: startQRLogin,
      onReview: advanceFromXiaomi,
      onBack: (step) => {
        window.clearTimeout(xiaomiAutoTimer);
        store.beginRead('qr-login');
        store.beginRead('onboarding-account');
        store.beginRead('onboarding-access');
        store.beginRead('initial-onboarding-account');
        store.set({
          onboardingStep: step as State["onboardingStep"],
          qr: { open: false, qrUrl: null, scanToken: null, state: "idle" },
        });
        render(store.get());
      },
      onAddReceivers: async () => {
        const { devices, fullConfig } = store.get();
        const existing = new Set(
          (fullConfig?.receivers ?? []).filter((r) => r.target_type === "speaker").map((r) => r.target_id)
        );
        const pending = devices.filter((device) => !existing.has(device.did));
        const results = await Promise.allSettled(
          pending.map((device) =>
            api.createReceiver({ name: device.alias || device.name, target_type: "speaker", target_id: device.did })
          )
        );
        const failed = results.filter((r) => r.status === "rejected").length;
        store.showToast(failed
          ? `已添加 ${results.length - failed} 台，${failed} 台失败`
          : `已添加 ${results.length} 台音箱`);
        const config = await api.getConfig().catch(() => store.get().fullConfig);
        store.set({
          fullConfig: config,
          onboardingStep: config?.airplay2_available ? "airplay2" : "complete",
        });
        render(store.get());
      },
      onSkipReceivers: () => {
        store.set({ onboardingStep: store.get().fullConfig?.airplay2_available ? "airplay2" : "complete" });
        render(store.get());
      },
      onRefreshReceivers: async () => {
        await loadDevices(true);
        if (applicationScope.active) render(store.get());
      },
      onAirPlay2: async (enabled, target) => {
        if (enabled && !target) throw new Error("请先选择播放目标；没有音箱时可暂不开启");
        const current = store.beginRead('onboarding-airplay2', true);
        const valid = () => applicationScope.active && current() && store.get().onboardingStep === 'airplay2';
        if (enabled && target) {
          const [target_type, target_id] = target.split(":", 2) as ["speaker" | "group", string];
          await api.saveAirPlay2Instance({ id: "airplay2", name: "MiCast", target_type, target_id, enabled: true });
          if (!valid()) return;
        }
        await api.setAirPlay2Enabled(enabled);
        if (!valid()) return;
        store.set({ onboardingStep: "complete" });
        render(store.get());
      },
      onTheme: (theme) => {
        store.setUi({ theme });
        applyTheme(theme);
        render(store.get());
      },
      onComplete: finishOnboarding,
    });
    shellMounted = false;
    return;
  }
  if (state.access.auth_enabled && !state.access.authenticated) {
    shellCleanup?.(); shellCleanup = null;
    disposeMain();
    syncModal(null);
    app.innerHTML = renderAccessLogin(state.access) + renderToast(state.toast);
    bindAccessLogin(app, async (username, password) => {
      await api.loginAccess(username, password);
      window.location.reload();
    });
    shellMounted = false;
    return;
  }

  const appName = state.fullConfig?.app.name ?? "MiCast";
  const activeSection = state.ui.activeSection;
  const tuningDid = state.ui.tuningDid;
  if (lastRenderedTuningDid && !tuningDid) disposeTuningView();

  let mainContent = "";

  if (tuningDid) {
    // Full-screen secondary page: replaces the section content without
    // taking a sidebar slot.
    mainContent = renderTuningView(state.devices.find((d) => d.did === tuningDid));
  } else switch (activeSection) {
    case "receivers":
      mainContent = renderReceiversView(state);
      break;
    case "devices":
      mainContent = renderDevicesView({
        devices: state.devices,
        expandedDid: state.ui.expandedDeviceDid,
        status: state.status?.status || "",
        pcmSource: state.status?.pcm_source || "",
        streamUrl: state.status?.stream_url || "",
        loggedIn: state.xiaomi.logged_in,
        // "unstable": the cloud is not answering even though tokens exist.
        cloudDegraded: state.xiaomi.status === "unstable",
        loadError: state.deviceLoadError,
        playback: state.playback,
      });
      break;
    case "settings":
      mainContent = renderSettingsView({
        audio: state.audio,
        config: state.fullConfig,
        appName,
        protocol: state.fullConfig?.airplay_protocol ?? "auto",
        airplay2Enabled: state.fullConfig?.airplay2_enabled ?? false,
        airplay2Available: state.fullConfig?.airplay2_available ?? false,
        dlnaEnabled: state.fullConfig?.dlna_enabled ?? false,
        dlnaStatus: state.fullConfig?.dlna_status ?? null,
        syncGroupsEnabled: state.fullConfig?.sync_groups_enabled ?? true,
        theme: state.ui.theme,
        status: state.status?.status || "",
        xiaomiLoggedIn: state.xiaomi.logged_in,
        cloudDegraded: state.xiaomi.status === "unstable",
        deviceCount: state.devices.length,
        access: state.access,
        saving: state.saving,
      });
      break;
    case "account":
      mainContent = renderAccountView(state);
      break;
    case "debug":
      mainContent = lastRenderedSection === "debug" && !lastRenderedTuningDid && !debugInteraction
        ? lastMainMarkup : renderDebugPanel(state, state.debug);
      break;
    case "airplay2":
      mainContent = renderAirPlay2View(state.airplay2, state.ui.airplay2Tab);
      break;
    case "topology":
      mainContent = renderTopologyView();
      break;
  }

  if (!shellMounted) {
    app.innerHTML =
      renderAppShell("", activeSection, appName, state.ui.theme) +
      `<div id="player-slot"></div>` +
      `<div id="qr-slot"></div>` +
      `<div id="recovery-slot"></div>` +
      renderToast(state.toast);
    bindGlobalUI(app);
    shellMounted = true;
  }

  const main = app.querySelector<HTMLElement>(".main-content");
  if (main) {
    main.dataset.activeSection = activeSection;
    const scrollContainer = main.closest<HTMLElement>(".app-body");
    const sectionChanged = activeSection !== lastRenderedSection || tuningDid !== lastRenderedTuningDid;
    const previousScrollTop = scrollContainer?.scrollTop ?? 0;
    // The tuning canvas owns live drag state; a poll-triggered re-render would
    // destroy it mid-gesture. While tuning stays open on the same speaker,
    // leave the DOM untouched (the page updates itself).
    const tuningUnchanged = tuningDid != null && tuningDid === lastRenderedTuningDid;
    const debugUnchanged = !sectionChanged && activeSection === "debug" && !debugInteraction;
    let replaced = false;
    if (debugUnchanged) updateDebugPanel(main, state);
    if (!tuningUnchanged && !debugUnchanged && (sectionChanged || mainContent !== lastMainMarkup)) {
      replaced = true;
      const openDetails = [...main.querySelectorAll<HTMLDetailsElement>('details')].map((el, index) => ({ index, open: el.open }));
      disposeMain();
      main.innerHTML = mainContent;
      bindSectionUI(main);
      lastMainMarkup = mainContent;
      if (!sectionChanged) openDetails.forEach(({ index, open }) => { const detail = main.querySelectorAll('details')[index]; if (detail) detail.open = open; });
    }
    if (sectionChanged) {
      scrollContainer?.scrollTo(0, 0);
    } else if (replaced && scrollContainer) {
      // Replacing a section can make it much shorter (source-tab changes,
      // uploads finishing, async button states). WebKit may keep the old
      // scroll offset for a frame and render an apparently empty page. Clamp
      // it both now and after layout so every in-place rerender stays valid.
      const restoreScroll = () => {
        const maxScrollTop = Math.max(0, scrollContainer.scrollHeight - scrollContainer.clientHeight);
        scrollContainer.scrollTo(0, Math.min(previousScrollTop, maxScrollTop));
      };
      restoreScroll();

    }
  }
  lastRenderedSection = activeSection;
  lastRenderedTuningDid = tuningDid;
  app.querySelectorAll<HTMLElement>("[data-section]").forEach((item) => {
    const selected = item.dataset.section === activeSection;
    item.classList.toggle("active", selected);
    if (selected) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  const title = app.querySelector<HTMLElement>("#app-title");
  if (title) title.textContent = appName;
  updateThemeToggle(app, state.ui.theme);
  const qrSlot = app.querySelector<HTMLElement>("#qr-slot");
  if (qrSlot) {
    const markup = renderQRSheet(state);
    if (qrSlot.innerHTML !== markup) {
      qrSlot.innerHTML = markup;
      bindQRSheet(qrSlot, closeQRSheet, () => void startQRLogin());
    }
    if (state.qr.open) syncModal(qrSlot.querySelector<HTMLElement>('[role="dialog"]'), closeQRSheet);
    else if (!document.querySelector('#player-slot [role="dialog"]')) syncModal(null);
  }
  const recoverySlot = app.querySelector<HTMLElement>("#recovery-slot");
  if (recoverySlot) {
    recoverySlot.innerHTML = renderXiaomiRecovery(state);
    recoverySlot.querySelectorAll<HTMLElement>("[data-recovery-close]").forEach((button) => button.addEventListener("click", () => {
      store.set({ recoveryDismissed: true });
      render(store.get());
    }));
    recoverySlot.querySelectorAll<HTMLElement>("[data-recovery-login]").forEach((button) => button.addEventListener("click", startQRLogin));
  }
  syncPlayerChrome();
}

// The playback-bar volume slider also edits per-speaker volumes; refresh the
// speakers page when it's the one on screen.
document.addEventListener("micast:render-devices", () => {
  // Through requestRender, not straight to render: the same "a rebuild must not
  // land inside a click" rule applies to the handlers that ask for a repaint.
  if (store.get().ui.activeSection === "devices") requestRender();
});

// True while a re-render would destroy something the user has not committed
// yet: half-typed text, a partly chosen select. A focused range slider is NOT
// one of those — its value is committed on release, and counting it as
// "interacting" froze every live update for as long as it kept focus, which is
// exactly what the field reported: "听感上已经变化了" while the page still
// showed the pre-drag numbers, until the user clicked somewhere else.
// A drag in progress is covered by the pointer guard below, not by focus.
function isInteracting(): boolean {
  const el = document.activeElement;
  if (!(el instanceof HTMLElement)) return false;
  const main = document.querySelector(".main-content");
  if (!main || !main.contains(el)) return false;
  if (el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) return true;
  if (!(el instanceof HTMLInputElement)) return false;
  return !["range", "checkbox", "radio", "button", "submit", "file", "color"].includes(el.type);
}

// A poll/push that arrived during an interaction is stored but not rendered;
// when the interaction ends (focus leaves the form control), flush it once.
let renderPending = false;

// Rendering while the pointer is down is what eats clicks: the button under the
// pointer is replaced between mousedown and mouseup, and the browser then
// dispatches no click at all. Field report (0.5.6): "创建组合 点第一下没反应，
// 点第二下才有反应" — the first click flushed the render that had been deferred
// while the name field was focused (focusout → rAF), and went missing; the
// second landed on fresh markup with nothing pending.
let pointerDown = false;
// Never let a missed pointerup freeze the page: live updates matter more than a
// perfectly ordered render.
const POINTER_FLAG_FALLBACK_MS = 5000;
let pointerFlagTimer: number | null = null;

function flushPendingRender() {
  if (!renderPending) return;
  if (pointerDown || isInteracting()) return;
  renderPending = false;
  render(store.get());
}

function setPointerDown(down: boolean) {
  pointerDown = down;
  if (pointerFlagTimer !== null) {
    window.clearTimeout(pointerFlagTimer);
    pointerFlagTimer = null;
  }
  if (!down) return;
  pointerFlagTimer = window.setTimeout(() => {
    pointerFlagTimer = null;
    pointerDown = false;
    flushPendingRender();
  }, POINTER_FLAG_FALLBACK_MS);
}

for (const event of ["pointerdown", "mousedown"] as const) {
  document.addEventListener(event, () => setPointerDown(true), true);
}
for (const event of ["pointerup", "pointercancel", "mouseup", "dragend"] as const) {
  document.addEventListener(event, () => {
    setPointerDown(false);
    // The click (and the submit it triggers) is dispatched in the same task as
    // pointerup, so a timeout is the first moment it is safe to rebuild.
    if (renderPending) window.setTimeout(flushPendingRender, 0);
  }, true);
}
window.addEventListener("blur", () => setPointerDown(false));

function requestRender() {
  if (pointerDown || isInteracting()) {
    renderPending = true;
    return;
  }
  renderPending = false;
  render(store.get());
}
// focusout bubbles; when the last focused control in main content loses
// focus and a render was skipped, apply it now that it's safe.
document.addEventListener("focusout", () => {
  if (!renderPending) return;
  // Wait for the browser to settle the new activeElement (may be another
  // control in the same form — still interacting), and for a click in progress
  // to finish delivering.
  requestAnimationFrame(flushPendingRender);
});

// Views that mutate state as a direct consequence of a finished user action
// (e.g. the EQ switch on a device card) dispatch this to force a re-render.
// It bypasses the interaction guard: the action is already complete, and the
// control that fired it (a switch) often keeps focus, which would otherwise
// defer the render indefinitely on pages that only update in place on polls.
window.addEventListener("micast:request-render", () => {
  renderPending = false;
  render(store.get());
});

function bindGlobalUI(container: HTMLElement) {
  bindNavigation(container, (section) => {
    // Top-level nav always leaves secondary pages (调音台 etc.) behind.
    store.setUi({ activeSection: section, tuningDid: null });
    render(store.get());
    if (section === "devices") {
      loadDevices();
    } else if (section === "receivers") {
      if (store.get().devices.length === 0) loadDevices();
      loadAirPlay2State();
    } else if (section === "account" && store.get().xiaomi.logged_in) {
      loadDevices();
    } else if (section === "debug") {
      loadDebugState();
    }
  });

  bindThemeToggle(container, () => {
    const current = store.get().ui.theme;
    const order: Theme[] = ["auto", "light", "dark"];
    const next = order[(order.indexOf(current) + 1) % order.length];
    store.setUi({ theme: next });
    applyTheme(next);
    render(store.get());
  });

  shellCleanup = bindTabBarDrag(container);


}

function bindSectionUI(container: HTMLElement) {
  const activeSection = store.get().ui.activeSection;
  if (store.get().ui.tuningDid) {
    bindTuningView(container, () => {
      store.setUi({ tuningDid: null });
      render(store.get());
    });
    return;
  }
  if (activeSection === "receivers") {
    bindReceiversView(container, () => render(store.get()));
  } else if (activeSection === "settings") {
    bindSettingsView(
      container,
      (theme) => {
        store.setUi({ theme });
        applyTheme(theme);
        render(store.get());
      },
      () => render(store.get()),
      () => {
        store.setUi({ activeSection: "airplay2" });
        render(store.get());
        loadAirPlay2State();
      },
      () => {
        store.setUi({ activeSection: "account" });
        render(store.get());
        if (store.get().xiaomi.logged_in) loadDevices();
      }
    );
  } else if (activeSection === "devices") {
    bindDevicesView(
      container,
      (did) => {
        store.setUi({ expandedDeviceDid: did });
        render(store.get());
      },
      (did) => {
        store.setUi({ tuningDid: did });
        render(store.get());
      }
    );
    // The speaker page is where an unusable account is felt first, so it must
    // offer the way back: retry the cloud, or start a fresh QR login.
    container.querySelector("[data-retry-devices]")?.addEventListener("click", () => {
      void loadDevices(true);
    });
    container.querySelector("[data-relogin]")?.addEventListener("click", () => {
      void startQRLogin();
    });
  } else if (activeSection === "account") {
    bindAccountView(container, {
      onBack: () => {
        store.setUi({ activeSection: "settings" });
        render(store.get());
      },
      onLogout: async () => {
        closeQRSheet();
        store.invalidateAccountReads();
        const current = store.beginRead('account-operation', true);
        try {
          await api.logoutXiaomi();
          if (!applicationScope.active || !current()) return;
          store.set({ xiaomi: { logged_in: false, user_id: null }, devices: [] });
          store.showToast("已退出登录");
          render(store.get());
        } catch (e) {
          if (!applicationScope.active || !current()) return;
          store.showToast(`退出失败: ${e instanceof Error ? e.message : "未知错误"}`);
        }
      },
      onQRLogin: startQRLogin,
      onRetry: () => loadDevices(true),
      onCookieLogin: async (userId, passToken) => {
        closeQRSheet();
        store.invalidateAccountReads();
        const current = store.beginRead('account-operation', true);
        try {
          await api.loginWithCookie(userId, passToken);
          if (!applicationScope.active || !current()) return;
          store.showToast("Cookie 登录成功");
          if (await refreshLoginState()) void loadDevices();
        } catch (e) {
          if (!applicationScope.active || !current()) return;
          store.showToast(`登录失败: ${e instanceof Error ? e.message : "未知错误"}`);
        }
      },
    });
  } else if (activeSection === "debug") {
    bindDebugPanel(container, (msg) => store.showToast(msg), () => {
      // Explicit workbench actions may change form structure; telemetry may not.
      debugInteraction = true;
      try { render(store.get()); } finally { debugInteraction = false; }
    });
  } else if (activeSection === "airplay2") {
    bindAirPlay2View(container, {
      onBack: () => {
        store.setUi({ activeSection: "settings" });
        render(store.get());
      },
      onTab: (airplay2Tab) => {
        store.setUi({ airplay2Tab });
        render(store.get());
      },
      onRefresh: loadAirPlay2State,
    });
  } else if (activeSection === "topology") {
    topologyCleanup = bindTopologyView(container);
  }

}

function closeQRSheet() {
  store.beginRead('qr-login');
  window.clearTimeout(xiaomiAutoTimer);
  store.set({ qr: { ...store.get().qr, open: false } });
  render(store.get());
}

function updateDevicesStatus(status: State["status"]) {
  if (!status) return;

  const isRunning = status.status === "running";
  const statusText = isRunning
    ? "运行中"
    : status.status === "error"
      ? "出错"
      : status.status || "未启动";
  const pill = document.querySelector<HTMLElement>(".status-pill");
  const stream = document.querySelector<HTMLElement>("[data-status-stream]");
  const source = document.querySelector<HTMLElement>("[data-status-source]");

  if (pill) {
    pill.classList.toggle("running", isRunning);
    pill.classList.toggle("error", status.status === "error");
    const label = pill.querySelector<HTMLElement>("[data-status-label]");
    if (label) label.textContent = statusText;
  }
  if (stream) stream.textContent = status.stream_url || "-";
  if (source) source.textContent = status.pcm_source || "-";
}

window.addEventListener("micast:toast", (event) => {
  const toast = document.querySelector<HTMLElement>(".toast");
  if (!toast) return;
  const detail = (event as CustomEvent<{ message: string; visible: boolean }>).detail;
  toast.textContent = detail.message;
  toast.classList.toggle("visible", detail.visible);
});

window.addEventListener("micast:access-required", async () => {
  try {
    store.set({ access: await api.getAccessStatus() });
    render(store.get());
  } catch {
    // The next request or reload will retry the access bootstrap.
  }
});

async function refreshLoginState() {
  const current = store.beginRead('account-status', true);
  try {
    const xiaomi = await api.getXiaomiStatus();
    if (!applicationScope.active || !current()) return false;
    store.set({ xiaomi });
    render(store.get());
    return xiaomi.logged_in;
  } catch (e) {
    if (!applicationScope.active || !current()) return false;
    store.showToast(`获取登录状态失败: ${e instanceof Error ? e.message : "未知错误"}`);
    return false;
  }
}

let xiaomiAutoTimer: number | undefined;

// 米家步骤之后统一的前进逻辑：登录了就一定进入「播放入口」步——空列表和
// 加载失败由视图层的空态/重扫负责，不再在这里静默跳过整步。
async function advanceFromXiaomi() {
  const current = store.beginRead('onboarding-account', true);
  const step = store.get().onboardingStep;
  const valid = () => applicationScope.active && current() && store.get().onboardingStep === step;
  const [fullConfig, xiaomi] = await Promise.all([api.getConfig(), api.getXiaomiStatus()]);
  if (!valid()) return;
  let devices = store.get().devices;
  let deviceLoadError: string | null = null;
  if (xiaomi.logged_in) {
    try {
      devices = await api.getDevices();
    } catch (error) {
      deviceLoadError = `获取音箱列表失败：${friendlyError(error)}`;
    }
  }
  if (!valid()) return;
  const nextStep = xiaomi.logged_in
    ? "receivers"
    : fullConfig.airplay2_available ? "airplay2" : "complete";
  store.set({
    fullConfig,
    xiaomi,
    devices,
    deviceLoadError,
    onboardingStep: nextStep,
    qr: { open: false, qrUrl: null, scanToken: null, state: "idle" },
  });
  render(store.get());
}

// 登录成功后短暂展示成功态，然后自动进入下一步；用户点“上一步”会取消。
function scheduleXiaomiAutoAdvance() {
  window.clearTimeout(xiaomiAutoTimer);
  const current = store.beginRead('onboarding-auto', true);
  xiaomiAutoTimer = window.setTimeout(() => {
    if (!applicationScope.active || !current()) return;
    const state = store.get();
    if (state.access?.setup_complete || state.onboardingStep !== "xiaomi" || !state.xiaomi.logged_in) return;
    void advanceFromXiaomi();
  }, 1500);
}

// QR 登录确认后：先刷新持久化的登录身份，再清二维码面板（顺序反过来会把
// 界面闪回登录卡片），在引导页里随后自动前进。
async function finishXiaomiLogin(validAttempt: () => boolean) {
  if (!validAttempt()) return;
  try {
    const xiaomi = await api.getXiaomiStatus();
    if (!validAttempt()) return;
    store.set({ xiaomi, qr: { open: false, qrUrl: null, scanToken: null, state: 'idle' } });
  } catch {
    if (!validAttempt()) return;
    // Keep the last known state; the status poll will retry.
  }
  store.set({ qr: { open: false, qrUrl: null, scanToken: null, state: "idle" } });
  const state = store.get();
  if (!state.access?.setup_complete && state.onboardingStep === "xiaomi" && state.xiaomi.logged_in) {
    await advanceFromXiaomi();
    return;
  }
  render(store.get());
  if (state.xiaomi.logged_in) void loadDevices(true);
}

async function finishOnboarding() {
  try {
    // The first application view is always the live link map. Persist this
    // before reload so a stale pre-onboarding section (for example 诊断) does
    // not win over the completion destination.
    store.setUi({ activeSection: "topology" });
    await api.completeSetup();
    window.location.reload();
  } catch (error) {
    store.showToast(`无法完成设置：${friendlyError(error)}`);
  }
}

function friendlyError(error: unknown): string {
  return safeUserMessage(error);
}

// A freshly detected expiry opens the QR sheet directly — the user should
// never have to hunt through settings to re-login. Skipped once dismissed.
function maybeAutoRecovery(xiaomi: { status?: string }) {
  if (
    xiaomi.status === "expired" &&
    !store.get().recoveryDismissed &&
    !store.get().qr.open
  ) {
    void startQRLogin();
  }
}

async function startQRLogin() {
  store.invalidateAccountReads();
  window.clearTimeout(xiaomiAutoTimer);
  const current = store.beginRead('qr-login', true);
  const valid = () => applicationScope.active && current() && store.get().qr.open;
  store.set({
    qr: { open: true, qrUrl: null, scanToken: null, state: "idle", error: null },
  });
  render(store.get());

  try {
    // A QR request reaches Xiaomi's account servers, which can be slow or out
    // of reach: without a bound here the sheet sat on "正在连接…" for as long as
    // the browser kept the request open, and the user had no way to tell that
    // nothing was coming.
    const { qr_url, scan_token } = await withTimeout(api.startQRLogin(), QR_START_TIMEOUT_MS);
    if (!valid()) return;
    store.set({
      qr: { open: true, qrUrl: qr_url, scanToken: scan_token, state: "waiting", error: null },
    });
    render(store.get());
    void pollQR(scan_token, valid);
  } catch (e) {
    if (!valid()) return;
    // Keep the sheet open with the reason and a retry: closing it left the user
    // with a toast they could miss and no way back in.
    store.set({
      qr: {
        open: true,
        qrUrl: null,
        scanToken: null,
        state: "error",
        error: e instanceof Error ? e.message : "未知错误",
      },
    });
    render(store.get());
  }
}

/** Slightly longer than the backend's own bound, so its message wins. */
const QR_START_TIMEOUT_MS = 20_000;

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(
      () => reject(new Error("等待小米账号服务器响应超时，请检查这台设备的外网连接后重试")),
      ms
    );
    promise.then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        window.clearTimeout(timer);
        reject(error);
      }
    );
  });
}

async function pollQR(scanToken: string, validAttempt: () => boolean) {
  const valid = () => validAttempt() && store.get().qr.scanToken === scanToken;
  const maxAttempts = 60;
  for (let i = 0; i < maxAttempts; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    if (!valid()) return;
    try {
      const result = await api.pollQRLogin(scanToken);
      const qr = store.get().qr;
      if (!valid()) return;

      if (result.status === "scanned") {
        store.set({ qr: { ...qr, state: "scanned" } });
      } else if (result.status === "confirmed") {
        store.set({ qr: { ...qr, state: "confirmed" } });
        render(store.get());
        store.showToast("登录成功");
        setTimeout(() => { if (valid()) void finishXiaomiLogin(valid); }, 1200);
        return;
      } else if (result.status === "expired") {
        store.set({ qr: { ...qr, state: "expired" } });
        return;
      }
      render(store.get());
    } catch {
      if (!valid()) return;
      // continue polling
    }
  }
  if (!valid()) return;
  store.set({ qr: { ...store.get().qr, state: "expired" } });
  render(store.get());
}

async function loadDevices(forceRefresh = false) {
  const current = store.beginRead('devices', true);
  try {
    const devices = await api.getDevices(forceRefresh);
    if (!applicationScope.active || !current()) return;
    store.set({ devices, deviceLoadError: null });
    if (["devices", "receivers", "account"].includes(store.get().ui.activeSection)) {
      requestRender();
    }
  } catch (e) {
    let xiaomi = store.get().xiaomi;
    try {
      xiaomi = await api.getXiaomiStatus();
    } catch {
      // Keep the last known account state if the status check itself fails.
    }
    if (!applicationScope.active || !current()) return;
    const message = xiaomi.logged_in
      ? `获取设备失败: ${e instanceof Error ? e.message : "未知错误"}`
      : "米家连接已失效，请重新连接";
    store.set({ devices: [], xiaomi, deviceLoadError: message });
    maybeAutoRecovery(xiaomi);
    if (["devices", "receivers", "account"].includes(store.get().ui.activeSection)) {
      requestRender();
    }
    store.showToast(message);
  }
}

async function loadDebugState() {
  try {
    const debug = await api.getDebugState();
    store.set({ debug });
    if (store.get().ui.activeSection === "debug") {
      render(store.get());
    }
  } catch (e) {
    store.showToast(`获取调试状态失败: ${e instanceof Error ? e.message : "未知错误"}`);
  }
}

async function loadAirPlay2State() {
  try {
    const airplay2 = await api.getAirPlay2State();
    store.set({ airplay2 });
    if (["airplay2", "receivers"].includes(store.get().ui.activeSection)) requestRender();
  } catch (e) {
    store.showToast(`获取 AirPlay 2 状态失败: ${e instanceof Error ? e.message : "未知错误"}`);
  }
}

async function loadInitialState() {
  const current = store.beginRead('initial-account', true);
  try {
    const [status, audio, config, xiaomi, airplay2] = await Promise.all([
      api.getStatus(),
      api.getAudioConfig(),
      api.getConfig(),
      api.getXiaomiStatus(true).catch(() => ({ ...store.get().xiaomi, status: 'unstable' as const })),
      api.getAirPlay2State().catch(() => null),
    ]);
    if (!applicationScope.active || !current()) return;
    document.documentElement.classList.toggle("is-fnos", config.deployment === "fnos");
    store.set({ status, audio, fullConfig: config, xiaomi, receivers: status.receivers, airplay2 });
    render(store.get());
    // Initial state can already be expired (for example after reinstalling
    // while retaining data). Do not wait for the 30-second poll to open recovery.
    maybeAutoRecovery(xiaomi);

    if (xiaomi.logged_in) {
      const playbackCurrent = store.beginRead('initial-playback', true);
      api.getPlaybackState(true).then((playback) => {
        if (!applicationScope.active || !playbackCurrent()) return;
        store.set({ playback });
        syncPlayerChrome();
      }).catch(() => undefined);
    }

    if (xiaomi.logged_in) {
      loadDevices();
    }
    if (store.get().ui.activeSection === "debug") {
      await loadDebugState();
    }
    if (store.get().ui.activeSection === "airplay2") {
      await loadAirPlay2State();
    }
  } catch (e) {
    if (!applicationScope.active || !current()) return;
    store.showToast(`加载状态失败: ${e instanceof Error ? e.message : "未知错误"}`);
  }
}

async function init() {
  const ui = store.get().ui;
  applyTheme(ui.theme);
  render(store.get());

  try {
    const access = await api.getAccessStatus();
    store.set({ access, onboardingStep: access.access_configured ? "xiaomi" : "access" });
    render(store.get());
    if (!access.setup_complete || (access.auth_enabled && !access.authenticated)) {
      // 重进引导且米家仍连着：直接展示成功态并自动进入下一步。
      if (store.get().onboardingStep === "xiaomi" && !access.setup_complete) {
        try {
          const current = store.beginRead('initial-onboarding-account', true);
          const xiaomi = await api.getXiaomiStatus();
          if (!applicationScope.active || !current() || store.get().onboardingStep !== 'xiaomi') return;
          store.set({ xiaomi });
          render(store.get());
          if (xiaomi.logged_in) scheduleXiaomiAutoAdvance();
        } catch {
          // Status check failed — the manual 继续/暂时跳过 buttons stay.
        }
      }
      return;
    }
  } catch (e) {
    bootError = friendlyError(e);
    render(store.get());
    return;
  }

  await loadInitialState();

  // Realtime state: WebSocket push when available, polling as fallback.
  function applyStatus(status: Status) {
    const changed = JSON.stringify(store.get().status) !== JSON.stringify(status);
    const trackChanged =
      JSON.stringify(store.get().status?.now_playing ?? null) !==
      JSON.stringify(status.now_playing ?? null);
    const interactionPending = store.get().saving;
    store.set(interactionPending ? { status } : { status, receivers: status.receivers });
    if (trackChanged) {
      // Cover/lyric updates ride the status push — repaint the player chrome
      // without waiting for the (slower) playback poll.
      syncPlayerChrome();
    }
    const activeSection = store.get().ui.activeSection;
    if (changed && activeSection === "devices") {
      updateDevicesStatus(status);
    } else if (changed && activeSection === "receivers" && !interactionPending) {
      requestRender();
    }
  }

  function applyPlayback(playback: PlaybackState) {
    if (JSON.stringify(playback) !== JSON.stringify(store.get().playback)) {
      store.set({ playback });
      syncPlayerChrome();
    }
  }

  let sharedSettingsBusy = false;
  async function syncSharedSettings() {
    if (sharedSettingsBusy || document.hidden) return;
    sharedSettingsBusy = true;
    const current = store.beginRead('shared-settings', true);
    const configBefore = store.get().fullConfig;
    const tuningBefore = store.get().devices.map(d => [d.did, d.eq?.revision]);
    try {
      const [fullConfig, audio, devices] = await Promise.all([
        api.getConfig(),
        api.getAudioConfig(),
        store.get().xiaomi.logged_in ? api.getDevices() : Promise.resolve(store.get().devices),
      ]);
      if (!businessScope.active || !current() || configBefore !== store.get().fullConfig ||
        JSON.stringify(tuningBefore) !== JSON.stringify(store.get().devices.map(d => [d.did, d.eq?.revision]))) return;
      const changed =
        JSON.stringify(fullConfig) !== JSON.stringify(store.get().fullConfig) ||
        JSON.stringify(audio) !== JSON.stringify(store.get().audio) ||
        JSON.stringify(devices) !== JSON.stringify(store.get().devices);
      if (changed) {
        store.set({ fullConfig, audio, devices });
        requestRender();
      }
    } catch {
      // Existing state remains usable while another client or the network is unavailable.
    } finally {
      sharedSettingsBusy = false;
    }
  }

  const businessScope = applicationScope;
  const publishTopology = async () => {
    if (store.get().ui.activeSection === "topology") {
      const topology = await api.getTopology();
      if (businessScope.active) window.dispatchEvent(new CustomEvent("micast:topology", { detail: topology }));
    }
  };
  const refreshBusiness = async () => {
    const [status, playback] = await Promise.all([api.getStatus(), api.getPlaybackState()]);
    if (!businessScope.active) return;
    applyStatus(status); applyPlayback(playback);
    await publishTopology();
  };
  const realtime = new RealtimeConnection({
    message: message => {
      if (message.type === "status") applyStatus(message.data);
      else if (message.type === "playback") applyPlayback(message.data);
      else if (message.type === "topology") window.dispatchEvent(new CustomEvent("micast:topology", { detail: message.data }));
      else if (message.type === "tuning") window.dispatchEvent(new CustomEvent("micast:tuning-change", { detail: message.data }));
      else if (message.type === "config") void syncSharedSettings();
    },
    refresh: refreshBusiness,
    fallback: async () => { await refreshBusiness(); await syncSharedSettings(); },
  });
  realtime.start();
  businessScope.own(() => realtime.dispose());

  // Xiaomi validity changes independently from transport status. Poll it
  // quietly so an expired cloud login is surfaced even when the user stays
  // on the playback page and no device refresh is running. Hidden tabs skip
  // the cloud round-trips: a background WebView firing a forced passToken
  // verify plus an uncached device-list pull every 30s per tab is a rate
  // limit / battery hazard, and re-entering the tab already refreshes.
  businessScope.interval(async () => {
    if (document.hidden) return;
    if (!store.get().xiaomi.ever_logged_in && !store.get().xiaomi.logged_in) return;
    const current = store.beginRead('account-poll', true);
    try {
      const xiaomi = await api.getXiaomiStatus(true);
      if (!businessScope.active || !current()) return;
      if (JSON.stringify(xiaomi) !== JSON.stringify(store.get().xiaomi)) {
        store.set({ xiaomi });
        maybeAutoRecovery(xiaomi);
        requestRender();
      }
      // The status endpoint only reports whether credentials are stored. A
      // stale serviceToken can therefore look connected until a real Xiaomi
      // request is made. Periodically refresh the device list so an expired
      // passToken is detected and the recovery QR is opened automatically.
      // A plain (cached) refresh keeps this cheap; only a state anomaly
      // (expired session or an empty device list) bypasses the server cache.
      if (xiaomi.logged_in && !store.get().qr.open) {
        const degraded = xiaomi.status === "expired" || store.get().devices.length === 0;
        await loadDevices(degraded);
      }
    } catch {
      // A status request failing is connectivity trouble, not proof of expiry.
    }
  }, 30000);

  let debugRefreshInFlight = false;
  businessScope.interval(async () => {
    if (document.hidden) return;  // background tab: stop polling entirely
    if (store.get().ui.activeSection !== "debug") return;
    if (debugRefreshInFlight) return;
    const log = document.querySelector<HTMLElement>("[data-runtime-log]");
    if (!log) return;
    debugRefreshInFlight = true;
    try {
      // The panel's own scope (filters plus a frozen window) travels with every
      // poll: freezing locks the interval shown, it does not stop the page —
      // the frozen bar still needs its new-record count.
      const debug = await api.getDebugState(currentLogQuery());
      if (!businessScope.active || !log.isConnected || store.get().ui.activeSection !== 'debug') return;
      store.set({ debug });
      const main = document.querySelector<HTMLElement>('.main-content');
      if (main) updateDebugPanel(main, store.get());
    } catch {
      // Keep the latest diagnostics visible during a temporary API failure.
    } finally {
      debugRefreshInFlight = false;
    }
  }, 1500);
}

bindRoutes(requestRender);
bindConnectivity(loadInitialState);
void init();

// ---- Desktop shell close prompt (packaged app only) ----
// The WebView2 window's X fires this via pywebview; the page shows a styled
// dialog and reports the choice back through the JS bridge. In plain browsers
// this is never invoked and pywebview.api does not exist.
declare global {
  interface Window {
    micastClosePrompt?: () => void;
    pywebview?: { api?: { desktop_quit?: () => void; desktop_hide?: () => void } };
  }
}

window.micastClosePrompt = () => {
  if (document.querySelector(".desktop-close-dialog")) return;
  const dialog = document.createElement("dialog");
  dialog.className = "confirm-dialog desktop-close-dialog";
  dialog.innerHTML = `<form method="dialog">
    <div class="confirm-dialog-copy">
      <h3>关闭 MiCast</h3>
      <p>退出后手机将无法继续投放；也可以收进系统托盘，在后台继续运行。</p>
    </div>
    <div class="confirm-dialog-actions">
      <button class="button plain" value="cancel">取消</button>
      <button class="button plain" value="tray">最小化到托盘</button>
      <button class="button danger" value="quit">退出 MiCast</button>
    </div>
  </form>`;
  document.body.appendChild(dialog);
  dialog.addEventListener("close", () => {
    const choice = dialog.returnValue;
    dialog.remove();
    const bridge = window.pywebview?.api;
    if (choice === "quit") bridge?.desktop_quit?.();
    else if (choice === "tray") bridge?.desktop_hide?.();
  }, { once: true });
  dialog.addEventListener("cancel", () => dialog.close("cancel"));
  dialog.showModal();
  dialog.querySelector<HTMLButtonElement>('[value="tray"]')?.focus();
};
