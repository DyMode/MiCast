/**
 * Tiny reactive store with UI persistence.
 */

import { safeUserMessage } from "./errors";
import type { AccessStatus, AirPlay2State, AudioConfig, Device, FullConfig, PlaybackState, ReceiverInfo, XiaomiStatus } from "./api";
import type { DebugState } from "./components/debug-panel";

export type Theme = "light" | "dark" | "auto";
export type Section = "receivers" | "devices" | "account" | "settings" | "debug" | "airplay2" | "topology";
export type AirPlay2Tab = "overview" | "instances" | "mappings";
export type ReceiverMode = "single" | "multi";

export interface State {
  access: AccessStatus | null;
  onboardingStep: "access" | "xiaomi" | "receivers" | "airplay2" | "complete";
  recoveryDismissed: boolean;
  status: import("./api").Status | null;
  audio: AudioConfig | null;
  fullConfig: FullConfig | null;
  devices: Device[];
  deviceLoadError: string | null;
  receivers: ReceiverInfo[];
  saving: boolean;
  playback: PlaybackState | null;
  qr: {
    open: boolean;
    qrUrl: string | null;
    scanToken: string | null;
    state: "idle" | "waiting" | "scanned" | "confirmed" | "expired" | "error";
    /** Why the QR could not be produced; set with state "error". */
    error?: string | null;
  };
  xiaomi: XiaomiStatus;
  debug: DebugState | null;
  airplay2: AirPlay2State | null;
  toast: {
    message: string;
    visible: boolean;
  };
  ui: {
    theme: Theme;
    activeSection: Section;
    expandedDeviceDid: string | null;
    /** Speaker open in the full-screen tuning page; null = normal sections. */
    tuningDid: string | null;
    /** Tuning page: whether the advanced section (target/calibrate/A-B/scenes) is expanded. */
    tuningAdvanced: boolean;
    confirmingReceiverId: string | null;
    airplay2Tab: AirPlay2Tab;
  };
}

export interface Receiver {
  did: string;
  name: string;
  status: "idle" | "running" | "error";
  streamUrl: string;
}

type Subscriber = (state: State, prev: State) => void;

const UI_STORAGE_KEY = "micast-ui";

function loadUiState(): State["ui"] {
  try {
    const raw = localStorage.getItem(UI_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      return {
        theme: ["light", "dark", "auto"].includes(parsed.theme) ? parsed.theme : "auto",
        activeSection: ["receivers", "devices", "account", "settings", "debug", "airplay2", "topology"].includes(
          parsed.activeSection
        )
          ? parsed.activeSection
          : "topology",
        // Expansion is a transient reading state, not a preference: restoring
        // the last-open card on load made one speaker appear to open itself.
        expandedDeviceDid: null,
        tuningDid: null,
        tuningAdvanced: Boolean(parsed.tuningAdvanced),
        confirmingReceiverId: null,
    airplay2Tab: ["overview", "instances", "mappings"].includes(parsed.airplay2Tab) ? parsed.airplay2Tab : "overview",
      };
    }
  } catch {
    // ignore
  }
  return {
    theme: "auto",
    activeSection: "topology",
    expandedDeviceDid: null,
    tuningDid: null,
    tuningAdvanced: false,
    confirmingReceiverId: null,
    airplay2Tab: "overview",
  };
}

const initialState: State = {
  access: null,
  onboardingStep: "access",
  recoveryDismissed: false,
  status: null,
  audio: null,
  fullConfig: null,
  devices: [],
  deviceLoadError: null,
  receivers: [],
  saving: false,
  playback: null,
  qr: {
    open: false,
    qrUrl: null,
    scanToken: null,
    state: "idle",
  },
  xiaomi: { logged_in: false, user_id: null },
  debug: null,
  airplay2: null,
  toast: { message: "", visible: false },
  ui: loadUiState(),
};

class Store {
  private state: State = { ...initialState };
  private subscribers: Subscriber[] = [];
  private toastTimer: number | null = null;

  get(): State {
    return this.state;
  }

  set(partial: Partial<State>) {
    const prev = this.state;
    this.state = { ...prev, ...partial };
    this.subscribers.forEach((fn) => fn(this.state, prev));
  }

  setUi(partial: Partial<State["ui"]>) {
    const prev = this.state;
    const next = { ...this.state.ui, ...partial };
    this.state = { ...this.state, ui: next };
    try {
      localStorage.setItem(UI_STORAGE_KEY, JSON.stringify(next));
    } catch {
      // ignore
    }
    this.subscribers.forEach((fn) => fn(this.state, prev));
  }

  subscribe(fn: Subscriber) {
    this.subscribers.push(fn);
    fn(this.state, this.state);
    return () => {
      this.subscribers = this.subscribers.filter((s) => s !== fn);
    };
  }

  showToast(message: string, duration = 2500) {
    message = safeUserMessage(message);
    if (this.toastTimer != null) window.clearTimeout(this.toastTimer);
    this.set({ toast: { message, visible: true } });
    window.dispatchEvent(new CustomEvent("micast:toast", { detail: { message, visible: true } }));
    this.toastTimer = window.setTimeout(() => {
      this.toastTimer = null;
      this.set({ toast: { message: "", visible: false } });
      window.dispatchEvent(
        new CustomEvent("micast:toast", { detail: { message: "", visible: false } })
      );
    }, duration);
  }
}

export const store = new Store();
