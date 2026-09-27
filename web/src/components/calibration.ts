import type { State } from "../state";
import type { SpeakerGroup } from "../api";
import { api } from "../api";
import { store } from "../state";
import { getTestMedia } from "../test-media";
import {
  delayLimit,
  delayStateLabel,
  escapeHtml,
  memberDelayState,
  message,
  replaceGroup,
  speakerName,
} from "./receivers-shared";

export function confirmCalibration(groupName: string): Promise<string | null> {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog";
    const media = getTestMedia();
    dialog.innerHTML = `<form method="dialog">
      <div class="confirm-dialog-copy"><h3>校准「${escapeHtml(groupName)}」？</h3><p>将循环播放测试音，退出后恢复原内容。</p></div>
      ${media ? `<label class="test-url-field"><span>校准音频</span><select class="input" name="calibration-media"><option value="">内置节拍（推荐）</option><option value="${escapeHtml(media.token)}">${escapeHtml(media.name)}</option></select></label>` : ""}
      <div class="confirm-dialog-actions"><button class="button plain" value="cancel">取消</button><button class="button primary" value="confirm">进入校准</button></div>
    </form>`;
    dialog.addEventListener("close", () => {
      const selected = dialog.querySelector<HTMLSelectElement>('[name="calibration-media"]')?.value ?? "";
      resolve(dialog.returnValue === "confirm" ? selected : null);
      dialog.remove();
    }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
  });
}

export function liveCalibration(group: SpeakerGroup, state: State): Promise<boolean> {
  return new Promise((resolve) => {
    const original = { ...group.delays_ms };
    const delays = { ...original };
    const members = group.speaker_ids.filter((did) => did !== group.anchor_did);
    const limit = delayLimit();
    const dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog calibration-dialog";
    dialog.innerHTML = `<form>
      <div class="confirm-dialog-copy"><h3>调整播放时间</h3><p>以「${escapeHtml(speakerName(group.anchor_did!, state))}」为基准，让其他音箱的节拍重合。</p></div>
      <div class="calibration-guide-list">${members.map((did) => {
        const value = delays[did] ?? 0;
        return `<fieldset class="calibration-guide-row" data-live-calibration="${escapeHtml(did)}"><legend>${escapeHtml(speakerName(did, state))}</legend>
          <div class="live-calibration-control">
            <button class="button plain" type="button" data-nudge="-100">−100</button><button class="button plain" type="button" data-nudge="-10">−10</button>
            <input type="range" min="-${limit}" max="${limit}" step="10" value="${value}" aria-label="播放时间调整">
            <button class="button plain" type="button" data-nudge="10">+10</button><button class="button plain" type="button" data-nudge="100">+100</button>
            <output>${value} ms</output>
          </div>
          <span class="live-calibration-state" data-delay-state="${escapeHtml(did)}"></span>
        </fieldset>`;
      }).join("")}</div>
      <p class="caption calibration-hint">先响（快）向 + 调，后响（慢）向 − 调。</p>
      <div class="confirm-dialog-actions"><button class="button plain" type="button" data-live-cancel>取消并还原</button><button class="button primary" type="submit">完成并保存</button></div>
    </form>`;
    let saved = false;
    const apply = async (did: string, value: number) => {
      if (value) delays[did] = value; else delete delays[did];
      const updated = await api.updateGroup(group.id, { delays_ms: delays });
      const config = store.get().fullConfig;
      if (config) store.set({ fullConfig: replaceGroup(config, updated) });
    };
    dialog.querySelectorAll<HTMLElement>("[data-live-calibration]").forEach((row) => {
      const did = row.dataset.liveCalibration!;
      const slider = row.querySelector<HTMLInputElement>('input[type="range"]')!;
      const output = row.querySelector<HTMLOutputElement>("output")!;
      const set = (value: number) => { slider.value = String(Math.max(-limit, Math.min(limit, value))); output.value = `${slider.value} ms`; };
      slider.addEventListener("input", () => set(Number(slider.value)));
      slider.addEventListener("change", () => apply(did, Number(slider.value)).catch((e) => store.showToast(`调整失败: ${message(e)}`)));
      row.querySelectorAll<HTMLButtonElement>("[data-nudge]").forEach((button) => button.addEventListener("click", () => {
        set(Number(slider.value) + Number(button.dataset.nudge));
        apply(did, Number(slider.value)).catch((e) => store.showToast(`调整失败: ${message(e)}`));
      }));
    });
    dialog.querySelector("form")?.addEventListener("submit", (event) => { event.preventDefault(); saved = true; dialog.close(); });
    dialog.querySelector("[data-live-cancel]")?.addEventListener("click", () => dialog.close());
    // The slider shows what was requested; this shows whether the delay line has
    // actually taken it. A raise only takes effect once the reserve has filled
    // (the speaker is fed silence until then), so without this the only way to
    // know was to keep listening.
    const unsubscribe = store.subscribe((current) => {
      const rows = dialog.querySelectorAll<HTMLElement>("[data-delay-state]");
      if (!rows.length) return;
      const group_now = current.fullConfig?.groups.find((item) => item.id === group.id) ?? group;
      rows.forEach((node) => {
        const value = memberDelayState(group_now, node.dataset.delayState!, current);
        node.textContent = delayStateLabel(value);
        // "实时" is the live edge: nothing to wait for.
        const holding = Boolean(value && !value.live);
        node.classList.toggle("ready", holding && Boolean(value!.ready));
        node.classList.toggle("pending", holding && !value!.ready);
      });
    });
    dialog.addEventListener("close", async () => {
      unsubscribe();
      if (!saved) {
        try { await api.updateGroup(group.id, { delays_ms: original }); }
        catch (e) { store.showToast(`还原延迟失败: ${message(e)}`); }
      }
      resolve(saved);
      dialog.remove();
    }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
  });
}

export function guideCalibration(
  group: SpeakerGroup,
  state: State,
  requestOffsets: Record<string, number>
): Promise<Record<string, number> | null> {
  return new Promise((resolve) => {
    const anchor = group.anchor_did!;
    const anchorName = speakerName(anchor, state);
    const members = group.speaker_ids.filter((did) => did !== anchor);
    const limit = delayLimit();
    const dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog calibration-dialog";
    dialog.innerHTML = `<form>
      <div class="confirm-dialog-copy"><h3>根据听感完成校准</h3><p>以「${escapeHtml(anchorName)}」为基准，选择每台音箱刚才听起来的先后。后台启动差只用于建议幅度，不决定方向。</p></div>
      <div class="calibration-guide-list">
        ${members.map((did) => {
          const name = speakerName(did, state);
          const current = group.delays_ms?.[did] ?? 0;
          const reference = Math.abs(requestOffsets[did] ?? 0);
          const magnitude = Math.abs(current) || Math.max(100, reference);
          const selected = current > 0 ? "ahead" : current < 0 ? "behind" : "";
          return `<fieldset class="calibration-guide-row">
            <legend>${escapeHtml(name)}</legend>
            <div class="calibration-direction" role="radiogroup" aria-label="${escapeHtml(name)} 与基准的先后">
              <label><input type="radio" name="direction:${escapeHtml(did)}" value="ahead" ${selected === "ahead" ? "checked" : ""} required><span>先响</span></label>
              <label><input type="radio" name="direction:${escapeHtml(did)}" value="sync" required><span>同步</span></label>
              <label><input type="radio" name="direction:${escapeHtml(did)}" value="behind" ${selected === "behind" ? "checked" : ""} required><span>后响</span></label>
            </div>
            <label class="calibration-amount"><span>调整幅度</span><input class="input" type="number" name="amount:${escapeHtml(did)}" min="0" max="${limit}" step="50" value="${Math.min(limit, magnitude)}"><span>ms</span></label>
          </fieldset>`;
        }).join("")}
      </div>
      <p class="caption calibration-hint">“先响”会延后这台音箱；“后响”会让其他音箱等待它。结果写入普通延迟，之后仍可拖动微调。</p>
      <div class="confirm-dialog-actions"><button class="button plain" type="button" data-calibration-cancel>暂不修改</button><button class="button primary" type="submit">应用校准</button></div>
    </form>`;
    let submitted = false;
    dialog.querySelector("[data-calibration-cancel]")?.addEventListener("click", () => dialog.close());
    dialog.querySelector("form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const data = new FormData(event.currentTarget as HTMLFormElement);
      const delays = { ...group.delays_ms };
      for (const did of members) {
        const direction = String(data.get(`direction:${did}`) || "");
        const amount = Math.max(0, Math.min(limit, Number(data.get(`amount:${did}`)) || 0));
        if (direction === "ahead") delays[did] = amount;
        else if (direction === "behind") delays[did] = -amount;
        else if (direction === "sync") delete delays[did];
      }
      submitted = true;
      resolve(delays);
      dialog.close();
    });
    dialog.addEventListener("close", () => {
      if (!submitted) resolve(null);
      dialog.remove();
    }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
  });
}
