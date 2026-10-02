import { updateLyricViewport } from "./lyric-viewport";
import type { Device, PlaybackState } from "../api";
import { icon } from "../icons";
import {
  closePlayerFullscreen,
  playerState,
  resetPlayerTransient,
  setTuneMenuOpen,
} from "../player/controller";
import { store } from "../state";
import { targetOwner, volumeTargets } from '../selectors';
import { EqCurveCanvas, type CurvePoint } from "./eq-curve-canvas";
import { SpectrumFeed } from "./spectrum-feed";
import { openTuning } from "./tuning-view";
import {
  bindTransportControls,
  bindVolumeControls,
  primaryTrack,
} from "./playback-bar";

// Esc closes the fullscreen player; registered once at module load (the
// overlay DOM is long-lived once open, so the listener lives here).
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && playerState.fullscreen) {
    closePlayerFullscreen();
  }
});

// The EQ visualization is an AMBIENT layer behind the lyrics (the blurred
// cover below both): no grid, no chrome — minimal mode draws only the curve
// and the live spectrum. The spectrum tap is shared machinery with the
// tuning page — the server only runs its FFT while somebody watches.
let overlayEq: EqCurveCanvas | null = null;
const overlayFeed = new SpectrumFeed((bands) => overlayEq?.setSpectrum(bands));

function activePlayback(): PlaybackState | null {
  const playback = store.get().playback;
  if (!playback || playback.devices.length === 0) return null;
  return playback.playing || playback.paused ? playback : null;
}

function selectedEqDevice(playback: PlaybackState): Device | undefined {
  const devices = store.get().devices;
  if (playerState.eqDeviceDid) {
    const chosen = devices.find((d) => d.did === playerState.eqDeviceDid);
    if (chosen && playback.devices.some((d) => d.did === playerState.eqDeviceDid)) return chosen;
  }
  return devices.find((d) => playback.devices.some((p) => p.did === d.did));
}

function eqPoints(device: Device | undefined): CurvePoint[] {
  const points = (device?.eq?.points ?? []).map(([freq, gain]) => ({ freq, gain }));
  // A speaker with EQ disabled (or no EQ data) still gets the flat 0 dB
  // line: honest "no shaping" baseline instead of a blank layer.
  return points.length ? points : [
    { freq: 20, gain: 0 },
    { freq: 20000, gain: 0 },
  ];
}

function contentPresentation(playback: PlaybackState) {
  const state = store.get();
  const track = primaryTrack(state.status);
  const hasLyrics = !!track?.lyric_lines?.some(line => line.trim());
  const hasMetadata = !!(track?.title?.trim() || track?.artist?.trim() || track?.album?.trim() || track?.cover || hasLyrics);
  const runtime = state.playback?.runtime ?? state.status?.runtime;
  const owners = new Set(playback.devices.map(device => targetOwner(state, device.did)?.owner).filter(Boolean));
  const sessions = runtime?.sessions.filter(session => ['active','quiet','paused'].includes(session.state) && (!owners.size || owners.has(session.owner))) ?? [];
  const protocols = new Set(sessions.map(session => session.protocol));
  const protocol = protocols.size === 1 ? [...protocols][0] : '';
  const name = ({airplay:'AirPlay', classic:'AirPlay', airplay2:'AirPlay 2', dlna:'DLNA'} as Record<string,string>)[protocol];
  return {hasLyrics, hasMetadata, source: name ? `正在通过 ${name} 播放` : '正在播放'};
}

function coverImg(cover: { url: string; rev: string }, cls: string): string {
  const src = new URL(`${cover.url}?v=${encodeURIComponent(cover.rev)}`, document.baseURI).href;
  return `<img class="${cls}" src="${src}" alt="" aria-hidden="true" loading="lazy">`;
}

/** Structural render key: when unchanged, the overlay's DOM would rebuild
 *  identically, so a rebuild would only flicker. Sender-volatile fields
 *  (title/artist/album — scrolling-lyrics senders alternate them every few
 *  seconds) are excluded: the identity lines refresh in place. Only the
 *  cover's presence/version, device set, lyric-block presence and the tune
 *  menu shape the DOM. */
export function playerOverlayKey(): string {
  const playback = activePlayback();
  const track = primaryTrack(store.get().status);
  return JSON.stringify({
    open: playerState.fullscreen && Boolean(playback),
    eqDid: playback ? selectedEqDevice(playback)?.did ?? null : null,
    dids: playback?.devices.map((d) => [d.did, targetOwner(store.get(), d.did)?.capabilities]) ?? [],
    cover: track?.cover ? [track.cover.url, track.cover.rev] : null,
    tuneMenu: playerState.tuneMenuOpen && Boolean(playback && playback.devices.length > 1),
  });
}

function tuneMenuMarkup(playback: PlaybackState, currentDid: string | null): string {
  if (!playerState.tuneMenuOpen || playback.devices.length <= 1) return "";
  return `<div class="player-overlay-tune-menu" role="menu" aria-label="选择要调音的音箱">
    ${playback.devices
      .map((d) => {
        const device = store.get().devices.find((dev) => dev.did === d.did);
        const name = device ? device.alias || device.name : d.name;
        return `<button type="button" role="menuitemradio" aria-checked="${d.did === currentDid}"
                 class="player-overlay-tune-item" data-player-tune-did="${escapeHtml(d.did)}">
            ${escapeHtml(name)}${d.did === currentDid ? icon("check") : ""}
          </button>`;
      })
      .join("")}
  </div>`;
}

export function renderPlayerOverlay(): string {
  const playback = activePlayback();
  if (!playback) return "";
  const multi = playback.devices.length > 1;
  const target = multi ? `${playback.devices.length} 台音箱` : playback.devices[0].name;
  const stateLabel = playback.playing
    ? `正在输出${playback.mixed_volume ? " · 音量不一致" : ""}`
    : "已暂停输出";
  const track = primaryTrack(store.get().status);
  const titleLine = track?.title || target;
  const presentation = contentPresentation(playback);
  const metaLine = track ? [track.artist, track.album].filter(Boolean).join(" · ") : "";
  const volume = playback.volume ?? 0;
  const volumeDisabled = volumeTargets(store.get(), playback.devices.map(d => d.did)).length ? '' : 'disabled';
  const volumeOutput = playback.volume === null || playback.volume === undefined
    ? "—"
    : `${playback.mixed_volume ? "~" : ""}${playback.volume}`;
  const eqDevice = selectedEqDevice(playback);

  const deviceRows = playback.devices
    .map((d) => {
      const v = d.volume ?? 0;
      const disabled = targetOwner(store.get(), d.did)?.capabilities?.volume_control === false ? 'disabled' : '';
      return `<div class="now-playing-device">
        <span class="now-playing-device-name" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</span>
        <button type="button" class="icon-button compact" ${disabled} data-mute="${escapeHtml(d.did)}" aria-pressed="${d.muted}" aria-label="${d.muted ? "取消静音" : "静音"}" title="${d.muted ? "取消静音" : "静音"}">${icon(d.muted ? "mute" : "speaker")}</button>
        <input type="range" ${disabled} min="0" max="100" value="${v}" data-device-slider="${escapeHtml(d.did)}"
               aria-label="${escapeHtml(d.name)} 音量" style="--volume:${v}%">
        <output data-device-output="${escapeHtml(d.did)}">${d.volume === null || d.volume === undefined ? "—" : v}</output>
      </div>`;
    })
    .join("");

  return `
    <div class="player-overlay" role="dialog" aria-modal="true" aria-label="正在播放">
      ${track?.cover ? `<div class="player-overlay-bg" aria-hidden="true">${coverImg(track.cover, "player-overlay-bg-img")}</div>` : ""}

      <div class="player-overlay-panel">
        <button type="button" class="player-overlay-dismiss" data-player-close aria-label="退出全屏" title="退出全屏">
          ${icon("chevron-down")}
        </button>
        <div class="player-overlay-body" data-player-layout="${presentation.hasLyrics ? 'lyrics' : 'centered'}">
          <div class="player-overlay-identity">
            ${track?.cover ? coverImg(track.cover, "player-overlay-cover") : `<div class="player-overlay-cover player-overlay-cover-fallback" aria-hidden="true">${icon("speaker")}</div>`}
            <div class="player-overlay-track">
              <strong data-player-title>${escapeHtml(titleLine)}</strong>
              <span data-player-meta ${metaLine ? "" : "hidden"}>${escapeHtml(metaLine)}</span>
              <small data-player-state>${escapeHtml(presentation.hasMetadata ? `${target} · ${stateLabel}` : playback.playing ? presentation.source : stateLabel)}</small>
              <small class="player-overlay-content-hint" data-player-content-hint ${presentation.hasMetadata ? "hidden" : ""}>播放源暂未提供歌曲信息</small>
            </div>
          </div>
          <div class="player-overlay-lyrics" data-player-lyrics aria-live="off" ${presentation.hasLyrics ? "" : "hidden"}></div>
        </div>
        <div class="player-overlay-dock">
          ${tuneMenuMarkup(playback, eqDevice?.did ?? null)}
          <div class="player-overlay-transport">
            <button class="icon-button" data-playback-toggle
                    aria-label="${playback.playing ? "暂停输出" : "继续输出"}">
              ${icon(playback.playing ? "pause" : "play")}
            </button>
            <button class="icon-button" data-playback-stop aria-label="结束输出" title="结束输出并断开手机连接">
              ${icon("square")}
            </button>
            <button class="icon-button" data-player-tune="${escapeHtml(eqDevice?.did ?? "")}"
                    aria-label="${multi ? "选择音箱调音" : "调音"}" title="${multi ? "调音（选择音箱）" : "调音（打开调音台）"}">
              ${icon("sliders")}
            </button>
          </div>
          <div class="player-overlay-volume">
            ${multi ? `<label class="volume-control" title="将全部音箱设为同一音量">
              <span class="volume-scope">全部音箱</span>
              <input type="range" ${volumeDisabled} min="0" max="100" value="${volume}" data-master-slider
                     aria-label="全部音箱音量" style="--volume:${volume}%">
              <output data-master-output>${volumeOutput}</output>
            </label>` : ""}
            ${multi ? `<details class="player-volume-details"><summary>各音箱音量 · ${playback.devices.length} 台</summary>${deviceRows}</details>` : deviceRows}
          </div>
        </div>
      </div>
    </div>`;
}

/** Dynamic in-place updates while the overlay is open: lyric lines, identity
 *  lines, slider positions, transport icon, state label. Never touches the
 *  cover image or the EQ canvas, so sender metadata churn and new lyric
 *  lines don't rebuild anything. */
export function refreshPlayerOverlay(): void {
  const slot = document.getElementById("player-slot");
  if (!slot || !slot.innerHTML) return;
  const playback = activePlayback();
  if (!playback) return;
  const track = primaryTrack(store.get().status);
  const multi = playback.devices.length > 1;
  const target = multi ? `${playback.devices.length} 台音箱` : playback.devices[0].name;
  const stateLabel = playback.playing
    ? `正在输出${playback.mixed_volume ? " · 音量不一致" : ""}`
    : "已暂停输出";

  const titleEl = slot.querySelector<HTMLElement>("[data-player-title]");
  if (titleEl) titleEl.textContent = track?.title || target;
  const metaEl = slot.querySelector<HTMLElement>("[data-player-meta]");
  if (metaEl) {
    const meta = track ? [track.artist, track.album].filter(Boolean).join(" · ") : "";
    metaEl.textContent = meta;
    metaEl.hidden = !meta;
  }
  const stateEl = slot.querySelector<HTMLElement>("[data-player-state]");
  const presentation = contentPresentation(playback);
  if (stateEl) stateEl.textContent = presentation.hasMetadata ? `${target} · ${stateLabel}` : playback.playing ? presentation.source : stateLabel;
  const hint = slot.querySelector<HTMLElement>('[data-player-content-hint]');
  if (hint) hint.hidden = presentation.hasMetadata;

  const lyricsBox = slot.querySelector<HTMLElement>("[data-player-lyrics]");
  const body = slot.querySelector<HTMLElement>('[data-player-layout]');
  const identity = slot.querySelector<HTMLElement>('.player-overlay-identity');
  const layout = presentation.hasLyrics ? 'lyrics' : 'centered';
  const changing = body?.dataset.playerLayout !== layout;
  const before = changing ? identity?.getBoundingClientRect() : null;
  if (changing) identity?.getAnimations().forEach(animation => animation.cancel());
  if (body) body.dataset.playerLayout = layout;
  if (lyricsBox) {
    lyricsBox.hidden = !presentation.hasLyrics;
    updateLyricViewport(lyricsBox, track?.lyric_lines ?? []);
  }
  if (changing && before && identity && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const after = identity.getBoundingClientRect();
    identity.animate([
      {transform: `translateX(${before.x + before.width / 2 - after.x - after.width / 2}px)`, opacity: .8},
      {transform: 'translateX(0)', opacity: 1},
    ], {duration: 250, easing: 'ease-out'});
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

  const toggle = slot.querySelector<HTMLElement>("[data-playback-toggle]");
  if (toggle) {
    toggle.innerHTML = icon(playback.playing ? "pause" : "play");
    toggle.setAttribute("aria-label", playback.playing ? "暂停输出" : "继续输出");
  }
}

export function teardownPlayerOverlayEq(): void {
  overlayEq?.destroy();
  overlayEq = null;
  overlayFeed.attach(null);
  resetPlayerTransient();
}

export function bindPlayerOverlay(container: HTMLElement) {
  container.querySelector("[data-player-close]")?.addEventListener("click", () => {
    closePlayerFullscreen();
  });
  const panel = container.querySelector<HTMLElement>(".player-overlay-panel");
  if (!panel) {
    teardownPlayerOverlayEq();
    return;
  }
  bindVolumeControls(panel, (store.get().playback?.devices ?? []).map((d) => d.did));
  bindTransportControls(panel);

  // The ambient EQ canvas: instantiated whenever it exists, pointer-dead.
  overlayEq?.destroy();
  overlayEq = null;
  const canvas = container.querySelector<HTMLCanvasElement>("[data-eq-canvas]");
  const playback = activePlayback();
  const eqDevice = playback ? selectedEqDevice(playback) : undefined;
  if (canvas && eqDevice) {
    overlayEq = new EqCurveCanvas(canvas, {
      points: eqPoints(eqDevice),
      readOnly: true,
      minimal: true,
    });
    overlayFeed.attach(eqDevice.did);
  } else {
    overlayFeed.attach(null);
  }

  // 调音: one playing device jumps straight to its tuning page; a group
  // opens the little device menu first (B). Selecting jumps and closes.
  const tuneButton = panel.querySelector<HTMLElement>("[data-player-tune]");
  tuneButton?.addEventListener("click", () => {
    const did = tuneButton.dataset.playerTune;
    if (!did) return;
    const current = activePlayback();
    if (current && current.devices.length > 1 && !playerState.tuneMenuOpen) {
      setTuneMenuOpen(true);
      return;
    }
    resetPlayerTransient();
    // Mount the tuning page FIRST, then drop the overlay in the same tick —
    // no flash of the section page underneath.
    openTuning(did);
    closePlayerFullscreen();
  });
  panel.querySelectorAll<HTMLElement>("[data-player-tune-did]").forEach((item) => {
    item.addEventListener("click", () => {
      const did = item.dataset.playerTuneDid;
      resetPlayerTransient();
      if (!did) return;
      openTuning(did);
      closePlayerFullscreen();
    });
  });
}

// Tuning edits (this page or elsewhere) refresh the read-only curve live.
window.addEventListener("micast:tuning-change", () => {
  if (!playerState.fullscreen) return;
  const playback = activePlayback();
  overlayEq?.setPoints(eqPoints(playback ? selectedEqDevice(playback) : undefined));
});

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
