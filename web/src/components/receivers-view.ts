import type { State } from "../state";
import { icon, brandIcon } from "../icons";
import { api } from "../api";
import { store } from "../state";
import {
  compactTargetLabel,
  escapeHtml,
  groupActionKey,
  groupMemberCount,
  message,
  pendingGroupActions,
  rebaseGroup,
  replaceGroup,
  setLargeDelayEnabled,
  settingsTargetIds,
  speakerName,
  targetLabel,
} from "./receivers-shared";
import { bindNetworkSections, getCreateFormNetworks, renderNetworkTargets } from "./network-targets";
import { bindGroupToggles, isGroupExpanded, renderGroupCodecChips, speakerAreaInner } from "./groups";
import { confirmCalibration, liveCalibration } from "./calibration";
import { activeSessions } from '../selectors';

// Create-form state lives outside the DOM: a status-poll re-render rebuilds
// the whole section and would otherwise wipe half-filled forms (the "checkbox
// unchecks itself" symptom).
const createFormSpeakers = new Set<string>();
let createFormName = "";

function resetCreateFormState() {
  createFormSpeakers.clear();
  getCreateFormNetworks().clear();
  createFormName = "";
}

export function renderReceiversView(state: State): string {
  setLargeDelayEnabled(Boolean(state.fullConfig?.large_delay_enabled));
  const { receivers, fullConfig, status } = state;
  const dlnaEnabled = fullConfig?.dlna_enabled ?? false;

  const content =
    receivers.length > 0
      ? `<div class="receiver-grid">${receivers
          .map((r) => {
            const sessions = activeSessions(state).filter(session => session.owner === r.did || session.owner === `dlna:${r.did}`);
            const casting = sessions.some(session => session.state === 'active');
            const paused = sessions.some(session => session.state === 'paused');
            const detail = compactTargetLabel(r.name, r.did, state);
            const definition = fullConfig?.receivers.find((item) => item.id === r.did);
            const mapped = Boolean(definition && settingsTargetIds(definition, state).length);
            const available = r.status === "running" && mapped;
            return `
              <div class="receiver-card">
                <div class="receiver-heading">
                  <div>
                    <div class="device-name">${escapeHtml(r.name)}</div>
                    <div class="receiver-protocols">
                      <span class="protocol-badge protocol-airplay">经典 AirPlay</span>
                      ${dlnaEnabled ? `<span class="protocol-badge protocol-dlna">DLNA</span>` : ""}
                    </div>
                    ${detail ? `<span class="caption">${escapeHtml(detail)}</span>` : ""}
                  </div>
                <span class="status-pill ${available ? "running" : r.status === "error" ? "error" : ""}">
                  ${!mapped ? "未设置" : casting ? "投送中" : paused ? "已暂停" : available ? "可连接" : r.status === "error" ? "不可用" : "准备中"}
                </span>
                </div>
              </div>
            `;
          })
          .join("")}</div>`
      : `
          <div class="group">
            <div class="empty-state">
              <div class="empty-state-icon">${icon("antenna")}</div>
              <span class="body">尚未添加播放入口</span>
              <span class="caption" style="max-width: 260px;">
                添加后，它会出现在手机的 AirPlay${dlnaEnabled ? " 和 DLNA" : ""} 列表中。
              </span>
            </div>
          </div>
        `;

  return `
    <div class="page-heading">
      <h2 class="page-title">播放</h2>
      <p>管理手机中看到的播放名称，以及每个名称实际播放到哪里。</p>
    </div>
    <div class="group-header">经典 AirPlay${dlnaEnabled ? " / DLNA" : ""} · ${receivers.length} 个入口</div>
    ${content}
    ${state.airplay2?.enabled ? renderAirPlay2Entries(state) : ""}
    ${renderManagement(state)}
  `;
}

function renderAirPlay2Entries(state: State): string {
  const entries = state.airplay2?.instances ?? [];
  if (!entries.length) return "";
  return `
    <div class="group-header">AirPlay 2 · ${entries.length} 个入口 <span class="feature-badge">实验性</span></div>
    <div class="receiver-grid">${entries.map((item) => {
      const available = item.enabled && item.status === "running";
      return `<div class="receiver-card">
        <div class="receiver-heading">
          <div class="receiver-entry-summary">
            <div class="receiver-entry-identity"><span class="device-name">${escapeHtml(item.name)}</span>
            <span class="protocol-badge protocol-airplay2">AirPlay 2</span>
            </div><span class="caption">播放到 ${escapeHtml(item.target_name)}</span>
          </div>
          <span class="status-pill ${available ? "running" : item.status === "error" ? "error" : ""}">${available ? "可连接" : item.status === "error" ? "不可用" : "准备中"}</span>
        </div>
      </div>`;
    }).join("")}</div>`;
}

function renderManagement(state: State): string {
  const config = state.fullConfig;
  if (!config) return "";
  const publishedReceivers = config.receivers.filter(
    (item) => config.sync_groups_enabled || item.target_type !== "group"
  );
  const usedTargets = new Set(
    config.receivers
      .filter((item) => item.target_type !== "selected" && item.target_id)
      .map((item) => `${item.target_type}:${item.target_id}`)
  );
  const targetOptions = [
    `<option value="" disabled selected>选择音箱或组合</option>`,
    ...state.devices.filter((item) => !usedTargets.has(`speaker:${item.did}`)).map((item) => `<option value="speaker:${escapeHtml(item.did)}">音箱 · ${escapeHtml(item.alias || item.name)}</option>`),
    ...(config.sync_groups_enabled
      ? config.groups.filter((item) => !usedTargets.has(`group:${item.id}`)).map((item) => `<option value="group:${escapeHtml(item.id)}">音箱组合 · ${escapeHtml(item.name)}</option>`)
      : []),
  ].join("");
  const hasAvailableTarget = targetOptions.includes('value="speaker:') || targetOptions.includes('value="group:');
  // fnOS ships one native AirPlay 2 receiver. Its only meaningful setting is
  // the playback target; creating more advertised receivers is unsupported.
  return `
    <details class="receiver-management" ${state.receivers.length ? '' : 'open'}>
    <summary><span>管理播放入口与组合</span><span class="caption">显示名称、播放目标与多音箱设置</span></summary>
    <div class="receiver-management-body">
    <div class="group-header">显示哪些播放入口</div>
    <div class="group">
      ${hasAvailableTarget ? `<form class="cell receiver-form" data-create-receiver>
        <div class="cell-content"><span class="cell-title">添加经典播放入口</span><span class="cell-subtitle">添加后会显示在手机的 AirPlay${config.dlna_enabled ? " 和 DLNA" : ""} 列表中</span></div>
        <select class="input" name="target" required>${targetOptions}</select>
        <button class="button primary" type="submit">添加</button>
      </form>` : `<div class="cell"><div class="cell-content"><span class="cell-title">所有音箱均已添加</span><span class="cell-subtitle">发现新音箱后可继续添加</span></div></div>`}
      ${publishedReceivers.map((item) => {
        // The toggle advertises the entry over AirPlay (and DLNA when on) —
        // name the actual places it appears, not "the phone".
        const visibleWhere = config.dlna_enabled ? "AirPlay / DLNA" : "AirPlay";
        return `<div class="cell receiver-entry-row" data-receiver-row="${escapeHtml(item.id)}">
        <div class="cell-content"><span class="cell-title">${escapeHtml(item.name)}</span><span class="cell-subtitle">${escapeHtml(targetLabel(item.id, state))}</span></div>
        <label class="receiver-visibility">
          <span>${item.enabled ? `显示在 ${visibleWhere}` : "已隐藏"}</span>
          <input class="switch" type="checkbox" ${item.enabled ? "checked" : ""} ${state.saving ? "disabled" : ""} data-receiver-enabled="${escapeHtml(item.id)}" aria-label="在播放列表中显示 ${escapeHtml(item.name)}">
        </label>
      </div>`;
      }).join("")}
    </div>

    <div class="group-header">音箱组合 <span class="feature-badge">实验性</span></div>
    <p class="group-header-hint">以时间基准为参照：比基准先响（快）向 + 调，比基准后响（慢）向 − 调。</p>
    <div class="group">
      ${!config.sync_groups_enabled ? `<div class="cell"><div class="cell-content"><span class="cell-title">音箱组合已关闭</span><span class="cell-subtitle">已有组合会保留，可在设置中重新开启</span></div></div>` : config.groups.map((group) => {
        const isStereo = group.mode === "stereo";
        const expanded = isGroupExpanded(group, state);
        const netCount = (group.airplay_targets?.length ?? 0) + (group.dlna_targets?.length ?? 0);
        // The mode is already the segmented control's job — repeating it in the
        // subtitle made the header say "立体声" twice.
        const summary = [
          group.speaker_ids.length ? `${group.speaker_ids.length} 台音箱` : "",
          netCount ? `${netCount} 台网络设备` : "",
        ].filter(Boolean).join(" + ") || "尚无成员";
        return `<div class="sync-group">
        <div class="cell sync-group-header">
          <button class="group-toggle group-identity ${expanded ? "expanded" : ""}" type="button" data-group-toggle="${escapeHtml(group.id)}" aria-expanded="${expanded}" aria-label="展开或收起组合 ${escapeHtml(group.name)}">
            <span class="group-identity-copy"><strong>${escapeHtml(group.name)}</strong><small>${escapeHtml(summary)}</small></span>${icon("chevron")}
          </button>
          <div class="group-formats">${renderGroupCodecChips(group, state)}</div>
          <div class="segmented-control ${pendingGroupActions.has(groupActionKey("mode", group.id)) ? "control-pending" : ""}" role="group" aria-label="播放模式" aria-busy="${pendingGroupActions.has(groupActionKey("mode", group.id))}">
            <button class="segment ${!isStereo ? "active" : ""}" data-group-mode="${escapeHtml(group.id)}" data-mode="mirror" ${pendingGroupActions.has(groupActionKey("mode", group.id)) ? "disabled" : ""}>同声播放</button>
            <button class="segment ${isStereo ? "active" : ""}" data-group-mode="${escapeHtml(group.id)}" data-mode="stereo" ${pendingGroupActions.has(groupActionKey("mode", group.id)) ? "disabled" : ""}>立体声</button>
          </div>
          <div class="group-header-actions">
            <button class="button plain" type="button" data-group-calibrate="${escapeHtml(group.id)}">辅助校准</button>
            <button class="button plain danger-text group-delete" data-delete-group="${escapeHtml(group.id)}">删除组合</button>
          </div>
        </div>
        <div class="sync-group-body" data-group-body="${escapeHtml(group.id)}" ${expanded ? "" : "hidden"}>
          ${speakerAreaInner(group, state)}
          ${renderNetworkTargets(group)}
        </div>
      </div>`;
      }).join("")}
      ${config.sync_groups_enabled ? `<form class="group-form" data-create-group>
        <div class="group-form-heading"><span class="cell-title">新建音箱组合</span><span class="cell-subtitle">选择至少两个播放设备</span></div>
        <input class="input group-form-name" name="name" maxlength="50" required placeholder="例如：全屋播放" aria-label="组合名称" value="${escapeHtml(createFormName)}">
        <fieldset class="speaker-checks"><legend>组合中的音箱</legend>${state.devices.map((item) => `<label><input type="checkbox" name="speaker" value="${escapeHtml(item.did)}" ${createFormSpeakers.has(item.did) ? "checked" : ""}> <span class="speaker-row-icon">${brandIcon("xiaomi")}</span> <span>${escapeHtml(item.alias || item.name)}</span></label>`).join("") || `<span class="caption">暂无可选音箱</span>`}</fieldset>
        <div class="codec-compatibility-hint" data-create-codec-hint>选择音箱后会检查共同兼容格式</div>
        <div data-create-network-picker></div>
        <div class="group-form-actions"><button class="button primary" type="submit">创建组合</button></div>
      </form>` : ""}
    </div></div></details>`;
}

export function bindReceiversView(container: HTMLElement, rerender: () => void) {  bindNetworkSections(container);
  container.querySelector<HTMLFormElement>("[data-create-receiver]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (store.get().saving) {
      store.showToast("上一个操作还在进行，请稍候再试");
      return;
    }
    const form = event.currentTarget as HTMLFormElement;
    const data = new FormData(form);
    const [target_type, target_id] = String(data.get("target") || "").split(":", 2);
    const state = store.get();
    const config = state.fullConfig;
    if (!config) return;
    const fallbackName = target_type === "speaker"
      ? state.devices.find((item) => item.did === target_id)?.alias || state.devices.find((item) => item.did === target_id)?.name
      : config.groups.find((item) => item.id === target_id)?.name;
    const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]');
    if (submit) { submit.disabled = true; submit.textContent = "正在添加…"; }
    store.set({ saving: true });
    try {
      await api.createReceiver({ name: fallbackName || "AirPlay", target_type: target_type as "selected" | "speaker" | "group", target_id: target_id || null });
      await refreshRuntimeState(); store.set({ saving: false }); rerender(); store.showToast("已添加到 AirPlay");
    } catch (e) {
      store.set({ saving: false });
      if (submit) { submit.disabled = false; submit.textContent = "添加"; }
      store.showToast(`创建失败: ${message(e)}`);
    }
  });

  container.querySelectorAll<HTMLInputElement>("[data-receiver-enabled]").forEach((input) => input.addEventListener("change", async () => {
    if (store.get().saving) return;
    const receiverId = input.dataset.receiverEnabled!;
    const enabled = input.checked;
    const previousReceivers = store.get().receivers;
    const previousConfig = store.get().fullConfig;
    const definition = previousConfig?.receivers.find((item) => item.id === receiverId);
    store.set({
      saving: true,
      fullConfig: previousConfig ? {
        ...previousConfig,
        receivers: previousConfig.receivers.map((item) => item.id === receiverId ? { ...item, enabled } : item),
      } : previousConfig,
      receivers: enabled
        ? previousReceivers.some((item) => item.did === receiverId)
          ? previousReceivers
          : [...previousReceivers, {
              did: receiverId,
              name: definition?.name || "AirPlay",
              status: "idle",
              stream_url: "",
              detail: "正在准备播放入口",
            }]
        : previousReceivers.filter((item) => item.did !== receiverId),
    });
    rerender();
    try {
      await api.updateReceiver(receiverId, { enabled });
      await refreshRuntimeState();
      store.set({ saving: false });
      rerender();
      store.showToast(enabled ? "播放入口已显示" : "播放入口已隐藏");
    } catch (e) {
      store.set({ fullConfig: previousConfig, receivers: previousReceivers, saving: false });
      input.checked = !enabled;
      rerender();
      store.showToast(`设置失败: ${message(e)}`);
    }
  }));

  container.querySelector<HTMLFormElement>("[data-create-group]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (store.get().saving) {
      // Never a silent no-op: "clicked and nothing happened" reads as a broken
      // button, which is how this guard was first reported.
      store.showToast("上一个操作还在进行，请稍候再试");
      return;
    }
    const form = event.currentTarget as HTMLFormElement;
    const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]');
    if (submit) { submit.disabled = true; submit.textContent = "正在创建…"; }
    store.set({ saving: true });
    try {
      // Read from the persistent sets, not the DOM: a poll re-render may have
      // rebuilt the form between the user's click and this submit.
      const createFormNetworks = getCreateFormNetworks();
      const created = await api.createGroup(
        createFormName.trim(),
        [...createFormSpeakers],
        [...createFormNetworks].filter((k) => k.startsWith("airplay:")).map((k) => k.slice(8)),
        [...createFormNetworks].filter((k) => k.startsWith("dlna:")).map((k) => k.slice(5))
      );
      resetCreateFormState();
      await refreshRuntimeState(); store.set({ saving: false }); rerender();
      const compatibility = created.codec_compatibility;
      const suffix = compatibility?.status === "confirmed"
        ? `，共同格式：${compatibility.confirmed_common_formats.join("、")}`
        : compatibility?.status === "incompatible"
          ? "，暂未找到共同格式，请先检测音箱"
          : "，建议先用 MP3，播放后自动确认";
      store.showToast(`音箱组合已创建${suffix}`);
    } catch (e) {
      store.set({ saving: false });
      if (submit) { submit.disabled = false; submit.textContent = "创建组合"; }
      store.showToast(`创建失败: ${message(e)}`);
    }
  });

  const createForm = container.querySelector<HTMLElement>("[data-create-group]");
  createForm?.querySelectorAll<HTMLInputElement>('input[name="speaker"]').forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) createFormSpeakers.add(checkbox.value);
      else createFormSpeakers.delete(checkbox.value);
      const hint = createForm?.querySelector<HTMLElement>("[data-create-codec-hint]");
      if (hint) hint.textContent = createFormSpeakers.size < 2 ? "选择至少两台音箱" : "建议先用 MP3；播放后会自动确认共同格式。";
    });
  });
  createForm?.querySelector<HTMLInputElement>('input[name="name"]')?.addEventListener("input", (event) => {
    createFormName = (event.target as HTMLInputElement).value;
  });

  container.querySelectorAll<HTMLElement>("[data-delete-group]").forEach((button) => button.addEventListener("click", async () => {
    const groupId = button.dataset.deleteGroup!;
    const previous = store.get();
    const config = previous.fullConfig;
    if (!config || previous.saving) return;
    const receiverIds = new Set(
      config.receivers
        .filter((item) => item.target_type === "group" && item.target_id === groupId)
        .map((item) => item.id)
    );
    store.set({
      saving: true,
      fullConfig: {
        ...config,
        groups: config.groups.filter((item) => item.id !== groupId),
        receivers: config.receivers.filter((item) => !receiverIds.has(item.id)),
      },
      receivers: previous.receivers.filter((item) => !receiverIds.has(item.did)),
    });
    rerender();
    try {
      await api.deleteGroup(groupId);
      await refreshRuntimeState();
      store.set({ saving: false });
      rerender();
      store.showToast("音箱组合已删除");
    } catch (e) {
      store.set({
        saving: false,
        fullConfig: previous.fullConfig,
        status: previous.status,
        receivers: previous.receivers,
      });
      rerender();
      store.showToast(`删除失败，已恢复: ${message(e)}`);
    }
  }));

  bindGroupToggles(container);

  const saveSpeakerMembership = async (groupId: string, speakerIds: string[], toastText: string) => {
    store.set({ saving: true });
    try {
      await api.updateGroup(groupId, { speaker_ids: speakerIds });
      await refreshRuntimeState();
      store.set({ saving: false });
      rerender();
      store.showToast(toastText);
    } catch (e) {
      store.set({ saving: false });
      rerender();
      store.showToast(`保存失败: ${message(e)}`);
    }
  };

  container.querySelectorAll<HTMLInputElement>("[data-speaker-add]").forEach((checkbox) => {
    checkbox.addEventListener("change", async () => {
      if (store.get().saving) return;
      const groupId = checkbox.dataset.speakerAdd!;
      const group = store.get().fullConfig?.groups.find((item) => item.id === groupId);
      if (!group) return;
      const name = checkbox.closest("label")?.querySelector("span")?.textContent ?? "";
      await saveSpeakerMembership(groupId, [...group.speaker_ids, checkbox.value], `已把「${name}」加入组合「${group.name}」`);
    });
  });

  container.querySelectorAll<HTMLElement>("[data-speaker-remove]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (store.get().saving) return;
      const groupId = button.dataset.groupId!;
      const did = button.dataset.speakerRemove!;
      const group = store.get().fullConfig?.groups.find((item) => item.id === groupId);
      if (!group) return;
      await saveSpeakerMembership(
        groupId,
        group.speaker_ids.filter((item) => item !== did),
        `已把「${speakerName(did, store.get())}」从组合「${group.name}」移除`
      );
    });
  });

  container.querySelectorAll<HTMLInputElement>("[data-group-delay]").forEach((input) => {
    input.addEventListener("input", () => {
      const output = input.nextElementSibling as HTMLOutputElement | null;
      if (output) output.value = `${input.value} ms`;
    });
    // Commit on release; mid-drag saves fight the user's finger.
    input.addEventListener("change", async () => {
      const groupId = input.dataset.groupDelay!;
      const speakerId = input.dataset.speakerId!;
      const config = store.get().fullConfig;
      const group = config?.groups.find((item) => item.id === groupId);
      if (!config || !group) return;
      const delays = { ...group.delays_ms, [speakerId]: Number(input.value) };
      const optimistic = { ...group, delays_ms: delays };
      store.set({ fullConfig: replaceGroup(config, optimistic) });
      try {
        const updated = await api.updateGroup(groupId, { delays_ms: delays });
        const latest = store.get().fullConfig;
        if (latest) store.set({ fullConfig: replaceGroup(latest, updated) });
      } catch (e) {
        store.set({ fullConfig: config });
        rerender();
        store.showToast(`延迟保存失败，已恢复: ${message(e)}`);
      }
    });
  });

  container.querySelectorAll<HTMLButtonElement>("[data-group-calibrate]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (store.get().saving) return;
      const groupId = button.dataset.groupCalibrate!;
      const group = store.get().fullConfig?.groups.find((item) => item.id === groupId);
      if (!group) return;
      const calibrationMedia = await confirmCalibration(group.name);
      if (calibrationMedia === null) return;
      button.disabled = true;
      button.textContent = "正在校准…";
      store.set({ saving: true });
      try {
        await api.startGroupCalibration(groupId, calibrationMedia || undefined);
        store.set({ saving: false });
        rerender();
        const saved = await liveCalibration(group, store.get());
        await api.stopGroupCalibration(groupId);
        rerender();
        store.showToast(saved ? "校准已保存" : "已还原校准前的延迟");
      } catch (e) {
        await api.stopGroupCalibration(groupId).catch(() => undefined);
        store.set({ saving: false });
        rerender();
        store.showToast(`校准失败: ${message(e)}`);
      }
    });
  });

  container.querySelectorAll<HTMLElement>("[data-group-anchor]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (store.get().saving) return;
      const groupId = button.dataset.groupAnchor!;
      const speakerId = button.dataset.speakerId!;
      const config = store.get().fullConfig;
      const group = config?.groups.find((item) => item.id === groupId);
      if (!config || !group || group.anchor_did === speakerId) return;
      const actionKey = groupActionKey("anchor", groupId);
      if (pendingGroupActions.has(actionKey)) return;
      pendingGroupActions.add(actionKey);
      const optimistic = rebaseGroup(group, speakerId);
      store.set({ fullConfig: replaceGroup(config, optimistic) });
      rerender();
      try {
        const updated = await api.updateGroup(groupId, { anchor_did: speakerId });
        const latest = store.get().fullConfig;
        if (latest) store.set({ fullConfig: replaceGroup(latest, updated) });
        rerender();
        store.showToast("时间基准已切换，当前播放时序保持不变");
      } catch (e) {
        store.set({ fullConfig: config });
        store.showToast(`设置失败，已恢复: ${message(e)}`);
      } finally {
        pendingGroupActions.delete(actionKey);
        rerender();
      }
    });
  });

  // Stereo mode: switching mode or channel rebuilds the pipelines server-side.
  // Apply the change optimistically so the UI flips instantly, then persist.
  container.querySelectorAll<HTMLElement>("[data-group-mode]").forEach((button) => {
    button.addEventListener("click", async () => {
      const groupId = button.dataset.groupMode!;
      const mode = button.dataset.mode as "mirror" | "stereo";
      const config = store.get().fullConfig;
      const group = config?.groups.find((item) => item.id === groupId);
      if (!config || !group || group.mode === mode) return;
      const actionKey = groupActionKey("mode", groupId);
      if (pendingGroupActions.has(actionKey)) return;
      if (mode === "stereo" && groupMemberCount(group) < 2) {
        store.showToast("立体声模式至少需要两个成员（音箱或网络设备）");
        return;
      }
      pendingGroupActions.add(actionKey);
      const optimistic = mode === "stereo"
        ? Object.fromEntries(group.speaker_ids.map((did, i) => [did, (group.channels?.[did] ?? (i === 0 ? "left" : "right"))]))
        : group.channels;
      store.set({
        fullConfig: {
          ...config,
          groups: config.groups.map((item) =>
            item.id === groupId ? { ...item, mode, channels: optimistic } : item
          ),
        },
      });
      rerender();
      try {
        const updated = await api.updateGroup(groupId, { mode });
        const latest = store.get().fullConfig;
        if (latest) {
          store.set({ fullConfig: { ...latest, groups: latest.groups.map((item) => item.id === groupId ? updated : item) } });
        }
        const status = await api.getStatus();
        store.set({ status, receivers: status.receivers });
        rerender();
        store.showToast(mode === "stereo" ? "已切换为立体声，左右声道各走一条流" : "已切换为镜像播放");
      } catch (e) {
        store.set({ fullConfig: config });
        store.showToast(`切换失败: ${message(e)}`);
      } finally {
        pendingGroupActions.delete(actionKey);
        rerender();
      }
    });
  });

  container.querySelectorAll<HTMLElement>("[data-group-channel]").forEach((button) => {
    button.addEventListener("click", async () => {
      const groupId = button.dataset.groupChannel!;
      const speakerId = button.dataset.speakerId!;
      const channel = button.dataset.channel as "left" | "right" | "both";
      const config = store.get().fullConfig;
      const group = config?.groups.find((item) => item.id === groupId);
      if (!config || !group || group.channels?.[speakerId] === channel) return;
      const actionKey = groupActionKey("channel", groupId, speakerId);
      if (pendingGroupActions.has(actionKey)) return;
      pendingGroupActions.add(actionKey);
      // Channel choice is free per speaker — several may share a side.
      const channels: Record<string, "left" | "right" | "both"> = { ...group.channels, [speakerId]: channel };
      store.set({
        fullConfig: {
          ...config,
          groups: config.groups.map((item) => (item.id === groupId ? { ...item, channels } : item)),
        },
      });
      rerender();
      try {
        const updated = await api.updateGroup(groupId, { channels });
        const latest = store.get().fullConfig;
        if (latest) {
          store.set({ fullConfig: { ...latest, groups: latest.groups.map((item) => item.id === groupId ? updated : item) } });
        }
        const status = await api.getStatus();
        store.set({ status, receivers: status.receivers });
        rerender();
      } catch (e) {
        store.set({ fullConfig: config });
        store.showToast(`声道设置失败: ${message(e)}`);
      } finally {
        pendingGroupActions.delete(actionKey);
        rerender();
      }
    });
  });

  // Loudness trims restart only the encoder server-side (the AirPlay session
  // survives), so they can be tuned while listening. Visuals update on input;
  // the value is committed on release (change) — no mid-drag saves. Signed
  // delay offsets live in the separate [data-group-delay] handler above.
  const bindTrim = (selector: string, format: (value: number) => string) => {
    container.querySelectorAll<HTMLInputElement>(selector).forEach((input) => {
      input.addEventListener("input", () => {
        const output = input.nextElementSibling as HTMLOutputElement | null;
        if (output) output.value = format(Number(input.value));
      });
      input.addEventListener("change", async () => {
        const groupId = input.dataset.groupGain!;
        const speakerId = input.dataset.speakerId!;
        const value = Number(input.value);
        const config = store.get().fullConfig;
        const group = config?.groups.find((item) => item.id === groupId);
        if (!config || !group) return;
        const gains = { ...group.gains_db, [speakerId]: value };
        store.set({ fullConfig: replaceGroup(config, { ...group, gains_db: gains }) });
        try {
          const updated = await api.updateGroup(groupId, { gains_db: gains });
          const latest = store.get().fullConfig;
          if (latest) store.set({ fullConfig: replaceGroup(latest, updated) });
        } catch (e) {
          store.set({ fullConfig: config });
          rerender();
          store.showToast(`响度保存失败，已恢复: ${message(e)}`);
        }
      });
    });
  };
  bindTrim("[data-group-gain]", (v) => `${v > 0 ? "+" : ""}${v.toFixed(1)} dB`);
}

async function refreshRuntimeState() {
  const [fullConfig, status] = await Promise.all([api.getConfig(), api.getStatus()]);
  store.set({ fullConfig, status, receivers: status.receivers });
}
