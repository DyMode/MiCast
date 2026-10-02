/** The single sync entry for the player chrome.
 *
 * main.ts used to own this: two update functions, two structural-key
 * caches and the render event listener, all inline. Here there is one
 * entry — syncPlayerChrome() — deciding per surface whether the structure
 * changed (rebuild) or only content did (in-place update). The structural
 * keys and update passes live next to their templates in the components;
 * this module only sequences them.
 */
import {
  bindPlaybackBar,
  isPlaybackBarInteracting,
  playbackBarKey,
  refreshPlaybackBar,
  renderPlaybackBar,
} from "../components/playback-bar";
import {
  bindPlayerOverlay,
  playerOverlayKey,
  refreshPlayerOverlay,
  renderPlayerOverlay,
  teardownPlayerOverlayEq,
} from "../components/player-overlay";
import { closePlayerFullscreen, playerState, resetPlayerTransient } from "./controller";
import { updateMediaSession } from "../media-session";
import { store } from "../state";
import { activePlayback as selectActivePlayback } from '../selectors';
import { syncModal } from "../ui/modal";

let lastBarKey = "";
let lastOverlayKey = "";

function activePlayback() {
  return selectActivePlayback(store.get());
}

function syncBar() {
  const slot = document.getElementById("playback-slot");
  if (!slot) return;
  // Don't clobber the slider (and its value) while the user is dragging it.
  if (isPlaybackBarInteracting()) return;
  const markup = renderPlaybackBar(store.get().playback);
  document.documentElement.classList.toggle("has-playback", Boolean(markup));
  const key = playbackBarKey();
  if (markup && key === lastBarKey && slot.innerHTML) {
    // Same structure: refresh text/sliders/icons in place — sender metadata
    // churn (title flips, volume pushes) must never rebuild the bar.
    refreshPlaybackBar();
    return;
  }
  lastBarKey = key;
  slot.innerHTML = markup;
  bindPlaybackBar(slot);
}

function syncOverlay() {
  const slot = document.getElementById("player-slot");
  if (!slot) return;
  if (isPlaybackBarInteracting()) return;
  const active = Boolean(activePlayback());
  if (playerState.fullscreen && !active) closePlayerFullscreen();
  const open = playerState.fullscreen && active;
  document.documentElement.classList.toggle("player-fullscreen-open", open);
  if (!open) {
    if (slot.innerHTML) slot.innerHTML = "";
    teardownPlayerOverlayEq();
    resetPlayerTransient();
    lastOverlayKey = "";
    if (!document.querySelector('#qr-slot [role="dialog"], [data-ab-modal]:not([hidden])')) syncModal(null);
    return;
  }
  const key = playerOverlayKey();
  if (key !== lastOverlayKey || !slot.innerHTML) {
    lastOverlayKey = key;
    slot.innerHTML = renderPlayerOverlay();
    bindPlayerOverlay(slot);
    refreshPlayerOverlay();
  } else {
    // Same structure: refresh text/sliders in place — a new lyric line or
    // volume push must not rebuild the cover image or the EQ canvas.
    refreshPlayerOverlay();
  }
  if (!document.querySelector('#qr-slot [role="dialog"]')) syncModal(slot.querySelector<HTMLElement>('[role="dialog"]'), closePlayerFullscreen);
}

export function syncPlayerChrome() {
  syncBar();
  document.documentElement.classList.toggle('playback-minimized', Boolean(document.querySelector('#playback-slot .is-minimized')));
  syncOverlay();
  updateMediaSession();
}

document.addEventListener("micast:render-playback", syncPlayerChrome);
