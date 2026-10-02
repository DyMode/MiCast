import { TuningEditMode } from './tuning-edit-mode';
/**
 * Full-screen tuning page: drawable EQ curve editor for one speaker.
 *
 * Opened as a secondary page (ui.tuningDid set) over the normal section
 * content — it takes no sidebar slot. Curve commits POST on release only;
 * the page mutates its canvas locally and never triggers a full re-render.
 */

import { api, type Device, type EqPresetsResponse, type SpeakerEq } from "../api";
import { icon } from "../icons";
import { appUrl } from "../paths";
import { store } from "../state";
import { CalibrationWizard } from "./calibration-wizard";
import { EqCurveCanvas, type CurvePoint } from "./eq-curve-canvas";
import { SpectrumFeed } from "./spectrum-feed";
import { syncModal } from '../ui/modal';
import { SurfaceScope } from '../ui/lifecycle';
import { deviceTuning } from '../selectors';

const PRESET_LABELS: Record<string, string> = {
  flat: "平直",
  bass: "低音增强",
  vocal: "人声清晰",
  night: "轻音",
  live: "现场感",
  harman: "Harman",
};

const TARGET_LABELS: Record<string, string> = {
  harman: "Harman 目标",
  diffuse_field: "扩散场",
};

interface TuningState {
  did: string;
  enabled: boolean;
  points: CurvePoint[];
  preset: string;
  target: string;
  revision: number;
  undoAvailable: boolean;
}

let presetsCache: EqPresetsResponse | null = null;
let editor: EqCurveCanvas | null = null;
// Live spectrum, shared machinery with the fullscreen player (see
// spectrum-feed.ts) — one implementation of WS push + poll fallback.
const spectrumFeed = new SpectrumFeed((bands) => editor?.setSpectrum(bands));
let commitTimer: number | null = null;
let tuningSyncCleanup: (() => void) | null = null;
let activeWizard: CalibrationWizard | null = null;

/** Stop the spectrum feed: socket, pending reconnect, and poll fallback. */
function closeSpectrumSocket() {
  spectrumFeed.attach(null);
}

/** Live spectrum for the editor's device; see SpectrumFeed.attach. */
function openSpectrumSocket(did: string) {
  spectrumFeed.attach(did);
}

export function tuningViewActive(): boolean {
  return store.get().ui.tuningDid != null;
}

export function openTuning(did: string) {
  store.setUi({ tuningDid: did });
}

export function closeTuning() {
  disposeTuningView();
  store.setUi({ tuningDid: null });
}

export function disposeTuningView() {
  if (document.querySelector('[data-ab-modal]:not([hidden])')) syncModal(null);
  activeWizard?.destroy();
  activeWizard = null;
  tuningSyncCleanup?.();
  tuningSyncCleanup = null;
  closeSpectrumSocket();
  editor?.destroy();
  editor = null;
  if (commitTimer != null) {
    window.clearTimeout(commitTimer);
    commitTimer = null;
  }
}

export function renderTuningView(device: Device | undefined): string {
  const eq = device?.eq;
  const name = device ? device.alias || device.name : "音箱";
  const advancedOpen = store.get().ui.tuningAdvanced;
  return `
    <div class="page-heading tuning-heading">
      <button type="button" class="icon-button" data-tuning-back aria-label="返回">${"<"}</button>
      <div>
        <h2 class="page-title">调音台 · ${escapeHtml(name)}</h2>
        <p>拖动控制点调整，点击空白添加；松手后自动应用。</p>
      </div>
    </div>
    <div class="tuning-edit-mode" data-tuning-edit-mode>
      <button type="button" class="button secondary" data-tuning-edit-toggle aria-pressed="false">启用编辑</button>
      <span data-tuning-undo-slot></span>
    </div>
    <div class="tuning-canvas-wrap">
      <canvas class="tuning-canvas" data-tuning-canvas aria-label="EQ 曲线编辑器"></canvas>
      <button type="button" class="point-delete-chip" data-point-delete hidden>删除控制点</button>
    </div>
    <section class="eq-point-editor" data-eq-point-editor hidden aria-label="编辑选中的控制点">
      <div class="eq-point-editor-header"><strong>编辑控制点</strong><button type="button" class="icon-button" data-eq-point-close aria-label="关闭控制点编辑">${icon('close')}</button></div>
      <div data-eq-point-fields></div>
    </section>
    <div class="tuning-toolbar">
      <button type="button" class="icon-button compact" data-tuning-undo ${eq?.undo_available ? "" : "disabled"} aria-label="撤销上一次调整" title="撤销上一次调整">${icon("undo")}</button>
      <label class="tuning-target">
        <span class="caption">曲线</span>
        <select data-tuning-curve aria-label="曲线库"></select>
      </label>
      <button type="button" class="button secondary" data-tuning-curve-save>保存</button>
      <button type="button" class="button secondary" data-tuning-curve-rename disabled>重命名</button>
      <button type="button" class="button secondary" data-tuning-curve-delete disabled>删除</button>
      <span class="tuning-divider-v"></span>
      <button type="button" class="button secondary" data-tuning-import>导入</button>
      <button type="button" class="button secondary" data-tuning-export>导出</button>
      <input type="file" accept=".txt,.eq,text/plain" data-tuning-import-file hidden>
      <span class="caption tuning-hint">文件兼容 AutoEq 格式</span>
    </div>
    <div class="tuning-toolbar">
      <span class="caption">叠加</span>
      <label class="tuning-enable">
        <input type="checkbox" class="switch" data-tuning-night ${eq?.night_mode ? "checked" : ""} aria-label="夜间模式">
        <span>夜间模式</span>
      </label>
      <label class="tuning-enable">
        <input type="checkbox" class="switch" data-tuning-loudness ${eq?.loudness_comp_enabled ? "checked" : ""} aria-label="响度补偿">
        <span>响度补偿</span>
      </label>
    </div>
    <div class="tuning-advanced">
      <button type="button" class="tuning-advanced-toggle ${advancedOpen ? "open" : ""}" data-tuning-advanced-toggle aria-expanded="${advancedOpen}">
        高级功能
      </button>
      <div class="tuning-advanced-body" data-tuning-advanced-body ${advancedOpen ? "" : "hidden"}>
        <div class="tuning-toolbar">
          <label class="tuning-target" title="叠加在画布背后的参考虚线：不是你的当前曲线，不改变声音">
            <span class="caption">参考曲线</span>
            <select data-tuning-target aria-label="参考曲线"></select>
          </label>
          <span class="caption tuning-hint">仅叠加显示作参考，不改变声音</span>

        </div>
        <div class="tuning-toolbar">
          <button type="button" class="button secondary" data-tuning-calibrate>自动校准<span class="tuning-badge">实验性</span></button>
          <button type="button" class="button secondary" data-tuning-ab-toggle>盲听对比</button>
        </div>
        <div class="tuning-wizard" data-tuning-wizard hidden></div>
      </div>
    </div>
    <div class="ab-modal" data-ab-modal hidden>
      <div class="ab-dialog" role="dialog" aria-modal="true" aria-label="盲听对比">
        <h3>盲听对比</h3>
        <div class="ab-pick" data-ab-pick>
          <label class="tuning-target">
            <span class="caption">对比项 1</span>
            <select data-ab-choice="0" aria-label="对比项 1"></select>
          </label>
          <label class="tuning-target">
            <span class="caption">对比项 2</span>
            <select data-ab-choice="1" aria-label="对比项 2"></select>
          </label>
        </div>
        <p class="caption ab-status" data-ab-status></p>
        <div class="ab-actions">
          <button type="button" class="button plain" data-ab-close>关闭</button>
          <button type="button" class="button secondary" data-ab-listen-a hidden>听 A</button>
          <button type="button" class="button secondary" data-ab-listen-b hidden>听 B</button>
          <button type="button" class="button secondary" data-ab-reveal hidden>揭示</button>
          <button type="button" class="button primary" data-ab-start>开始</button>
        </div>
      </div>
    </div>
    <div class="tuning-footer caption">
      仅作用于这台音箱；曲线不同的音箱会使用独立音频流。
    </div>
  `;
}

export function bindTuningView(container: HTMLElement, onClose: () => void) {
  tuningSyncCleanup?.();
  tuningSyncCleanup = null;
  editor?.destroy();
  editor = null;
  closeSpectrumSocket();
  const did = store.get().ui.tuningDid;
  if (!did) return;
  const device = store.get().devices.find((d) => d.did === did);
  const eq = device?.eq;
  const scope = new SurfaceScope();

  const state: TuningState = {
    did,
    enabled: eq?.enabled ?? false,
    points: (eq?.points ?? []).map(([freq, gain]) => ({ freq, gain })),
    preset: eq?.preset ?? "",
    target: eq?.target ?? "",
    revision: eq?.revision ?? 0,
    undoAvailable: Boolean(eq?.undo_available),
  };

  let wizard: CalibrationWizard | null = null;
  // Blind test (modal): two user-chosen curves are randomly assigned to A/B.
  // On open playback pauses and slot A's curve is already applied, so 「开始」
  // is a pure resume with no encoder-rebuild gap.
  let ab: {
    original: CurvePoint[];
    choiceKeys: [string, string];
    aIsFirst: boolean;
    started: boolean;
    heardA: boolean;
    heardB: boolean;
    revealed: boolean;
    pausedByUs: boolean;
  } | null = null;

  container.querySelector<HTMLElement>("[data-tuning-back]")?.addEventListener("click", () => {
    activeWizard?.destroy();
    activeWizard = null;
    wizard = null;
    closeSpectrumSocket();
    tuningSyncCleanup?.();
    tuningSyncCleanup = null;
    editor?.setSpectrum(null);
    onClose();
  });

  let selectedPoint: number | null = null;
  const canvas = container.querySelector<HTMLCanvasElement>("[data-tuning-canvas]");
  const nightToggle = container.querySelector<HTMLInputElement>("[data-tuning-night]");
  const loudnessToggle = container.querySelector<HTMLInputElement>("[data-tuning-loudness]");
  const targetSelect = container.querySelector<HTMLSelectElement>("[data-tuning-target]");
  const curveSelect = container.querySelector<HTMLSelectElement>("[data-tuning-curve]");
  const curveSaveBtn = container.querySelector<HTMLButtonElement>("[data-tuning-curve-save]");
  const curveRenameBtn = container.querySelector<HTMLButtonElement>("[data-tuning-curve-rename]");
  const curveDeleteBtn = container.querySelector<HTMLButtonElement>("[data-tuning-curve-delete]");
  const undoBtn = container.querySelector<HTMLButtonElement>("[data-tuning-undo]");
  const editMode = new TuningEditMode(container, scope, () => editor, () => ({
    pointCount: state.points.length, undoAvailable: state.undoAvailable,
  }));
  const canEdit = () => editMode.enabled;
  const updateEditMode = () => editMode.update();

  // ---- curve library (global): presets + user-saved curves ----

  const samePoints = (a: CurvePoint[], b: Array<[number, number]>) =>
    a.length === b.length &&
    a.every((p, i) => Math.abs(p.freq - b[i][0]) < 0.05 && Math.abs(p.gain - b[i][1]) < 0.005);

  /** Which library entry the current canvas state corresponds to. */
  const currentCurveKey = (): string => {
    if (state.preset && presetsCache?.presets[state.preset]) return `preset:${state.preset}`;
    for (const [name, pts] of Object.entries(presetsCache?.saved ?? {})) {
      if (samePoints(state.points, pts)) return `saved:${name}`;
    }
    return "current";
  };

  const refreshCurveSelect = () => {
    if (!curveSelect) return;
    const key = currentCurveKey();
    const group = (label: string, options: string) =>
      options ? `<optgroup label="${label}">${options}</optgroup>` : "";
    const option = (value: string, label: string) =>
      `<option value="${escapeHtml(value)}" ${value === key ? "selected" : ""}>${escapeHtml(label)}</option>`;
    curveSelect.innerHTML =
      `<optgroup label="当前">${option("current", "自定义曲线（未保存）")}</optgroup>` +
      group(
        "我的曲线",
        Object.keys(presetsCache?.saved ?? {})
          .map((name) => option(`saved:${name}`, name))
          .join("")
      ) +
      group(
        "系统预设",
        Object.keys(PRESET_LABELS)
          .map((k) => option(`preset:${k}`, PRESET_LABELS[k]))
          .join("")
      );
    curveSelect.value = key;
    if (curveSelect.value !== key) curveSelect.value = "current"; // unsaved edit
    const isSaved = curveSelect.value.startsWith("saved:");
    if (curveRenameBtn) curveRenameBtn.disabled = !isSaved;
    if (curveDeleteBtn) curveDeleteBtn.disabled = !isSaved;
  };

  /** Selecting a library entry replaces the canvas and commits. */
  const applyCurveKey = (key: string) => {
    if (!canEdit()) return;
    if (key === "current") return;
    const [kind, name] = [key.split(":")[0], key.slice(key.indexOf(":") + 1)];
    const gains =
      kind === "preset" ? presetsCache?.presets[name] : presetsCache?.saved?.[name];
    if (!gains) return;
    const points = gains.map(([freq, gain]) => ({ freq, gain }));
    editor?.setPoints(points);
    state.points = points;
    state.preset = kind === "preset" ? name : "";
    save();
  };

  const markCurve = () => { refreshCurveSelect(); refreshPointEditor(); };

  /** Reference overlay: built-in targets, or any curve from the library. */
  const resolveReference = (key: string): CurvePoint[] | null => {
    if (!key) return null;
    const toPoints = (gains: [number, number][]) =>
      gains.map(([freq, gain]) => ({ freq, gain }));
    if (presetsCache?.targets[key]) return toPoints(presetsCache.targets[key]);
    if (key.startsWith("saved:")) {
      const gains = presetsCache?.saved?.[key.slice(6)];
      return gains ? toPoints(gains) : null;
    }
    if (key.startsWith("preset:")) {
      const gains = presetsCache?.presets[key.slice(7)];
      return gains ? toPoints(gains) : null;
    }
    return null;
  };

  const applyTarget = (key: string) => {
    editor?.setTarget(resolveReference(key) ?? undefined);
  };

  /** Same library as the curve picker, plus the built-in standard targets. */
  const refreshTargetSelect = () => {
    if (!targetSelect) return;
    const option = (value: string, label: string) =>
      `<option value="${escapeHtml(value)}" ${value === state.target ? "selected" : ""}>${escapeHtml(label)}</option>`;
    const group = (label: string, options: string) =>
      options ? `<optgroup label="${label}">${options}</optgroup>` : "";
    targetSelect.innerHTML =
      option("", "无") +
      group(
        "标准目标",
        Object.entries(TARGET_LABELS)
          .map(([k, label]) => option(k, label))
          .join("")
      ) +
      group(
        "我的曲线",
        Object.keys(presetsCache?.saved ?? {})
          .map((name) => option(`saved:${name}`, name))
          .join("")
      ) +
      group(
        "系统预设",
        Object.keys(PRESET_LABELS)
          // harman 同时是标准目标和预设（同一条曲线）——只在标准目标里出现。
          .filter((k) => presetsCache?.presets[k] && !(k in TARGET_LABELS))
          .map((k) => option(`preset:${k}`, PRESET_LABELS[k]))
          .join("")
      );
    // A since-deleted reference collapses to 无.
    if (targetSelect.value !== state.target) targetSelect.value = "";
  };

  // ---- backend sync (never a full re-render, so an open drag survives) ----

  const syncLocal = (resp: SpeakerEq) => {
    if (!scope.active) return;
    store.updateDeviceTuning(state.did, resp);
    if (nightToggle) nightToggle.checked = Boolean(resp.night_mode);
    if (loudnessToggle) loudnessToggle.checked = Boolean(resp.loudness_comp_enabled);
    state.revision = resp.revision ?? state.revision;
    state.undoAvailable = Boolean(resp.undo_available);
    if (undoBtn) undoBtn.disabled = !canEdit() || !state.undoAvailable;
  };

  const syncFull = (resp: SpeakerEq) => {
    syncLocal(resp);
    state.enabled = resp.enabled;
    state.points = (resp.points ?? []).map(([f, g]) => ({ freq: f, gain: g }));
    state.preset = resp.preset ?? "";
    state.target = resp.target ?? "";
    editor?.setPoints(state.points);
    applyTarget(state.target);
    markCurve();
    refreshTargetSelect();
  };

  let remoteSyncBusy = false;
  let localWrites = 0;
  const localWrite = <T>(work: () => Promise<T>): Promise<T> => {
    localWrites += 1;
    return work().finally(() => { localWrites -= 1; });
  };
  const refreshRemote = async (force = false) => {
    if (remoteSyncBusy || ((commitTimer != null || localWrites > 0) && !force)) return;
    if (!force && container.querySelector('[data-eq-point-fields]')?.contains(document.activeElement)) return;
    remoteSyncBusy = true;
    try {
      const latest = await api.getDeviceTuning(state.did);
      if (!scope.active) return;
      const revision = latest.revision ?? 0;
      const changed = revision > state.revision;
      if (force || changed) {
        syncFull(latest);
        if (changed) store.showToast("调音已在另一端更新，已载入最新设置");
      }
    } catch {
      // Realtime sync is best-effort; ordinary saves still surface failures.
    } finally {
      remoteSyncBusy = false;
    }
  };

  const onTuningChange = (event: Event) => {
    const revisions = (event as CustomEvent<Record<string, number>>).detail ?? {};
    if ((revisions[state.did] ?? 0) > state.revision) void refreshRemote();
  };
  window.addEventListener("micast:tuning-change", onTuningChange);
  const tuningPoll = window.setInterval(() => {
    if (!document.hidden) void refreshRemote();
  }, 2500);
  tuningSyncCleanup = () => {
    scope.dispose();
    window.removeEventListener("micast:tuning-change", onTuningChange);
    window.clearInterval(tuningPoll);
  };

  const postCurve = () =>
    localWrite(() => api.setDeviceEqCurve(state.did, {
        enabled: state.enabled,
        points: state.points.map((p) => [p.freq, p.gain]),
        preset: state.preset,
        target: state.target,
        revision: state.revision,
      })).then(syncLocal);

  let curveRevision = 0;

  const save = (patch: Partial<TuningState> = {}) => {
    Object.assign(state, patch);
    const revision = ++curveRevision;
    // Debounce: rapid commits (drag releases, preset taps) collapse into one
    // pipeline rebuild; each rebuild costs a sub-second encoder gap.
    if (commitTimer != null) window.clearTimeout(commitTimer);
    commitTimer = window.setTimeout(() => {
      commitTimer = null;
      postCurve()
        .then(() => {
          if (revision === curveRevision) store.showToast("EQ 已应用");
        })
        .catch((e) => {
          if (revision === curveRevision) {
            store.showToast(`EQ 保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
            void refreshRemote(true);
          }
        });
    }, 300);
  };

  function refreshPointEditor() {
    const host = container.querySelector<HTMLElement>('[data-eq-point-fields]');
    if (!host) return;
    const panel = container.querySelector<HTMLElement>('[data-eq-point-editor]');
    if (panel) panel.hidden = selectedPoint === null || !canEdit();
    // A single persistent editor owns focus and button presses across commits.
    const point = selectedPoint === null ? null : state.points[selectedPoint];
    if (point) {
      if (!host.firstElementChild) host.innerHTML = `<div class="eq-point-row">
        <label>频率 <span class="caption">Hz</span><input class="input" type="number" min="20" max="20000" step="1" data-eq-field="freq"></label>
        <label>增益 <span class="caption">dB</span><input class="input" type="number" min="-12" max="12" step="0.1" data-eq-field="gain"></label>
        <button type="button" class="icon-button" data-eq-remove aria-label="删除控制点">${icon('trash')}</button>
      </div>`;
      (host.firstElementChild as HTMLElement).dataset.eqPoint = String(selectedPoint);
      for (const key of ['freq', 'gain'] as const) {
        const input = host.querySelector<HTMLInputElement>(`[data-eq-field="${key}"]`)!;
        if (document.activeElement !== input) input.value = key === 'freq' ? String(Math.round(point.freq)) : point.gain.toFixed(1);
        input.setAttribute('aria-label', key === 'freq' ? '控制点频率' : '控制点增益');
      }
      const remove = host.querySelector<HTMLButtonElement>('[data-eq-remove]')!;
      remove.dataset.eqRemove = String(selectedPoint);
      remove.disabled = false;
    }
    updateEditMode();
  }
  const numericFields = container.querySelector<HTMLElement>('[data-eq-point-fields]');
  const commitPoints = () => {
    const selected = selectedPoint !== null ? state.points[selectedPoint] : null;
    state.points.sort((a, b) => a.freq - b.freq);
    selectedPoint = selected ? state.points.indexOf(selected) : null;
    state.preset = '';
    editor?.setPoints(state.points);
    selectedPoint = selected ? state.points.indexOf(selected) : null;
    markCurve();
    save();
  };
  numericFields?.addEventListener('change', event => {
    if (!canEdit()) return;
    const field = event.target as HTMLInputElement;
    if (!field.matches('[data-eq-field]') || !field.reportValidity()) return;
    const index = Number(field.closest<HTMLElement>('[data-eq-point]')?.dataset.eqPoint);
    const key = field.dataset.eqField as 'freq' | 'gain';
    const value = Number(field.value);
    if (!state.points[index] || !Number.isFinite(value)) return;
    if (key === 'freq' && state.points.some((p, i) => i !== index && p.freq === value)) {
      field.setCustomValidity('这个频率已有控制点'); field.reportValidity(); field.setCustomValidity(''); return;
    }
    state.points[index][key] = value;
    commitPoints();
  });
  numericFields?.addEventListener('click', event => {
    if (!canEdit()) return;
    const button = (event.target as HTMLElement).closest<HTMLElement>('[data-eq-remove]');
    if (!button) return;
    state.points.splice(Number(button.dataset.eqRemove), 1);
    selectedPoint = null;
    commitPoints();
  });
  container.querySelector('[data-eq-add-point]')?.addEventListener('click', () => {
    if (!canEdit()) return;
    let freq = 1000;
    while (state.points.some(p => p.freq === freq) && freq < 20000) freq += 100;
    if (freq > 20000 || state.points.some(p => p.freq === freq)) return;
    state.points.push({ freq, gain: 0 });
    commitPoints();
  });
  container.querySelector('[data-eq-point-close]')?.addEventListener('click', () => { selectedPoint = null; editor?.clearPointSelection(); refreshPointEditor(); });
  refreshPointEditor();

  // Immediate commit (A/B switching) — flush any pending debounce first.
  // Awaitable so the blind test can sequence "apply curve → resume playback".
  const commitNow = () => {
    const revision = ++curveRevision;
    if (commitTimer != null) {
      window.clearTimeout(commitTimer);
      commitTimer = null;
    }
    return postCurve()
      .then(() => {
        if (revision === curveRevision) store.showToast("EQ 已应用");
      })
      .catch((e) => {
        if (revision === curveRevision) {
          store.showToast(`EQ 保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
          void refreshRemote(true);
        }
      });
  };

  // ---- canvas ----

  if (canvas) {
    const deleteChip = container.querySelector<HTMLButtonElement>("[data-point-delete]");
    editor = new EqCurveCanvas(canvas, {
      points: state.points,
      readOnly: !canEdit(),
      freqRange: [20, 20000],
      gainRange: [-12, 12],
      onCommit: (points) => {
        if (!canEdit()) return;
        state.points = points;
        // A hand edit detaches the curve from whichever preset it started
        // as — clear the tag BEFORE marking, or the select keeps showing
        // the preset name over a modified curve.
        state.preset = "";
        markCurve();
        save();
      },
      onSelect: (index) => {
        selectedPoint = canEdit() ? index : null;
        refreshPointEditor();
      },
    });
    deleteChip?.addEventListener("click", () => {
      const index = Number(deleteChip.dataset.index);
      if (editor && Number.isInteger(index)) editor.deletePoint(index);
      deleteChip.hidden = true;
    });
    // Presets/targets/library arrive async; apply once loaded.
    if (!presetsCache) {
      api
        .getEqPresets()
        .then((r) => {
          presetsCache = r;
          if (!scope.active) return;
          applyTarget(state.target);
          refreshCurveSelect();
          refreshTargetSelect();
        })
        .catch(() => undefined);
    } else {
      applyTarget(state.target);
    }
    openSpectrumSocket(state.did);
  }
  refreshCurveSelect();
  refreshTargetSelect();
  updateEditMode();

  // ---- primary controls ----

  const advancedToggle = container.querySelector<HTMLElement>("[data-tuning-advanced-toggle]");
  const advancedBody = container.querySelector<HTMLElement>("[data-tuning-advanced-body]");
  advancedToggle?.addEventListener("click", () => {
    const open = advancedBody ? advancedBody.hidden : false;
    if (advancedBody) advancedBody.hidden = !open;
    advancedToggle.classList.toggle("open", open);
    advancedToggle.setAttribute("aria-expanded", String(open));
    // Persist across visits. The poll-skipped re-render keeps the DOM as-is;
    // the flags above already flipped it.
    store.setUi({ tuningAdvanced: open });
  });

  nightToggle?.addEventListener("change", () => {
    localWrite(() => api.setDeviceNightMode(state.did, nightToggle.checked, state.revision))
      .then(syncLocal)
      .catch((e) => {
        nightToggle.checked = !nightToggle.checked;
        store.showToast(`夜间模式切换失败: ${e instanceof Error ? e.message : "未知错误"}`);
        void refreshRemote(true);
      });
  });

  loudnessToggle?.addEventListener("change", () => {
    localWrite(() => api.setDeviceLoudness(state.did, loudnessToggle.checked, state.revision))
      .then(syncLocal)
      .catch((e) => {
        loudnessToggle.checked = !loudnessToggle.checked;
        store.showToast(`响度补偿切换失败: ${e instanceof Error ? e.message : "未知错误"}`);
        void refreshRemote(true);
      });
  });

  undoBtn?.addEventListener("click", async () => {
    if (undoBtn.disabled) return;
    undoBtn.disabled = true;
    try {
      const resp = await localWrite(() => api.undoDeviceTuning(state.did, state.revision));
      syncFull(resp);
      store.showToast("已撤销上一次调音调整");
    } catch (e) {
      store.showToast(`撤销失败: ${e instanceof Error ? e.message : "未知错误"}`);
      await refreshRemote(true);
    }
  });

  targetSelect?.addEventListener("change", async () => {
    const previous = state.target;
    state.target = targetSelect.value;
    applyTarget(state.target);
    targetSelect.disabled = true;
    try {
      const resp = await localWrite(() => api.setDeviceEqTarget(state.did, state.target, state.revision));
      syncLocal(resp);
    } catch (e) {
      state.target = previous;
      targetSelect.value = previous;
      applyTarget(previous);
      store.showToast(`参考曲线保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
      void refreshRemote(true);
    } finally {
      targetSelect.disabled = false;
    }
  });

  curveSelect?.addEventListener("change", () => {
    applyCurveKey(curveSelect.value);
    refreshCurveSelect();
  });

  const applyCurveList = (curves: Record<string, [number, number][]>) => {
    presetsCache = { ...(presetsCache ?? { presets: {}, targets: {}, freq_range: [20, 20000], gain_range: [-12, 12] }), saved: curves };
    refreshCurveSelect();
    refreshTargetSelect();
  };

  curveSaveBtn?.addEventListener("click", async () => {
    const name = window.prompt("给这条曲线起个名字：");
    if (!name?.trim()) return;
    try {
      const resp = await api.saveCurve(name.trim(), state.points.map((p) => [p.freq, p.gain]));
      applyCurveList(resp.curves);
      store.showToast(`已保存曲线「${name.trim()}」`);
    } catch (e) {
      store.showToast(`保存失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });

  curveRenameBtn?.addEventListener("click", async () => {
    const oldName = curveSelect?.value.startsWith("saved:") ? curveSelect.value.slice(6) : null;
    if (!oldName) return;
    const newName = window.prompt("新名字：", oldName);
    if (!newName?.trim() || newName.trim() === oldName) return;
    try {
      const resp = await api.renameCurve(oldName, newName.trim());
      applyCurveList(resp.curves);
      if (curveSelect) curveSelect.value = `saved:${newName.trim()}`;
      store.showToast("已重命名");
    } catch (e) {
      store.showToast(`重命名失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });

  curveDeleteBtn?.addEventListener("click", async () => {
    const name = curveSelect?.value.startsWith("saved:") ? curveSelect.value.slice(6) : null;
    if (!name || !window.confirm(`删除曲线「${name}」？音箱上正在使用的曲线不受影响。`)) return;
    try {
      const resp = await api.deleteCurve(name);
      applyCurveList(resp.curves);
      store.showToast(`已删除「${name}」`);
    } catch (e) {
      store.showToast(`删除失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });

  // ---- import / export ----

  const importFile = container.querySelector<HTMLInputElement>("[data-tuning-import-file]");
  container.querySelector<HTMLElement>("[data-tuning-import]")?.addEventListener("click", () => importFile?.click());
  importFile?.addEventListener("change", async () => {
    const file = importFile.files?.[0];
    importFile.value = "";
    if (!file) return;
    try {
      const text = await file.text();
      const resp = await localWrite(() => api.importGraphicEq(state.did, text, state.revision));
      syncFull(resp);
      store.showToast("已导入 AutoEq 曲线");
    } catch (e) {
      store.showToast(`导入失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });

  container.querySelector<HTMLElement>("[data-tuning-export]")?.addEventListener("click", () => {
    // pywebview (WebView2) ignores in-page blob downloads; navigating to an
    // attachment endpoint triggers the native download in both webview and
    // plain browsers.
    window.location.assign(appUrl(`/api/tuning/${encodeURIComponent(state.did)}/export.txt`));
  });

  // ---- A/B blind comparison (modal) ----

  const abModal = container.querySelector<HTMLElement>("[data-ab-modal]");
  const abStatus = container.querySelector<HTMLElement>("[data-ab-status]");
  const abPick = container.querySelector<HTMLElement>("[data-ab-pick]");
  const abStart = container.querySelector<HTMLElement>("[data-ab-start]");
  const abListenA = container.querySelector<HTMLElement>("[data-ab-listen-a]");
  const abListenB = container.querySelector<HTMLElement>("[data-ab-listen-b]");
  const abReveal = container.querySelector<HTMLElement>("[data-ab-reveal]");
  const abCloseBtn = container.querySelector<HTMLElement>("[data-ab-close]");
  const abChoices = [
    container.querySelector<HTMLSelectElement>('[data-ab-choice="0"]'),
    container.querySelector<HTMLSelectElement>('[data-ab-choice="1"]'),
  ];

  // Candidates: the current curve, flat, every preset, and the saved library.
  const abOptions = (): Array<[string, string]> => {
    const opts: Array<[string, string]> = [
      ["current", "当前曲线"],
      ["flat", "平直"],
    ];
    for (const key of Object.keys(PRESET_LABELS)) {
      if (key !== "flat" && presetsCache?.presets[key]) opts.push([`preset:${key}`, PRESET_LABELS[key]]);
    }
    for (const name of Object.keys(presetsCache?.saved ?? {})) {
      opts.push([`saved:${name}`, name]);
    }
    return opts;
  };

  const abChoiceLabel = (key: string) => abOptions().find(([k]) => k === key)?.[1] ?? key;

  const abResolve = (key: string): CurvePoint[] => {
    if (key === "current") return ab?.original.map((p) => ({ ...p }) as CurvePoint) ?? [];
    if (key === "flat") return [];
    const [kind, name] = [key.split(":")[0], key.slice(key.indexOf(":") + 1)];
    const gains = kind === "preset" ? presetsCache?.presets[name] : presetsCache?.saved?.[name];
    return (gains ?? []).map(([freq, gain]) => ({ freq, gain }));
  };

  const abSlotCurve = (slot: "a" | "b"): CurvePoint[] => {
    if (!ab) return [];
    const key = slot === "a" === ab.aIsFirst ? ab.choiceKeys[0] : ab.choiceKeys[1];
    return abResolve(key);
  };

  const abApply = async (slot: "a" | "b") => {
    if (!ab) return;
    state.points = abSlotCurve(slot);
    state.preset = "";
    // True blind: the canvas keeps showing the original curve until reveal.
    if (ab.revealed) {
      editor?.setPoints(state.points);
      markCurve();
    }
    abListenA?.classList.toggle("active", slot === "a");
    abListenB?.classList.toggle("active", slot === "b");
    await commitNow();
  };

  const abSync = () => {
    if (!ab || !abStatus) return;
    if (ab.revealed) {
      const aKey = ab.aIsFirst ? ab.choiceKeys[0] : ab.choiceKeys[1];
      const bKey = ab.aIsFirst ? ab.choiceKeys[1] : ab.choiceKeys[0];
      abStatus.textContent = `A = ${abChoiceLabel(aKey)} · B = ${abChoiceLabel(bKey)}`;
    } else if (ab.started) {
      abStatus.textContent = "A、B 可以随时切换着听；两条都听过之后就可以揭示了。";
    } else {
      abStatus.textContent = "A/B 已随机分配，音源已提前切到 A。点「开始」继续播放。";
    }
    if (abStart) abStart.hidden = ab.started;
    if (abListenA) abListenA.hidden = !ab.started;
    if (abListenB) abListenB.hidden = !ab.started;
    if (abReveal) abReveal.hidden = ab.revealed || !(ab.heardA && ab.heardB);
    abPick?.querySelectorAll("select").forEach((s) => (s.disabled = ab!.started));
  };

  // Re-apply slot A while paused whenever the picks change (pre-start only),
  // so playback resumes straight into the right curve.
  const abPreloadA = () => {
    if (!ab || ab.started) return;
    void abApply("a");
  };

  const abOpen = () => {
    if (ab) return;
    const opts = abOptions();
    abChoices.forEach((sel, i) => {
      if (!sel) return;
      sel.innerHTML = opts
        .map(([key, label]) => `<option value="${key}">${label}</option>`)
        .join("");
      sel.value = i === 0 ? "current" : "flat";
    });
    const playback = store.get().playback;
    ab = {
      original: state.points.map((p) => ({ ...p })),
      choiceKeys: ["current", "flat"],
      aIsFirst: Math.random() < 0.5,
      started: false,
      heardA: false,
      heardB: false,
      revealed: false,
      pausedByUs: Boolean(playback?.playing && !playback.paused),
    };
    if (abModal) abModal.hidden = false;
    syncModal(abModal?.querySelector<HTMLElement>('[role="dialog"]') ?? null, () => abClose());
    abSync();
    // Pause first, then cut the stream over to slot A's curve while silent.
    if (ab.pausedByUs) void api.pause().catch(() => undefined);
    void abApply("a");
  };

  const abClose = () => {
    if (!ab) return;
    const { original, started, pausedByUs } = ab;
    ab = null;
    if (abModal) abModal.hidden = true;
    syncModal(null);
    // Slot A was applied on open, so restore even if the test never started —
    // but skip the rebuild when nothing actually changed.
    if (JSON.stringify(state.points) !== JSON.stringify(original)) {
      state.points = original.map((p) => ({ ...p }));
      state.preset = "";
      editor?.setPoints(state.points);
      markCurve();
      void commitNow();
    }
    // If we paused and the user never resumed, restart playback for them.
    if (pausedByUs && !started) void api.play().catch(() => undefined);
  };

  container.querySelector<HTMLElement>("[data-tuning-ab-toggle]")?.addEventListener("click", abOpen);
  abStart?.addEventListener("click", () => {
    if (!ab || ab.started) return;
    if (JSON.stringify(abResolve(ab.choiceKeys[0])) === JSON.stringify(abResolve(ab.choiceKeys[1]))) {
      if (abStatus) abStatus.textContent = "两个对比项的曲线相同，没法对比，换一条再开始。";
      return;
    }
    ab.started = true;
    ab.heardA = true;
    abSync();
    // Slot A was already applied on open/change — this is a pure resume.
    void api.play().catch(() => undefined);
  });
  abListenA?.addEventListener("click", () => {
    if (!ab) return;
    ab.heardA = true;
    abSync();
    void abApply("a");
  });
  abListenB?.addEventListener("click", () => {
    if (!ab) return;
    ab.heardB = true;
    abSync();
    void abApply("b");
  });
  abReveal?.addEventListener("click", () => {
    if (!ab) return;
    ab.revealed = true;
    // Show the curve that is actually playing now.
    editor?.setPoints(state.points);
    markCurve();
    abSync();
  });
  abCloseBtn?.addEventListener("click", abClose);
  abChoices.forEach((sel, i) =>
    sel?.addEventListener("change", () => {
      if (!ab) return;
      ab.choiceKeys[i] = sel.value;
      abPreloadA();
    })
  );

  // After calibration (and optional level match) the speaker's curve and the
  // group gains live server-side; re-read them without a full re-render so the
  // open editor reflects what was applied.
  const refreshFromDevice = async () => {
    try {
      const devices = await api.getDevices();
      store.set({ devices, deviceLoadError: null });
    } catch {
      // The apply() toast already confirmed success; keep local state on error.
    }
    const resp = deviceTuning(store.get(), did);
    if (resp) syncFull(resp);
  };

  const wizardHost = container.querySelector<HTMLElement>("[data-tuning-wizard]");
  container.querySelector<HTMLElement>("[data-tuning-calibrate]")?.addEventListener("click", () => {
    if (!wizardHost) return;
    wizard?.destroy();
    wizardHost.hidden = false;
    wizardHost.innerHTML = "";
    wizard = new CalibrationWizard(wizardHost, did, () => {
      void refreshFromDevice();
    });
    activeWizard = wizard;
  });
}

function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
