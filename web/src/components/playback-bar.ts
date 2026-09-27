import { api, type PlaybackState } from "../api";
import { icon } from "../icons";
import { store } from "../state";
import { setVolume } from "../volume-service";

// True while the user is holding any slider in the bar; playback pushes must
// not re-render the bar (and reset a slider to the server value) mid-drag.
let dragging = false;
let mobileMinimized = false;
let mobileRestoring = false;
export function isPlaybackBarInteracting(): boolean {
  return dragging;
}

// Expanded state survives poll-driven re-renders and page reloads.
const EXPANDED_KEY = "micast-playback-expanded";
let expanded = (() => {
  try {
    return localStorage.getItem(EXPANDED_KEY) === "1";
  } catch {
    return false;
  }
})();

function masterVolume(playback: PlaybackState): number | null {
  if (playback.volume !== null && playback.volume !== undefined) return playback.volume;
  const known = playback.devices.map((d) => d.volume).filter((v): v is number => v !== null && v !== undefined);
  if (!known.length) return null;
  return Math.round(known.reduce((sum, v) => sum + v, 0) / known.length);
}

function deviceVolume(playback: PlaybackState, did: string): number {
  return playback.devices.find((d) => d.did === did)?.volume ?? 0;
}

function sliderMarkup(value: number, attrs: string, label: string): string {
  return `<input type="range" min="0" max="100" value="${value}" ${attrs}
           aria-label="${escapeHtml(label)}" style="--volume:${value}%">`;
}

export function renderPlaybackBar(playback: PlaybackState | null): string {
  if (!playback || playback.devices.length === 0) return "";
  // Stay visible while any device is playing or paused, so a paused session
  // (AirPlay still connected) always offers a way back.
  const active = playback.playing || playback.paused;
  if (!active) return "";

  const multi = playback.devices.length > 1;
  const volume = masterVolume(playback);
  const target = multi ? `${playback.devices.length} 台音箱` : playback.devices[0].name;
  const stateLabel = playback.playing
    ? `正在输出${playback.mixed_volume ? " · 音量不一致" : ""}`
    : "已暂停输出";
  const shownVolume = volume ?? 0;
  const volumeOutput = volume === null ? "—" : `${playback.mixed_volume ? "~" : ""}${volume}`;

  const detailRows = multi
    ? playback.devices
        .map((d) => {
          const v = d.volume ?? 0;
          return `<div class="now-playing-device">
            <span class="now-playing-device-name" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</span>
            <button type="button" class="icon-button compact" data-mute="${escapeHtml(d.did)}" aria-pressed="${d.muted}" aria-label="${d.muted ? "取消静音" : "静音"}" title="${d.muted ? "取消静音" : "静音"}">${icon(d.muted ? "mute" : "speaker")}</button>
            ${sliderMarkup(v, `data-device-slider="${escapeHtml(d.did)}"`, `${d.name} 音量`)}
            <output data-device-output="${escapeHtml(d.did)}">${d.volume === null || d.volume === undefined ? "—" : v}</output>
          </div>`;
        })
        .join("")
    : "";

  return `
    <button class="header-playback-trigger ${mobileMinimized ? "visible" : ""}" data-playback-restore
            aria-label="打开播放控制：${escapeHtml(target)}" title="打开播放控制">
      ${icon(playback.playing ? "speaker" : "pause")}
      <span class="header-playback-label"><strong>${escapeHtml(target)}</strong><small>${stateLabel}</small></span>
      <span class="header-playback-state ${playback.playing ? "is-playing" : "is-paused"}" aria-hidden="true"></span>
    </button>
    <section class="now-playing visible ${mobileMinimized ? "mobile-minimized" : ""} ${mobileRestoring ? "restoring" : ""} ${expanded && multi ? "expanded" : ""}" aria-label="播放控制">
      <div class="now-playing-main">
        <div class="now-playing-icon">${icon("speaker")}</div>
        <div class="now-playing-copy" title="${escapeHtml(target)}">
          <strong>${escapeHtml(target)}</strong>
          <span>${stateLabel}</span>
        </div>
        <label class="volume-control now-playing-master" title="${multi ? "将全部音箱设为同一音量" : "调整这台音箱的音量"}">
          ${sliderMarkup(shownVolume, "data-master-slider", multi ? "全部音箱音量" : "音箱音量")}
          <output data-master-output>${volumeOutput}</output>
        </label>
        <div class="now-playing-controls">
          <button class="icon-button compact" data-playback-toggle
                  aria-label="${playback.playing ? "暂停输出" : "继续输出"}">
            ${icon(playback.playing ? "pause" : "play")}
          </button>
          <button class="icon-button compact" data-playback-stop aria-label="结束输出" title="结束输出并断开手机连接">
            ${icon("close")}
          </button>
          <button class="icon-button compact now-playing-minimize" data-playback-minimize aria-label="收起到顶部" title="收起到顶部">
            ${icon("minimize")}
          </button>
          <button type="button" class="icon-button compact" data-mute="${escapeHtml(playback.devices.map(d => d.did).join(","))}" aria-pressed="${playback.muted}" aria-label="${playback.muted ? "取消全部静音" : "全部静音"}" title="${playback.muted ? "取消全部静音" : "全部静音"}">${icon(playback.muted ? "mute" : "speaker")}</button>
          ${multi ? `<button class="icon-button compact now-playing-expand" data-playback-expand
                  aria-label="${expanded ? "收起各音箱音量" : "展开各音箱音量"}" aria-expanded="${expanded}">
            ${icon("chevron")}
          </button>` : ""}
        </div>
      </div>
      ${multi ? `<div class="now-playing-details" ${expanded ? "" : "hidden"}>${detailRows}</div>` : ""}
    </section>`;
}

export function bindPlaybackBar(container: HTMLElement) {
  const section = container.querySelector<HTMLElement>(".now-playing");
  if (!section) return;
  const main = section.querySelector<HTMLElement>(".now-playing-main");
  const master = section.querySelector<HTMLInputElement>("[data-master-slider]");
  const masterOutput = section.querySelector<HTMLOutputElement>("[data-master-output]");
  // Capture the rendered targets; a delayed request must not jump to a new
  // session if the current playback changes during a drag.
  const targets = (store.get().playback?.devices ?? []).map((d) => d.did);
  // Sliders commit ONLY on release (change fires on pointerup / key end).
  // While the pointer is down we update visuals locally — committing mid-drag
  // makes the thumb lag behind the finger ("不跟手") as the server value
  // round-trips back.
  const lastCommitted = new Map<string, number>();

  container.querySelector("[data-playback-minimize]")?.addEventListener("click", () => {
    if (section.classList.contains("minimizing")) return;
    section.classList.add("minimizing");
    container.querySelector("[data-playback-restore]")?.classList.add("visible");
    window.setTimeout(() => {
      mobileMinimized = true;
      document.dispatchEvent(new CustomEvent("micast:render-playback"));
    }, 180);
  });
  container.querySelector("[data-playback-restore]")?.addEventListener("click", () => {
    mobileMinimized = false;
    mobileRestoring = true;
    document.dispatchEvent(new CustomEvent("micast:render-playback"));
    mobileRestoring = false;
  });

  const commit = (key: string, dids: string[], slider: HTMLInputElement, output: HTMLOutputElement | null) => {
    const value = Number(slider.value);
    if (lastCommitted.get(key) === value) return;
    lastCommitted.set(key, value);
    setVolume(value, dids).catch((e) => {
      const rollback = Number(slider.defaultValue || 0);
      lastCommitted.set(key, rollback);
      slider.value = String(rollback);
      slider.style.setProperty("--volume", `${rollback}%`);
      if (output) output.value = String(rollback);
      store.showToast(`音量设置失败: ${e instanceof Error ? e.message : "未知错误"}`);
    });
  };

  const watchDrag = (slider: HTMLInputElement) => {
    slider.addEventListener("pointerdown", () => { dragging = true; });
    const end = () => { dragging = false; };
    slider.addEventListener("pointerup", end);
    slider.addEventListener("pointercancel", end);
    slider.addEventListener("lostpointercapture", end);
    slider.addEventListener("blur", end);
  };

  // Master: dragging unifies every speaker and mirrors the value onto each
  // per-device slider immediately.
  if (master) {
    lastCommitted.set("master", Number(master.value));
    watchDrag(master);
    master.addEventListener("input", () => {
      const value = Number(master.value);
      master.style.setProperty("--volume", `${value}%`);
      if (masterOutput) masterOutput.value = String(value);
      section.querySelectorAll<HTMLInputElement>("[data-device-slider]").forEach((child) => {
        child.value = String(value);
        child.style.setProperty("--volume", `${value}%`);
        const out = section.querySelector<HTMLOutputElement>(`[data-device-output="${child.dataset.deviceSlider}"]`);
        if (out) out.value = String(value);
      });
    });
    master.addEventListener("change", () => {
      // Mirror the committed value into each child's rollback baseline.
      section.querySelectorAll<HTMLInputElement>("[data-device-slider]").forEach((child) => {
        lastCommitted.set(`device:${child.dataset.deviceSlider}`, Number(child.value));
      });
      commit("master", targets, master, masterOutput);
    });
  }

  // Per-device sliders (expanded panel). While dragging one, the master shows
  // the live average of all speakers.
  section.querySelectorAll<HTMLInputElement>("[data-device-slider]").forEach((slider) => {
    const did = slider.dataset.deviceSlider!;
    const output = section.querySelector<HTMLOutputElement>(`[data-device-output="${did}"]`);
    const key = `device:${did}`;
    lastCommitted.set(key, Number(slider.value));
    watchDrag(slider);
    slider.addEventListener("input", () => {
      const value = Number(slider.value);
      slider.style.setProperty("--volume", `${value}%`);
      if (output) output.value = String(value);
      const all = Array.from(section.querySelectorAll<HTMLInputElement>("[data-device-slider]"));
      if (master && all.length > 1) {
        const avg = Math.round(all.reduce((sum, s) => sum + Number(s.value), 0) / all.length);
        const mixed = new Set(all.map((s) => s.value)).size > 1;
        master.value = String(avg);
        master.style.setProperty("--volume", `${avg}%`);
        if (masterOutput) masterOutput.value = `${mixed ? "~" : ""}${avg}`;
      }
    });
    slider.addEventListener("change", () => commit(key, [did], slider, output));
  });

  container.querySelector("[data-playback-expand]")?.addEventListener("click", () => {
    expanded = !expanded;
    try {
      localStorage.setItem(EXPANDED_KEY, expanded ? "1" : "0");
    } catch {
      // ignore
    }
    document.dispatchEvent(new CustomEvent("micast:render-playback"));
  });

  container.querySelector("[data-playback-toggle]")?.addEventListener("click", async () => {
    const current = store.get().playback;
    if (!current) return;
    try {
      if (current.playing) await api.pause();
      else await api.play();
      store.set({
        playback: { ...current, playing: !current.playing, paused: current.playing },
      });
      document.dispatchEvent(new CustomEvent("micast:render-playback"));
    } catch (e) {
      store.showToast(`播放控制失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });

  container.querySelector("[data-playback-stop]")?.addEventListener("click", async () => {
    const current = store.get().playback;
    if (!current) return;
    try {
      await api.stopPlayback();
      store.set({
        playback: { ...current, playing: false, paused: false, devices: [] },
      });
      mobileMinimized = false;
      document.dispatchEvent(new CustomEvent("micast:render-playback"));
    } catch (e) {
      store.showToast(`停止失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
