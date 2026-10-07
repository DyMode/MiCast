import { api, type FullConfig, type PortStatus } from "../api";
import { store } from "../state";
import { SurfaceScope } from '../ui/lifecycle';
import { renderControlChannel, bindControlChannel, refreshCapabilities } from "./device-capabilities";

let advancedScope: SurfaceScope | null = null;
export function disposeAdvancedView() {
  advancedScope?.dispose(true);
  advancedScope = null;
}

interface AdvancedProps {
  config: FullConfig | null;
}

export function renderAdvancedView(props: AdvancedProps): string {
  const { config } = props;
  return `
    <div class="page-heading">
      <button class="button plain back-link" data-advanced-back>‹ 返回设置</button>
      <h2 class="page-title">高级设置</h2>
      <p>端口、控制通道与播放行为。</p>
    </div>
    ${config?.ports?.length ? `
    <div class="group-header">服务端口</div>
    <div class="group">
      ${config.ports.map(renderPortRow).join("")}
    </div>
    ` : ""}
    <div class="group-header">控制通道</div>
    <p class="group-header-hint">协议声明不代表实际出声。自动模式按设备与音频格式判断，播放开始后保持同一通道。</p>
    ${renderControlChannel()}
    <div class="group-header">播放行为</div>
    <div class="group">
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
    </div>
  `;
}

export function bindAdvancedView(
  container: HTMLElement,
  onBack: () => void,
  onStateChange: () => void
) {
  disposeAdvancedView();
  const scope = advancedScope = new SurfaceScope();
  container.querySelector("[data-advanced-back]")?.addEventListener("click", onBack);
  bindControlChannel(container);
  refreshCapabilities();

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

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
