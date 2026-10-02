/** Single owner of the player chrome's mutable view state.
 *
 * Root fix, not a patch: previously `view`/`fullscreen`/`expanded` lived in
 * playback-bar.ts, `eqDid`/`tuneMenuOpen` in player-overlay.ts and the
 * render caches in main.ts — five pieces of state with implicit coupling
 * across three files. Every piece lives HERE now; the components import
 * these accessors, and the one thing they may do to state is call the
 * setters below (which fan out the single re-render event).
 *
 * Templates, structural signatures and in-place updates stay colocated
 * with their markup in playback-bar.ts / player-overlay.ts — each
 * rendered field has exactly one home: either its component's signature
 * (structure) or its component's update pass (everything else).
 */

export type PlayerView = "minimized" | "normal";

const VIEW_KEY = "micast-player-view";
const EXPANDED_KEY = "micast-playback-expanded";

function loadView(): PlayerView {
  try {
    const saved = localStorage.getItem(VIEW_KEY);
    if (saved === "minimized" || saved === "normal") return saved;
    return window.matchMedia('(max-width: 1023px)').matches ? "minimized" : "normal";
  } catch {
    return "normal";
  }
}

function loadExpanded(): boolean {
  try {
    return localStorage.getItem(EXPANDED_KEY) === "1";
  } catch {
    return false;
  }
}

// minimized/normal persist across reloads (a quiet preference); fullscreen
// is transient — reopening the app must never surprise you with a takeover.
let view: PlayerView = loadView();
let fullscreen = false;
let expanded = loadExpanded();
let eqDeviceDid: string | null = null;
let tuneMenuOpen = false;

export const playerState = {
  get view(): PlayerView {
    return view;
  },
  get fullscreen(): boolean {
    return fullscreen;
  },
  get expanded(): boolean {
    return expanded;
  },
  get eqDeviceDid(): string | null {
    return eqDeviceDid;
  },
  get tuneMenuOpen(): boolean {
    return tuneMenuOpen;
  },
};

function notify() {
  document.dispatchEvent(new CustomEvent("micast:render-playback"));
}

export function setPlayerView(next: PlayerView): void {
  view = next;
  try {
    localStorage.setItem(VIEW_KEY, next);
  } catch {
    // ignore
  }
  notify();
}

export function openPlayerFullscreen(): void {
  if (fullscreen) return;
  fullscreen = true;
  notify();
}

export function closePlayerFullscreen(): void {
  if (!fullscreen) return;
  fullscreen = false;
  notify();
}

export function togglePlayerExpanded(): void {
  expanded = !expanded;
  try {
    localStorage.setItem(EXPANDED_KEY, expanded ? "1" : "0");
  } catch {
    // ignore
  }
  notify();
}

export function setEqDeviceDid(did: string | null): void {
  eqDeviceDid = did;
  notify();
}

export function setTuneMenuOpen(open: boolean): void {
  tuneMenuOpen = open;
  notify();
}

/** Any close path resets the transient menu with the overlay. */
export function resetPlayerTransient(): void {
  tuneMenuOpen = false;
}
