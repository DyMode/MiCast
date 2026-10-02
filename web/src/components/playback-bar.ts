import { api, type NowPlayingTrack, type PlaybackState, type Status } from "../api";
import { icon } from "../icons";
import {
  closePlayerFullscreen,
  openPlayerFullscreen,
  playerState,
  setPlayerView,
  togglePlayerExpanded,
} from "../player/controller";
import { store } from "../state";
import { setVolume } from "../volume-service";
import { targetOwner, volumeTargets } from '../selectors';

// True while the user is holding any slider in the bar; playback pushes must
// not re-render the bar (and reset a slider to the server value) mid-drag.
let dragging = false;
export function isPlaybackBarInteracting(): boolean {
  return dragging;
}

// Transient transition flags (animation classes for one render pass only).
let minimizing = false;
let restoring = false;

// View state (minimized/normal/fullscreen/expanded) is owned by
// player/controller.ts — this module renders and binds from it.

/** The track the player surfaces: the first now-playing entry that has a
 *  title, falling back to one that at least carries lyric lines or a cover.
 *  Everything else (empty object, title-less noise) reads as "no track". */
export function primaryTrack(status: Status | null): NowPlayingTrack | null {
  const tracks = status?.now_playing ?? {};
  let fallback: NowPlayingTrack | null = null;
  const state = store.get();
  const runtime = state.playback?.runtime ?? status?.runtime;
  const owners = new Set(state.playback?.devices.map(device => targetOwner(state, device.did)?.owner).filter(Boolean));
  for (const [owner, track] of Object.entries(tracks)) {
    if (runtime && (owners.size ? !owners.has(owner) : !runtime.sessions.some(
      session => session.owner === owner && ['active', 'quiet', 'paused'].includes(session.state)
    ))) continue;
    if (track.title) return track;
    if (!fallback && (track.lyric_lines?.length || track.cover)) fallback = track;
  }
  return fallback;
}

function coverImg(track: NowPlayingTrack | null, cls: string, label: string): string {
  if (!track?.cover) return "";
  const src = new URL(
    `${track.cover.url}?v=${encodeURIComponent(track.cover.rev)}`,
    document.baseURI,
  ).href;
  return `<img class="${cls}" src="${src}" alt="" aria-hidden="true" loading="lazy"
           title="${escapeHtml(label)}">`;
}

function masterVolume(playback: PlaybackState): number | null {
  if (playback.volume !== null && playback.volume !== undefined) return playback.volume;
  const known = playback.devices.map((d) => d.volume).filter((v): v is number => v !== null && v !== undefined);
  if (!known.length) return null;
  return Math.round(known.reduce((sum, v) => sum + v, 0) / known.length);
}

function sliderMarkup(value: number, attrs: string, label: string): string {
  return `<input type="range" min="0" max="100" value="${value}" ${attrs}
           aria-label="${escapeHtml(label)}" style="--volume:${value}%">`;
}

/** Structural render key for the bar: when unchanged, the markup would be
 *  identical and a rebuild would only flicker (cover reload, slider
 *  resets). Volatile sender fields (title/artist/album — scrolling-lyrics
 *  senders alternate them constantly) and volumes are excluded on purpose:
 *  they refresh in place via refreshPlaybackBar. */
export function playbackBarKey(): string {
  const playback = store.get().playback;
  const track = primaryTrack(store.get().status);
  return JSON.stringify({
    view: playerState.view,
    expanded: playerState.expanded,
    playback: playback
      ? {
          playing: playback.playing,
          paused: playback.paused,
          devices: playback.devices.map((d) => [d.did, d.name, d.muted, targetOwner(store.get(), d.did)?.capabilities]),
        }
      : null,
    // Only the cover's existence/version shapes the bar; the text on it
    // updates dynamically.
    cover: track ? [track.cover?.url ?? null, track.cover?.rev ?? null] : null,
  });
}

/** In-place refresh of the bar's dynamic bits: track lines, slider
 *  positions, transport icon, mute states. Runs whenever the structural key
 *  is unchanged — a sender re-pushing metadata must never rebuild the bar. */
export function refreshPlaybackBar(): void {
  const slot = document.getElementById("playback-slot");
  if (!slot || !slot.innerHTML) return;
  const playback = store.get().playback;
  if (!playback) return;
  const track = primaryTrack(store.get().status);
  const multi = playback.devices.length > 1;
  const target = multi ? `${playback.devices.length} 台音箱` : playback.devices[0].name;
  const stateLabel = playback.playing
    ? `正在输出${playback.mixed_volume ? " · 音量不一致" : ""}`
    : "已暂停输出";
  const titleLine = track?.title ?? target;
  const subLine = track
    ? [track.artist, stateLabel].filter(Boolean).join(" · ")
    : stateLabel;

  const copy = slot.querySelector<HTMLElement>(".now-playing-copy");
  if (copy) {
    const strong = copy.querySelector("strong");
    const span = copy.querySelector("span");
    if (strong) strong.textContent = titleLine;
    if (span) span.textContent = subLine;
  }
  const label = slot.querySelector<HTMLElement>(".header-playback-label");
  if (label) {
    const strong = label.querySelector("strong");
    const small = label.querySelector("small");
    if (strong) strong.textContent = titleLine;
    if (small) small.textContent = subLine;
  }

  const master = slot.querySelector<HTMLInputElement>("[data-master-slider]");
  if (master && document.activeElement !== master) {
    const v = playback.volume ?? 0;
    master.value = String(v);
    master.style.setProperty("--volume", `${v}%`);
  }
  const masterOut = slot.querySelector<HTMLOutputElement>("[data-master-output]");
  if (masterOut) {
    masterOut.value =
      playback.volume === null || playback.volume === undefined
        ? "—"
        : `${playback.mixed_volume ? "~" : ""}${playback.volume}`;
  }
  slot.querySelectorAll<HTMLInputElement>("[data-device-slider]").forEach((slider) => {
    const device = playback.devices.find((d) => d.did === slider.dataset.deviceSlider);
    if (!device || document.activeElement === slider) return;
    const v = device.volume ?? 0;
    slider.value = String(v);
    slider.style.setProperty("--volume", `${v}%`);
    const out = slot.querySelector<HTMLOutputElement>(`[data-device-output="${device.did}"]`);
    if (out) out.value = device.volume === null || device.volume === undefined ? "—" : String(v);
  });

  const toggle = slot.querySelector<HTMLElement>("[data-playback-toggle]");
  if (toggle) {
    toggle.innerHTML = icon(playback.playing ? "pause" : "play");
    toggle.setAttribute("aria-label", playback.playing ? "暂停输出" : "继续输出");
  }
  slot.querySelectorAll<HTMLButtonElement>("[data-mute]").forEach((btn) => {
    const dids = (btn.dataset.mute ?? "").split(",");
    const allMuted = dids.length > 0 && dids.every((did) =>
      playback.devices.find((d) => d.did === did)?.muted
    );
    btn.setAttribute("aria-pressed", String(allMuted));
    btn.innerHTML = icon(allMuted ? "mute" : "speaker");
  });
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
  const track = primaryTrack(store.get().status);
  // Track known: title leads, artist + state follow. Track unknown: the
  // speaker target and state keep the pre-player wording.
  const titleLine = track?.title ?? target;
  const subLine = track
    ? [track.artist, stateLabel].filter(Boolean).join(" · ")
    : stateLabel;
  const shownVolume = volume ?? 0;
  const volumeIds = volumeTargets(store.get(), playback.devices.map(d => d.did));
  const volumeDisabled = volumeIds.length ? '' : 'disabled';
  const volumeOutput = volume === null ? "—" : `${playback.mixed_volume ? "~" : ""}${volume}`;
  const cover = coverImg(track, "now-playing-cover", "全屏播放");

  const detailRows = multi
    ? playback.devices
        .map((d) => {
          const v = d.volume ?? 0;
          const capabilities = targetOwner(store.get(), d.did)?.capabilities;
          const disabled = capabilities?.volume_control === false ? 'disabled' : '';
          const volumeHint = capabilities?.volume_readback === false ? '最近设置的音量；音箱不支持读取' : '音箱音量';
          return `<div class="now-playing-device">
            <span class="now-playing-device-name" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</span>
            <button type="button" class="icon-button compact" ${disabled} data-mute="${escapeHtml(d.did)}" aria-pressed="${d.muted}" aria-label="${d.muted ? "取消静音" : "静音"}" title="${d.muted ? "取消静音" : "静音"}">${icon(d.muted ? "mute" : "speaker")}</button>
            ${sliderMarkup(v, `${disabled} data-device-slider="${escapeHtml(d.did)}"`, `${d.name} 音量`)}
            <output title="${volumeHint}" data-device-output="${escapeHtml(d.did)}">${d.volume === null || d.volume === undefined ? "—" : v}</output>
          </div>`;
        })
        .join("")
    : "";

  return `
    <button class="header-playback-trigger ${playerState.view === "minimized" ? "visible" : ""}" data-playback-restore
            aria-label="打开播放控制：${escapeHtml(target)}" title="打开播放控制">
      ${cover || icon(playback.playing ? "speaker" : "pause")}
      <span class="header-playback-label"><strong>${escapeHtml(titleLine)}</strong><small>${escapeHtml(subLine)}</small></span>
      <span class="header-playback-state ${playback.playing ? "is-playing" : "is-paused"}" aria-hidden="true"></span>
    </button>
    <section class="now-playing visible ${playerState.view === "minimized" ? "is-minimized" : ""} ${restoring ? "restoring" : ""} ${minimizing ? "minimizing" : ""} ${playerState.expanded && multi ? "expanded" : ""}" aria-label="播放控制">
      <div class="now-playing-main">
        <button type="button" class="now-playing-icon" data-player-fullscreen aria-label="全屏播放" title="全屏播放">
          ${cover || icon("speaker")}
        </button>
        <div class="now-playing-copy" title="${escapeHtml(track ? `${titleLine} · ${target}` : target)}">
          <strong>${escapeHtml(titleLine)}</strong>
          <span>${escapeHtml(subLine)}</span>
        </div>
        ${playerState.view === "minimized" ? `<button type="button" class="icon-button compact now-playing-restore" data-playback-restore
                  aria-label="展开播放器" title="展开播放器">${icon(window.matchMedia("(min-width: 1024px)").matches ? "chevron-down" : "chevron-up")}</button>` : ""}
        <label class="volume-control now-playing-master" title="${multi ? "将全部音箱设为同一音量" : "调整这台音箱的音量"}">
          ${sliderMarkup(shownVolume, `${volumeDisabled} data-master-slider`, multi ? "全部音箱音量" : "音箱音量")}
          <output data-master-output>${volumeOutput}</output>
        </label>
        <div class="now-playing-controls">
          <button class="icon-button compact" data-playback-toggle
                  aria-label="${playback.playing ? "暂停输出" : "继续输出"}">
            ${icon(playback.playing ? "pause" : "play")}
          </button>
          <button class="icon-button compact" data-playback-stop aria-label="结束输出" title="结束输出并断开手机连接">
            ${icon("square")}
          </button>
          <button class="icon-button compact" data-playback-minimize aria-label="收起播放器" title="收起播放器">
            ${icon("minimize")}
          </button>
          <button class="icon-button compact" data-player-fullscreen aria-label="全屏播放" title="全屏播放">
            ${icon("maximize")}
          </button>
          <button type="button" class="icon-button compact" ${volumeDisabled} data-mute="${escapeHtml(volumeIds.join(","))}" aria-pressed="${playback.muted}" aria-label="${playback.muted ? "取消全部静音" : "全部静音"}" title="${playback.muted ? "取消全部静音" : "全部静音"}">${icon(playback.muted ? "mute" : "speaker")}</button>
          ${multi ? `<button class="icon-button compact now-playing-expand" data-playback-expand
                  aria-label="${playerState.expanded ? "收起各音箱音量" : "展开各音箱音量"}" aria-expanded="${playerState.expanded}">
            <span>各音箱音量</span>${icon("chevron-down")}
          </button>` : ""}
        </div>
      </div>
      ${multi ? `<div class="now-playing-details" ${playerState.expanded ? "" : "hidden"}>${detailRows}</div>` : ""}
    </section>`;
}

export function bindPlaybackBar(container: HTMLElement) {
  const section = container.querySelector<HTMLElement>(".now-playing");
  if (!section) return;
  // Capture the rendered targets; a delayed request must not jump to a new
  // session if the current playback changes during a drag.
  const targets = (store.get().playback?.devices ?? []).map((d) => d.did);

  container.querySelector("[data-playback-minimize]")?.addEventListener("click", () => {
    if (minimizing) return;
    minimizing = true;
    // Re-render once so the section carries the .minimizing class, then
    // commit the collapsed view after the fade-out has played.
    document.dispatchEvent(new CustomEvent("micast:render-playback"));
    window.setTimeout(() => {
      minimizing = false;
      setPlayerView("minimized");
    }, 180);
  });
  container.querySelectorAll("[data-playback-restore]").forEach((el) => {
    el.addEventListener("click", () => {
      restoring = true;
      setPlayerView("normal");
      // dispatchEvent renders synchronously; the flag only matters for that pass.
      restoring = false;
    });
  });
  // Minimized: the whole card is the restore affordance, not just the cover
  // icon — clicking anywhere (or pressing Enter/Space) expands it again.
  if (section.classList.contains("is-minimized")) {
    section.addEventListener("click", () => setPlayerView("normal"));
    section.setAttribute("role", "button");
    section.setAttribute("aria-label", "展开播放器");
    section.tabIndex = 0;
    section.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        setPlayerView("normal");
      }
    });
  }
  section.querySelectorAll("[data-player-fullscreen]").forEach((el) => {
    el.addEventListener("click", () => {
      // In the minimized card the identity row IS the restore affordance —
      // fullscreen is reachable from the expanded bar.
      if (section.classList.contains("is-minimized")) setPlayerView("normal");
      else openPlayerFullscreen();
    });
  });

  bindVolumeControls(section, targets);

  container.querySelector("[data-playback-expand]")?.addEventListener("click", () => {
    togglePlayerExpanded();
  });

  bindTransportControls(container);
}

/** Play/pause toggle + stop, for any scope carrying the transport buttons
 *  (the bar and the fullscreen overlay). Stop also collapses the player:
 *  with nothing playing there is nothing to minimize or expand. */
export function bindTransportControls(scope: HTMLElement | Document) {
  scope.querySelector("[data-playback-toggle]")?.addEventListener("click", async () => {
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

  scope.querySelector("[data-playback-stop]")?.addEventListener("click", async () => {
    const current = store.get().playback;
    if (!current) return;
    try {
      await api.stopPlayback();
      store.set({
        playback: { ...current, playing: false, paused: false, devices: [] },
      });
      setPlayerView("normal");
      closePlayerFullscreen();
      document.dispatchEvent(new CustomEvent("micast:render-playback"));
    } catch (e) {
      store.showToast(`停止失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });
}

/** Volume sliders for any scope carrying the player's data-attributes
 *  (the bar's drawer and the fullscreen overlay share the markup shape).
 *  Sliders commit ONLY on release (change fires on pointerup / key end);
 *  while the pointer is down the visuals update locally — committing
 *  mid-drag makes the thumb lag behind the finger ("不跟手") as the server
 *  value round-trips back. */
export function bindVolumeControls(section: HTMLElement, targets: string[]) {
  targets = volumeTargets(store.get(), targets);
  const master = section.querySelector<HTMLInputElement>("[data-master-slider]");
  const masterOutput = section.querySelector<HTMLOutputElement>("[data-master-output]");
  const lastCommitted = new Map<string, number>();

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
        if (child.disabled) return;
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
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
