import type { Device, PlaybackState, SpeakerEq } from "../api";
import { api } from "../api";
import { store } from "../state";
import { setVolume } from "../volume-service";
import { brandIcon, icon } from "../icons";
import { openTuning } from "./tuning-view";

export const EQ_PRESET_LABELS: Record<string, string> = {
  flat: "平直",
  bass: "低音增强",
  vocal: "人声清晰",
  night: "轻音",
  live: "现场感",
  harman: "Harman",
};

interface DeviceVisual {
  html: string;
  className: string;
}

const PRODUCT_IMAGES: Record<string, string> = {
  lx01: "assets/devices/xiaomi-wifispeaker-lx01.png",
  lx06: "assets/devices/xiaomi-wifispeaker-lx06-card.png",
  l06a: "assets/devices/xiaomi-wifispeaker-l06a.png",
  s12: "assets/devices/xiaomi-wifispeaker-s12.png",
  l16a: "assets/devices/xiaomi-wifispeaker-l16a.png",
  oh2: "assets/devices/xiaomi-wifispeaker-oh2-card.png",
  oh2p: "assets/devices/xiaomi-wifispeaker-oh2p-card.png",
  l05b: "assets/devices/xiaomi-wifispeaker-l05b.png",
  l05c: "assets/devices/xiaomi-wifispeaker-l05c.png",
  l15a: "assets/devices/xiaomi-wifispeaker-l15a.png",
  l17a: "assets/devices/xiaomi-wifispeaker-l17a.png",
};

interface DevicesProps {
  devices: Device[];
  expandedDid: string | null;
  status: string;
  pcmSource: string;
  streamUrl: string;
  loggedIn: boolean;
  /** Tokens exist but the cloud is not answering (offers a re-login). */
  cloudDegraded?: boolean;
  loadError: string | null;
  playback: PlaybackState | null;
}

export function renderDevicesView(props: DevicesProps): string {
  const { devices, expandedDid, status, loggedIn, loadError, playback, cloudDegraded } = props;
  const onlineCount = devices.filter((item) => item.presence === "online").length;
  const offlineCount = devices.filter((item) => item.presence === "offline").length;
  const unknownCount = devices.length - onlineCount - offlineCount;
  const statusClass = onlineCount > 0
    ? offlineCount > 0 || unknownCount > 0 ? "warning" : "running"
    : devices.length ? (offlineCount === devices.length ? "error" : "warning")
      : loadError ? "error" : "";
  // A failed load is not "no devices": saying so was how a dead login looked
  // like an empty account with nothing to click.
  const statusText = devices.length
    ? offlineCount > 0 ? `${offlineCount} 台离线` : unknownCount > 0 ? `${unknownCount} 台待确认` : "全部在线"
    : loadError ? "加载失败" : loggedIn ? "尚无设备" : "未登录";

  return `
    <div class="page-heading">
      <h2 class="page-title">音箱</h2>
      <p>查看音箱状态、调整音量和修改显示名称。</p>
    </div>

    <div class="group-header">音箱状态</div>
    <div class="group">
      <div class="cell">
        <div class="cell-icon blue">${icon("wave")}</div>
        <div class="cell-content">
          <span class="cell-title">${devices.length
            ? `已发现 ${devices.length} 台音箱`
            : loadError ? "音箱列表获取失败" : "尚未发现音箱"}</span>
          <span class="cell-subtitle">${devices.length
            ? `${onlineCount} 台在线${offlineCount ? `，${offlineCount} 台离线` : ""}${unknownCount ? `，${unknownCount} 台待确认` : ""}`
            : loadError ? "账号里的音箱没有取到，可以重试或重新登录" : "登录后会自动显示账号下的音箱"}</span>
        </div>
        <span class="status-pill ${statusClass}" data-status-label>${statusText}</span>
      </div>
      ${renderMasterVolume(devices, playback)}
    </div>

    <div class="group-header">我的音箱</div>
    <div class="device-list">
      ${devices.length === 0
        ? `
            <div class="group">
              <div class="empty-state">
                <div class="empty-state-icon">${icon("speaker")}</div>
                <span class="body">${loadError ? "设备加载失败" : loggedIn ? "暂无设备" : "还没有连接米家账号"}</span>
                <span class="caption">${
                  loadError ||
                  (cloudDegraded
                    ? "米家云端暂时没有响应：先重试；重新登录同样需要这台设备能访问小米账号服务器"
                    : loggedIn
                      ? "当前账号下未发现支持的音箱"
                      : "扫码登录后会自动显示账号下的音箱")
                }</span>
                <div class="empty-state-actions">
                  <button class="button secondary compact" type="button" data-retry-devices>重试</button>
                  <button class="button primary compact" type="button" data-relogin>重新登录米家</button>
                </div>
              </div>
            </div>
          `
        : devices
            .map((d) => renderDeviceCard(d, expandedDid === d.did, playback))
            .join("")}
    </div>
  `;
}

function deviceVolume(device: Device, playback: PlaybackState | null): number {
  const live = playback?.devices.find((item) => item.did === device.did);
  return device.volume ?? live?.volume ?? 50;
}

function deviceMuted(device: Device, playback: PlaybackState | null): boolean {
  const live = playback?.devices.find((item) => item.did === device.did);
  return live?.muted ?? device.muted ?? false;
}

function renderMasterVolume(devices: Device[], playback: PlaybackState | null): string {
  const online = devices.filter((d) => d.presence === "online");
  if (online.length < 2) return "";
  const volumes = online.map((d) => deviceVolume(d, playback));
  const volume = Math.round(volumes.reduce((sum, v) => sum + v, 0) / volumes.length);
  const mixed = new Set(volumes).size > 1;
  const allMuted = online.length > 0 && online.every((d) => deviceMuted(d, playback));
  const dids = online.map((d) => d.did).join(",");
  return `
    <div class="cell">
      <div class="cell-icon ${mixed ? "gray" : "blue"}" data-master-icon>${icon("speaker")}</div>
      <div class="cell-content">
        <span class="cell-title">全部音量</span>
        <span class="cell-subtitle" data-master-subtitle>${mixed ? `${online.length} 台音箱音量不同` : `${online.length} 台音箱`}</span>
      </div>
      <label class="volume-control device-volume-inline ${mixed ? "mixed" : ""}" title="全部音箱音量" data-master-control>
        <span class="volume-icon">${volume === 0 ? "×" : icon("speaker")}</span>
        <input type="range" min="0" max="100" value="${volume}" data-master-volume="${escapeHtml(dids)}" aria-label="全部音箱音量" style="--volume:${volume}%">
        <output>${volume}</output>
      </label>
      <button type="button" class="icon-button compact" data-mute="${escapeHtml(dids)}" aria-pressed="${allMuted}" aria-label="${allMuted ? "取消全部静音" : "全部静音"}" title="${allMuted ? "取消全部静音" : "全部静音"}">${icon(allMuted ? "mute" : "speaker")}</button>
    </div>
    <div class="cell">
      <div class="cell-content"><span class="cell-title">相对调节</span><span class="cell-subtitle">保留音量差，每次 5 格</span></div>
      <div class="stepper" role="group" aria-label="全部音箱相对调节">
        <button type="button" data-volume-step="-5" data-volume-targets="${escapeHtml(dids)}" aria-label="全部音箱降低 5 格">−5</button>
        <span class="stepper-divider" aria-hidden="true"></span>
        <button type="button" data-volume-step="5" data-volume-targets="${escapeHtml(dids)}" aria-label="全部音箱提高 5 格">+5</button>
      </div>
    </div>
  `;
}

function renderDeviceCard(device: Device, expanded: boolean, playback: PlaybackState | null): string {
  const isOnline = device.presence === "online";
  const displayName = device.alias || device.name;
  const volume = deviceVolume(device, playback);
  const muted = deviceMuted(device, playback);
  const visual = deviceVisual(device);

  return `
    <div class="device-card ${expanded ? "expanded" : ""}" data-did="${device.did}">
      <div class="device-card-header" data-device-header role="button" tabindex="0"
           aria-expanded="${expanded}" aria-label="${escapeHtml(displayName)}详情，${expanded ? "已展开" : "已收起"}">
        <div class="cell-icon device-brand ${isOnline ? visual.className : "gray"}">${visual.html}</div>
        <div class="device-info">
          <span class="device-name">${escapeHtml(displayName)}</span>
          <span class="device-meta">${escapeHtml(device.name)} · ${device.model} · ${isOnline ? "在线" : "离线"}</span>
        </div>
        <div class="device-actions">
          <label class="volume-control device-volume-inline" title="${escapeHtml(displayName)}音量">
            <span class="volume-icon">${icon("speaker")}</span>
            <input type="range" min="0" max="100" value="${volume}" data-device-volume="${escapeHtml(device.did)}" aria-label="${escapeHtml(displayName)}音量" style="--volume:${volume}%">
            <output>${volume}</output>
          </label>
          <button type="button" class="icon-button compact" data-mute="${escapeHtml(device.did)}" aria-pressed="${muted}" aria-label="${muted ? "取消静音" : "静音"}" title="${muted ? "取消静音" : "静音"}">${icon(muted ? "mute" : "speaker")}</button>
          <span class="device-expand-icon ${expanded ? "expanded" : ""}" aria-hidden="true">${icon("chevron")}</span>
        </div>
      </div>
      ${expanded ? renderDeviceDetails(device, playback) : ""}
    </div>
  `;
}

function deviceVisual(device: Device): DeviceVisual {
  const productImage = PRODUCT_IMAGES[device.model.toLowerCase()];
  if (productImage) {
    return {
      html: `<img class="device-product-image" src="${productImage}" alt="">`,
      className: "device-product",
    };
  }
  const identity = `${device.name} ${device.model}`.toLowerCase();
  if (identity.includes("xiaomi") || identity.includes("redmi") || identity.includes("小爱")) {
    return { html: brandIcon("xiaomi"), className: "xiaomi" };
  }
  return { html: icon("speaker"), className: "blue" };
}

function renderDeviceDetails(device: Device, _playback: PlaybackState | null): string {
  return `
    <div class="device-card-body">
      <div class="device-detail-row">
        <span class="caption">显示名称</span>
        <input type="text" class="alias-input" value="${escapeHtml(device.alias || device.name)}" placeholder="${escapeHtml(device.name)}" required maxlength="64" data-alias-input style="max-width: 220px;" aria-label="${escapeHtml(device.alias || device.name)}的音箱别名与 AirPlay 名称">
      </div>
      <div class="device-detail-row">
        <span class="caption">原生型号</span>
        <span class="cell-value">${escapeHtml(device.model)}</span>
      </div>
      <div class="device-detail-row">
        <span class="caption">设备 ID</span>
        <span class="cell-value footnote">${escapeHtml(device.did)}</span>
      </div>
      ${renderCodecCapabilities(device)}
      ${renderEqSection(device)}
    </div>
  `;
}

/** "3 分钟前" for a capability verdict, so a stale record is visible as such. */
function formatRelative(unixSeconds: number): string {
  const age = Math.max(0, Date.now() / 1000 - unixSeconds);
  if (age < 90) return "刚刚确认";
  if (age < 3600) return `${Math.round(age / 60)} 分钟前`;
  if (age < 86400) return `${Math.round(age / 3600)} 小时前`;
  return `${Math.round(age / 86400)} 天前`;
}

function renderCodecCapabilities(device: Device): string {
  const details = device.codec_capability_details ?? {};
  const formats = device.codec_formats ?? Object.keys(details);
  const labels = device.codec_labels ?? {};
  if (!Object.keys(details).length) {
    return `<div class="device-detail-row"><span class="caption">格式兼容性</span><span class="cell-value">空闲时自动检测</span></div>`;
  }
  // Compact chips instead of a sentence: four verdicts, their timestamps and
  // the reason used to be strung together with "·" and could not be scanned.
  // The evidence stays available in the tooltip.
  const chips = formats.map((fmt) => {
    const meta = details[fmt];
    const name = escapeHtml(meta?.label || labels[fmt] || fmt);
    const verdict = !meta
      ? { mark: "—", tone: "unknown", text: "未测" }
      : meta.status === "supported"
        ? { mark: "✓", tone: "ok", text: "支持" }
        : meta.status === "unverified"
          ? { mark: "?", tone: "unknown", text: "需试听" }
          : { mark: "✕", tone: "bad", text: "不支持" };
    const when = meta?.verified_at ? ` · ${formatRelative(meta.verified_at)}` : "";
    const reason = meta?.reason ? CODEC_REASON_TEXT[meta.reason] || meta.reason : "尚未测过";
    return `<span class="codec-chip ${verdict.tone}" title="${escapeHtml(
      `${meta?.label || labels[fmt] || fmt}：${verdict.text}${when}\n${reason}`
    )}">${name} ${verdict.mark}</span>`;
  });
  return `<div class="device-detail-row codec-row"><span class="caption">格式兼容性</span><span class="cell-value codec-chips">${chips.join("")}</span></div>`;
}

/** Why a format carries the verdict it does — shown on hover, not in the row. */
const CODEC_REASON_TEXT: Record<string, string> = {
  stream_pull_confirmed: "播放时音箱持续取流，自动确认",
  stream_verified: "播放时自动确认",
  auto_probe: "空闲时后台自动检测",
  active_probe: "诊断页实测",
  no_stream_pull: "播放时音箱没有取流，判定不支持",
  pcm_passthrough_unverifiable: "PCM 直通无法远程验证：音箱会照常取流，是否有声只能靠听",
  model_rejects_pcm_passthrough: "该型号会读取 PCM 流但不出声",
};

function renderEqSection(device: Device): string {
  const eq: SpeakerEq | undefined = device.eq;
  const statusLabel = !eq?.enabled
    ? "已关闭"
    : eq.preset && eq.preset in EQ_PRESET_LABELS
      ? EQ_PRESET_LABELS[eq.preset]
      : eq.points.length
        ? "自定义曲线"
        : "平直";
  return `
    <div class="device-detail-row eq-title-row">
      <span class="caption">均衡器 EQ</span>
      <div class="eq-entry">
        <span class="cell-value">${statusLabel}</span>
        <input type="checkbox" class="switch" data-eq-toggle ${eq?.enabled ? "checked" : ""} aria-label="启用均衡器">
        ${eq?.enabled ? `<button type="button" class="button secondary" data-tuning-open>调音台</button>` : ""}
      </div>
    </div>
  `;
}

function bindEqSection(container: HTMLElement, onOpenTuning: (did: string) => void) {
  container.querySelectorAll<HTMLElement>("[data-did]").forEach((card) => {
    const did = card.dataset.did!;
    const toggle = card.querySelector<HTMLInputElement>("[data-eq-toggle]");
    toggle?.addEventListener("change", () => {
      const device = store.get().devices.find((d) => d.did === did);
      api
        .setDeviceEqCurve(did, {
          enabled: toggle.checked,
          points: device?.eq?.points ?? [],
          preset: device?.eq?.preset ?? "",
          target: device?.eq?.target ?? "",
        })
        .then((resp) => {
          // The 调音台 entry only exists while EQ is on — re-render so it
          // appears/disappears with the switch. The store alone does not
          // re-render, and the switch still holds focus here, so an explicit
          // request is required (polls only update this page in place).
          store.set({
            devices: store.get().devices.map((d) => (d.did === did ? { ...d, eq: resp } : d)),
          });
          window.dispatchEvent(new CustomEvent("micast:request-render"));
        })
        .catch((e) => {
          store.showToast(`EQ 保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
        });
    });
    card.querySelector<HTMLElement>("[data-tuning-open]")?.addEventListener("click", () => {
      onOpenTuning(did);
    });
  });
}

export function bindDevicesView(
  container: HTMLElement,
  onExpandedChange: (did: string | null) => void,
  onOpenTuning: (did: string) => void = (did) => {
    openTuning(did);
  }
) {
  bindEqSection(container, onOpenTuning);
  container.querySelectorAll<HTMLElement>("[data-device-header]").forEach((el) => {
    const toggle = () => {
      const card = el.closest("[data-did]") as HTMLElement | null;
      const did = card?.dataset.did;
      if (!did) return;

      const current = store.get().ui.expandedDeviceDid;
      onExpandedChange(current === did ? null : did);
    };
    el.addEventListener("click", (e) => {
      // Don't toggle expand when interacting with a slider, switch or button.
      const target = e.target as HTMLElement;
      if (target.closest("input, button")) return;
      toggle();
    });
    // The header is the disclosure control, so it has to answer the keyboard
    // the same way a button would.
    el.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      toggle();
    });
  });

  container.querySelectorAll("[data-alias-input]").forEach((el) => {
    const input = el as HTMLInputElement;
    const saveAlias = async () => {
      const card = input.closest("[data-did]") as HTMLElement | null;
      const did = card?.dataset.did;
      if (!did) return;
      const alias = input.value.trim();
      if (!alias) {
        store.showToast("名称不能为空");
        input.value = input.defaultValue;
        return;
      }
      if (store.get().saving) return;
      store.set({ saving: true });
      try {
        await api.setAlias(did, alias);
        const [devices, fullConfig, status] = await Promise.all([api.getDevices(), api.getConfig(), api.getStatus()]);
        store.set({ devices, fullConfig, status, receivers: status.receivers, saving: false });
        store.showToast("名称已更新，AirPlay 列表将自动刷新");
      } catch (err) {
        store.set({ saving: false });
        store.showToast(`保存失败: ${err instanceof Error ? err.message : "未知错误"}`);
      }
    };

    input.addEventListener("blur", saveAlias);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        input.blur();
      }
    });
  });

  container.querySelectorAll<HTMLInputElement>("[data-device-volume]").forEach((input) => {
    input.addEventListener("input", () => {
      const value = Number(input.value);
      input.style.setProperty("--volume", `${value}%`);
      const output = input.nextElementSibling as HTMLOutputElement | null;
      if (output) output.value = String(value);
    });
    // Commit on release only — keeps the thumb glued to the finger.
    input.addEventListener("change", async () => {
      try {
        await setVolume(Number(input.value), [input.dataset.deviceVolume!]);
        store.showToast("音箱音量已调整");
      } catch (e) {
        store.showToast(`音量设置失败: ${e instanceof Error ? e.message : "未知错误"}`);
      }
    });
  });

  const master = container.querySelector<HTMLInputElement>("[data-master-volume]");
  if (master) {
    const dids = (master.dataset.masterVolume || "").split(",").filter(Boolean);
    const output = master.nextElementSibling as HTMLOutputElement | null;
    master.addEventListener("input", () => {
      const value = Number(master.value);
      master.style.setProperty("--volume", `${value}%`);
      if (output) output.value = String(value);
      // Dragging means "unify": light the control immediately.
      container.querySelector<HTMLElement>("[data-master-control]")?.classList.remove("mixed");
      // Mirror the change on each per-device slider immediately for feedback.
      container.querySelectorAll<HTMLInputElement>("[data-device-volume]").forEach((input) => {
        if (!dids.includes(input.dataset.deviceVolume!)) return;
        input.value = String(value);
        input.style.setProperty("--volume", `${value}%`);
        const deviceOutput = input.nextElementSibling as HTMLOutputElement | null;
        if (deviceOutput) deviceOutput.value = String(value);
      });
    });
    master.addEventListener("change", async () => {
      const value = Number(master.value);
      try {
        await setVolume(value, dids);
        // All targets now share one volume: light the control up in place.
        const control = container.querySelector<HTMLElement>("[data-master-control]");
        control?.classList.remove("mixed");
        const iconCell = container.querySelector<HTMLElement>("[data-master-icon]");
        iconCell?.classList.remove("gray");
        iconCell?.classList.add("blue");
        const subtitle = container.querySelector<HTMLElement>("[data-master-subtitle]");
        if (subtitle) subtitle.textContent = `同时调整 ${dids.length} 台在线音箱`;
      } catch (e) {
        store.showToast(`音量调整失败: ${e instanceof Error ? e.message : "未知错误"}`);
      }
    });
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
