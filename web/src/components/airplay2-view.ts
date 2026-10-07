import type { AirPlay2State } from "../api";
import { api } from "../api";
import { icon } from "../icons";
import { store, type AirPlay2Tab } from "../state";

export function renderAirPlay2View(state: AirPlay2State | null, tab: AirPlay2Tab): string {
  if (!state) return `<div class="page-heading"><h2 class="page-title">AirPlay 2</h2><p>正在读取服务和播放入口…</p></div>`;
  return `
    <div class="page-heading airplay2-heading">
      <button class="button plain back-link" data-airplay2-back>‹ 返回播放</button>
      <h2 class="page-title">AirPlay 2 <span class="feature-badge">实验性</span></h2>
      <p>管理 AirPlay 2 播放入口及其对应音箱。</p>
    </div>
    <div class="segmented-control airplay2-tabs" role="tablist" aria-label="AirPlay 2 管理">
      ${tabButton("overview", "概览", tab)}${state.can_add_instances ? tabButton("instances", "播放入口", tab) : ""}${tabButton("mappings", "播放目标", tab)}
    </div>
    ${tab === "overview" ? overview(state) : tab === "instances" && state.can_add_instances ? instances(state) : mappings(state)}
  `;
}

function tabButton(value: AirPlay2Tab, label: string, active: AirPlay2Tab): string {
  return `<button class="segment ${value === active ? "active" : ""}" data-airplay2-tab="${value}" role="tab" aria-selected="${value === active}">${label}</button>`;
}

function overview(state: AirPlay2State): string {
  const s = state.summary;
  const orchestrationReady = state.orchestration.status === "running";
  const summary = `
    <div class="airplay2-summary" aria-label="AirPlay 2 状态概览">
      ${summaryItem("link", "服务状态", state.orchestration.detail, orchestrationReady)}
      ${summaryItem("airplay", "播放入口", `${s.instances_running}/${s.instances_total} 个运行中`, s.instances_total > 0 && s.instances_running === s.instances_total)}
      ${summaryItem("check", "播放目标", `${s.mappings_healthy}/${s.mappings_total} 个正常`, s.mappings_total > 0 && s.mappings_healthy === s.mappings_total)}
    </div>`;
  return `
    ${summary}
    <div class="group-header">播放入口</div>
    <div class="group">${state.instances.length ? state.instances.slice(0, 3).map((item) => instanceRow(item, state.can_add_instances, state.enabled)).join("") : emptyRow("尚未创建播放入口", orchestrationReady ? "可以创建第一个 AirPlay 2 播放入口" : state.orchestration.detail)}</div>
  `;
}

function instances(state: AirPlay2State): string {
  if (state.orchestration.status !== "running") {
    return `<div class="group">${emptyRow("AirPlay 2 暂不可用", state.orchestration.detail)}</div>`;
  }
  return `
    <div class="group-header">播放入口</div>
    <div class="group">${state.instances.length ? state.instances.map((item) => instanceRow(item, state.can_add_instances, state.enabled)).join("") : emptyRow("尚未创建播放入口", "播放入口可以对应一台音箱或一个音箱组合")}</div>
    <div class="group-header">新建播放入口</div>
    ${state.targets.length ? `<form class="group airplay2-node-form" data-airplay2-instance-form>
      <label><span>显示名称</span><input class="input" name="name" required maxlength="50" placeholder="为播放入口命名"></label>
      <label><span>播放目标</span><select class="input" name="target">${targetOptions(state)}</select></label>
      <button class="button secondary full" type="submit">创建播放入口</button>
    </form>` : `<div class="group">${emptyRow("暂时无法创建播放入口", "需要先添加可用音箱或音箱组合")}</div>`}
  `;
}

function mappings(state: AirPlay2State): string {
  if (state.instances.length === 0) {
    return prerequisiteEmpty("尚未创建播放入口", "创建播放入口后，才能选择最终播放的音箱。", "创建播放入口", "instances");
  }
  return `<div class="group-header">播放入口与目标音箱</div><div class="group">${state.instances.length ? state.instances.map((item) => `
    <form class="cell mapping-edit-row" data-instance-mapping="${item.id}" data-instance-name="${escapeHtml(item.name)}" data-mapping-enabled="${item.enabled}">
      <div class="cell-content"><span class="cell-title">${escapeHtml(item.name)}</span><span class="cell-subtitle">播放到 ${escapeHtml(item.target_name)}</span></div>
      <select class="input" name="target">${targetOptions(state, item.target_type, item.target_id)}</select>
      <button class="button secondary" type="submit">保存</button>
    </form>`).join("") : emptyRow("尚无播放映射", "创建实例后可以选择播放目标")}</div>`;
}

function targetOptions(state: AirPlay2State, selectedType?: string, selectedId?: string | null): string {
  return state.targets.map((target) => `<option value="${target.type}:${target.id}" ${target.type === selectedType && target.id === selectedId ? "selected" : ""}>${escapeHtml(target.name)} · ${target.type === "group" ? "音箱组合" : "音箱"}</option>`).join("");
}

function summaryItem(iconName: "link" | "airplay" | "check", label: string, value: string, healthy: boolean): string {
  return `<div class="airplay2-summary-item"><span class="cell-icon ${healthy ? "green" : "gray"}">${icon(iconName)}</span><span>${label}</span><strong>${value}</strong></div>`;
}


function instanceRow(item: AirPlay2State["instances"][number], controls = true, globalEnabled = true): string {
  const running = item.status === "running";
  let actions = "";
  if (!controls) {
    // Single-entry installs (fnOS) have no per-instance switch: enable/disable
    // goes through the global settings toggle. Show the state, never a 409.
    actions = `<span class="plain-state ${item.enabled ? "success" : ""}">${item.enabled ? "已开启" : "已关闭"}${item.enabled && !globalEnabled ? " · 请在设置中开启" : ""}</span>`;
  } else {
    actions = `<div class="airplay2-node-actions"><input type="checkbox" class="switch" data-instance-enabled="${item.id}" ${item.enabled ? "checked" : ""} aria-label="${escapeHtml(item.name)}启用状态"><button class="button danger plain" data-instance-delete="${item.id}">删除</button></div>`;
  }
  return `<div class="cell"><div class="cell-icon ${running ? "green" : "gray"}">${icon("airplay")}</div><div class="cell-content"><span class="cell-title">${escapeHtml(item.name)}</span><span class="cell-subtitle">播放到 ${escapeHtml(item.target_name)}</span><span class="cell-subtitle">${escapeHtml(item.detail)}</span></div>${actions}</div>`;
}

function prerequisiteEmpty(title: string, subtitle: string, action: string, tab: AirPlay2Tab): string {
  return `<div class="group"><div class="empty-state"><div class="empty-state-icon">${icon("link")}</div><span class="body">${title}</span><span class="caption">${subtitle}</span><button class="button secondary" data-airplay2-open-tab="${tab}">${action}</button></div></div>`;
}

function emptyRow(title: string, subtitle: string): string { return `<div class="cell"><div class="cell-content"><span class="cell-title">${title}</span><span class="cell-subtitle">${subtitle}</span></div></div>`; }
function escapeHtml(text: string): string { return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;"); }

export function bindAirPlay2View(container: HTMLElement, handlers: { onBack: () => void; onTab: (tab: AirPlay2Tab) => void; onRefresh: () => Promise<void> }) {
  container.querySelector("[data-airplay2-back]")?.addEventListener("click", handlers.onBack);
  container.querySelectorAll<HTMLElement>("[data-airplay2-tab]").forEach((el) => el.addEventListener("click", () => handlers.onTab(el.dataset.airplay2Tab as AirPlay2Tab)));
  container.querySelectorAll<HTMLElement>("[data-airplay2-open-tab]").forEach((button) => button.addEventListener("click", () => handlers.onTab(button.dataset.airplay2OpenTab as AirPlay2Tab)));
  container.querySelectorAll<HTMLInputElement>("[data-instance-enabled]").forEach((input) => input.addEventListener("change", async () => {
    try { await api.setAirPlay2InstanceEnabled(input.dataset.instanceEnabled || "", input.checked); await handlers.onRefresh(); store.showToast(`播放入口已${input.checked ? "启用" : "停用"}`); }
    catch (error) { input.checked = !input.checked; store.showToast(`操作失败: ${error instanceof Error ? error.message : "未知错误"}`); }
  }));
  container.querySelectorAll<HTMLButtonElement>("[data-instance-delete]").forEach((button) => button.addEventListener("click", async () => {
    if (!await confirmAction("删除播放入口？", "MiCast 将删除入口和对应的播放映射。服务暂时离线时，将在恢复后继续清理。", "删除入口")) return;
    try { const result = await api.deleteAirPlay2Instance(button.dataset.instanceDelete || ""); await handlers.onRefresh(); store.showToast(result.warning || "播放入口已删除"); }
    catch (error) { store.showToast(`删除失败: ${error instanceof Error ? error.message : "未知错误"}`); }
  }));
  container.querySelector<HTMLFormElement>("[data-airplay2-instance-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const data = new FormData(form);
    const button = form.querySelector<HTMLButtonElement>('button[type="submit"]');
    const buttonLabel = button?.textContent || "创建播放入口";
    const [target_type, target_id] = String(data.get("target") || ":").match(/^([^:]+):(.*)$/)!.slice(1) as ["speaker" | "group" | "dlna", string];
    if (button) { button.disabled = true; button.textContent = "正在创建…"; }
    try {
      await api.saveAirPlay2Instance({ name: String(data.get("name") || ""), target_type, target_id });
      form.reset(); await handlers.onRefresh(); store.showToast("播放入口已创建，正在启动");
      [2000, 6000, 12000].forEach((delay) => window.setTimeout(() => void handlers.onRefresh(), delay));
    } catch (error) {
      store.showToast(`创建失败: ${error instanceof Error ? error.message : "未知错误"}`);
      if (button) { button.disabled = false; button.textContent = buttonLabel; }
    }
  });
  container.querySelectorAll<HTMLFormElement>("[data-instance-mapping]").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const [target_type, target_id] = String(data.get("target") || ":").match(/^([^:]+):(.*)$/)!.slice(1) as ["speaker" | "group" | "dlna", string];
    try {
      await api.saveAirPlay2Instance({ id: form.dataset.instanceMapping, name: form.dataset.instanceName || "AirPlay 2", target_type, target_id, enabled: form.dataset.mappingEnabled === "true" });
      await handlers.onRefresh(); store.showToast("播放映射已保存");
    } catch (error) { store.showToast(`保存失败: ${error instanceof Error ? error.message : "未知错误"}`); }
  }));
}

function confirmAction(title: string, message: string, action: string): Promise<boolean> {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog";
    dialog.innerHTML = `<form method="dialog">
      <div class="confirm-dialog-copy"><h3>${escapeHtml(title)}</h3><p>${escapeHtml(message)}</p></div>
      <div class="confirm-dialog-actions">
        <button class="button plain" value="cancel">取消</button>
        <button class="button danger" value="confirm">${escapeHtml(action)}</button>
      </div>
    </form>`;
    document.body.appendChild(dialog);
    dialog.addEventListener("close", () => {
      const confirmed = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(confirmed);
    }, { once: true });
    dialog.addEventListener("cancel", () => dialog.close("cancel"));
    dialog.showModal();
    dialog.querySelector<HTMLButtonElement>('[value="cancel"]')?.focus();
  });
}
