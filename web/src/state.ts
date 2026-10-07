/**
 * Tiny reactive store with UI persistence.
 */

import { safeUserMessage } from "./errors";
import type { AccessStatus, AirPlay2State, AudioConfig, Device, FullConfig, PlaybackState, ReceiverInfo, XiaomiStatus } from "./api";
import type { DebugState } from "./components/debug-panel";

export type Theme = "light" | "dark" | "auto";
export type Section = "receivers" | "devices" | "account" | "settings" | "advanced" | "debug" | "airplay2" | "topology";
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
        activeSection: ["receivers", "devices", "account", "settings", "advanced", "debug", "airplay2", "topology"].includes(
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
  private retiredRuntimeEpochs = new Set<string>();
  private runtimeEpoch: string | undefined;
  private runtimeRevision = -1;
  private runtimeSequences = new Map<string, number>();
  private readSequences = new Map<string, number>();
  private accountRevision = 0;

  invalidateAccountReads() { this.accountRevision++; }

  beginRead(channel: string, accountBound = false) {
    const sequence = (this.readSequences.get(channel) ?? 0) + 1;
    this.readSequences.set(channel, sequence);
    const account = this.accountRevision;
    return () => this.readSequences.get(channel) === sequence &&
      (!accountBound || account === this.accountRevision);
  }

  acceptRuntime(runtime: import('./api').RuntimeState | undefined, channel: string, publishEpoch = true): boolean {
    if (!runtime) return this.runtimeEpoch === undefined;
    if (this.runtimeEpoch && !runtime.epoch) return false;
    if (runtime.epoch && this.retiredRuntimeEpochs.has(runtime.epoch)) return false;
    if (runtime.epoch !== this.runtimeEpoch) {
      if (this.runtimeEpoch) this.retiredRuntimeEpochs.add(this.runtimeEpoch);
      this.runtimeEpoch = runtime.epoch;
      this.runtimeRevision = -1;
      this.runtimeSequences.clear();
      if (publishEpoch) {
        // A topology snapshot can be the first message after a server restart.
        // Retire projections before observers select an owner from old data.
        this.set({ status: null, playback: null });
      }
    }
    if (runtime.revision < this.runtimeRevision) return false;
    const sequence = runtime.sequence ?? runtime.revision;
    if (sequence < (this.runtimeSequences.get(channel) ?? -1)) return false;
    this.runtimeRevision = runtime.revision;
    this.runtimeSequences.set(channel, sequence);
    return true;
  }

  get(): State {
    return this.state;
  }

  get runtimeGeneration() { return this.runtimeEpoch; }

  updateDeviceTuning(did: string, eq: Device['eq']) {
    this.set({ devices: this.state.devices.map(device => device.did === did ? { ...device, eq } : device) });
  }

  set(partial: Partial<State>) {
    const prev = this.state;
    if (partial.xiaomi && (partial.xiaomi.user_id !== prev.xiaomi.user_id ||
      partial.xiaomi.logged_in !== prev.xiaomi.logged_in)) {
      this.invalidateAccountReads();
      // An independent status refresh may observe login before QR confirmation.
      // Retire the obsolete sheet together with its account-bound callbacks.
      if (prev.qr.open && partial.qr === undefined) partial = {...partial, qr: {...prev.qr, open:false}};
    }
    if (partial.status) {
      if (!this.acceptRuntime(partial.status.runtime, 'status', false)) {
        partial = { ...partial, status: prev.status, ...(partial.receivers ? { receivers: prev.receivers } : {}) };
      } else if (partial.status.runtime?.epoch && prev.playback?.runtime?.epoch &&
        partial.status.runtime.epoch !== prev.playback.runtime.epoch && partial.playback === undefined) {
        partial = { ...partial, playback: null };
      }
    }
    if (partial.playback && !this.acceptRuntime(partial.playback.runtime, 'playback', false)) {
      partial = { ...partial, playback: prev.playback };
    } else if (partial.playback?.runtime?.epoch && prev.status?.runtime?.epoch &&
      partial.playback.runtime.epoch !== prev.status.runtime.epoch && partial.status === undefined) {
      partial = { ...partial, status: null };
    }
    const config = partial.fullConfig;
    if (config && ((config.runtime_epoch && this.retiredRuntimeEpochs.has(config.runtime_epoch)) ||
      (config.runtime_epoch === prev.fullConfig?.runtime_epoch &&
        (config.config_revision ?? 0) < (prev.fullConfig?.config_revision ?? 0)))) {
      partial = { ...partial, fullConfig: prev.fullConfig, ...(partial.audio ? { audio: prev.audio } : {}) };
    }
    this.state = { ...prev, ...partial };
    if (this.runtimeEpoch) {
      if (this.state.status && this.state.status.runtime?.epoch !== this.runtimeEpoch) this.state.status = null;
      if (this.state.playback && this.state.playback.runtime?.epoch !== this.runtimeEpoch) this.state.playback = null;
    }
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
