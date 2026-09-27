/**
 * Minimal typed API client for MiCast.
 */

import { appUrl } from "./paths";
import { safeUserMessage } from "./errors";

/** The failure path shared by every request, JSON or not: the log endpoints
 *  answer with a file body, so they cannot go through apiFetch's `res.json()`. */
async function apiError(res: Response): Promise<Error> {
  const text = await res.text().catch(() => "");
  if (res.status === 401 && text.includes("需要登录 MiCast")) {
    window.dispatchEvent(new Event("micast:access-required"));
  }
  let detail = "";
  try {
    const payload = JSON.parse(text) as { detail?: unknown };
    if (typeof payload.detail === "string") detail = payload.detail;
  } catch {
    detail = text;
  }
  return new Error(safeUserMessage(detail));
}

async function apiFetch(path: string, init?: RequestInit) {
  const res = await fetch(appUrl(path), init);
  if (!res.ok) throw await apiError(res);
  return res.json();
}

/** Count of records the server put in the body, for a toast that does not have
 *  to parse the file back out of the blob. */
function logCountHeader(res: Response): number {
  return Number(res.headers.get("X-MiCast-Log-Count")) || 0;
}

export interface AccessStatus {
  access_configured: boolean;
  setup_complete: boolean;
  auth_enabled: boolean;
  username: string;
  authenticated: boolean;
}

export interface AudioConfig {
  format: "mp3" | "flac" | "wav";
  bitrate: "128k" | "192k" | "320k";
  sample_rate: 44100 | 48000;
  auto_transcode: boolean;
}

export interface AppConfig {
  name: string;
}

export type ReceiverMode = "single" | "multi";
export type AirPlayProtocol = "auto" | "classic" | "airplay2";
export type AirPlayEngine = "local" | "airplay2";

export interface FullConfig {
  deployment: string;
  audio: AudioConfig;
  app: AppConfig;
  receiver_mode: ReceiverMode;
  airplay_protocol: AirPlayProtocol;
  airplay_engine: AirPlayEngine;
  dlna_enabled: boolean;
  sync_groups_enabled: boolean;
  large_delay_enabled: boolean;
  touchscreen_lyrics: boolean;
  default_volume: number;
  default_volume_enabled: boolean;
  stale_session_timeout: number;
  sender_volume_mode: "independent" | "linked";
  notify_webhook_url: string;
  airplay2_enabled: boolean;
  network_discovery_enabled: boolean;
  airplay2_available: boolean;
  airplay2_mode: "disabled" | "single" | "multi";
  airplay2_can_add_instances: boolean;
  storage: {
    mode: "managed" | "portable" | "installed" | "development";
    data_dir: string;
    log_dir: string;
  };
  dlna_status: { status: string; detail: string };
  selected_device_id: string | null;
  ports?: PortStatus[];
  /** Present on newer backends; AirPlay 2 instances are entries too. */
  airplay2_instances?: Array<{ id: string; name: string }>;
  receivers: ReceiverDefinition[];
  groups: SpeakerGroup[];
  speaker_names: Record<string, string>;
}

export interface PortStatus {
  id: string;
  name: string;
  protocol: "tcp" | "udp";
  mode: "auto" | "custom" | "env" | "fixed";
  preferred: number | null;
  actual: number | number[] | null;
  status: "listening" | "hosted" | "off" | "error";
  detail: string;
  editable: boolean;
}

export interface UpdateInfo {
  current_version: string;
  latest_version: string;
  update_available: boolean;
  release_url: string;
  release_notes: string;
  published_at: string | null;
  can_download: boolean;
  asset: { name: string; size: number; download_url: string } | null;
  checked_at: number;
}

export interface UpdateDownloadStatus {
  state: "idle" | "downloading" | "done" | "error";
  progress: number;
  total: number;
  path: string | null;
  error: string | null;
}

export interface AirPlay2Instance {
  id: string;
  name: string;
  enabled: boolean;
  status: string;
  detail: string;
  target_type: "speaker" | "group";
  target_id: string | null;
  target_name: string;
}

export interface AirPlay2State {
  enabled: boolean;
  mode: "disabled" | "single" | "multi";
  can_add_instances: boolean;
  instances: AirPlay2Instance[];
  orchestration: {
    available: boolean;
    status: "running" | "error" | "disabled" | "unavailable";
    detail: string;
  };
  summary: {
    instances_running: number;
    instances_total: number;
    mappings_healthy: number;
    mappings_total: number;
  };
  targets: Array<{ type: "speaker" | "group"; id: string; name: string }>;
}

export interface ReceiverDefinition {
  id: string;
  name: string;
  target_type: "selected" | "speaker" | "group";
  target_id: string | null;
  enabled: boolean;
}

export interface SpeakerGroup {
  id: string;
  name: string;
  speaker_ids: string[];
  // Signed offset (ms) per member, relative to `anchor_did`. Positive = later,
  // negative = earlier. The anchor's own offset is always 0 (and omitted).
  delays_ms: Record<string, number>;
  anchor_did: string | null;
  mode: "mirror" | "stereo";
  channels: Record<string, "left" | "right" | "both">;
  gains_db: Record<string, number>;
  airplay_targets?: string[];
  dlna_targets?: string[];
  network_channels?: Record<string, "left" | "right">;
}

export interface CodecCompatibility {
  members: Record<string, Record<string, boolean>>;
  labels?: Record<string, string>;
  formats?: string[];
  possible_common_formats: string[];
  confirmed_common_formats: string[];
  recommended_format: string | null;
  status: "confirmed" | "needs_check" | "incompatible";
  unknown_members: string[];
  /** Formats whose only verdict is older than the trust window. */
  stale_formats?: Record<string, string[]>;
}

export type NetworkDeviceKind = "speaker" | "tv" | "projector";

export interface NetworkDevice {
  volume_control?: boolean;
  volume_readback?: boolean;
  id: string;
  name: string;
  host?: string;
  port?: number;
  model: string;
  kind: NetworkDeviceKind;
  online: boolean;
  supported: boolean;
  unsupported_reason: string;
  attached_group: string | null;
  stream_status: string;
  stream_detail: string;
}

export interface Status {
  status: string;
  pcm_source: string;
  audio: AudioConfig;
  stream_url: string;
  error_count: number;
  receivers: ReceiverInfo[];
  airplay_protocol: AirPlayProtocol;
  airplay_engine: AirPlayEngine;
  orchestration: {
    configured: boolean;
    status: string;
    detail: string;
  };
  diagnostics: {
    raop: Record<string, { active_sessions: number; total_sessions: number; decode_errors: number; dropped_packets: number; resend_requests: number; timing_requests: number; timing_responses: number; input_buffer_ms: number }>;
    streams: Record<string, { clients: number; bytes_sent: number; dropped_chunks: number; flowing: boolean; latency: LatencyMetrics }>;
    sinks: Record<string, Record<string, SinkLatencyMetrics>>;
  };
}

export interface XiaomiStatus {
  logged_in: boolean;
  user_id: string | null;
  /** "unstable": tokens are stored but the cloud is not answering (see
   * XiaomiAuth.cloud_degraded) — the account may be fine, so offer a re-login
   * instead of silently showing an empty speaker list. */
  status?: "connected" | "unstable" | "expired" | "disconnected" | "never_connected";
  cloud?: { failures: number; last_ok_at: number; last_failure_at: number };
  ever_logged_in?: boolean;
}

export interface SpeakerEq {
  enabled: boolean;
  /** Curve control points as [freqHz, gainDb] pairs, sorted by freq. */
  points: [number, number][];
  preset: string;
  target?: string;
  night_mode?: boolean;
  loudness_comp_enabled?: boolean;
  content_profile?: string;
  /** Saved per-speaker scene curves keyed by profile name. */
  profiles?: Record<string, [number, number][]>;
  revision?: number;
  undo_available?: boolean;
}

export interface EqPresetsResponse {
  presets: Record<string, [number, number][]>;
  targets: Record<string, [number, number][]>;
  /** Global user-named curve library. */
  saved?: Record<string, [number, number][]>;
  freq_range: [number, number];
  gain_range: [number, number];
}

export interface Device {
  did: string;
  name: string;
  alias: string;
  model: string;
  presence?: string;
  play_error?: string | null;
  codec_capabilities?: Record<string, boolean>;
  /** Verdict per format, keyed by the app's own names (mp3/flac/wav/pcm).
   * "unverified": the speaker pulled the stream but raw passthrough cannot be
   * proven from the server side, so no verdict is claimed. */
  codec_capability_details?: Record<
    string,
    {
      status: "supported" | "unsupported" | "unverified";
      verified_at: number;
      reason?: string;
      label?: string;
    }
  >;
  /** The formats the app can serve, in canonical order, with display labels. */
  codec_formats?: string[];
  codec_labels?: Record<string, string>;
  playing?: boolean;
  muted?: boolean;
  enabled: boolean;
  selected: boolean;
  volume?: number | null;
  eq?: SpeakerEq;
}

export interface ReceiverInfo {
  did: string;
  name: string;
  status: "idle" | "running" | "error";
  stream_url: string;
  detail?: string;
}

export interface PlaybackState {
  playing: boolean;
  paused: boolean;
  volume: number | null;
  mixed_volume: boolean;
  muted: boolean;
  devices: Array<{
    did: string;
    name: string;
    volume: number | null;
    playing: boolean;
    paused: boolean;
    muted: boolean;
    state: "playing" | "paused" | "idle";
  }>;
}

/** Cumulative audio-path counters plus a rolling event log ("black box"):
 * connection-scoped counters reset on every speaker reconnect, so a periodic
 * stutter would otherwise be invisible in the live status. */
export interface AudioPathMetrics {
  encode: { chunks: number; p50_ms: number; p95_ms: number; max_ms: number; stalls: number; stall_max_ms: number };
  encoder_gap: { count: number; max_ms: number };
  source: {
    stalls: number;
    stall_max_ms: number;
    silence_fills: number;
    /** Holes between delivered chunks: a bursty sender's fingerprint. */
    gaps?: number;
    gap_max_ms?: number;
  };
  client: {
    connects: number;
    reconnects: number;
    lag_skips: number;
    lag_skip_ms_max: number;
    lag_skip_bytes: number;
    queue_drops: number;
    queue_peak_items: number;
    queue_peak_ms: number;
  };
  drops: {
    tee: number;
    tee_by_entry?: Record<string, number>;
    encoder_in: number;
    encoder_out: number;
  };
  pace?: { sleeps: number; total_ms: number; max_ms: number };
  /** Process CPU and event-loop lag: whether OUR side is the bottleneck. */
  runtime?: {
    cpu_percent: number;
    cores: number;
    loop_lag_ms: number;
    loop_lag_max_ms: number;
  };
  events: Array<{
    at: number;
    kind: string;
    ms: number | null;
    detail: string | null;
    /** Entry the event belongs to, plus the user-facing phrasing. */
    entry?: string | null;
    label?: string | null;
  }>;
}

/** One entry's health as the audio supervisor sees it. */
export interface EntryHealthSnapshot {
  state: "idle" | "starting" | "healthy" | "quiet" | "paused" | "degraded_our_side" | "degraded_speaker" | "bursty" | "unhealthy";
  for_s: number;
  reason: string;
  escalations: number;
  last_action: string;
  last_action_ok: boolean | null;
  buffer_override_s: number | null;
}

export interface DebugState {
  logged_in: boolean;
  selected_device_id: string | null;
  devices: Array<{ did: string; name: string; hardware: string; presence: string; miotDID: string }>;
  /** Devices come from the last successful cloud list, not a fresh fetch: the
   * diagnostics page and the report must work while the cloud is unreachable. */
  devices_cached?: boolean;
  /** Consecutive failed cloud calls and when the last one worked. */
  cloud?: { failures: number; last_ok_at: number; last_failure_at: number };
  pcm_source: string;
  stream_url: string;
  audio_config: AudioConfig;
  bridge_status: { status: string; error_count: number };
  stream_clients: number;
  stream_bytes_sent: number;
  diagnostics: {
    raop: Record<string, { active_sessions: number; total_sessions: number; decode_errors: number; dropped_packets: number; resend_requests: number; timing_requests: number; timing_responses: number; input_buffer_ms: number }>;
    streams: Record<string, {
      clients: number;
      bytes_sent: number;
      dropped_chunks: number;
      /** Cross-format drop estimate in milliseconds. */
      dropped_ms?: number;
      flowing: boolean;
      latency: LatencyMetrics;
      /** AirPlay 2 has no RAOP session, so its input figure comes from the
       * pipeline's own pacing (fed audio leading the wall clock). */
      input_buffer_ms?: number;
      input?: { ahead_ms: number; buffered_ms: number; starved_ms: number };
      /** Pacing sleep: how long the pump held itself back to realtime. */
      pace?: { sleeps: number; total_ms: number; max_ms: number };
      /** Branch buffer depth vs its time budget (where paced pumps can lose PCM). */
      tee?: { depth_ms: number; capacity_ms: number; dropped: number };
      pipeline_drops?: { in: number; out: number };
    }>;
    /** Live sender sessions split by ingress; absent on older backends. */
    sessions?: { active: string[]; classic: string[]; airplay2: string[] };
    /** Cumulative audio-path black box; absent on older backends. */
    audio?: AudioPathMetrics;
    /** Per-entry health from the audio supervisor; absent on older backends. */
    entries?: Record<string, EntryHealthSnapshot>;
    /** Per-speaker delay-line state; absent on older backends. */
    sinks?: Record<string, Record<string, SinkLatencyMetrics>>;
  };
  logs: LogSlice;
}

export interface LogRecord {
  at: number;
  time: string;
  level: string;
  logger: string;
  message: string;
}

/** One server-side selection of the log buffer, plus what it left out. */
export interface LogSlice {
  /** Oldest first. */
  items: LogRecord[];
  total: number;
  shown: number;
  truncated: boolean;
  /** First/last matching record; null when nothing matched. */
  covered: { from: number | null; to: number | null };
  buffer_total: number;
  buffer_capacity: number;
  /** The server's clock: the only one a relative window may be resolved on. */
  server_time: number;
  /** Matching records newer than `until`; always 0 outside a frozen window. */
  new_count: number;
  buckets: Record<string, number>;
}

/**
 * Which slice of the log buffer the diagnostics page is asking for. Two
 * orthogonal axes on purpose: one dropdown mixing "whose records" with "which
 * severities" made the options overlap and the labels lie. Mirrors
 * micast/runtime_log.py:in_scope — the report and the clipboard are filtered
 * server-side with the same predicate, so they carry the lines the user read.
 */
export interface LogQuery {
  /** "app" = MiCast/AirPlay's own logger tree, "all" = every library. */
  source: "app" | "all";
  /** "all" levels, or "warn" for WARNING/ERROR/CRITICAL only. */
  level: "all" | "warn";
  /** Duration preset the server resolves at request time; wins over nothing,
   *  loses to an absolute pair. */
  window?: "5m" | "15m" | "30m" | "1h" | "session";
  /** Absolute epoch seconds for a frozen window. */
  since?: number;
  until?: number;
}

export function logQueryString(query: LogQuery): string {
  const params = new URLSearchParams();
  params.set("log_source", query.source);
  params.set("log_level", query.level);
  if (query.window !== undefined) params.set("log_window", query.window);
  if (query.since !== undefined) params.set("log_since", String(query.since));
  if (query.until !== undefined) params.set("log_until", String(query.until));
  return params.toString();
}

export interface TestMedia {
  token: string;
  name: string;
  size: number;
  duration: number | null;
  codec: string;
  media_type: string;
  converted: boolean;
}

export interface LatencyMetrics {
  encoding_ms: number;
  stream_buffer_ms: number;
  send_queue_ms: number;
  /** Per-client delay-line figures; absent on older backends. */
  target_delay_ms?: number;
  retained_buffer_ms?: number;
  estimated_ms: number;
}

export interface SinkLatencyMetrics {
  manual_ms: number;
  startup_ms: number;
  effective_ms: number;
  buffer_ms: number;
  /** Times the speaker was served from the delay line during a source gap. */
  bridges?: number;
}

export interface TopologyNode {
  id: string;
  kind: "source" | "engine" | "pipeline" | "stream" | "cloud" | "speaker";
  label: string;
  protocol?: string;
  status?: string;
  active?: boolean;
  enabled?: boolean;
  [key: string]: unknown;
}

export interface TopologyEdge {
  from: string;
  to: string;
  protocol?: string;
  direction?: "push" | "pull" | "control";
  latency_ms?: number;
  segments?: Record<string, number>;
  estimated?: boolean;
  active?: boolean;
  stalled?: boolean;
  compensation_ms?: number;
  audio_delay_ms?: number;
  [key: string]: unknown;
}

export interface Topology {
  ts: number;
  status: string;
  nodes: TopologyNode[];
  edges: TopologyEdge[];
}

export const api = {
  getAccessStatus(): Promise<AccessStatus> {
    return apiFetch("/api/access/status");
  },

  setupAccess(payload: { auth_enabled: boolean; username: string; password: string; password_confirm: string }): Promise<{ ok: boolean }> {
    return apiFetch("/api/access/setup", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },

  completeSetup(): Promise<{ ok: boolean }> {
    return apiFetch("/api/access/setup/complete", { method: "POST" });
  },

  loginAccess(username: string, password: string): Promise<{ ok: boolean }> {
    return apiFetch("/api/access/login", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }),
    });
  },

  logoutAccess(): Promise<{ ok: boolean }> {
    return apiFetch("/api/access/logout", { method: "POST" });
  },

  updateAccess(payload: { auth_enabled: boolean; username: string; password: string; password_confirm: string }): Promise<{ ok: boolean }> {
    return apiFetch("/api/access/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  getStatus(): Promise<Status> {
    return apiFetch("/api/status");
  },

  getAudioConfig(): Promise<AudioConfig> {
    return apiFetch("/api/config/audio");
  },

  setAudioConfig(config: Partial<AudioConfig>): Promise<AudioConfig> {
    return apiFetch("/api/config/audio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
  },

  getConfig(): Promise<FullConfig> {
    return apiFetch("/api/config");
  },

  setAppName(name: string): Promise<AppConfig> {
    return apiFetch("/api/config/app-name", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  },

  setReceiverMode(mode: ReceiverMode): Promise<{ receiver_mode: ReceiverMode }> {
    return apiFetch("/api/config/receiver-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
  },

  setAirPlayProtocol(protocol: AirPlayProtocol): Promise<{ airplay_protocol: AirPlayProtocol }> {
    return apiFetch("/api/config/airplay-protocol", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ protocol }),
    });
  },

  setDlnaEnabled(enabled: boolean): Promise<{ dlna_enabled: boolean }> {
    return apiFetch("/api/config/dlna", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  },

  setPort(id: string, port: number | null): Promise<{ ok: boolean; restart_required: boolean; ports: PortStatus[] }> {
    return apiFetch("/api/config/ports", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, port }),
    });
  },

  setSyncGroupsEnabled(enabled: boolean): Promise<{ sync_groups_enabled: boolean }> {
    return apiFetch("/api/config/sync-groups", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  },

  setLargeDelayEnabled(enabled: boolean): Promise<{ large_delay_enabled: boolean }> {
    return apiFetch("/api/config/large-delay", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  },

  setTouchscreenLyrics(enabled: boolean): Promise<{ touchscreen_lyrics: boolean }> {
    return apiFetch("/api/config/touchscreen-lyrics", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  },

  setDefaultVolume(volume: number, enabled: boolean): Promise<{ default_volume: number; default_volume_enabled: boolean }> {
    return apiFetch("/api/config/default-volume", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume, enabled }),
    });
  },

  setStaleSessionTimeout(seconds: number): Promise<{ stale_session_timeout: number }> {
    return apiFetch("/api/config/stale-session-timeout", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seconds }),
    });
  },

  setSenderVolumeMode(mode: "independent" | "linked"): Promise<{
    sender_volume_mode: "independent" | "linked";
    dlna_recast_required: boolean;
  }> {
    return apiFetch("/api/config/sender-volume", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }),
    });
  },

  setNotifyWebhook(url: string): Promise<{ notify_webhook_url: string }> {
    return apiFetch("/api/config/notify-webhook", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
  },

  setAirPlay2Enabled(enabled: boolean): Promise<{ airplay2_enabled: boolean }> {
    return apiFetch("/api/config/airplay2", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  },

  setNetworkDiscoveryEnabled(enabled: boolean): Promise<{ network_discovery_enabled: boolean }> {
    return apiFetch("/api/config/network-discovery", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  },

  getAirPlay2State(): Promise<AirPlay2State> {
    return apiFetch("/api/airplay2");
  },

  saveAirPlay2Instance(instance: { id?: string; name: string; target_type: "speaker" | "group"; target_id: string; enabled?: boolean }): Promise<AirPlay2Instance> {
    return apiFetch("/api/airplay2/instances", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(instance),
    });
  },

  setAirPlay2InstanceEnabled(id: string, enabled: boolean): Promise<AirPlay2Instance> {
    return apiFetch(`/api/airplay2/instances/${encodeURIComponent(id)}/enabled`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }),
    });
  },

  deleteAirPlay2Instance(id: string): Promise<{ ok: boolean; warning?: string | null }> {
    return apiFetch(`/api/airplay2/instances/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  getXiaomiStatus(verify = false): Promise<XiaomiStatus> {
    return apiFetch(`/api/xiaomi/status${verify ? "?verify=true" : ""}`);
  },

  logoutXiaomi(): Promise<{ ok: boolean }> {
    return apiFetch("/api/xiaomi/logout", { method: "POST" });
  },

  startQRLogin(): Promise<{ qr_url: string; scan_token: string }> {
    return apiFetch("/api/xiaomi/login/qr/start", { method: "POST" });
  },

  checkUpdate(force = false): Promise<UpdateInfo> {
    return apiFetch(`/api/update/check${force ? "?force=true" : ""}`);
  },

  startUpdateDownload(): Promise<{ started: boolean; asset: string }> {
    return apiFetch("/api/update/download", { method: "POST" });
  },

  getUpdateDownloadStatus(): Promise<UpdateDownloadStatus> {
    return apiFetch("/api/update/download/status");
  },

  applyUpdate(): Promise<{ ok: boolean; path: string }> {
    return apiFetch("/api/update/apply", { method: "POST" });
  },

  pollQRLogin(scanToken: string): Promise<{ status: string; user_id?: string; pass_token?: string }> {
    return apiFetch(`/api/xiaomi/login/qr/poll?scan_token=${encodeURIComponent(scanToken)}`);
  },

  loginWithCookie(userId: string, passToken: string): Promise<{ success: boolean }> {
    return apiFetch("/api/xiaomi/login/cookie", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, pass_token: passToken }),
    });
  },

  getDevices(refresh = false): Promise<Device[]> {
    return apiFetch(refresh ? "/api/devices?refresh=1" : "/api/devices");
  },

  getEqPresets(): Promise<EqPresetsResponse> {
    return apiFetch("/api/devices/eq/presets");
  },

  getDeviceTuning(did: string): Promise<{ did: string } & SpeakerEq> {
    return apiFetch(`/api/tuning/${encodeURIComponent(did)}`);
  },

  /** Save a speaker's EQ curve (control points). Committed on release, not mid-drag. */
  setDeviceEqCurve(did: string, eq: SpeakerEq): Promise<{ did: string } & SpeakerEq> {
    return apiFetch("/api/tuning/eq", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did, ...eq, expected_revision: eq.revision }),
    });
  },

  setDeviceEqTarget(did: string, target: string, expectedRevision?: number): Promise<{ did: string } & SpeakerEq> {
    return apiFetch("/api/tuning/target", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did, target, expected_revision: expectedRevision }),
    });
  },

  setDeviceNightMode(did: string, enabled: boolean, expectedRevision?: number): Promise<{ did: string } & SpeakerEq> {
    return apiFetch("/api/tuning/night-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did, enabled, expected_revision: expectedRevision }),
    });
  },

  setDeviceLoudness(did: string, enabled: boolean, expectedRevision?: number): Promise<{ did: string } & SpeakerEq> {
    return apiFetch("/api/tuning/loudness", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did, enabled, expected_revision: expectedRevision }),
    });
  },

  undoDeviceTuning(did: string, expectedRevision?: number): Promise<{ did: string } & SpeakerEq> {
    return apiFetch("/api/tuning/undo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did, expected_revision: expectedRevision }),
    });
  },

  /** Save the current curve into the global library under `name`. */
  saveCurve(name: string, points: [number, number][]): Promise<{ curves: Record<string, [number, number][]> }> {
    return apiFetch("/api/tuning/curves/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, points }),
    });
  },

  renameCurve(oldName: string, newName: string): Promise<{ curves: Record<string, [number, number][]> }> {
    return apiFetch("/api/tuning/curves/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old: oldName, new: newName }),
    });
  },

  deleteCurve(name: string): Promise<{ curves: Record<string, [number, number][]> }> {
    return apiFetch("/api/tuning/curves/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  },

  saveDeviceProfile(did: string, profile: string): Promise<{ did: string } & SpeakerEq> {
    return apiFetch("/api/tuning/profile/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did, profile }),
    });
  },

  switchDeviceProfile(did: string, profile: string, expectedRevision?: number): Promise<{ did: string } & SpeakerEq> {
    return apiFetch("/api/tuning/profile/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did, profile, expected_revision: expectedRevision }),
    });
  },

  deleteDeviceProfile(did: string, profile: string): Promise<{ did: string } & SpeakerEq> {
    return apiFetch("/api/tuning/profile/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did, profile }),
    });
  },

  exportGraphicEq(did: string): Promise<{ did: string; graphic_eq: string; points: [number, number][] }> {
    return apiFetch(`/api/tuning/${encodeURIComponent(did)}/export`);
  },

  getSpectrum(did: string): Promise<{ bands: number[] | null }> {
    return apiFetch(`/api/tuning/${encodeURIComponent(did)}/spectrum`);
  },

  importGraphicEq(did: string, text: string, expectedRevision?: number): Promise<{ did: string } & SpeakerEq> {
    return apiFetch(`/api/tuning/${encodeURIComponent(did)}/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, expected_revision: expectedRevision }),
    });
  },

  calibrationStart(did: string): Promise<{ token: string; duration_seconds: number }> {
    return apiFetch("/api/tuning/calibration/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did }),
    });
  },

  calibrationStop(token: string): Promise<{ ok: boolean }> {
    return apiFetch("/api/tuning/calibration/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
  },

  calibrationAnalyze(
    wav: Blob,
    did: string,
    target: string
  ): Promise<{
    did: string;
    measured: { freqs: number[]; gains: number[] };
    points: [number, number][];
    level_dbfs: number;
  }> {
    const form = new FormData();
    form.append("file", wav, "recording.wav");
    return apiFetch(
      `/api/tuning/calibration/analyze?did=${encodeURIComponent(did)}&target=${encodeURIComponent(target)}`,
      { method: "POST", body: form }
    );
  },

  calibrationApply(
    did: string,
    points: [number, number][],
    target: string,
    expectedRevision?: number
  ): Promise<{ did: string } & SpeakerEq> {
    return apiFetch("/api/tuning/calibration/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did, points, target, expected_revision: expectedRevision }),
    });
  },

  levelMatch(groupId: string, levels: Record<string, number>): Promise<{ gains_db: Record<string, number> }> {
    return apiFetch("/api/tuning/level-match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group_id: groupId, levels }),
    });
  },

  selectDevice(did: string): Promise<{ selected: string }> {
    return apiFetch("/api/devices/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did }),
    });
  },

  setAlias(did: string, alias: string): Promise<{ did: string; alias: string }> {
    return apiFetch("/api/devices/alias", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did, alias }),
    });
  },

  setEnabled(did: string, enabled: boolean): Promise<{ did: string; enabled: boolean }> {
    return apiFetch("/api/devices/enabled", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ did, enabled }),
    });
  },

  getReceivers(): Promise<ReceiverInfo[]> {
    return apiFetch("/api/receivers");
  },

  createReceiver(payload: Pick<ReceiverDefinition, "name" | "target_type" | "target_id">): Promise<ReceiverDefinition> {
    return apiFetch("/api/receivers/definitions", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },

  updateReceiver(id: string, payload: Partial<ReceiverDefinition>): Promise<ReceiverDefinition> {
    return apiFetch(`/api/receivers/definitions/${encodeURIComponent(id)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },

  deleteReceiver(id: string): Promise<{ ok: boolean }> {
    return apiFetch(`/api/receivers/definitions/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  getAirPlayDevices(): Promise<NetworkDevice[]> {
    return apiFetch("/api/airplay-devices");
  },

  getDlnaDevices(): Promise<NetworkDevice[]> {
    return apiFetch("/api/dlna-devices");
  },

  createGroup(
    name: string,
    speakerIds: string[],
    airplayTargets: string[] = [],
    dlnaTargets: string[] = []
  ): Promise<SpeakerGroup & { codec_compatibility?: CodecCompatibility }> {
    return apiFetch("/api/receivers/groups", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        speaker_ids: speakerIds,
        airplay_targets: airplayTargets,
        dlna_targets: dlnaTargets,
      }),
    });
  },

  getGroupCompatibility(speakerIds: string[]): Promise<CodecCompatibility> {
    return apiFetch("/api/receivers/groups/compatibility", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speaker_ids: speakerIds }),
    });
  },

  updateGroup(id: string, payload: Partial<SpeakerGroup>): Promise<SpeakerGroup> {
    return apiFetch(`/api/receivers/groups/${encodeURIComponent(id)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },

  calibrateGroupDelay(id: string): Promise<{ ok: boolean; group: SpeakerGroup; measured_spread_ms: number; request_offsets_ms: Record<string, number> }> {
    return apiFetch(`/api/debug/groups/${encodeURIComponent(id)}/calibrate-delay`, {
      method: "POST",
    });
  },

  startGroupCalibration(id: string, mediaToken?: string): Promise<{ ok: boolean; group: SpeakerGroup }> {
    return apiFetch(`/api/debug/groups/${encodeURIComponent(id)}/calibration/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(mediaToken ? { media_token: mediaToken } : {}),
    });
  },

  stopGroupCalibration(id: string): Promise<{ ok: boolean }> {
    return apiFetch(`/api/debug/groups/${encodeURIComponent(id)}/calibration/stop`, { method: "POST" });
  },

  deleteGroup(id: string): Promise<{ ok: boolean }> {
    return apiFetch(`/api/receivers/groups/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  play(): Promise<{ ok: boolean; url: string }> {
    return apiFetch("/api/playback/play", { method: "POST" });
  },

  pause(): Promise<{ ok: boolean }> {
    return apiFetch("/api/playback/pause", { method: "POST" });
  },

  stopPlayback(): Promise<{ ok: boolean; stopped: string[]; disconnected: number }> {
    return apiFetch("/api/playback/stop", { method: "POST" });
  },

  kickStream(receiverId: string): Promise<{ ok: boolean; kicked: number; sender_sessions: number; stopped: string[] }> {
    return apiFetch(`/api/debug/stream/${encodeURIComponent(receiverId)}/kick`, { method: "POST" });
  },

  refreshPipelines(): Promise<{ ok: boolean; status: string }> {
    return apiFetch("/api/debug/pipelines/refresh", { method: "POST" });
  },

  resetAll(): Promise<{ ok: boolean }> {
    return apiFetch("/api/config/reset", { method: "POST" });
  },

  getPlaybackState(refresh = false): Promise<PlaybackState> {
    return apiFetch(`/api/playback/state${refresh ? "?refresh=true" : ""}`);
  },

  setVolume(volume: number, deviceIds?: string[], relative = false): Promise<{ devices: Array<{ did: string; ok: boolean; volume?: number; error?: string }> }> {
    return apiFetch("/api/playback/volume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [relative ? "delta" : "volume"]: volume, device_ids: deviceIds }),
    });
  },

  setMute(muted: boolean, deviceIds?: string[]): Promise<{ muted: boolean; devices: Array<{ did: string; ok: boolean; muted?: boolean; error?: string }> }> {
    return apiFetch("/api/playback/mute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ muted, device_ids: deviceIds }),
    });
  },

  async getDeviceVolume(did: string): Promise<number> {
    const result: { devices: Array<{ ok: boolean; volume?: number; error?: string }> } = await apiFetch("/api/playback/volume/levels", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_ids: [did] }),
    });
    const item = result.devices[0];
    if (!item?.ok || item.volume === undefined) throw new Error(item?.error ?? "无法读取音量");
    return item.volume;
  },

  getDebugState(query?: LogQuery): Promise<DebugState> {
    const params = query ? logQueryString(query) : "";
    return apiFetch(`/api/debug/state${params ? `?${params}` : ""}`);
  },

  async downloadDebugReport(query: LogQuery): Promise<{ filename: string; size: number; count: number }> {
    const res = await fetch(appUrl(`/api/debug/report?${logQueryString(query)}`));
    if (!res.ok) {
      const text = await res.text().catch(() => "Unknown error");
      throw new Error(`HTTP ${res.status}: ${text}`);
    }
    const count = logCountHeader(res);
    const blob = await res.blob();
    const disposition = res.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="?([^";]+)"?/)?.[1] || "micast-diagnostic.json";
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    link.click();
    URL.revokeObjectURL(link.href);
    return { filename, size: blob.size, count };
  },

  /** The same selection as the report, as text for the clipboard. */
  async copyRuntimeLog(query: LogQuery): Promise<{ text: string; count: number }> {
    const res = await fetch(appUrl(`/api/debug/logs.txt?${logQueryString(query)}`));
    if (!res.ok) throw await apiError(res);
    return { text: await res.text(), count: logCountHeader(res) };
  },

  clearRuntimeLog(): Promise<{ cleared: number }> {
    return apiFetch("/api/debug/logs/clear", { method: "POST" });
  },

  getTopology(): Promise<Topology> {
    return apiFetch("/api/topology");
  },

  debugTTS(text: string, deviceId?: string): Promise<{ ok: boolean; result: unknown }> {
    return apiFetch("/api/debug/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, device_id: deviceId }),
    });
  },

  debugPlayUrl(url: string, method = "music_url"): Promise<{ ok: boolean; result: unknown }> {
    return apiFetch("/api/debug/play_url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, method }),
    });
  },

  uploadTestMedia(file: File): Promise<TestMedia> {
    const body = new FormData();
    body.append("file", file);
    return apiFetch("/api/debug/media", { method: "POST", body });
  },

  deleteTestMedia(token: string): Promise<{ ok: boolean }> {
    return apiFetch(`/api/debug/media/${encodeURIComponent(token)}`, { method: "DELETE" });
  },

  startDebugTest(payload: { device_ids: string[]; source: "builtin" | "upload" | "url"; media_token?: string; url?: string }): Promise<{ ok: boolean; session_id: string; members: string[]; source: string }> {
    return apiFetch("/api/debug/test/start", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },

  stopDebugTest(sessionId: string, restore = true): Promise<{ ok: boolean; restored: number }> {
    return apiFetch("/api/debug/test/stop", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId, restore }),
    });
  },

  runCodecTest(deviceIds: string[]): Promise<{ ok: boolean; results: Record<string, Record<string, boolean | null>>; common_formats: string[]; restore_failed: string[] }> {
    return apiFetch("/api/debug/codec-test", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device_ids: deviceIds }),
    });
  },

  debugAction(action: "pause" | "play" | "stop" | "volume", payload?: { volume?: number }): Promise<{ ok: boolean; result: { name?: string; [key: string]: unknown } }> {
    return apiFetch("/api/debug/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...payload }),
    });
  },
};
