import { api, type NetworkDevice } from "../api";
import { store, type State } from "../state";
import { icon } from "../icons";
import { escapeHtml, message } from "./receivers-shared";
import { dlnaProofLabel, refreshCapabilities } from "./device-capabilities";

let devices: NetworkDevice[] = [];
let fetchedAt = 0;
let loading = false;
let error = "";
let editing: string | null = null;
const drafts = new Map<string, { name: string; target: string }>();
const busy = new Set<string>();
const testing = new Set<string>();

export function localDevice(id: string): NetworkDevice | undefined {
  return devices.find(device => device.id === id);
}

export function renderLocalBridge(state: State): string {
  const enabled = !!state.fullConfig?.network_discovery_enabled;
  const entries = [
    ...(state.fullConfig?.receivers.filter(item => item.target_type === "dlna") ?? []),
    ...(state.fullConfig?.airplay2_instances ?? []).filter(item => item.target_type === "dlna").map(item => ({
      ...item, enabled: state.fullConfig?.airplay2_mode === "single" ? !!state.fullConfig.airplay2_enabled : item.enabled, id: `airplay2:${item.id}`, target_name: localDevice(item.target_id || "")?.name || item.target_name || item.name,
    })),
  ];
  const rows = devices.filter(device => !entries.some(entry => entry.target_id === device.id));
  const classic = state.fullConfig?.protocol_status?.airplay;
  return `<section class="local-bridge" data-local-bridge>
    <div class="group-header">局域网设备 <span class="protocol-badge protocol-dlna">本地 DLNA</span></div>
    <p class="group-header-hint">把 DLNA 设备添加到手机 AirPlay 列表，无需米家登录。设备原有 DLNA 入口仍可使用。</p>
    ${classic?.status === "unsupported" ? `<p class="caption">当前部署无法提供经典 AirPlay；添加时将使用当前部署支持的 AirPlay 2 入口。</p>` : ""}
    ${classic?.status === "disabled" && state.fullConfig?.airplay_engine !== "airplay2" ? `<p class="caption">经典 AirPlay 已关闭。创建入口后，可在设置中开启接收服务。</p>` : ""}
    <div class="group">
      <div class="cell"><span class="cell-icon">${icon("antenna")}</span><div class="cell-content"><span class="cell-title">网络发现</span><span class="cell-subtitle">${enabled ? "发现同一网络的设备；可随时在设置中关闭" : "发现已暂停；开启后查找可播放的 DLNA 设备"}</span></div><button class="button ${enabled ? "plain" : "primary"}" data-local-scan ${loading ? "disabled" : ""}>${loading ? "正在发现…" : enabled ? "重新发现" : "开启并发现"}</button></div>
      ${error ? `<div class="local-feedback" role="status">${escapeHtml(error)}</div>` : ""}
      ${entries.map(entry => {
        const device = localDevice(entry.target_id || "");
        return `<div class="local-device-row"><div class="cell"><span class="cell-icon">${icon("loudspeaker")}</span><div class="cell-content"><span class="cell-title">${escapeHtml(entry.name)}</span><span class="cell-subtitle">${entry.id.startsWith("airplay2:") ? "AirPlay 2" : "AirPlay"} → ${escapeHtml(device?.name || entry.target_name || "DLNA 设备")} · ${enabled ? device?.online ? "在线" : "目标离线" : "发现已暂停"}</span></div><label class="receiver-visibility"><span>${entry.enabled ? "显示入口" : "已隐藏"}</span><input class="switch" type="checkbox" data-local-enabled="${escapeHtml(entry.id)}" ${entry.enabled ? "checked" : ""} aria-label="显示 ${escapeHtml(entry.name)}"></label><button class="button plain" data-local-edit="${escapeHtml(entry.id)}" aria-expanded="${editing === entry.id}">${editing === entry.id ? "收起" : "编辑入口"}</button></div>${editing === entry.id ? editor(entry.id, entry.name, entry.target_id || "", true) : ""}</div>`;
      }).join("")}
      ${enabled ? rows.map(device => `<div class="local-device-row"><div class="cell"><span class="cell-icon">${icon("loudspeaker")}</span><div class="cell-content"><span class="cell-title">${escapeHtml(device.name)}</span><span class="cell-subtitle">${escapeHtml(device.model || "DLNA 播放设备")} · ${device.online ? "在线" : "离线"} · ${escapeHtml(dlnaProofLabel(device.id) ?? (device.supported ? "未验证实际播放" : device.unsupported_reason))}</span></div><button class="button secondary" data-local-edit="${escapeHtml(device.id)}" ${!device.online || !device.supported ? "disabled" : ""} aria-expanded="${editing === device.id}">${editing === device.id ? "收起" : "添加 AirPlay 入口"}</button></div>${editing === device.id ? editor(device.id, device.name, device.id, false) : ""}</div>`).join("") : ""}
      ${enabled && !rows.length && !loading ? `<div class="cell"><div class="cell-content"><span class="cell-title">${entries.length ? "暂无其他可添加设备" : "暂未发现 DLNA 播放设备"}</span><span class="cell-subtitle">确认设备与 MiCast 在同一网络，且设备支持 DLNA 播放。并非所有小爱音箱都支持。</span></div></div>` : ""}
    </div>
  </section>`;
}

function editor(key: string, name: string, target: string, existing: boolean): string {
  const draft = drafts.get(key) ?? { name, target };
  const config = store.get().fullConfig;
  const ap2 = key.startsWith("airplay2:") || !existing && (config?.airplay_engine === "airplay2" || config?.protocol_status?.airplay?.status === "unsupported");
  const fixed = ap2 && config?.airplay2_mode === "single";
  const device = localDevice(draft.target);
  const pending = busy.has(key);
  const test = testing.has(draft.target);
  const options = devices.filter(item => item.online && item.supported);
  if (!options.some(item => item.id === draft.target)) options.unshift({ id: draft.target, name: device?.name || "当前设备（离线）" } as NetworkDevice);
  return `<form class="local-bridge-editor" data-local-form="${escapeHtml(key)}" data-existing="${existing}">
    <label>AirPlay 显示名称<input class="input" name="name" value="${escapeHtml(draft.name)}" maxlength="${ap2 ? 50 : 64}" required ${pending ? "disabled" : ""}></label>
    ${existing ? `<label>播放设备<select class="input" name="target" ${pending || test ? "disabled" : ""}>${options.map(item => `<option value="${escapeHtml(item.id)}" ${item.id === draft.target ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label>` : `<input type="hidden" name="target" value="${escapeHtml(draft.target)}">`}
    <p class="caption">${ap2 ? "添加 AirPlay 2 入口。" : "仅添加 AirPlay 入口。"}测试会让设备播放提示音，不会调整设备音量。</p>
    ${fixed ? `<p class="caption">当前安装提供一个固定 AirPlay 2 入口。绑定其他设备会替换它的播放目标。</p>` : ""}
    <div class="local-bridge-actions"><button class="button plain" type="button" data-local-test="${escapeHtml(draft.target)}" data-mode="sample" ${test || !device?.online ? "disabled" : ""}>测试提示音</button><button class="button plain" type="button" data-local-test="${escapeHtml(draft.target)}" data-mode="stream" ${test || !device?.online ? "disabled" : ""}>测试持续流</button>${test ? `<button class="button plain danger-text" type="button" data-local-test-stop="${escapeHtml(draft.target)}">停止测试</button>` : ""}</div>
    <p class="caption" aria-live="polite">${test ? "正在测试，请确认设备出声…" : escapeHtml(device?.test_result?.detail || "尚未测试；可先保存，再用手机实际播放验证。")}</p>
    <div class="local-bridge-actions">${existing && !fixed ? `<button class="button plain danger-text" type="button" data-local-delete="${escapeHtml(key)}" ${pending ? "disabled" : ""}>删除入口</button>` : ""}<button class="button primary" type="submit" ${pending ? "disabled" : ""}>${pending ? "正在保存…" : existing ? "保存修改" : "创建入口"}</button></div>
  </form>`;
}

export function bindLocalBridge(container: HTMLElement, rerender: () => void): void {
  const section = container.querySelector<HTMLElement>("[data-local-bridge]");
  if (!section) return;
  const refresh = async () => {
    const [fullConfig, status] = await Promise.all([api.getConfig(), api.getStatus()]);
    store.set({ fullConfig, status, receivers: status.receivers });
  };
  const fill = () => {
    if (!section.isConnected || section.contains(document.activeElement) && document.activeElement?.matches("input, select")) return;
    section.outerHTML = renderLocalBridge(store.get());
    bindLocalBridge(container, rerender);
  };
  const fetchDevices = async (scan = false) => {
    if (loading) return;
    loading = true; error = "";
    try {
      if (scan) {
        if (!store.get().fullConfig?.network_discovery_enabled) {
          await api.setNetworkDiscoveryEnabled(true); await refresh();
        }
        await api.rescanDlnaDevices();
      }
      const found = await api.getDlnaDevices();
      devices = Array.isArray(found) ? found : [];
      fetchedAt = Date.now();
    } catch (e) { error = `发现失败：${message(e)}`; fetchedAt = Date.now(); }
    finally { loading = false; fill(); refreshCapabilities(); }
  };
  section.querySelector("[data-local-scan]")?.addEventListener("click", () => {
    void fetchDevices(true);
    const button = section.querySelector<HTMLButtonElement>("[data-local-scan]");
    if (button) { button.disabled = true; button.textContent = "正在发现…"; }
  });
  section.querySelectorAll<HTMLButtonElement>("[data-local-edit]").forEach(button => button.addEventListener("click", () => {
    const key = button.dataset.localEdit!;
    editing = editing === key ? null : key;
    fill();
    container.querySelector<HTMLInputElement>("[data-local-form] input[name=name]")?.focus();
  }));
  section.querySelectorAll<HTMLFormElement>("[data-local-form]").forEach(form => {
    const key = form.dataset.localForm!;
    const remember = () => {
      const name = form.elements.namedItem("name") as HTMLInputElement;
      const target = form.elements.namedItem("target") as HTMLInputElement | HTMLSelectElement;
      drafts.set(key, { name: name.value, target: target.value });
    };
    form.addEventListener("input", remember);
    form.addEventListener("change", () => { remember(); if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); fill(); });
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (busy.has(key)) return;
      remember(); const draft = drafts.get(key)!;
      busy.add(key); (document.activeElement as HTMLElement)?.blur(); fill();
      try {
        const config = store.get().fullConfig;
        const useAirPlay2 = key.startsWith("airplay2:") || form.dataset.existing !== "true" && (config?.airplay_engine === "airplay2" || config?.protocol_status?.airplay?.status === "unsupported");
        if (useAirPlay2) {
          if (!config?.airplay2_available) throw new Error("当前部署无法提供 AirPlay 入口，请使用支持局域网接收的部署方式");
          const id = key.startsWith("airplay2:") ? key.slice(9) : config.airplay2_mode === "single" ? "airplay2" : undefined;
          await api.saveAirPlay2Instance({ id, name: draft.name, target_type: "dlna", target_id: draft.target });
          if (!config.airplay2_enabled) await api.setAirPlay2Enabled(true);
        } else if (form.dataset.existing === "true") {
          const current = store.get().fullConfig?.receivers.find(item => item.id === key);
          await api.updateReceiver(key, { name: draft.name, ...(draft.target !== current?.target_id ? { target_id: draft.target } : {}) });
        } else await api.createReceiver({ name: draft.name, target_type: "dlna", target_id: draft.target });
        await refresh(); drafts.delete(key); editing = null;
        store.showToast("AirPlay 入口已保存");
      } catch (e) { store.showToast(`保存失败：${message(e)}`); }
      finally { busy.delete(key); rerender(); }
    });
  });
  section.querySelectorAll<HTMLButtonElement>("[data-local-test]").forEach(button => button.addEventListener("click", async () => {
    const id = button.dataset.localTest!; if (testing.has(id)) return;
    testing.add(id); fill();
    try {
      const result = await api.testDlnaDevice(id, button.dataset.mode as "sample" | "stream");
      const device = localDevice(id); if (device) device.test_result = result;
      store.showToast(result.detail);
    } catch (e) { const device = localDevice(id); if (device) device.test_result = { status: "error", detail: message(e) }; store.showToast(`测试结束：${message(e)}`); }
    finally { testing.delete(id); fill(); }
  }));
  section.querySelectorAll<HTMLButtonElement>("[data-local-test-stop]").forEach(button => button.addEventListener("click", async () => {
    try { await api.stopDlnaTest(button.dataset.localTestStop!); store.showToast("测试已停止"); }
    catch (e) { store.showToast(`停止失败：${message(e)}`); }
  }));
  section.querySelectorAll<HTMLInputElement>("[data-local-enabled]").forEach(input => input.addEventListener("change", async () => {
    const id = input.dataset.localEnabled!; input.disabled = true;
    try {
      if (id.startsWith("airplay2:")) {
        if (store.get().fullConfig?.airplay2_mode === "single") await api.setAirPlay2Enabled(input.checked);
        else await api.setAirPlay2InstanceEnabled(id.slice(9), input.checked);
      }
      else await api.updateReceiver(id, { enabled: input.checked });
      await refresh(); rerender();
    } catch (e) { input.checked = !input.checked; store.showToast(`设置失败：${message(e)}`); }
    finally { input.disabled = false; }
  }));
  section.querySelectorAll<HTMLButtonElement>("[data-local-delete]").forEach(button => button.addEventListener("click", async () => {
    const id = button.dataset.localDelete!; if (busy.has(id)) return;
    busy.add(id);
    try { if (id.startsWith("airplay2:")) await api.deleteAirPlay2Instance(id.slice(9)); else await api.deleteReceiver(id); await refresh(); editing = null; drafts.delete(id); store.showToast("播放入口已删除"); }
    catch (e) { store.showToast(`删除失败：${message(e)}`); }
    finally { busy.delete(id); rerender(); }
  }));
  if (store.get().fullConfig?.network_discovery_enabled && Date.now() - fetchedAt > 10_000) void fetchDevices();
}
