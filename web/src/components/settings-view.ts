import { renderProtocolRow, bindProtocolRecovery } from './protocol-settings';
import type { AccessStatus, AirPlayProtocol, AudioConfig, FullConfig, PortStatus } from "../api";
import { api } from "../api";
import { store, type Theme } from "../state";
import { icon } from "../icons";
import { renderThemeControl } from "./app-shell";
import { SurfaceScope } from '../ui/lifecycle';

let settingsScope: SurfaceScope | null = null;
export function disposeSettingsView() {
  settingsScope?.dispose(true);
  settingsScope = null;
  if (updatePollTimer) clearInterval(updatePollTimer);
  updatePollTimer = null;
}

interface SettingsProps {
  audio: AudioConfig | null;
  config: FullConfig | null;
  appName: string;
  protocol: AirPlayProtocol;
  airplay2Enabled: boolean;
  airplay2Available: boolean;
  dlnaEnabled: boolean;
  dlnaStatus: { status: string; detail: string } | null;
  syncGroupsEnabled: boolean;
  theme: Theme;
  status: string;
  xiaomiLoggedIn: boolean;
  cloudDegraded?: boolean;
  deviceCount: number;
  access: AccessStatus | null;
  saving?: boolean;
}

const formats: Array<AudioConfig["format"]> = ["mp3", "flac", "wav"];
const bitrates: Array<AudioConfig["bitrate"]> = ["128k", "192k", "320k"];
const sampleRates: Array<AudioConfig["sample_rate"]> = [44100, 48000];

// Audio changes coalesce module-wide (survives re-renders): rapid clicks apply
// the optimistic UI immediately and only the final state restarts the encoder.
let audioQueue: Partial<AudioConfig> | null = null;
let audioBusy = false;
let lastConfirmedAudio: AudioConfig | null = null;

export function renderSettingsView(props: SettingsProps): string {
  const {
    audio, config, appName, airplay2Enabled, airplay2Available, dlnaEnabled, dlnaStatus,
    syncGroupsEnabled, theme, status, xiaomiLoggedIn, cloudDegraded, deviceCount, access,
  } = props;
  if (audio && (!lastConfirmedAudio || !audioBusy)) lastConfirmedAudio = audio;
  const statusLabel = status === "running"
    ? "系统就绪"
    : status === "degraded"
      ? "部分可用"
      : status === "error"
        ? "播放功能不可用"
        : "正在准备";

  const transcoding = audio?.auto_transcode ?? true;

  const formatHtml = audio
    ? renderSegments(
        "format",
        formats.map((f) => ({ value: f, label: f.toUpperCase(), active: audio.format === f })),
        !transcoding
      )
    : "";

  const bitrateHtml = audio
    ? renderSegments(
        "bitrate",
        bitrates.map((b) => ({ value: b, label: b.replace("k", ""), active: audio.bitrate === b })),
        !transcoding || audio.format !== "mp3"
      )
    : "";

  const sampleRateHtml = audio
    ? renderSegments(
        "samplerate",
        sampleRates.map((sr) => ({ value: String(sr), label: `${sr / 1000}k`, active: audio.sample_rate === sr })),
        !transcoding
      )
    : "";

  return `
    <div class="page-heading">
      <h2 class="page-title">设置</h2>
      <p>管理播放方式、音质与实验功能。</p>
    </div>

    <div class="hero-card ${status === "error" ? "error" : status === "degraded" ? "warning" : ""}">
      <span class="caption">当前状态</span>
      <div class="title-1">${statusLabel}</div>
      <span class="caption">${audio ? (transcoding ? `${audio.format.toUpperCase()} · ${audio.bitrate} · ${audio.sample_rate / 1000} kHz` : "PCM 直出 · 不转码") : "加载中…"}</span>
    </div>

    <div class="group-header">音频编码</div>
    <p class="group-header-hint">AirPlay、AirPlay 2 和 DLNA 共用音频处理；编码、EQ、组合延迟与左右声道按输出目标应用。</p>
    <div class="group">
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">转码</span>
          <span class="cell-subtitle">${transcoding ? "按下方设置编码 AirPlay 实时输出" : "AirPlay 使用 PCM 原始音频直出；部分型号不会出声，听不到就改回转码"}</span>
        </div>
        <input type="checkbox" class="switch" id="auto-transcode" ${audio?.auto_transcode ? "checked" : ""} aria-label="开启转码">
      </div>
      ${renderCell("格式", transcoding ? "MP3 兼容性最好，FLAC/WAV 为无损" : "转码已关闭，此项不生效", formatHtml)}
      ${renderCell("码率", !transcoding ? "转码已关闭，此项不生效" : audio?.format === "mp3" ? "仅对 MP3 生效" : "当前格式不使用码率", bitrateHtml)}
      ${renderCell("采样率", transcoding ? "AirPlay 默认 48 kHz" : "转码已关闭，此项不生效", sampleRateHtml)}
    </div>

    <div class="group-header">外观</div>
    <div class="group">
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">主题</span>
          <span class="cell-subtitle">跟随系统时随设备的深浅色设置自动切换</span>
        </div>
        ${renderThemeControl(theme)}
      </div>
    </div>

    <div class="group-header">应用</div>
    <div class="group">
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">应用名称</span>
          <span class="cell-subtitle">仅用于网页标题；播放名称跟随音箱或组合名称</span>
        </div>
        <input type="text" class="input" id="app-name-input" value="${escapeHtml(appName)}" placeholder="MiCast" style="max-width: 160px;">
      </div>
    </div>

    <div class="group-header">服务</div>
    <div class="group">
      <button class="cell settings-link" type="button" data-open-account>
        <div class="cell-icon blue">${icon("link")}</div>
        <div class="cell-content">
          <span class="cell-title">米家</span>
          <span class="cell-subtitle">${xiaomiLoggedIn
            ? cloudDegraded
              ? "云端暂时没有响应，可打开重新连接"
              : `已连接${deviceCount ? ` · ${deviceCount} 台音箱` : ""}`
            : "未连接，连接后自动同步音箱"}</span>
        </div>
        <span class="settings-link-arrow" aria-hidden="true">›</span>
      </button>
    </div>


    <div class="group-header">管理访问</div>
    <div class="group">
      <button class="cell settings-link" type="button" data-access-settings-toggle>
        <div class="cell-icon blue">${icon("lock")}</div>
        <div class="cell-content">
          <span class="cell-title">账号与密码</span>
          <span class="cell-subtitle">${access?.auth_enabled ? `已启用 · ${escapeHtml(access.username)}` : "未启用，局域网内可直接访问"}</span>
        </div>
        <span class="settings-link-arrow" aria-hidden="true">›</span>
      </button>
      <form class="access-settings-form" data-access-settings hidden>
        <label class="choice-row ${access?.auth_enabled ? "selected" : ""}"><input type="radio" name="access_mode" value="protected" ${access?.auth_enabled ? "checked" : ""}><span><strong>使用账号密码</strong><small>其他设备将重新登录</small></span></label>
        <div class="access-settings-fields" ${access?.auth_enabled ? "" : "hidden"}>
          <label>用户名<input class="input" name="username" maxlength="64" autocomplete="username" value="${escapeHtml(access?.username ?? "admin")}"></label>
          <label>新密码<input class="input" name="password" type="password" minlength="6" autocomplete="new-password" placeholder="至少 6 个字符"></label>
          <label>确认新密码<input class="input" name="password_confirm" type="password" minlength="6" autocomplete="new-password"></label>
        </div>
        <label class="choice-row warning-choice ${!access?.auth_enabled ? "selected" : ""}"><input type="radio" name="access_mode" value="open" ${!access?.auth_enabled ? "checked" : ""}><span><strong>不设置管理账号</strong><small>局域网内无需登录</small></span></label>
        <div class="group-form-actions"><button class="button primary" type="submit">保存管理访问</button></div>
        <p class="settings-form-status" data-access-settings-status aria-live="polite"></p>
      </form>
      ${access?.auth_enabled && access.authenticated ? `
        <button class="cell settings-link access-logout-row" type="button" data-access-logout>
          <div class="cell-icon">${icon("lock")}</div>
          <div class="cell-content">
            <span class="cell-title danger-text">退出登录</span>
            <span class="cell-subtitle">仅退出当前浏览器</span>
          </div>
        </button>
      ` : ""}
    </div>

    <div class="group-header">播放方式</div>
    <div class="group">
      ${renderProtocolRow(config, 'airplay')}
      ${renderProtocolRow(config, 'dlna')}
    </div>
    ${dlnaEnabled && dlnaStatus?.status === "error" ? `<div class="inline-notice error"><strong>DLNA 暂不可用</strong><span>请检查 MiCast 的网络访问权限后重试。</span></div>` : ""}
    ${dlnaEnabled && dlnaStatus?.status !== "error" ? `<div class="inline-notice"><strong>DLNA 生效方式</strong><span>开关立即生效；投放音量控制会在下次投放媒体时生效。若正在播放，请先在播放器中停止，再重新选择音箱并投放。</span></div>` : ""}

    <div class="group-header">播放增强</div>
    <div class="group">
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">触屏歌词与封面</span>
          <span class="cell-subtitle">为带屏音箱匹配封面和滚动歌词</span>
        </div>
        <input type="checkbox" class="switch" id="touchscreen-lyrics" ${config?.touchscreen_lyrics ? "checked" : ""} aria-label="开启触屏歌词与封面">
      </div>
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">起播音量</span>
          <span class="cell-subtitle">开始播放时设置音箱音量；关闭则保持原音量</span>
        </div>
        <div class="settings-inline-control">
          <input type="checkbox" class="switch" id="default-volume-enabled" ${config?.default_volume_enabled ? "checked" : ""} aria-label="启用起播音量">
          <input type="number" class="input settings-number" id="default-volume" min="0" max="100" step="5" value="${config?.default_volume ?? 0}" ${config?.default_volume_enabled ? "" : "disabled"} aria-label="起播音量">
        </div>
      </div>
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">暂停会话过期</span>
          <span class="cell-subtitle">AirPlay 暂停超过该时长后自动结束会话并停止音箱播放；0 表示不自动结束（秒）</span>
        </div>
        <input type="number" class="input settings-number" id="stale-session-timeout" min="0" max="3600" step="10" value="${config?.stale_session_timeout ?? 60}" aria-label="暂停会话过期时间（秒）">
      </div>
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">投放音量控制</span>
            <span class="cell-subtitle">独立音量保留音箱设置；音量联动直接控制音箱。AirPlay 下次连接生效，DLNA 下次投放生效</span>
        </div>
        <select class="input" id="sender-volume-mode" aria-label="投放音量控制" style="max-width: 10rem">
          <option value="independent" ${config?.sender_volume_mode !== "linked" ? "selected" : ""}>独立音量</option>
          <option value="linked" ${config?.sender_volume_mode === "linked" ? "selected" : ""}>音量联动</option>
        </select>
      </div>
      <div class="cell settings-input-cell">
        <div class="cell-content">
          <span class="cell-title">登录失效通知</span>
          <span class="cell-subtitle">小米登录失效时发送提醒；留空关闭</span>
        </div>
        <input type="url" class="input" id="notify-webhook" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…" value="${escapeHtml(config?.notify_webhook_url ?? "")}" aria-label="通知 Webhook 地址">
        <div class="settings-save-row"><span class="caption" id="notify-webhook-status" aria-live="polite"></span><button class="button secondary" id="notify-webhook-save" type="button">保存通知地址</button></div>
      </div>
    </div>

    <div class="group-header">实验功能</div>
    <div class="group">
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">音箱组合 <span class="feature-badge">实验性</span></span>
          <span class="cell-subtitle">让两台或更多音箱一起播放；关闭后仍会保留已有组合</span>
        </div>
        <input type="checkbox" class="switch" id="sync-groups-enabled" ${syncGroupsEnabled ? "checked" : ""} aria-label="开启音箱组合">
      </div>
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">大延迟</span>
          <span class="cell-subtitle">将音箱组合的延迟调节范围从 ±5 秒扩大到 ±15 秒</span>
        </div>
        <input type="checkbox" class="switch" id="large-delay-enabled" ${config?.large_delay_enabled ? "checked" : ""} aria-label="开启大延迟范围">
      </div>
      ${renderProtocolRow(config, 'airplay2')}
      ${airplay2Available && airplay2Enabled ? `<button class="cell settings-link" type="button" data-open-airplay2>
        <div class="cell-icon blue">${icon("airplay")}</div>
        <div class="cell-content">
          <span class="cell-title">AirPlay 2 管理</span>
          <span class="cell-subtitle">${config?.airplay2_mode === "single" ? "设置播放名称、状态和目标音箱" : "管理播放入口及其对应音箱"}</span>
        </div>
        <span class="settings-link-arrow" aria-hidden="true">›</span>
      </button>` : ""}
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">网络发现 <span class="feature-badge">实验性</span></span>
          <span class="cell-subtitle">扫描局域网中的 AirPlay / DLNA 播放设备</span>
        </div>
        <input type="checkbox" class="switch" id="network-discovery-enabled" ${config?.network_discovery_enabled ? "checked" : ""} aria-label="开启网络发现">
      </div>
    </div>
    ${config && !config.network_discovery_enabled
      ? `<div class="inline-notice"><strong>网络发现已关闭</strong><span>不会扫描局域网播放设备，也无法把音频投放到外部设备。</span></div>`
      : ""}
    ${config?.ports?.length ? `
    <div class="group-header">高级设置 · 服务端口</div>
    <div class="group">
      ${config.ports.map(renderPortRow).join("")}
    </div>
    ` : ""}

    <div class="group-header">数据与版本</div>
    <div class="group">
      ${config?.storage?.shared_dir ? `<div class="cell">
        <div class="cell-icon gray">${icon("folder")}</div>
        <div class="cell-content">
          <span class="cell-title">应用文件 · micast</span>
          <span class="cell-subtitle">日志、诊断导出与脱敏配置副本</span>
          <span class="cell-subtitle">${escapeHtml(config.storage.shared_dir)}</span>
        </div>
      </div>` : ""}

      <div class="cell">
        <div class="cell-icon gray">${icon("folder")}</div>
        <div class="cell-content">
          <span class="cell-title">${storageModeLabel(config?.storage?.mode)}</span>
          <span class="cell-subtitle" title="${escapeHtml(config?.storage?.data_dir ?? "")}">${escapeHtml(config?.storage?.data_dir ?? "正在读取数据目录…")}</span>
        </div>
      </div>
      <div class="cell">
        <div class="cell-icon gray">${icon("file")}</div>
        <div class="cell-content">
          <span class="cell-title">日志目录</span>
          <span class="cell-subtitle" title="${escapeHtml(config?.storage?.log_dir ?? "")}">${escapeHtml(config?.storage?.log_dir ?? "正在读取日志目录…")}</span>
        </div>
      </div>
      <div class="cell">
        <div class="cell-icon blue">${icon("download")}</div>
        <div class="cell-content">
          <span class="cell-title">软件更新</span>
          <span class="cell-subtitle" data-update-status>当前版本读取中…</span>
          <a class="cell-subtitle update-releases" href="https://github.com/DyMode/MiCast/releases" target="_blank" rel="noopener noreferrer">GitHub 发布页 ↗</a>
        </div>
        <div class="update-actions">
          <button class="button compact primary" type="button" data-update-download hidden>下载更新</button>
          <button class="button compact primary" type="button" data-update-apply hidden>安装并重启</button>
          <button class="button compact secondary" type="button" data-update-check>检查更新</button>
        </div>
      </div>
      <div class="cell">
        <div class="cell-icon red">${icon("trash")}</div>
        <div class="cell-content">
          <span class="cell-title danger-text">清空数据</span>
          <span class="cell-subtitle">删除全部配置、米家登录与管理账号，回到初始引导页</span>
        </div>
        <button class="button compact secondary danger-text" type="button" data-reset-all>清空数据</button>
      </div>
    </div>


  `;
}

const portModeLabels: Record<PortStatus["mode"], string> = {
  auto: "自动",
  custom: "自定义",
  env: "环境固定",
  fixed: "固定端口",
};

function portActualText(p: PortStatus): string {
  // Only a bound socket earns a chip. "未监听" is a state word, not a port, and
  // it already reads in the status column.
  if (p.actual == null) return "";
  if (Array.isArray(p.actual)) {
    if (!p.actual.length) return "";
    // A scanned range can hold four sockets; listing them all wrapped the
    // status onto a second line and dwarfed the port it belongs to.
    return p.actual.length <= 2
      ? p.actual.join("、")
      : `${p.actual[0]}–${p.actual[p.actual.length - 1]}`;
  }
  return String(p.actual);
}

function portActualTitle(p: PortStatus): string {
  if (Array.isArray(p.actual) && p.actual.length) return `已绑定 ${p.actual.join("、")}`;
  return "";
}

function renderPortRow(p: PortStatus): string {
  const actual = portActualText(p);
  const stateClass = p.status === "error" ? "error" : p.status === "listening" ? "success" : "";
  const stateText = p.status === "error" ? "异常" : p.status === "listening" ? "监听中" : p.status === "hosted" ? "已托管" : "未监听";
  // Only render the slots this row actually needs: non-editable rows are a
  // plain right-aligned status (like the toggle rows above); editable rows
  // add a wide-enough input; 恢复 appears only for custom ports.
  const inputSlot = p.editable
    ? `<input type="number" class="input settings-number" data-port-input="${p.id}" min="1024" max="65535"
          placeholder="${p.preferred ?? ""}" value="${p.mode === "custom" ? p.preferred ?? "" : ""}"
          data-committed="${p.mode === "custom" ? p.preferred ?? "" : ""}"
          aria-label="${escapeHtml(p.name)}首选端口">`
    : "";
  const actionSlot = p.editable && p.mode === "custom"
    ? `<button class="button compact secondary" type="button" data-port-reset="${p.id}">恢复</button>`
    : "";
  return `
    <div class="cell port-row">
      <div class="cell-content">
        <span class="cell-title">${escapeHtml(p.name)} <span class="feature-badge">${portModeLabels[p.mode]}</span></span>
        <span class="cell-subtitle">${escapeHtml(p.detail)}</span>
      </div>
      <div class="port-control">
        <span class="plain-state ${stateClass}" data-port-status="${p.id}" aria-live="polite" title="${escapeHtml(portActualTitle(p))}">${stateText}</span>
        ${actual ? `<span class="meta-chip port-actual">${escapeHtml(actual)}</span>` : ""}
        ${inputSlot}
        ${actionSlot ? `<span class="port-actions">${actionSlot}</span>` : ""}
      </div>
    </div>
  `;
}

function renderCell(title: string, subtitle: string, control: string): string {
  return `
    <div class="cell">
      <div class="cell-content">
        <span class="cell-title">${title}</span>
        <span class="cell-subtitle">${subtitle}</span>
      </div>
      ${control}
    </div>
  `;
}

function storageModeLabel(mode?: FullConfig["storage"]["mode"]): string {
  return ({
    managed: "系统托管数据",
    portable: "Windows 便携版",
    installed: "Windows 安装版",
    development: "本地开发版",
  } as const)[mode ?? "managed"];
}

function renderSegments(
  group: string,
  items: Array<{ value: string; label: string; active: boolean }>,
  disabled = false
): string {
  return `
    <div class="segmented-control" role="group" aria-label="${group}" ${disabled ? 'style="opacity:0.5"' : ""}>
      ${items
        .map(
          (item) => `
            <button class="segment ${item.active ? "active" : ""}"
                    data-${group}="${item.value}"
                    aria-pressed="${item.active}" ${disabled ? "disabled" : ""}>${item.label}</button>
          `
        )
        .join("")}
    </div>
  `;
}

export function bindSettingsView(
  container: HTMLElement,
  onThemeChange: (theme: Theme) => void,
  onStateChange: () => void,
  onOpenAirPlay2: () => void,
  onOpenAccount: () => void
) {
  disposeSettingsView();
  const scope = settingsScope = new SurfaceScope();
  bindFeatureSwitch(container, "#airplay-enabled", "airplay_enabled", api.setAirplayEnabled, onStateChange);
  bindProtocolRecovery(container, scope, onStateChange);
  container.querySelector("[data-open-airplay2]")?.addEventListener("click", onOpenAirPlay2);
  container.querySelector("[data-open-account]")?.addEventListener("click", onOpenAccount);
  const accessForm = container.querySelector<HTMLFormElement>("[data-access-settings]");
  container.querySelector("[data-access-settings-toggle]")?.addEventListener("click", () => {
    if (accessForm) accessForm.hidden = !accessForm.hidden;
  });
  const syncAccessForm = () => {
    const protectedMode = accessForm?.querySelector<HTMLInputElement>('input[name="access_mode"]:checked')?.value === "protected";
    const fields = accessForm?.querySelector<HTMLElement>(".access-settings-fields");
    if (fields) fields.hidden = !protectedMode;
    accessForm?.querySelectorAll<HTMLElement>(".choice-row").forEach((row) => row.classList.toggle("selected", (row.querySelector("input") as HTMLInputElement)?.checked));
  };
  accessForm?.querySelectorAll<HTMLInputElement>('input[name="access_mode"]').forEach((input) => input.addEventListener("change", syncAccessForm));
  accessForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(accessForm);
    const enabled = data.get("access_mode") === "protected";
    const button = accessForm.querySelector<HTMLButtonElement>('button[type="submit"]');
    if (button) { button.disabled = true; button.textContent = "正在保存…"; }
    try {
      await api.updateAccess({
        auth_enabled: enabled,
        username: String(data.get("username") || "admin"),
        password: enabled ? String(data.get("password") || "") : "",
        password_confirm: enabled ? String(data.get("password_confirm") || "") : "",
      });
      store.set({ access: await api.getAccessStatus() });
      store.showToast(enabled ? "管理访问已启用" : "管理访问已关闭");
      onStateChange();
    } catch (error) {
      store.showToast(`保存失败: ${error instanceof Error ? error.message : "未知错误"}`);
      if (button) { button.disabled = false; button.textContent = "保存管理访问"; }
    }
  });
  container.querySelector<HTMLButtonElement>("[data-access-logout]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      await api.logoutAccess();
      window.location.reload();
    } catch (error) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      store.showToast(`退出失败: ${error instanceof Error ? error.message : "未知错误"}`);
    }
  });
  const updateAudio = async (changes: Partial<AudioConfig>) => {
    audioQueue = { ...(audioQueue ?? {}), ...changes };
    // Optimistic UI for every click, even while a save is in flight.
    const current = store.get().audio;
    if (current) {
      store.set({ audio: { ...current, ...changes }, saving: true });
      onStateChange();
    }
    if (audioBusy) return;
    audioBusy = true;
    try {
      while (audioQueue) {
        const batch = audioQueue;
        audioQueue = null;
        try {
          const updated = await api.setAudioConfig(batch);
          lastConfirmedAudio = updated;
          const status = await api.getStatus();
          store.set({ audio: updated, status, receivers: status.receivers });
          onStateChange();
        } catch (e) {
          audioQueue = null;
          if (lastConfirmedAudio) {
            store.set({ audio: lastConfirmedAudio });
            onStateChange();
          }
          store.showToast(`保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
          break;
        }
      }
      store.showToast("设置已保存");
    } finally {
      audioBusy = false;
      store.set({ saving: false });
    }
  };

  container.querySelectorAll("[data-format]").forEach((el) => {
    el.addEventListener("click", () => {
      const format = (el as HTMLElement).dataset.format as AudioConfig["format"];
      updateAudio({ format });
    });
  });

  container.querySelectorAll("[data-bitrate]").forEach((el) => {
    el.addEventListener("click", () => {
      const bitrate = (el as HTMLElement).dataset.bitrate as AudioConfig["bitrate"];
      updateAudio({ bitrate });
    });
  });

  container.querySelectorAll("[data-samplerate]").forEach((el) => {
    el.addEventListener("click", () => {
      const sampleRate = Number((el as HTMLElement).dataset.samplerate) as AudioConfig["sample_rate"];
      updateAudio({ sample_rate: sampleRate });
    });
  });

  const autoSwitch = container.querySelector("#auto-transcode");
  if (autoSwitch) {
    autoSwitch.addEventListener("change", (e) => {
      updateAudio({ auto_transcode: (e.target as HTMLInputElement).checked });
    });
  }

  bindFeatureSwitch(
    container, "#dlna-enabled", "dlna_enabled", api.setDlnaEnabled, onStateChange
  );
  bindFeatureSwitch(
    container,
    "#airplay2-enabled",
    "airplay2_enabled",
    api.setAirPlay2Enabled,
    onStateChange
  );
  bindFeatureSwitch(
    container,
    "#sync-groups-enabled",
    "sync_groups_enabled",
    api.setSyncGroupsEnabled,
    onStateChange
  );
  bindFeatureSwitch(
    container,
    "#large-delay-enabled",
    "large_delay_enabled",
    api.setLargeDelayEnabled,
    onStateChange
  );
  bindFeatureSwitch(
    container,
    "#network-discovery-enabled",
    "network_discovery_enabled",
    api.setNetworkDiscoveryEnabled,
    onStateChange
  );
  bindFeatureSwitch(
    container,
    "#touchscreen-lyrics",
    "touchscreen_lyrics",
    api.setTouchscreenLyrics,
    onStateChange
  );

  const defaultVolumeInput = container.querySelector<HTMLInputElement>("#default-volume");
  const defaultVolumeEnabled = container.querySelector<HTMLInputElement>("#default-volume-enabled");
  defaultVolumeEnabled?.addEventListener("change", async () => {
    try {
      const result = await api.setDefaultVolume(Number(defaultVolumeInput?.value ?? 0), defaultVolumeEnabled.checked);
      const config = store.get().fullConfig;
      if (config) store.set({ fullConfig: { ...config, ...result } });
      if (defaultVolumeInput) defaultVolumeInput.disabled = !defaultVolumeEnabled.checked;
    } catch (e) {
      defaultVolumeEnabled.checked = !defaultVolumeEnabled.checked;
      store.showToast(`保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });
  container.querySelector<HTMLSelectElement>("#sender-volume-mode")?.addEventListener("change", async (event) => {
    const select = event.currentTarget as HTMLSelectElement;
    try {
      const result = await api.setSenderVolumeMode(select.value as "independent" | "linked");
      const config = store.get().fullConfig;
      if (config) store.set({ fullConfig: { ...config, ...result } });
      store.showToast(result.dlna_recast_required
        ? "已保存；当前 DLNA 媒体仍使用原设置，请停止并重新投放"
        : "已保存；AirPlay 下次连接、DLNA 下次投放时生效");
    } catch (e) {
      select.value = store.get().fullConfig?.sender_volume_mode ?? "independent";
      store.showToast(`保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });
  if (defaultVolumeInput) {
    defaultVolumeInput.addEventListener("input", () => {
      scope.debounce('volume', async () => {
        const volume = Math.max(0, Math.min(100, parseInt(defaultVolumeInput.value || "0", 10) || 0));
        try {
          await api.setDefaultVolume(volume, defaultVolumeEnabled?.checked ?? false);
          const config = store.get().fullConfig;
          if (config) store.set({ fullConfig: { ...config, default_volume: volume } });
          store.showToast(`起播音量已设为 ${volume}`);
        } catch (e) {
          store.showToast(`保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
        }
      }, 500);
    });
  }

  const staleTimeoutInput = container.querySelector<HTMLInputElement>("#stale-session-timeout");
  if (staleTimeoutInput) {
    staleTimeoutInput.addEventListener("input", () => {
      scope.debounce('timeout', async () => {
        const seconds = Math.max(0, Math.min(3600, parseInt(staleTimeoutInput.value || "0", 10) || 0));
        try {
          await api.setStaleSessionTimeout(seconds);
          const config = store.get().fullConfig;
          if (config) store.set({ fullConfig: { ...config, stale_session_timeout: seconds } });
          store.showToast(seconds === 0 ? "已关闭暂停会话自动过期" : `暂停会话 ${seconds} 秒后自动结束`);
        } catch (e) {
          store.showToast(`保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
        }
      }, 500);
    });
  }

  const webhookInput = container.querySelector<HTMLInputElement>("#notify-webhook");
  if (webhookInput) {
    webhookInput.addEventListener("input", () => {
      scope.debounce('webhook', async () => {
        const url = webhookInput.value.trim();
        try {
          await api.setNotifyWebhook(url);
          const config = store.get().fullConfig;
          if (config) store.set({ fullConfig: { ...config, notify_webhook_url: url } });
          store.showToast(url ? "通知地址已保存" : "登录失效通知已关闭");
        } catch (e) {
          store.showToast(`保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
        }
      }, 600);
    });
  }

  const applyPortChange = async (id: string, value: number | null) => {
    const statusEl = container.querySelector<HTMLElement>(`[data-port-status="${id}"]`);
    try {
      const result = await api.setPort(id, value);
      const config = store.get().fullConfig;
      if (config) store.set({ fullConfig: { ...config, ports: result.ports } });
      onStateChange();
      store.showToast(result.restart_required ? "已保存，重启应用后生效" : "已保存并重新应用");
    } catch (e) {
      if (statusEl) statusEl.textContent = "保存失败";
      store.showToast(`保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  };
  // Port inputs auto-save on commit (change fires on blur/Enter) — no save
  // button; an unchanged value or a failed validation never hits the server.
  container.querySelectorAll<HTMLInputElement>("[data-port-input]").forEach((input) => {
    input.addEventListener("change", () => {
      const id = input.dataset.portInput ?? "";
      const raw = input.value.trim();
      if (raw && !/^\d+$/.test(raw)) {
        store.showToast("端口必须是 1024-65535 的数字");
        input.value = input.dataset.committed ?? "";
        return;
      }
      if (raw === (input.dataset.committed ?? "")) return;
      input.dataset.committed = raw;
      void applyPortChange(id, raw ? Number(raw) : null);
    });
  });
  container.querySelectorAll<HTMLElement>("[data-port-reset]").forEach((el) => {
    el.addEventListener("click", () => void applyPortChange(el.dataset.portReset ?? "", null));
  });

  container.querySelectorAll("[data-theme]").forEach((el) => {
    el.addEventListener("click", () => {
      const theme = (el as HTMLElement).dataset.theme as Theme;
      onThemeChange(theme);
    });
  });

  container.querySelectorAll("[data-airplayprotocol]").forEach((el) => {
    el.addEventListener("click", async () => {
      if (store.get().saving) return;
      const protocol = (el as HTMLElement).dataset.airplayprotocol as AirPlayProtocol;
      const previous = store.get().fullConfig;
      store.set({ saving: true });
      if (previous) {
        store.set({ fullConfig: { ...previous, airplay_protocol: protocol } });
        onStateChange();
      }
      try {
        await api.setAirPlayProtocol(protocol);
        const [config, status] = await Promise.all([api.getConfig(), api.getStatus()]);
        store.set({ fullConfig: config, status, receivers: status.receivers, saving: false });
        onStateChange();
        store.showToast("AirPlay 设置已应用");
      } catch (e) {
        if (previous) {
          store.set({ fullConfig: previous });
          onStateChange();
        }
        store.set({ saving: false });
        store.showToast(`切换失败: ${e instanceof Error ? e.message : "未知错误"}`);
      }
    });
  });

  const appNameInput = container.querySelector("#app-name-input") as HTMLInputElement | null;
  if (appNameInput) {
    appNameInput.addEventListener("input", () => {
      scope.debounce('name', async () => {
        const name = appNameInput.value.trim();
        if (!name) return;
        try {
          await api.setAppName(name);
          const config = await api.getConfig();
          store.set({ fullConfig: config });
          store.showToast("名称已保存");
        } catch (e) {
          store.showToast(`保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
        }
      }, 600);
    });
  }

  bindUpdateSection(container, scope);
  bindResetAll(container);
}

function bindResetAll(container: HTMLElement) {
  const button = container.querySelector<HTMLButtonElement>("[data-reset-all]");
  if (!button) return;
  let confirmTimer: ReturnType<typeof setTimeout> | null = null;
  button.addEventListener("click", async () => {
    // 两段确认：第一次点击变成红色确认态，3 秒内再点才真正执行。
    if (!confirmTimer) {
      button.textContent = "确认清空？再点一次";
      confirmTimer = setTimeout(() => {
        confirmTimer = null;
        button.textContent = "清空数据";
      }, 3000);
      return;
    }
    clearTimeout(confirmTimer);
    confirmTimer = null;
    button.disabled = true;
    button.textContent = "正在清空…";
    try {
      await api.resetAll();
      store.showToast("数据已清空，即将回到引导页");
      setTimeout(() => window.location.reload(), 800);
    } catch (e) {
      button.disabled = false;
      button.textContent = "清空数据";
      store.showToast(`清空失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });
}

let updatePollTimer: ReturnType<typeof setInterval> | null = null;

function bindUpdateSection(container: HTMLElement, scope: SurfaceScope) {
  // A settings section can be rebound after a shell render. Stop any polling
  // owned by the previous DOM before attaching handlers to the new section.
  if (updatePollTimer) {
    clearInterval(updatePollTimer);
    updatePollTimer = null;
  }
  const statusEl = container.querySelector<HTMLElement>("[data-update-status]");
  const checkBtn = container.querySelector<HTMLButtonElement>("[data-update-check]");
  const downloadBtn = container.querySelector<HTMLButtonElement>("[data-update-download]");
  const applyBtn = container.querySelector<HTMLButtonElement>("[data-update-apply]");
  if (!statusEl || !checkBtn) return;

  const stopPolling = () => {
    if (updatePollTimer) { clearInterval(updatePollTimer); updatePollTimer = null; }
  };

  const showDownloadResult = async () => {
    const dl = await api.getUpdateDownloadStatus();
    if (!scope.active) return;
    if (dl.state === "downloading") {
      statusEl.textContent = dl.total > 0
        ? `正在下载更新… ${Math.min(100, Math.round((dl.progress / dl.total) * 100))}%（${formatBytes(dl.progress)} / ${formatBytes(dl.total)}）`
        : `正在下载更新… ${formatBytes(dl.progress)}`;
      return;
    }
    stopPolling();
    if (dl.state === "done") {
      statusEl.textContent = "下载完成，可以安装了";
      if (downloadBtn) downloadBtn.hidden = true;
      if (applyBtn) applyBtn.hidden = false;
    } else if (dl.state === "error") {
      statusEl.textContent = `下载失败：${dl.error ?? "未知错误"}`;
      if (downloadBtn) { downloadBtn.hidden = false; downloadBtn.disabled = false; downloadBtn.textContent = "重新下载"; }
    }
  };

  const runCheck = async (force: boolean) => {
    checkBtn.disabled = true;
    checkBtn.textContent = "检查中…";
    try {
      const info = await api.checkUpdate(force);
      if (!scope.active) return;
      if (info.update_available) {
        // exe 版提供应用内下载；其他部署通过常驻的 GitHub 发布页链接更新。
        statusEl.textContent = info.can_download
          ? `发现新版本 v${info.latest_version}（当前 v${info.current_version}）`
          : `发现新版本 v${info.latest_version}（当前 v${info.current_version}），请前往 GitHub 发布页更新`;
        if (info.can_download && info.asset && downloadBtn) {
          downloadBtn.hidden = false;
          downloadBtn.textContent = `下载更新（${formatBytes(info.asset.size)}）`;
        }
      } else {
        statusEl.textContent = `当前已是最新版本 v${info.current_version}`;
      }
    } catch (e) {
      statusEl.textContent = e instanceof Error ? e.message : "检查更新失败";
    } finally {
      checkBtn.disabled = false;
      checkBtn.textContent = "检查更新";
    }
  };

  checkBtn.addEventListener("click", () => runCheck(true));
  runCheck(false); // 打开设置页时先用缓存静默检查一次

  downloadBtn?.addEventListener("click", async () => {
    downloadBtn.disabled = true;
    downloadBtn.textContent = "正在下载…";
    try {
      await api.startUpdateDownload();
      if (!scope.active) return;
      stopPolling();
      let busy = false;
      updatePollTimer = setInterval(() => {
        if (busy || document.hidden) return;
        busy = true;
        showDownloadResult().catch(() => stopPolling()).finally(() => { busy = false; });
      }, 800);
    } catch (e) {
      downloadBtn.disabled = false;
      downloadBtn.textContent = "下载更新";
      store.showToast(e instanceof Error ? e.message : "下载失败");
    }
  });

  applyBtn?.addEventListener("click", async () => {
    applyBtn.disabled = true;
    applyBtn.textContent = "正在启动安装…";
    try {
      await api.applyUpdate();
      statusEl.textContent = "安装程序已启动，MiCast 即将退出并完成更新";
    } catch (e) {
      applyBtn.disabled = false;
      applyBtn.textContent = "安装并重启";
      store.showToast(e instanceof Error ? e.message : "启动安装失败");
    }
  });
}

function formatBytes(bytes: number): string {
  if (!bytes) return "0 MB";
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function bindFeatureSwitch(
  container: HTMLElement,
  selector: string,
  key: "airplay_enabled" | "dlna_enabled" | "sync_groups_enabled" | "large_delay_enabled" | "airplay2_enabled" | "touchscreen_lyrics" | "network_discovery_enabled",
  save: (enabled: boolean) => Promise<unknown>,
  rerender: () => void
) {
  container.querySelector<HTMLInputElement>(selector)?.addEventListener("change", async (event) => {
    if (store.get().saving) return;
    const enabled = (event.currentTarget as HTMLInputElement).checked;
    const previous = store.get().fullConfig;
    if (!previous) return;
    store.set({ saving: true, fullConfig: { ...previous, [key]: enabled } });
    rerender();
    try {
      await save(enabled);
      const [fullConfig, status] = await Promise.all([api.getConfig(), api.getStatus()]);
      store.set({ fullConfig, status, receivers: status.receivers, saving: false });
      rerender();
      const label = key === "airplay_enabled" ? "AirPlay" : key === "dlna_enabled" ? "DLNA" : key === "sync_groups_enabled" ? "音箱组合" : key === "large_delay_enabled" ? "大延迟" : key === "touchscreen_lyrics" ? "触屏歌词与封面" : key === "network_discovery_enabled" ? "网络发现" : "AirPlay 2";
      store.showToast(`${label}已${enabled ? "开启" : "关闭"}`);
    } catch (error) {
      store.set({ fullConfig: previous, saving: false });
      rerender();
      store.showToast(`设置失败: ${error instanceof Error ? error.message : "未知错误"}`);
    }
  });
}
