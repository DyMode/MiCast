import { api, type DebugState, type LogQuery } from "../api";
import { icon, type IconName } from "../icons";
import { store } from "../state";
import { setVolume } from "../volume-service";
import type { State } from "../state";
import { EQ_PRESET_LABELS } from "./devices-view";
import { getTestMedia, setTestMedia } from "../test-media";
import { renderControlRuntime, bindControlRuntime } from "./device-capabilities";

export type { DebugState, LogQuery };

/**
 * Resolve a "-q{n}" EQ-split suffix to something meaningful: the backend
 * numbers each distinct non-flat EQ signature in order of first appearance
 * (config.py receiver_stream_variants). Reproduce that order here, then name
 * the split after the speaker's EQ preset. The bare number means nothing to
 * the user — anything unresolved is just "自定义音效".
 */
function eqSplitTag(state: State, receiverId: string, channelTag: "L" | "R" | null, q: number): string {
  const fallback = "自定义音效";
  const config = state.fullConfig;
  if (!config) return fallback;
  const receiver = config.receivers.find((r) => r.id === receiverId);
  if (!receiver) return fallback;
  let dids: string[] = [];
  let baseOf: (did: string) => "" | "L" | "R" = () => "";
  if (receiver.target_type === "group") {
    const group = config.groups.find((g) => g.id === receiver.target_id);
    if (!group) return fallback;
    dids = group.speaker_ids;
    baseOf = (did) => {
      const ch = group.channels?.[did];
      return ch === "right" ? "R" : ch === "left" ? "L" : "";
    };
  } else if (receiver.target_type === "speaker" && receiver.target_id) {
    dids = [receiver.target_id];
  } else if (receiver.target_type === "selected" && config.selected_device_id) {
    dids = [config.selected_device_id];
  }
  const wantBase = channelTag ?? "";
  // Signatures are the canonical curve points, deduped like the backend's
  // receiver_stream_variants (rounded to 0.1 Hz / 0.01 dB).
  const signatureOf = (points: [number, number][]) =>
    normalizePoints(points).map(([f, g]) => `${f.toFixed(1)}:${g.toFixed(2)}`).join(",");
  const signatures: string[] = [];
  for (const did of dids) {
    if (baseOf(did) !== wantBase) continue;
    const eq = state.devices.find((d) => d.did === did)?.eq;
    if (!eq?.enabled || !eq.points.some(([, g]) => Math.abs(g) >= 0.05)) continue;
    const sig = signatureOf(eq.points);
    if (!signatures.includes(sig)) signatures.push(sig);
  }
  const wanted = signatures[q - 1];
  if (!wanted) return fallback;
  const owner = dids
    .map((did) => state.devices.find((d) => d.did === did))
    .find((d) => d?.eq?.enabled && signatureOf(d.eq.points) === wanted);
  const preset = owner?.eq?.preset;
  return (preset && EQ_PRESET_LABELS[preset]) || "自定义音效";
}

/** Sort/clamp/dedupe control points, mirroring curve_fit.normalize_points. */
function normalizePoints(points: [number, number][]): [number, number][] {
  const byFreq = new Map<number, number>();
  for (const [f, g] of points) {
    const freq = Math.min(20000, Math.max(20, f));
    byFreq.set(freq, Math.min(12, Math.max(-12, g)));
  }
  return [...byFreq.entries()].sort((a, b) => a[0] - b[0]).slice(0, 24);
}

let debugTargetKey = "";
let debugTestSource: "builtin" | "upload" | "url" = "builtin";
let activeTestSession = "";
let debugTestBusy: "upload" | "start" | "stop" | "tts" | "" = "";

// The log scope lives outside the rendered markup so a re-render of the panel
// (the test workbench rerenders everything) does not silently snap the user's
// choice back to the default.
let logFilter: { source: "app" | "all"; level: "all" | "warn" } = { source: "app", level: "all" };
/** Non-null while the panel shows a locked interval instead of the live tail.
 *  `key` is the preset it came from, or "custom" for a hand-picked interval —
 *  the 时间范围 select shows it directly rather than reverse-matching a label. */
let logFreeze: { key: string; label: string; since: number; until: number } | null = null;
let logServerTime = 0;   // last `debug.logs.server_time`

const LOG_SOURCE_LABELS: Record<LogQuery["source"], string> = {
  app: "MiCast 与 AirPlay",
  all: "全部来源",
};
const LOG_LEVEL_LABELS: Record<LogQuery["level"], string> = {
  all: "全部级别",
  warn: "仅警告与错误",
};

/** Mirrors micast/runtime_log.py:WINDOW_SECONDS — freezing needs the seconds to
 *  turn a preset into an absolute interval on the server's clock. */
const WINDOW_SECONDS: Record<string, number> = { "5m": 300, "15m": 900, "30m": 1800, "1h": 3600 };
const WINDOW_LABELS: Record<string, string> = { "5m": "最近 5 分钟", "15m": "最近 15 分钟", "30m": "最近 30 分钟", "1h": "最近 1 小时", session: "本次运行" };

/** The 时间范围 select, in order: follow, one row per preset, then the dialog. */
const FREEZE_OPTIONS: Array<[string, string]> = [
  ["follow", "跟随最新"],
  ["5m", "冻结最近 5 分钟"],
  ["15m", "冻结最近 15 分钟"],
  ["30m", "冻结最近 30 分钟"],
  ["1h", "冻结最近 1 小时"],
  ["custom", "冻结自定义时段…"],
];

/** What the panel is asking the server for right now. */
export function currentLogQuery(): LogQuery {
  const base = { source: logFilter.source, level: logFilter.level };
  return logFreeze ? { ...base, since: logFreeze.since, until: logFreeze.until } : base;
}

function selectedTestDeviceIds(state: State, debug: DebugState | null): string[] {
  // The select visually falls back to its first option when nothing matches —
  // mirror that here, or the start button stays disabled until the user
  // re-picks the device the dropdown already shows.
  const key =
    debugTargetKey ||
    (debug?.selected_device_id ? `speaker:${debug.selected_device_id}` : "") ||
    (debug?.devices.length ? `speaker:${debug.devices[0].did}` : "");
  if (key.startsWith("group:")) {
    return state.fullConfig?.groups.find((group) => group.id === key.slice(6))?.speaker_ids ?? [];
  }
  return key.startsWith("speaker:") ? [key.slice(8)] : [];
}

function formatDuration(seconds: number | null): string {
  if (seconds === null) return "时长未知";
  const rounded = Math.max(0, Math.round(seconds));
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, "0")}`;
}

const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);

/**
 * Live sender sessions split by ingress. AirPlay 2 runs on shairport + PCM
 * sources and never opens a RAOP session, so counting the RAOP counters alone
 * reported an idle input while an AirPlay 2 sender was playing. An older
 * backend without `sessions` degrades to the RAOP-only reading.
 */
function inputSessions(debug: DebugState | null): { classic: number; airplay2: number; total: number } {
  const raop = Object.values(debug?.diagnostics?.raop || {});
  const classic = sum(raop.map((item) => item.active_sessions));
  const airplay2 = debug?.diagnostics?.sessions?.airplay2?.length ?? 0;
  return { classic, airplay2, total: classic + airplay2 };
}

interface TeeDepth {
  depth_ms: number;
  capacity_ms: number;
  dropped: number;
  clients: number;
}

/**
 * Branch depths, one per stream that owns a tee. Variant streams (-q1, -L, -R)
 * used to share their entry's tee and report an empty object, so anything
 * without a usable depth has to be dropped here rather than rendered as NaN.
 * The depth that matters while playing is the one feeding a stream the
 * speakers are actually pulling; idle branches (a variant nobody plays) are
 * only a fallback.
 */
function teeDepthsOf(debug: DebugState | null): TeeDepth[] {
  const depths = Object.values(debug?.diagnostics?.streams || {})
    .map((item) => (item.tee ? { ...item.tee, clients: item.clients || 0 } : null))
    .filter((item): item is TeeDepth => !!item && Number.isFinite(item.depth_ms));
  const serving = depths.filter((item) => item.clients > 0);
  return serving.length ? serving : depths;
}

type RingState = "ok" | "warn" | "bad" | "idle";

/** One stage of the signal path, phrased as a headline value plus a qualifier. */
interface Stage {
  label: string;
  icon: IconName;
  value: string;
  note: string;
  state: RingState;
}

/** Same class vocabulary for the plain status words in a cell. */
function rowStateClass(state: string): string {
  if (state === "error") return "error";
  if (state === "warn") return "warn";
  return "success";
}

/** Stream ids carry suffixes (-q1, -L, -R); several of them belong to one entry. */
function baseEntryId(streamId: string): string {
  return streamId.replace(/-q\d+$/, "").replace(/-(L|R)$/, "");
}

function entryLabel(entryId: string, state: State): string {
  const receiver = state.fullConfig?.receivers?.find((item) => item.id === entryId);
  if (receiver?.name) return receiver.name;
  const instance = state.fullConfig?.airplay2_instances?.find((item) => item.id === entryId);
  return instance?.name || entryId;
}

/**
 * The whole page's living part: one conclusion, the four stages that carry it,
 * and — only when something is actually wrong — what the supervisor did about
 * it. Everything here is live-updated in place by the poller, so the chain and
 * the alert lines must stay in this container.
 */
export function renderStatusOverview(debug: DebugState | null, state: State): string {
  const teeDepths = teeDepthsOf(debug);
  const streams = Object.values(debug?.diagnostics?.streams || {});
  const audio = debug?.diagnostics?.audio;
  const entries = debug?.diagnostics?.entries || {};
  const sessions = inputSessions(debug);
  const flowingClients = sum(streams.filter((item) => item.flowing).map((item) => item.clients));
  const playing = sessions.total > 0 || flowingClients > 0;

  // --- the 60-second window everything below reasons from -----------------
  // Cumulative counters stay in the connection detail; a verdict fired from a
  // lifetime total stays red forever after one bad minute ("playing fine" with
  // a red chain was the complaint). Kinds: micast/audio_metrics.py.
  const win: Record<string, { count: number; ms_max: number; by_entry: Record<string, number> }> =
    audio?.window || {};
  const slot = (kind: string) => win[kind] || { count: 0, ms_max: 0, by_entry: {} };
  const linkSkip = slot("link_skip").count;
  const linkResend = slot("link_resend").count;
  const linkDecodeErr = slot("link_decode_error").count;
  const stallWin = slot("source_stall");
  const gapWin = slot("source_gap");
  const encStallWin = slot("encoder_stall");
  const lagWin = slot("lag_skip");
  const silenceWin = slot("silence_fill");
  const reconnectWin = slot("client_reconnect");
  const loopWin = slot("loop_lag");
  const ourLossBlocks =
    slot("tee_drop").count + slot("encoder_drop").count + slot("queue_drop").count;
  const cpuPercent = audio?.runtime?.cpu_percent ?? 0;

  // The supervisor already separates a sender's own pause (quiet/paused) from
  // trouble it is fighting (degraded/unhealthy) — reuse that instead of
  // guessing from counters.
  const entryList = Object.entries(entries);
  const notRecovered = entryList.filter(([, health]) => health.state === "unhealthy");
  const recovering = entryList.filter(([, health]) =>
    health.state === "degraded_our_side" || health.state === "degraded_speaker",
  );
  const stalledNow =
    stallWin.count > 0 &&
    entryList.some(([, health]) =>
      health.state === "degraded_our_side" || health.state === "unhealthy",
    );

  // Window keys are device ids (speaker side) or stream ids (our side);
  // resolve both to a name, falling back to the raw key.
  const nameOfKey = (key: string): string => {
    const viaEntry = entryLabel(key, state);
    if (viaEntry !== key) return viaEntry;
    return state.devices.find((device) => device.did === key)?.name || key;
  };
  const topEntryName = (s: { by_entry: Record<string, number> }): string => {
    const ranked = Object.entries(s.by_entry).sort((a, b) => b[1] - a[1]);
    return ranked.length ? nameOfKey(ranked[0][0]) : "";
  };

  // Conclusions: fixed sentences, at most one audio line plus one account
  // line. The first audio match wins, ordered by where the responsibility
  // lies; a line only fires when the listener can hear it — absorbed jitter
  // shows nowhere, and "all fine" shows nothing at all.
  type Verdict = { severity: "bad" | "warn"; text: string };
  const alerts: Verdict[] = [];

  // Account family: independent of the audio path, so it may sit next to an
  // audio verdict. Expired login explains the cloud failures too, so only one
  // of the two ever shows.
  if (state.xiaomi.status === "expired") {
    alerts.push({ severity: "bad", text: "小米账号登录已失效。" });
  } else if ((debug?.cloud?.failures ?? 0) >= 3) {
    alerts.push({
      severity: "bad",
      text: `无法连接米家云端（连续 ${debug?.cloud?.failures} 次无响应）。`,
    });
  }

  // Anything that erased a whole second of audio upgrades its line to red,
  // even when the cause itself sits in the yellow tier.
  const heavyLoss =
    ourLossBlocks >= 10 ||
    Math.max(stallWin.ms_max, gapWin.ms_max, lagWin.ms_max, loopWin.ms_max) >= 1000;
  const audioVerdict = ((): Verdict | null => {
    // Verdicts describe what the listener hears; with nothing playing there is
    // nothing to hear, so a trailing-minute leftover must not outlive playback.
    if (!playing) return null;
    if (notRecovered.length) {
      return { severity: "bad", text: `${nameOfKey(notRecovered[0][0])}无法恢复，当前无声音。` };
    }
    if (stalledNow) {
      return { severity: "bad", text: "投送端已停止送音频。" };
    }
    if (linkSkip >= 20 || linkResend >= 10) {
      const parts = [`跳过 ${linkSkip} 个包`];
      if (linkResend) parts.push(`重传 ${linkResend} 次`);
      if (linkDecodeErr) parts.push(`解码失败 ${linkDecodeErr} 次`);
      return {
        severity: heavyLoss ? "bad" : "warn",
        text: `接收端记录到音频包跳过或重传（${parts.join(" · ")}）。`,
      };
    }
    if (ourLossBlocks > 0 && (loopWin.ms_max >= 200 || cpuPercent >= 80)) {
      return {
        severity: heavyLoss ? "bad" : "warn",
        text: `转码性能不足，音频已丢弃 ${ourLossBlocks} 块（CPU ${Math.round(cpuPercent)}% · 事件循环最长阻塞 ${Math.round(loopWin.ms_max)}ms）。`,
      };
    }
    if ((lagWin.count > 0 || silenceWin.count > 0) && ourLossBlocks === 0) {
      const name = topEntryName(lagWin.count >= silenceWin.count ? lagWin : silenceWin);
      const parts: string[] = [];
      if (lagWin.count) parts.push(`跳至实时 ${lagWin.count} 次`);
      if (silenceWin.count) parts.push(`补静音 ${silenceWin.count} 次`);
      return {
        severity: heavyLoss ? "bad" : "warn",
        text: `${name || "音箱"}跟不上取流（${parts.join(" · ")}）。`,
      };
    }
    if (recovering.length) {
      return { severity: "warn", text: `${nameOfKey(recovering[0][0])}发生断音，已自动重建。` };
    }
    return null;
  })();
  if (audioVerdict) alerts.push(audioVerdict);

  // Chain tiles share the verdicts' window: green = nothing lost in the last
  // minute, yellow = audio lost but the stream is still moving, red = silence.
  // Absorbed jitter colours nothing, so a healthy session stays green.
  const anyBadEntry = notRecovered.length > 0;
  const sourceState: RingState = !playing
    ? "idle"
    : stalledNow || anyBadEntry
      ? "bad"
      : linkSkip > 0 || linkResend > 0 || linkDecodeErr > 0
        ? "warn"
        : "ok";
  const encodeState: RingState = !playing
    ? "idle"
    : ourLossBlocks >= 10 || loopWin.ms_max >= 1000
      ? "bad"
      : ourLossBlocks > 0 || loopWin.ms_max >= 200
        ? "warn"
        : "ok";
  const streamState: RingState = !playing
    ? "idle"
    : ourLossBlocks >= 10 || linkSkip >= 100
      ? "bad"
      : linkSkip > 0 || linkResend > 0 || ourLossBlocks > 0
        ? "warn"
        : flowingClients === 0
          ? "warn"
          : "ok";
  const speakerState: RingState = !playing
    ? "idle"
    : anyBadEntry
      ? "bad"
      : lagWin.count > 0 || silenceWin.count > 0 || reconnectWin.count > 0 || recovering.length > 0
        ? "warn"
        : flowingClients === 0
          ? "warn"
          : "ok";

  const stages: Stage[] = [
    {
      label: "音源",
      icon: "phone",
      state: sourceState,
      value: !playing
        ? "无投放"
        : sessions.airplay2 && sessions.classic
          ? `${sessions.classic + sessions.airplay2} 路`
          : sessions.airplay2
            ? "AirPlay 2"
            : "手机 1",
      note: !playing
        ? ""
        : [
            sessions.airplay2 && sessions.classic ? `手机 ${sessions.classic} · AP2 ${sessions.airplay2}` : "",
            stalledNow ? "已停滞" : "",
            linkSkip ? `接收跳包 ${linkSkip}` : "",
          ].filter(Boolean).join(" · "),
    },
    {
      label: "转码",
      icon: "wave",
      state: encodeState,
      value: audio ? `${audio.encode.p95_ms} ms` : debug?.audio_config.format.toUpperCase() || "—",
      note: !playing
        ? ""
        : ourLossBlocks
          ? `丢弃 ${ourLossBlocks} 块`
          : loopWin.ms_max >= 200
            ? `阻塞 ${Math.round(loopWin.ms_max)}ms`
            : encStallWin.count
              ? `${encStallWin.count} 次偏大`
              : "P95 稳定",
    },
    {
      label: "流",
      icon: "server",
      state: streamState,
      value: !playing ? "无连接" : `${flowingClients} 台`,
      // What is wrong, not only how much is buffered: a red ring here most
      // often means the sender→device link lost packets, and saying so is the
      // difference between "MiCast is broken" and "the Wi-Fi is".
      note: !playing
        ? ""
        : [
            linkSkip ? `接收跳包 ${linkSkip}` : "",
            ourLossBlocks ? `丢弃 ${ourLossBlocks} 块` : "",
            teeDepths.length ? `缓冲 ${Math.round(Math.max(...teeDepths.map((item) => item.depth_ms)))}ms` : "",
          ].filter(Boolean).join(" · ") || "取流中",
    },
    {
      label: "音箱",
      icon: "speaker",
      state: speakerState,
      value: !playing ? "未连接" : flowingClients ? `${flowingClients} 台` : "0 台",
      note: !playing
        ? ""
        : flowingClients
          ? [
              lagWin.count ? `跳至实时 ${lagWin.count} 次` : "",
              silenceWin.count ? `补静音 ${silenceWin.count} 次` : "",
            ].filter(Boolean).join(" · ") || "正在接收"
          : "未取流",
    },
  ];

  const headline =
    debug === null
      ? "正在读取状态…"
      : !["running", "degraded"].includes(debug.bridge_status.status)
        ? "服务未运行"
        : !playing
          ? debug.bridge_status.status === "degraded" ? "部分功能不可用" : "空闲"
          : speakerState === "ok" && sourceState !== "bad" && encodeState !== "bad"
            ? debug.bridge_status.status === "degraded" ? "正在播放 · 部分功能不可用" : "正在播放"
            : "播放异常";
  const headlineState: RingState =
    debug === null
      ? "idle"
      : !["running", "degraded"].includes(debug.bridge_status.status) || headline === "播放异常"
        ? "bad"
        : !playing
          ? "idle"
          : sourceState === "warn" || encodeState === "warn" || speakerState === "warn"
            ? "warn"
            : "ok";
  const serving = Object.entries(debug?.diagnostics?.streams || {}).filter(
    ([, item]) => item.clients > 0,
  );
  const servingNames = [...new Set(serving.map(([id]) => entryLabel(baseEntryId(id), state)))];
  const perStreamLatency = serving
    .map(([, item]) => (item.input_buffer_ms || 0) + (item.latency?.estimated_ms || 0))
    .filter((value) => value > 0);
  const format = `${debug?.audio_config.format.toUpperCase()} ${
    debug?.audio_config.sample_rate ? `${debug.audio_config.sample_rate / 1000}k` : ""
  }`.trim();
  const latencyText = perStreamLatency.length
    ? perStreamLatency.length > 1
      ? `${Math.min(...perStreamLatency)}–${Math.max(...perStreamLatency)}ms`
      : `约 ${perStreamLatency[0]}ms`
    : "";
  const headlineDetail = !playing
    ? sessions.total === 0
      ? "没有正在投放的音频"
      : "发送端在场但还没有音箱取流"
    : [
        servingNames.length > 1
          ? `${servingNames.length} 路播放中 · ${servingNames.join(" / ")}`
          : servingNames[0],
        format,
        latencyText ? `延迟 ${latencyText}` : "",
      ]
        .filter(Boolean)
        .join(" · ");

  const events = (audio?.events || []).slice(-5).reverse();

  return `
      <div class="diagnostic-headline ${headlineState === "bad" ? "is-bad" : headlineState === "warn" ? "is-warn" : ""}">
        <strong><span class="headline-state" data-state="${headlineState}"></span>${headline}</strong>
        <span>${headlineDetail}</span>
      </div>
      <div class="signal-chain" data-flow="${playing ? "on" : "off"}" role="list">
        ${stages
          .map(
            (stage) => `
        <div class="signal-node" role="listitem" data-state="${stage.state}">
          <span class="signal-node-icon">${icon(stage.icon)}</span>
          <span class="signal-node-text">
            <span class="signal-node-label">${stage.label}</span>
            <span class="signal-node-value">${stage.value}</span>
            ${stage.note ? `<span class="signal-node-note">${stage.note}</span>` : ""}
          </span>
        </div>`,
          )
          .join("")}
      </div>
      ${alerts
        .map(
          (alert) => `
      <div class="diagnostic-alert ${alert.severity === "bad" ? "is-bad" : ""}">${alert.text}</div>`,
        )
        .join("")}
      ${
        events.length
          ? `<div class="diagnostic-events">${events
              .map((event) => `<span>${formatEvent(event, state)}</span>`)
              .join("")}</div>`
          : ""
      }`;
}

const EVENT_LABELS: Record<string, string> = {
  encoder_stall: "编码间隔偏大",
  encoder_gap: "编码输出间隔",
  source_stall: "音源停滞",
  source_gap: "音源空缺",
  lag_skip: "延迟线跳过",
  silence_fill: "补静音",
  tee_drop: "PCM 分发丢弃",
  encoder_drop: "编码器丢弃",
  client_reconnect: "音箱重连",
};

// These events carry their meaning in the detail string rather than in a
// duration; the log shows that text instead of the raw kind.
const DETAIL_EVENTS = new Set(["health", "recovery", "recovered", "buffer_raised"]);

function formatEvent(
  event: { at: number; kind: string; ms: number | null; detail: string | null; entry?: string | null; label?: string | null },
  state: State,
): string {
  const time = formatClock(event.at);
  if (event.entry && event.label) {
    const suffix = event.ms ? ` ${Math.round(event.ms)}ms` : "";
    return `${time} ${entryLabel(event.entry, state)} ${event.label}${suffix}`;
  }
  if (DETAIL_EVENTS.has(event.kind) && event.detail) {
    return `${time} ${event.detail}`;
  }
  const label = EVENT_LABELS[event.kind] || event.kind;
  return `${time} ${label}${event.ms ? ` ${Math.round(event.ms)}ms` : ""}`;
}

function formatClock(at: number): string {
  const date = new Date(at * 1000);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}:${String(date.getSeconds()).padStart(2, "0")}`;
}

/**
 * Cumulative audio-path black box: connection-scoped counters reset on every
 * speaker reconnect, so the live status can read all-zero while the listener
 * hears a periodic hiccup. These counters survive reconnects, and the event
 * log names what happened and for how long.
 *
 * Rendered as stat tiles rather than one row per metric: the rows were a
 * single column of label/description/word, which took a full screen for six
 * numbers. The tiles pair a headline figure with its qualifiers and carry the
 * severity in the accent bar.
 */
export function renderAudioPath(debug: DebugState | null): string {
  const audio = debug?.diagnostics?.audio;
  if (!audio) {
    return `<div class="cell"><span class="cell-subtitle">当前版本暂未提供音频路径统计</span></div>`;
  }
  const branchDepths = teeDepthsOf(debug);
  // Bytes the delay line is holding back per stream: the jitter budget the
  // speaker's own reader gets to work with. Zero here while playing means
  // every encoder hiccup is passed straight through.
  const retainedMs = Object.values(debug?.diagnostics?.streams || {})
    .filter((item) => item.clients > 0)
    .map((item) => item.latency?.retained_buffer_ms)
    .filter((value): value is number => Number.isFinite(value));
  const encodeLost = audio.drops.encoder_in + audio.drops.encoder_out;
  // Times a speaker was served from the delay line during a source arrival gap
  // (the alternative was a hole in its playback, not a drop).
  const bridgeCount = Object.values(debug?.diagnostics?.sinks || {})
    .flatMap((sinks) => Object.values(sinks))
    .reduce((total, sink) => total + (sink.bridges || 0), 0);
  const totalDrops = audio.drops.tee + audio.drops.encoder_in + audio.drops.encoder_out;
  const tiles = [
    {
      // Not "how long encoding takes": this is the interval between two encoded
      // chunks leaving the pipeline (measured around the pump loop). Encoding
      // itself measures ~3% of real time here; a long interval means the source
      // had nothing to hand over — check 音源供给 and 投送链路 first.
      label: "输出间隔",
      value: `${audio.encode.p95_ms}`,
      unit: "ms P95",
      note: `P50 ${audio.encode.p50_ms} ms · 最长 ${audio.encode.max_ms} ms · 正常约等于音频块时长`,
      state: encodeLost > 0 ? "bad" : audio.encode.stalls > 0 ? "warn" : "ok",
    },
    {
      label: "音源供给",
      value: audio.source.stalls > 0 ? `${audio.source.stalls}` : "正常",
      unit: audio.source.stalls > 0 ? "次停滞" : "",
      note: [
        audio.source.stalls > 0 ? `最长 ${audio.source.stall_max_ms} ms` : "",
        `读取等待 ${audio.source.gaps ?? 0} 次${audio.source.gap_max_ms ? `（最长 ${Math.round(audio.source.gap_max_ms)} ms）` : ""}`,
        audio.pace ? `我方节流 ${audio.pace.sleeps} 次/${Math.round(audio.pace.max_ms)} ms` : "",
      ]
        .filter(Boolean)
        .join(" · "),
      state: audio.source.stalls > 0 ? "bad" : "ok",
    },
    {
      label: "客户端缓冲",
      value: `${audio.client.queue_peak_ms}`,
      unit: "ms 峰值",
      note: [
        `${audio.client.queue_peak_items} 块`,
        retainedMs.length ? `延迟线 ${Math.round(Math.max(...retainedMs))} ms` : "",
        bridgeCount ? `衔接 ${bridgeCount} 次` : "",
        `重连 ${audio.client.reconnects} 次`,
      ]
        .filter(Boolean)
        .join(" · "),
      state: audio.client.lag_skips > 0 || audio.client.queue_drops > 0 ? "bad" : "ok",
      flag:
        audio.client.lag_skips > 0
          ? `跳过 ${audio.client.lag_skips} 次`
          : audio.client.queue_drops > 0
            ? `丢弃 ${audio.client.queue_drops} 次`
            : "",
    },
    {
      label: "链路丢弃",
      value: `${totalDrops}`,
      unit: "块/包",
      note: [
        `PCM ${audio.drops.tee}${
          audio.drops.tee_by_entry
            ? Object.entries(audio.drops.tee_by_entry)
                .map(([id, count]) => `（${id} ${count}）`)
                .join(" ")
            : ""
        }`,
        `编码入 ${audio.drops.encoder_in} · 编码出 ${audio.drops.encoder_out}`,
        branchDepths.length ? `缓冲窗口 ${Math.round(Math.max(...branchDepths.map((item) => item.capacity_ms)))}ms` : "",
      ]
        .filter(Boolean)
        .join(" · "),
      state: totalDrops > 0 ? "bad" : "ok",
    },
  ] as Array<{ label: string; value: string; unit: string; note: string; state: string; flag?: string }>;

  // Runtime health: this process's own CPU load and how late the loop ran its
  // own timer. A stutter with every drop counter at zero is either "the box is
  // saturated" (CPU at/near one core, long loop lag) or the sender's shape —
  // these two tiles are what separates them.
  const runtime = audio.runtime;
  if (runtime) {
    tiles.push({
      label: "CPU 占用",
      value: `${runtime.cpu_percent.toFixed(0)}`,
      unit: "%",
      note: `本进程全部编码线程 · ${runtime.cores || 1} 核`,
      state: runtime.cpu_percent >= 90 ? "bad" : runtime.cpu_percent >= 60 ? "warn" : "ok",
    });
    tiles.push({
      label: "事件循环延迟",
      value: `${runtime.loop_lag_ms}`,
      unit: "ms 当前",
      note: `最长 ${runtime.loop_lag_max_ms} ms`,
      state:
        runtime.loop_lag_max_ms >= 500 ? "bad" : runtime.loop_lag_max_ms >= 200 ? "warn" : "ok",
      flag:
        runtime.loop_lag_max_ms >= 500
          ? "阻塞严重"
          : runtime.loop_lag_max_ms >= 200
            ? "偶有阻塞"
            : "",
    });
  }

  const events = audio.events.slice(-8).reverse();
  return `
      <div class="metric-grid">
        ${tiles
          .map(
            (tile) => `
        <div class="metric-tile" data-state="${tile.state}">
          <span class="metric-label">${tile.label}</span>
          <span class="metric-headline">
            <span class="metric-value">${tile.value}<small>${tile.unit}</small></span>
            ${tile.flag ? `<span class="metric-flag">${tile.flag}</span>` : ""}
          </span>
          <span class="metric-note">${tile.note}</span>
        </div>`,
          )
          .join("")}
      </div>
      <div class="metric-events">
        <span class="metric-label">最近事件</span>
        ${
          events.length === 0
            ? `<span class="metric-note">本次运行尚无异常事件</span>`
            : `<div class="diagnostic-events">${events
                .map(
                  (event) =>
                    `<span>${formatClock(event.at)} ${
                      EVENT_LABELS[event.kind] || event.kind
                    }${event.ms ? ` ${Math.round(event.ms)} ms` : ""}</span>`,
                )
                .join("")}</div>`
        }
      </div>`;
}


/**
 * Which diagnostics sections the user opened.
 *
 * Held here, not in the DOM: every full re-render (a test button, a config
 * change, the 5s settings sync) replaces the panel's markup and a <details>
 * with no explicit `open` snaps shut — which read as "it folded itself back up
 * while I was looking at it".
 */
const openSections = new Set<string>();

export function renderDebugPanel(state: State, debug: DebugState | null): string {
  const raop = Object.values(debug?.diagnostics?.raop || {});
  const sessions = inputSessions(debug);
  const selectedStream = findSelectedAirPlayStream(state, debug);
  const selectedSessionActive = selectedStream
    ? (debug?.diagnostics.raop[selectedStream.id]?.active_sessions || 0) > 0
    : false;
  return `
    <div class="page-heading">
      <h2 class="page-title">诊断</h2>
      <p>音频从手机到音箱的每一环，以及最近的运行记录。</p>
    </div>

    <div class="group diagnostic-overview" data-connection-checks>
      ${renderStatusOverview(debug, state)}
    </div>

    <details class="diagnostic-details" data-debug-section="streams" ${openSections.has("streams") ? "open" : ""}>
      <summary><span><strong>连接明细</strong><small data-stream-summary>${renderStreamSummary(debug)}</small></span></summary>
      <div class="group" data-stream-list>
        ${renderStreamRows(debug, state)}
      </div>
      <div class="group" data-audio-path>
        ${renderAudioPath(debug)}
      </div>
      <div class="technical-metrics" data-technical-metrics>
        ${renderTechnicalMetrics(debug)}
      </div>
    </details>

    <details class="diagnostic-details" data-debug-section="test" ${openSections.has("test") ? "open" : ""}>
      <summary><span><strong>播放诊断</strong><small>播放测试音频，确认音箱能否出声</small></span></summary>
      <div class="group diagnostic-workbench">
        <div class="debug-warning"><strong>诊断会播放一段测试音频</strong><span>用来确认目标音箱能否出声，以及多台音箱是否同步；结束后会尝试恢复原播放。</span></div>
        <label class="debug-target-row">
          <span><strong>1. 选择诊断目标</strong><small>可以检查一台音箱或整个组合</small></span>
          <select class="input" data-debug-target aria-label="测试目标" ${debug?.devices.length ? "" : "disabled"}>
            ${debug === null ? `<option>正在加载音箱…</option>` : [
              ...debug.devices.map((device) => `<option value="speaker:${escapeHtml(device.did)}" ${(debugTargetKey || `speaker:${debug.selected_device_id}`) === `speaker:${device.did}` ? "selected" : ""}>${escapeHtml(device.name)}</option>`),
              ...(state.fullConfig?.groups ?? []).map((group) => `<option value="group:${escapeHtml(group.id)}" ${debugTargetKey === `group:${group.id}` ? "selected" : ""}>${escapeHtml(group.name)} · ${group.speaker_ids.length} 台音箱</option>`),
            ].join("")}
          </select>
        </label>
        <div class="diagnostic-step-label"><strong>2. 选择测试声音</strong><span>内置节拍适合快速检查，也可以使用熟悉的音频</span></div>
        <div class="test-source-tabs" role="radiogroup" aria-label="测试音频来源">
          ${([['builtin', '内置节拍'], ['upload', '上传音频'], ['url', '音频地址']] as const).map(([value, label]) => `<label><input type="radio" name="debug-source" value="${value}" ${debugTestSource === value ? "checked" : ""}><span>${label}</span></label>`).join("")}
        </div>
        <div class="test-source-panel">
          ${debugTestSource === "builtin" ? `<div><strong>内置节拍</strong><p>短促、清晰，适合确认音箱能否播放和多台音箱是否同步。</p></div>` : ""}
          ${debugTestSource === "upload" ? `<div class="test-upload">
            ${getTestMedia() ? `<div class="test-media-file"><div><strong>${escapeHtml(getTestMedia()!.name)}</strong><span>${formatDuration(getTestMedia()!.duration)} · ${(getTestMedia()!.size / 1048576).toFixed(1)} MB${getTestMedia()!.converted ? " · 已转换" : ""}</span></div><button class="button plain" type="button" data-remove-test-media>移除</button></div>` : `<label class="test-file-picker"><input type="file" accept=".mp3,.aac,.m4a,.flac,.wav,.ogg,.ape,audio/*" data-test-file><strong>${debugTestBusy === "upload" ? "正在处理音频…" : "选择音频文件"}</strong><span>MP3、AAC、M4A、FLAC、WAV、OGG 或 APE，最大 50 MB</span></label>`}
          </div>` : ""}
          ${debugTestSource === "url" ? `<label class="test-url-field"><span>音频地址</span><input type="url" data-debug-url placeholder="输入可直接访问的音频地址" class="input"></label>` : ""}
        </div>
        <div class="test-session-bar" aria-live="polite">
          <div><strong>${activeTestSession ? "测试声音正在播放" : debugTestBusy ? "正在准备诊断…" : "3. 开始播放检查"}</strong><span>${activeTestSession ? "听音箱是否出声、是否同步，完成后停止" : "开始后会暂时接管所选目标"}</span></div>
          <button class="button ${activeTestSession ? "secondary" : "primary"}" type="button" data-debug-test-action ${debugTestBusy || !selectedTestDeviceIds(state, debug).length ? "disabled" : ""}>${debugTestBusy === "start" ? "正在播放…" : debugTestBusy === "stop" ? "正在恢复…" : activeTestSession ? "结束诊断并恢复" : debugTestSource === "builtin" ? "播放测试节拍" : "播放测试音频"}</button>
        </div>
        <div class="diagnostic-step-label diagnostic-tools-label"><strong>单项检查</strong><span>仅在对应问题出现时使用</span></div>
        <div class="diagnostic-utilities">
          <button class="diagnostic-test" id="btn-debug-codecs" ${selectedTestDeviceIds(state, debug).length ? "" : "disabled"}><strong>测试音频格式</strong><span>检查 MP3、FLAC、WAV；PCM 请试听确认</span><em>开始检查</em></button>
          <button class="diagnostic-test" id="btn-debug-tts" ${selectedTestDeviceIds(state, debug).length !== 1 ? "" : "disabled"}><strong>测试米家语音</strong><span>朗读测试语句，检查账号与响应</span><em>开始检查</em></button>
          <button class="diagnostic-test" id="btn-debug-play-stream" ${selectedStream ? "" : "disabled"}><strong>重新接入当前 AirPlay</strong><span>${selectedStream
            ? selectedSessionActive ? `重新播放“${escapeHtml(selectedStream.name)}”当前收到的内容` : `“${escapeHtml(selectedStream.name)}”当前没有收到音频`
            : "所选音箱没有对应的独立播放入口"}</span><em>尝试接入</em></button>
        </div>
        <label class="debug-volume volume-control test-volume-row">
          <span><strong>音箱音量</strong><small>修改目标音箱的真实音量</small></span>
          <input type="range" min="0" max="100" value="${state.devices.find(d => d.did === selectedTestDeviceIds(state, debug)[0])?.volume ?? 0}" id="debug-volume" ${selectedTestDeviceIds(state, debug).length ? "" : "disabled"} aria-label="测试音箱音量" style="--volume:${state.devices.find(d => d.did === selectedTestDeviceIds(state, debug)[0])?.volume ?? 0}%">
          <output id="debug-volume-output">${state.devices.find(d => d.did === selectedTestDeviceIds(state, debug)[0])?.volume ?? "—"}</output>
        </label>
        <div class="cell">
          <div class="cell-content">
            <span class="cell-title">重建全部管道</span>
            <span class="cell-subtitle">清掉卡住的会话与异常状态；播放会短暂中断</span>
          </div>
          <button class="button compact secondary" type="button" data-refresh-pipelines ${debug ? "" : "disabled"}>重建</button>
        </div>
      </div>
    </details>

    ${renderControlRuntime(openSections.has("control"))}

    <details class="diagnostic-details" data-debug-section="log" ${openSections.has("log") ? "open" : ""}>
      <summary><span><strong>运行记录</strong><small>最近的连接与播放情况</small></span></summary>
      <section class="runtime-log-panel" aria-label="运行记录">
      <div class="runtime-log-toolbar">
        <div class="live-indicator${logFreeze ? " paused" : ""}"><span></span><strong>${logFreeze ? "已冻结" : "自动更新"}</strong></div>
        <select class="log-filter" data-log-source aria-label="日志来源">
          ${(["app", "all"] as const).map((value) => `<option value="${value}" ${logFilter.source === value ? "selected" : ""}>${LOG_SOURCE_LABELS[value]}</option>`).join("")}
        </select>
        <select class="log-filter" data-log-level aria-label="日志级别">
          ${(["all", "warn"] as const).map((value) => `<option value="${value}" ${logFilter.level === value ? "selected" : ""}>${LOG_LEVEL_LABELS[value]}</option>`).join("")}
        </select>
        <select class="log-filter log-freeze" data-log-freeze aria-label="时间范围">
          ${FREEZE_OPTIONS.map(([value, label]) => `<option value="${value}" ${freezeSelectValue() === value ? "selected" : ""}>${label}</option>`).join("")}
        </select>
        <div class="runtime-log-actions"><button class="button plain log-action" type="button" data-log-copy>复制</button>
        <button class="button plain log-action" type="button" data-log-clear>清空</button>
        <button class="button plain log-action log-report-action" type="button" data-log-report title="下载脱敏后的设置、实时状态与所选时段的日志，反馈问题时请附上">导出报告</button></div>
      </div>
      <div class="log-freeze-bar" data-log-freeze-bar ${logFreeze ? "" : "hidden"}>${renderFreezeBar(debug)}</div>
      <div class="runtime-log" role="log" aria-label="运行记录" data-runtime-log>
        ${renderRuntimeLogRows(debug)}
      </div>
      <div class="runtime-log-footer" data-log-footer ${debug?.logs.truncated ? "" : "hidden"}>${renderLogFooter(debug)}</div>
      </section>
    </details>

  `;
}

export function renderTechnicalMetrics(debug: DebugState | null): string {
  const raop = Object.values(debug?.diagnostics?.raop || {});
  const sessions = inputSessions(debug);
  const serviceLabel =
    ({
      running: "运行中",
      idle: "空闲",
      error: "出错",
      stopped: "已停止",
      starting: "启动中",
    } as Record<string, string>)[debug?.bridge_status.status ?? ""] || debug?.bridge_status.status || "-";
  return [
    `<span class="meta-chip">服务 ${serviceLabel}</span>`,
    `<span class="meta-chip">AirPlay 会话 经典 ${sessions.classic} · AP2 ${sessions.airplay2}</span>`,
    `<span class="meta-chip">时钟响应 ${sum(raop.map((item) => item.timing_responses))}/${sum(raop.map((item) => item.timing_requests))}</span>`,
    `<span class="meta-chip">补包 ${sum(raop.map((item) => item.resend_requests))}</span>`,
    `<span class="meta-chip">已发送 ${((debug?.stream_bytes_sent ?? 0) / 1024).toFixed(1)} KB</span>`,
    `<span class="meta-chip">编码 ${debug?.audio_config.format.toUpperCase() || "-"} · ${debug?.audio_config.bitrate || ""} · ${debug?.audio_config.sample_rate ? `${debug.audio_config.sample_rate / 1000} kHz` : "-"}</span>`,
    `<span class="meta-chip">延迟 ${latencyBreakdown(debug)}</span>`,
  ].join("");
}

/** The 连接明细 summary: what is playing, out of how many streams. */
export function renderStreamSummary(debug: DebugState | null): string {
  const streams = Object.values(debug?.diagnostics?.streams || {});
  const active = streams.filter((item) => item.clients > 0 && item.flowing).length;
  const attention = Object.values(debug?.diagnostics?.entries || {}).filter(
    (item) => item.state !== "idle" && item.state !== "healthy",
  ).length;
  const clients = sum(streams.map((item) => item.clients));
  const parts = [active ? `${active} 路播放中` : "无播放", `${clients} 个取流连接`, `${streams.length} 条音频管道`];
  if (attention) parts.push(`${attention} 项待观察`);
  return parts.join(" · ");
}

interface StreamRow {
  id: string;
  name: string;
  /** "playing" | "waiting" (sender connected, nobody pulling) | "idle" */
  status: "playing" | "waiting" | "idle";
  meta: string;
  kickable: boolean;
}

function streamRows(debug: DebugState | null, state: State): StreamRow[] {
  const streams = debug?.diagnostics?.streams || {};
  const raop = debug?.diagnostics?.raop || {};
  return Object.keys(streams)
    .map((id) => {
      const s = streams[id];
      // Stream ids carry suffixes: stereo channels "-L"/"-R" and per-EQ
      // splits "-q1"… (topology.py: "<receiver>-L-q1"). Strip both, resolve
      // the receiver's display name, then reattach human-readable tags.
      let baseId = id;
      const tags: string[] = [];
      let channelTag: "L" | "R" | null = null;
      const eqMatch = baseId.match(/-q(\d+)$/);
      let eqTag: string | null = null;
      if (eqMatch) {
        baseId = baseId.slice(0, -eqMatch[0].length);
        eqTag = eqMatch[1];
      }
      const channelMatch = baseId.match(/-(L|R)$/);
      if (channelMatch) {
        channelTag = channelMatch[1] as "L" | "R";
        tags.unshift(channelTag === "L" ? "左" : "右");
        baseId = baseId.slice(0, -2);
      }
      if (eqTag !== null) tags.push(eqSplitTag(state, baseId, channelTag, Number(eqTag)));
      const baseName = state.status?.receivers.find((r) => r.did === baseId)?.name
        || state.airplay2?.instances.find((r) => r.id === baseId)?.name
        || baseId;
      const name = tags.length ? `${baseName} · ${tags.join(" · ")}` : baseName;
      const sessions = raop[id]?.active_sessions ?? raop[baseId]?.active_sessions ?? 0;
      const mb = (s.bytes_sent / 1048576).toFixed(1);
      const active = s.clients > 0 && s.flowing;
      const status: StreamRow["status"] = active ? "playing" : sessions > 0 || s.clients > 0 ? "waiting" : "idle";
      const meta = active
        ? `${s.clients} 台取流 · ${mb} MB${s.dropped_chunks ? ` · 丢弃 ${s.dropped_chunks}` : ""}`
        : sessions > 0
          ? `手机已连接 · 已发 ${mb} MB`
          : s.clients > 0
            ? `正在清理 · 已发 ${mb} MB`
            : `已发 ${mb} MB`;
      return { id, name, status, meta, kickable: active || sessions > 0 };
    })
    // Live rows first: the idle variants of one entry used to bury the stream
    // that actually carried audio under four "0.0 MB" rows.
    .sort((a, b) => {
      const order = { playing: 0, waiting: 1, idle: 2 } as const;
      return order[a.status] - order[b.status] || a.name.localeCompare(b.name, "zh");
    });
}

export function renderStreamRows(debug: DebugState | null, state: State): string {
  const rows = streamRows(debug, state);
  if (!rows.length) {
    return `<div class="cell"><span class="cell-subtitle">暂无传输连接</span></div>`;
  }
  const statusLabel = { playing: "播放中", waiting: "等待", idle: "空闲" } as const;
  // The status word already leads the line, so `meta` carries facts only —
  // otherwise an idle row read "空闲 · 空闲 · 已发 0.0 MB".
  return `<div class="stream-list">${rows
    .map(
      (row) => `
      <div class="stream-row" data-status="${row.status}">
        <span class="stream-dot" aria-hidden="true"></span>
        <div class="stream-identity">
          <span class="stream-name" title="${escapeHtml(row.name)}">${escapeHtml(row.name)}</span>
          <span class="stream-meta">${statusLabel[row.status]} · ${escapeHtml(row.meta)}</span>
        </div>
        <button class="button plain stream-kick" data-kick-stream="${escapeHtml(row.id)}" ${row.kickable ? "" : "disabled"}>断开</button>
      </div>`,
    )
    .join("")}</div>`;
}

export function bindStreamKicks(container: HTMLElement, showToast: (msg: string) => void) {
  container.querySelectorAll<HTMLElement>("[data-kick-stream]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.kickStream;
      if (!id) return;
      btn.setAttribute("disabled", "");
      try {
        const result = await api.kickStream(id);
        showToast(`已断开 ${result.sender_sessions} 个手机会话，并停止目标音箱`);
      } catch (e) {
        btn.removeAttribute("disabled");
        showToast(`断开失败: ${e instanceof Error ? e.message : "未知错误"}`);
      }
    });
  });
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function latencyBreakdown(debug: DebugState | null): string {
  const raop = Object.values(debug?.diagnostics?.raop || {});
  const streams = Object.values(debug?.diagnostics?.streams || {});
  const input = Math.max(0, ...raop.map((item) => item.input_buffer_ms || 0));
  const active = streams.filter((item) => item.clients > 0);
  if (!active.length) return "等待音箱取流";
  const encoding = Math.max(0, ...active.map((item) => item.latency?.encoding_ms || 0));
  const buffer = Math.max(0, ...active.map((item) => item.latency?.stream_buffer_ms || 0));
  const queue = Math.max(0, ...active.map((item) => item.latency?.send_queue_ms || 0));
  return `输入 ${input} ms · 编码 ${encoding} ms · 缓冲 ${buffer} ms · 队列 ${queue} ms`;
}

export function bindDebugPanel(container: HTMLElement, showToast: (msg: string) => void, rerender?: () => void) {
  const source = container.querySelector<HTMLSelectElement>("[data-log-source]");
  const level = container.querySelector<HTMLSelectElement>("[data-log-level]");
  // Remember which sections are open, so a re-render rebuilds them open again.
  container.querySelectorAll<HTMLDetailsElement>("details[data-debug-section]").forEach((section) => {
    section.addEventListener("toggle", () => {
      const key = section.dataset.debugSection;
      if (!key) return;
      if (section.open) openSections.add(key);
      else openSections.delete(key);
    });
  });
  const rerenderTestPanel = () => {
    (document.activeElement as HTMLElement | null)?.blur?.();
    rerender?.();

  };
  bindStreamKicks(container, showToast);
  bindControlRuntime(container, rerenderTestPanel);
  const refreshBtn = container.querySelector<HTMLButtonElement>("[data-refresh-pipelines]");
  refreshBtn?.addEventListener("click", async () => {
    refreshBtn.disabled = true;
    refreshBtn.textContent = "正在重建…";
    try {
      await api.refreshPipelines();
      showToast("管道已重建");
    } catch (e) {
      showToast(`刷新失败: ${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      rerender?.();
    }
  });
  container.querySelector<HTMLSelectElement>("[data-debug-target]")?.addEventListener("change", (event) => {
    debugTargetKey = (event.currentTarget as HTMLSelectElement).value;
    rerenderTestPanel();
  });
  container.querySelectorAll<HTMLInputElement>('input[name="debug-source"]').forEach((input) => input.addEventListener("change", () => {
    debugTestSource = input.value as typeof debugTestSource;
    rerenderTestPanel();
  }));
  container.querySelector<HTMLInputElement>("[data-test-file]")?.addEventListener("change", async (event) => {
    const file = (event.currentTarget as HTMLInputElement).files?.[0];
    if (!file) return;
    debugTestBusy = "upload";
    rerenderTestPanel();
    try {
      const existing = getTestMedia();
      if (existing) await api.deleteTestMedia(existing.token).catch(() => undefined);
      const uploaded = await api.uploadTestMedia(file);
      setTestMedia(uploaded);
      showToast(`${uploaded.name} 已准备好`);
    } catch (e) {
      showToast(`上传失败: ${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      debugTestBusy = "";
      rerenderTestPanel();
    }
  });
  container.querySelector("[data-remove-test-media]")?.addEventListener("click", async () => {
    const media = getTestMedia();
    setTestMedia(null);
    rerenderTestPanel();
    if (media) await api.deleteTestMedia(media.token).catch(() => undefined);
  });
  container.querySelector("[data-debug-test-action]")?.addEventListener("click", async () => {
    debugTestBusy = activeTestSession ? "stop" : "start";
    rerenderTestPanel();
    try {
      if (activeTestSession) {
        const session = activeTestSession;
        activeTestSession = "";
        const result = await api.stopDebugTest(session, true);
        showToast(result.restored ? "测试已停止，原播放已恢复" : "测试已停止");
      } else {
        const state = store.get();
        const devices = selectedTestDeviceIds(state, state.debug);
        const url = container.querySelector<HTMLInputElement>("[data-debug-url]")?.value.trim();
        const media = getTestMedia();
        if (debugTestSource === "upload" && !media) throw new Error("请先上传测试音频");
        if (debugTestSource === "url" && !url) throw new Error("请输入音频地址");
        const result = await api.startDebugTest({
          device_ids: devices,
          source: debugTestSource,
          ...(media ? { media_token: media.token } : {}),
          ...(url ? { url } : {}),
        });
        activeTestSession = result.session_id;
        showToast(`测试已发送到 ${result.members.length} 台音箱`);
      }
    } catch (e) {
      showToast(`测试失败: ${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      debugTestBusy = "";
      rerenderTestPanel();
    }
  });
  container.querySelector("#btn-debug-codecs")?.addEventListener("click", async () => {
    const devices = selectedTestDeviceIds(store.get(), store.get().debug);
    if (!devices.length || debugTestBusy) return;
    debugTestBusy = "start";
    rerenderTestPanel();
    showToast("正在逐项检测格式，当前音频会在完成后自动恢复");
    try {
      const result = await api.runCodecTest(devices);
      const common = result.common_formats.length ? result.common_formats.join("、") : "暂无已确认共同格式";
      showToast(result.restore_failed.length ? `检测完成：${common}；部分音箱恢复失败` : `检测完成：${common}`);
      await api.getDevices(true).then((items) => store.set({ devices: items }));
    } catch (e) {
      showToast(`格式检测失败: ${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      debugTestBusy = "";
      rerenderTestPanel();
    }
  });
  source?.addEventListener("change", () => {
    logFilter = { ...logFilter, source: (source.value as LogQuery["source"]) || "app" };
    logServerTime = store.get().debug?.logs.server_time ?? logServerTime;
    refreshLogViews();
  });
  level?.addEventListener("change", () => {
    logFilter = { ...logFilter, level: (level.value as LogQuery["level"]) || "all" };
    logServerTime = store.get().debug?.logs.server_time ?? logServerTime;
    refreshLogViews();
  });
  const freeze = container.querySelector<HTMLSelectElement>("[data-log-freeze]");
  freeze?.addEventListener("change", () => {
    if (freeze.value !== "custom") {
      applyFreezeChoice(freeze.value, freeze);
      return;
    }
    // 自定义时段 lives in a dialog, not in the select: whatever comes back
    // decides the value, and a cancelled dialog must not leave the control
    // claiming a custom window the panel is not showing.
    void openLogScopeDialog("freeze").then(() => {
      freeze.value = freezeSelectValue();
    });
  });
  // Delegated, on the section rather than the whole page: the freeze bar's own
  // markup is replaced on every poll (so a listener on the button would not
  // survive), while the section itself is rebuilt by each render (so a listener
  // out here cannot pile up).
  container.querySelector<HTMLElement>(".runtime-log-panel")?.addEventListener("click", (event) => {
    if (!(event.target as HTMLElement | null)?.closest("[data-log-unfreeze]")) return;
    applyFreezeChoice("follow", container.querySelector<HTMLSelectElement>("[data-log-freeze]"));
  });
  container.querySelector("[data-log-copy]")?.addEventListener("click", () => {
    void openLogScopeDialog("copy");
  });
  container.querySelector("[data-log-clear]")?.addEventListener("click", async () => {
    const confirmed = await confirmAction(
      "清空运行记录？",
      "只清除本进程内存中的日志；已经导出的报告不受影响。清空后新的记录会继续累积。",
      "清空",
    );
    if (!confirmed) return;
    try {
      const result = await api.clearRuntimeLog();
      // Drop the store's copy as well: without it a later re-render would paint
      // the cleared lines back from stale state. Everything describing what the
      // buffer held is zeroed; its capacity and clock still stand.
      const current = store.get().debug;
      if (current) {
        store.set({
          debug: {
            ...current,
            logs: {
              ...current.logs,
              items: [],
              total: 0,
              shown: 0,
              truncated: false,
              new_count: 0,
              covered: { from: null, to: null },
              buffer_total: 0,
              buckets: Object.fromEntries(Object.keys(current.logs.buckets).map((key) => [key, 0])),
            },
          },
        });
      }
      refreshLogViews();
      showToast(`已清空 ${result.cleared} 条运行记录`);
    } catch (e) {
      showToast(`清空失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });
  container.querySelector("[data-log-report]")?.addEventListener("click", () => {
    void openLogScopeDialog("export");
  });
  const ttsBtn = container.querySelector("#btn-debug-tts");
  ttsBtn?.addEventListener("click", async () => {
    const did = selectedTestDeviceIds(store.get(), store.get().debug)[0];
    debugTestBusy = "tts";
    rerenderTestPanel();
    try {
      await api.debugTTS("调试测试", did);
      showToast("语音命令已发送，请确认音箱是否出声");
    } catch (e) {
      showToast(`TTS 失败: ${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      debugTestBusy = "";
      rerenderTestPanel();
    }
  });

  container.querySelector("#btn-debug-play-stream")?.addEventListener("click", async () => {
    try {
      const latestDebug = await api.getDebugState();
      const selectedStream = findSelectedAirPlayStream(store.get(), latestDebug);
      if (!selectedStream) {
        showToast("所选音箱尚未添加到 AirPlay");
        return;
      }
      await api.debugPlayUrl(selectedStream.url);
      showToast(`已接回“${selectedStream.name}”`);
    } catch (e) {
      showToast(`播放失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
  });


  const volume = container.querySelector<HTMLInputElement>("#debug-volume");
  const volumeOutput = container.querySelector<HTMLOutputElement>("#debug-volume-output");
  let volumeTimer: ReturnType<typeof setTimeout> | null = null;
  volume?.addEventListener("input", () => {
    const value = Number(volume.value);
    volume.style.setProperty("--volume", `${value}%`);
    if (volumeOutput) volumeOutput.value = String(value);
    if (volumeTimer) clearTimeout(volumeTimer);
    volumeTimer = setTimeout(async () => {
      try {
        const dids = selectedTestDeviceIds(store.get(), store.get().debug);
        if (!dids.length) return;
        await setVolume(value, dids);
      }
      catch (e) { showToast(`音量设置失败: ${e instanceof Error ? e.message : "未知错误"}`); }
    }, 100);
  });
}

function findSelectedAirPlayStream(state: State, debug: DebugState | null): { id: string; name: string; url: string } | null {
  const selectedDid = selectedTestDeviceIds(state, debug)[0];
  if (!selectedDid) return null;
  const definition = state.fullConfig?.receivers.find(
    (item) => item.enabled && item.target_type === "speaker" && item.target_id === selectedDid
  );
  if (!definition) return null;
  const runtime = state.status?.receivers.find((item) => item.did === definition.id);
  if (!runtime?.stream_url) return null;
  return { id: definition.id, name: definition.name, url: runtime.stream_url };
}

export function renderRuntimeLogRows(debug: DebugState | null): string {
  const items = debug?.logs.items || [];
  if (!items.length) {
    // Three different reasons, three different sentences: a page that never
    // loaded its data is not a process that logged nothing, and an empty window
    // is not an empty buffer.
    const text =
      debug === null
        ? "正在读取运行记录…"
        : debug.logs.buffer_total
          ? "该时段没有记录，换个时间或筛选看看"
          : "暂无运行记录（应用刚启动，或日志已被清空）";
    return `<div class="empty-log">${text}</div>`;
  }
  // Oldest first, exactly as the server sent the slice: the newest line stays
  // at the bottom of the container, which is where `updateRuntimeLog` pins the
  // scroll and where the row markup has always lived.
  return items.map((item) => `<article class="runtime-log-row ${item.level.toLowerCase()}">
    <div class="runtime-log-meta"><time>${escapeHtml(item.time)}</time><span>${escapeHtml(item.level)}</span><code>${escapeHtml(shortLogger(item.logger))}</code></div>
    <p>${escapeHtml(item.message)}</p>
  </article>`).join("");
}

/** Which toolbar option represents the current freeze: the preset it came from,
 *  or 自定义 for a hand-picked interval. */
function freezeSelectValue(): string {
  return logFreeze?.key ?? "follow";
}

/** The frozen bar's inner markup. One renderer for the first paint and the
 *  poller's refresh, so the two cannot drift. */
function renderFreezeBar(debug: DebugState | null): string {
  if (!logFreeze) return "";
  const logs = debug?.logs;
  return `<span><strong>已冻结 ${formatClock(logFreeze.since)}–${formatClock(logFreeze.until)}</strong>${
    logs ? `<small>共 ${logs.total} 条</small>` : ""
  }</span>
    ${logs && logs.new_count > 0 ? `<span class="log-freeze-new">有 ${logs.new_count} 条新记录</span>` : ""}
    <button class="button plain log-action" type="button" data-log-unfreeze>恢复跟随</button>`;
}

/** The truncation line's text; empty unless the slice was capped. */
function renderLogFooter(debug: DebugState | null): string {
  const logs = debug?.logs;
  if (!logs?.truncated) return "";
  return `该时段共 ${logs.total} 条 · 显示最近 ${logs.shown} 条`;
}

/**
 * Re-render the panel from the state we already have, then — unless this call
 * is already the follow-up — ask the server for the slice the new choice
 * describes. A filter or a frozen window only exist server-side, so without the
 * fetch the rows would sit on the old answer for a whole poll interval and the
 * control would look broken.
 */
function refreshLogViews(reload = true): void {
  const debug = store.get().debug;
  const log = document.querySelector<HTMLElement>("[data-runtime-log]");
  if (log) updateRuntimeLog(log, debug);
  updateLogChrome(debug);
  if (!reload) return;
  void api
    .getDebugState(currentLogQuery())
    .then((fresh) => {
      store.set({ debug: fresh });
      refreshLogViews(false);
    })
    // A failed refresh is the poller's problem: it retries on its own cadence.
    .catch(() => undefined);
}

/**
 * Refresh the panel's chrome — the live dot, the frozen bar and the truncation
 * footer — around the log body, which owns its own scroll position. Called by
 * the poller on every tick; the body itself stays `updateRuntimeLog`'s job.
 */
export function updateLogChrome(debug: DebugState | null): void {
  if (debug) logServerTime = debug.logs.server_time;
  const indicator = document.querySelector<HTMLElement>(".runtime-log-panel .live-indicator");
  if (indicator) {
    indicator.classList.toggle("paused", logFreeze !== null);
    const label = indicator.querySelector("strong");
    if (label) label.textContent = logFreeze ? "已冻结" : "自动更新";
  }
  const bar = document.querySelector<HTMLElement>("[data-log-freeze-bar]");
  if (bar) {
    bar.innerHTML = renderFreezeBar(debug);
    bar.hidden = logFreeze === null;
  }
  const footer = document.querySelector<HTMLElement>("[data-log-footer]");
  if (footer) {
    footer.textContent = renderLogFooter(debug);
    footer.hidden = !debug?.logs.truncated;
  }
}

/** Apply a choice from the 时间范围 select. `select` is left claiming 跟随最新
 *  whenever the choice could not be honoured, so the control never describes a
 *  window the panel is not showing. */
function applyFreezeChoice(key: string, select: HTMLSelectElement | null): void {
  if (key === "follow") {
    logFreeze = null;
    if (select) select.value = "follow";
    refreshLogViews();
    return;
  }
  const seconds = WINDOW_SECONDS[key];
  if (!seconds || !logServerTime) {
    store.showToast("还没收到状态，稍后再冻结");
    if (select) select.value = "follow";
    return;
  }
  logFreeze = {
    key,
    label: WINDOW_LABELS[key],
    since: logServerTime - seconds,
    until: logServerTime,
  };
  if (select) select.value = key;
  refreshLogViews();
}

/** The download toast's size, so the user can tell what they are about to send. */
function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1048576) return `${Math.round(size / 1024)} KB`;
  return `${(size / 1048576).toFixed(1)} MB`;
}

function confirmAction(title: string, message: string, action: string): Promise<boolean> {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog";
    dialog.innerHTML = `<form method="dialog">
      <div class="confirm-dialog-copy"><h3>${escapeHtml(title)}</h3><p>${escapeHtml(message)}</p></div>
      <div class="confirm-dialog-actions">
        <button class="button plain" value="cancel">取消</button>
        <button class="button danger" value="confirm">${escapeHtml(action)}</button>
      </div>
    </form>`;
    document.body.appendChild(dialog);
    dialog.addEventListener("close", () => {
      const confirmed = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(confirmed);
    }, { once: true });
    dialog.addEventListener("cancel", () => dialog.close("cancel"));
    dialog.showModal();
    dialog.querySelector<HTMLButtonElement>('[value="cancel"]')?.focus();
  });
}

function shortLogger(name: string): string {
  return name.replace("micast.raop.server", "AirPlay").replace("micast.", "MiCast · ");
}

type LogScopeMode = "copy" | "export" | "freeze";

/** What the shared dialog got back: the chosen window, with absolute bounds
 *  only when the user named a clock interval. A duration preset travels as a
 *  key alone — the server resolves it against its own clock at request time. */
interface LogScopeChoice {
  key: string;
  /** Name of the window for the confirmation toast. */
  label: string;
  since?: number;
  until?: number;
}

const LOG_SCOPE_MODES: Record<LogScopeMode, { title: string; confirm: string }> = {
  copy: { title: "复制运行记录", confirm: "复制到剪贴板" },
  export: { title: "导出运行记录", confirm: "下载报告" },
  freeze: { title: "冻结自定义时段", confirm: "冻结" },
};

/** HH:MM:SS for a clock input, blank when we have no time to prefill it with. */
function clockInput(at: number): string {
  return at ? formatClock(at) : "";
}

/**
 * A `HH:MM:SS` pick resolved against an anchor date. The inputs carry no date,
 * so a pick that lands more than a minute past the anchor's own clock belongs
 * to the previous day — that is what makes a span across midnight work.
 */
function resolveClockInput(value: string, anchor: number): number | null {
  const match = value.match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
  if (!match) return null;
  const date = new Date(anchor * 1000);
  date.setHours(Number(match[1]), Number(match[2]), Number(match[3] ?? "0"), 0);
  if (date.getTime() / 1000 - anchor > 60) date.setDate(date.getDate() - 1);
  return date.getTime() / 1000;
}

/** Run the chosen selection through the server, so the text and the file carry
 *  exactly the records the window names. */
async function runLogScope(mode: LogScopeMode, query: LogQuery, scopeLabel: string): Promise<void> {
  if (mode === "copy") {
    try {
      const { text, count } = await api.copyRuntimeLog(query);
      await navigator.clipboard.writeText(text);
      store.showToast(`已复制 ${count} 条日志`);
    } catch {
      store.showToast("复制失败，请手动选择日志");
    }
    return;
  }
  try {
    const { size, count } = await api.downloadDebugReport(query);
    store.showToast(`报告已下载 · ${formatBytes(size)} · ${count} 条 · ${scopeLabel}`);
  } catch (e) {
    store.showToast(`下载失败: ${e instanceof Error ? e.message : "未知错误"}`);
  }
}

/**
 * Ask which window the copy, the export or a manual freeze should cover. The
 * three share one panel because they ask the same question, and because the
 * count next to each row comes from the same buckets the server would hand back.
 */
function openLogScopeDialog(mode: LogScopeMode): Promise<void> {
  return new Promise((resolve) => {
    const debug = store.get().debug;
    const logs = debug?.logs ?? null;
    const buckets = logs?.buckets ?? {};
    const covered = logs?.covered ?? { from: null, to: null };
    const anchor = covered.to ?? logs?.server_time ?? 0;
    // 本次运行 is defined as "whatever the buffer holds", so it has no bounds to
    // freeze; a custom interval covers that ground better. Absent buckets (no
    // response yet) fall back to the full preset list, counts at zero.
    const keys = Object.keys(buckets).length ? Object.keys(buckets) : Object.keys(WINDOW_LABELS);
    const presetKeys = keys.filter((key) => mode !== "freeze" || key !== "session");
    const rows = [
      ...(logFreeze
        ? [{ value: "frozen", label: "已冻结时段", count: `${logs?.total ?? 0} 条`, default: true }]
        : []),
      ...presetKeys.map((key) => ({
        value: key,
        label: WINDOW_LABELS[key] ?? key,
        count: `${buckets[key] ?? 0} 条`,
        default: !logFreeze && key === "15m",
      })),
      { value: "custom", label: "自定义时段…", count: "–", default: false },
    ];
    if (!rows.some((row) => row.default)) rows[0].default = true;
    const defaultSince = logFreeze?.since ?? covered.from ?? logs?.server_time ?? 0;
    const defaultUntil = logFreeze?.until ?? covered.to ?? logs?.server_time ?? 0;
    const coveredHint =
      covered.from !== null && covered.to !== null
        ? `缓冲区保留 ${formatClock(covered.from)}–${formatClock(covered.to)}`
        : "缓冲区还没有记录";

    const dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog log-scope-dialog";
    dialog.innerHTML = `<form method="dialog">
      <div class="confirm-dialog-copy">
        <h3>${escapeHtml(LOG_SCOPE_MODES[mode].title)}</h3>
        <p>${escapeHtml(`${LOG_SOURCE_LABELS[logFilter.source]} · ${LOG_LEVEL_LABELS[logFilter.level]}`)}</p>
      </div>
      <div class="log-scope-options" role="radiogroup" aria-label="时间范围">
        ${rows
          .map(
            (row) => `<label class="log-scope-option">
          <input type="radio" name="log-scope" value="${escapeHtml(row.value)}" ${row.default ? "checked" : ""}>
          <span>${escapeHtml(row.label)}</span>
          <em>${escapeHtml(row.count)}</em>
        </label>`,
          )
          .join("")}
      </div>
      <div class="log-scope-custom" data-log-custom hidden>
        <label><span>开始</span><input type="time" step="1" data-log-since aria-label="开始时间" value="${clockInput(defaultSince)}"></label>
        <label><span>结束</span><input type="time" step="1" data-log-until aria-label="结束时间" value="${clockInput(defaultUntil)}"></label>
        <p class="log-scope-hint">${escapeHtml(coveredHint)}</p>
      </div>
      <div class="confirm-dialog-actions">
        <button class="button plain" value="cancel">取消</button>
        <button class="button primary" value="confirm">${escapeHtml(LOG_SCOPE_MODES[mode].confirm)}</button>
      </div>
    </form>`;

    const custom = dialog.querySelector<HTMLElement>("[data-log-custom]");
    const syncCustom = () => {
      const value = dialog.querySelector<HTMLInputElement>('input[name="log-scope"]:checked')?.value;
      if (custom) custom.hidden = value !== "custom";
    };
    dialog.querySelectorAll<HTMLInputElement>('input[name="log-scope"]').forEach((input) =>
      input.addEventListener("change", syncCustom),
    );
    syncCustom();

    /** Null when the choice is not usable; the caller keeps the dialog open. */
    const resolveChoice = (): LogScopeChoice | null => {
      const value = dialog.querySelector<HTMLInputElement>('input[name="log-scope"]:checked')?.value;
      if (!value) return null;
      if (value === "frozen" && logFreeze) {
        return {
          key: value,
          label: `${formatClock(logFreeze.since)}–${formatClock(logFreeze.until)}`,
          since: logFreeze.since,
          until: logFreeze.until,
        };
      }
      if (value !== "custom") return { key: value, label: WINDOW_LABELS[value] ?? value };
      if (!anchor) {
        store.showToast("还没收到状态，稍后再选时段");
        return null;
      }
      const since = resolveClockInput(dialog.querySelector<HTMLInputElement>("[data-log-since]")?.value ?? "", anchor);
      const until = resolveClockInput(dialog.querySelector<HTMLInputElement>("[data-log-until]")?.value ?? "", anchor);
      if (since === null || until === null) {
        store.showToast("请填写开始与结束时间");
        return null;
      }
      if (since >= until) {
        store.showToast("结束时间要晚于开始时间");
        return null;
      }
      return {
        key: value,
        label: `${formatClock(since)}–${formatClock(until)}`,
        since,
        until,
      };
    };

    const apply = (choice: LogScopeChoice) => {
      if (mode !== "freeze") {
        const query: LogQuery =
          choice.since !== undefined && choice.until !== undefined
            ? { source: logFilter.source, level: logFilter.level, since: choice.since, until: choice.until }
            : { source: logFilter.source, level: logFilter.level, window: choice.key as LogQuery["window"] };
        void runLogScope(mode, query, choice.label);
        return;
      }
      const seconds = WINDOW_SECONDS[choice.key];
      if (choice.since !== undefined && choice.until !== undefined) {
        logFreeze = {
          key: choice.key === "frozen" ? "custom" : choice.key,
          label: WINDOW_LABELS[choice.key] ?? choice.label,
          since: choice.since,
          until: choice.until,
        };
      } else if (seconds && logServerTime) {
        logFreeze = {
          key: choice.key,
          label: WINDOW_LABELS[choice.key],
          since: logServerTime - seconds,
          until: logServerTime,
        };
      } else {
        store.showToast("还没收到状态，稍后再冻结");
        return;
      }
      refreshLogViews();
    };

    // Validate before the dialog closes, not after: a rejected interval has to
    // leave the panel the user just built on screen.
    dialog.querySelector<HTMLButtonElement>('[value="confirm"]')?.addEventListener("click", (event) => {
      if (!resolveChoice()) event.preventDefault();
    });
    dialog.addEventListener("close", () => {
      const choice = dialog.returnValue === "cancel" ? null : resolveChoice();
      dialog.remove();
      resolve();
      if (choice) apply(choice);
    });
    dialog.addEventListener("cancel", () => dialog.close("cancel"));
    document.body.appendChild(dialog);
    dialog.showModal();
    dialog.querySelector<HTMLButtonElement>('[value="cancel"]')?.focus();
  });
}

/**
 * Re-render the log rows while preserving a deliberate reading position:
 * pinned to the newest entry when the user is already at (or near) the bottom,
 * untouched when they have scrolled up to read history. Call this instead of
 * assigning innerHTML directly — the newest line lives at the end, so an
 * unanchored replace reads as the list endlessly scrolling.
 */
export function updateRuntimeLog(log: HTMLElement, debug: DebugState | null): void {
  const markup = renderRuntimeLogRows(debug);
  if (log.innerHTML === markup) return;
  const outer = log.closest<HTMLElement>('.app-body');
  const outerTop = outer?.scrollTop ?? 0;
  const previousTop = log.scrollTop;
  const nested = getComputedStyle(log).overflowY !== 'visible';
  const pinned = nested && log.scrollHeight - previousTop - log.clientHeight <= 48;
  log.innerHTML = markup;
  if (nested) log.scrollTop = pinned ? log.scrollHeight : previousTop;
  // A growing desktop log must never commandeer the application's scroller.
  if (outer) outer.scrollTop = outerTop;
}


/** Refresh live projections without remounting diagnostic controls or scrollers. */
export function updateDebugPanel(container: HTMLElement, state: State): void {
  const debug = state.debug;
  if (!debug) return;
  const scroller = container.closest<HTMLElement>('.app-body');
  const scrollTop = scroller?.scrollTop ?? 0;
  const log = container.querySelector<HTMLElement>('[data-runtime-log]');
  if (log) updateRuntimeLog(log, debug);
  updateLogChrome(debug);
  const update = (selector: string, markup: string) => {
    const node = container.querySelector<HTMLElement>(selector);
    if (!node || node.innerHTML === markup) return null;
    node.innerHTML = markup;
    return node;
  };
  update('[data-connection-checks]', renderStatusOverview(debug, state));
  const streams = update('[data-stream-list]', renderStreamRows(debug, state));
  if (streams) bindStreamKicks(streams, message => store.showToast(message));
  const summary = container.querySelector<HTMLElement>('[data-stream-summary]');
  if (summary) summary.textContent = renderStreamSummary(debug);
  update('[data-audio-path]', renderAudioPath(debug));
  update('[data-technical-metrics]', renderTechnicalMetrics(debug));
  if (scroller) scroller.scrollTop = scrollTop;
}
