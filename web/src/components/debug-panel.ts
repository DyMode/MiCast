import { api, type DebugState } from "../api";
import { icon } from "../icons";
import { store } from "../state";
import { setVolume } from "../volume-service";
import type { State } from "../state";
import { EQ_PRESET_LABELS } from "./devices-view";
import { getTestMedia, setTestMedia } from "../test-media";

export type { DebugState };

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

function inputSubtitle(sessions: { classic: number; airplay2: number; total: number }): string {
  if (sessions.total === 0) return "目前没有手机传输音频";
  if (sessions.classic && sessions.airplay2) return `${sessions.classic} 个手机与 AirPlay 2 正在传输音频`;
  if (sessions.airplay2) return "AirPlay 2 正在传输音频";
  return `${sessions.classic} 个手机正在传输音频`;
}

export function renderConnectionChecks(debug: DebugState | null, state: State): string {
  const raop = Object.values(debug?.diagnostics?.raop || {});
  const streams = Object.values(debug?.diagnostics?.streams || {});
  const sessions = inputSessions(debug);
  const streamClients = sum(streams.filter((item) => item.flowing).map((item) => item.clients));
  const playbackActive = sessions.total > 0 || streamClients > 0;
  const transportErrors = sum(raop.map((item) => item.dropped_packets + item.decode_errors)) + sum(streams.map((item) => item.dropped_chunks));
  // Input side: classic AirPlay reports it on the RAOP session; AirPlay 2 has
  // no RAOP session, so its figure rides on the stream (pipeline pacing).
  const inputBufferMs = Math.max(
    0,
    ...raop.map((item) => item.input_buffer_ms || 0),
    ...streams.map((item) => item.input_buffer_ms || 0),
  );
  const streamLatency = Math.max(0, ...streams.filter((item) => item.flowing).map((item) => item.latency?.estimated_ms || 0));
  const latencyMs = inputBufferMs + streamLatency;
  const latencyLabel = sessions.total === 0
    ? "等待音频"
    : streamClients === 0
      ? "等待音箱取流"
      : `约 ${latencyMs} ms（输入 ${inputBufferMs} + 链路 ${streamLatency}）`;
  const latencyState = streamClients === 0 ? "未测量" : latencyMs <= 500 ? "稳定" : latencyMs <= 1000 ? "较高" : "过高";
  return `
      <div class="cell">
        <div class="cell-icon ${sessions.total > 0 ? "green" : "gray"}">${icon("antenna")}</div>
        <div class="cell-content">
          <span class="cell-title">音频输入</span>
          <span class="cell-subtitle">${inputSubtitle(sessions)}</span>
        </div>
        <span class="plain-state ${sessions.total > 0 ? "success" : ""}">${sessions.total > 0 ? "已连接" : "等待播放"}</span>
      </div>
      <div class="cell">
        <div class="cell-icon ${streamClients > 0 ? "green" : "gray"}">${icon("speaker")}</div>
        <div class="cell-content">
          <span class="cell-title">音箱输出</span>
          <span class="cell-subtitle">${streamClients > 0 ? `${streamClients} 台音箱正在接收音频` : "当前没有音箱接收 MiCast 音频"}</span>
        </div>
        <span class="plain-state ${streamClients > 0 ? "success" : ""}">${streamClients > 0 ? "正在接收" : "未连接"}</span>
      </div>
      <div class="cell">
        <div class="cell-icon ${playbackActive && transportErrors > 0 ? "red" : playbackActive ? "green" : "gray"}">${playbackActive && transportErrors > 0 ? "!" : playbackActive ? "✓" : "—"}</div>
        <div class="cell-content">
          <span class="cell-title">音频传输质量</span>
          <span class="cell-subtitle">${!playbackActive ? "开始播放后检查当前传输质量" : transportErrors > 0 ? `当前会话检测到 ${transportErrors} 个丢包、解码或流错误` : "当前传输未检测到丢包或解码错误"}</span>
        </div>
        <span class="plain-state ${playbackActive ? (transportErrors > 0 ? "error" : "success") : ""}">${!playbackActive ? "等待播放" : transportErrors > 0 ? "需要检查" : "正常"}</span>
      </div>
      <div class="cell">
        <div class="cell-icon ${streamClients > 0 ? (latencyMs > 1000 ? "red" : "green") : "gray"}">${icon("clock")}</div>
        <div class="cell-content">
          <span class="cell-title">传输延迟 <small>估算</small></span>
          <span class="cell-subtitle">${latencyLabel}</span>
        </div>
        <span class="plain-state ${streamClients > 0 && latencyMs <= 500 ? "success" : latencyMs > 1000 ? "error" : ""}">${latencyState}</span>
      </div>`;
}

const EVENT_LABELS: Record<string, string> = {
  encoder_stall: "编码停顿",
  encoder_gap: "编码输出间隔",
  source_stall: "音源停滞",
  lag_skip: "延迟线跳过",
  tee_drop: "PCM 分发丢弃",
  encoder_drop: "编码器丢弃",
  client_reconnect: "音箱重连",
};

function formatClock(at: number): string {
  const date = new Date(at * 1000);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}:${String(date.getSeconds()).padStart(2, "0")}`;
}

/**
 * Cumulative audio-path black box: connection-scoped counters reset on every
 * speaker reconnect, so the live status can read all-zero while the listener
 * hears a periodic hiccup. These counters survive reconnects, and the event
 * log names what happened and for how long.
 */
export function renderAudioPath(debug: DebugState | null): string {
  const audio = debug?.diagnostics?.audio;
  if (!audio) {
    return `<div class="cell"><span class="cell-subtitle">当前版本暂未提供音频路径统计</span></div>`;
  }
  const stallState = audio.encode.stalls > 0 ? "error" : "success";
  const rows = [
    {
      title: "编码耗时",
      subtitle: `P50 ${audio.encode.p50_ms} ms · P95 ${audio.encode.p95_ms} ms · 最长 ${audio.encode.max_ms} ms`,
      value: audio.encode.stalls > 0 ? `${audio.encode.stalls} 次停顿` : "稳定",
      state: stallState,
    },
    {
      title: "音源供给",
      subtitle:
        audio.source.stalls > 0
          ? `停滞 ${audio.source.stalls} 次（最长 ${audio.source.stall_max_ms} ms），补静音 ${audio.source.silence_fills} 次`
          : "未检测到音源停滞",
      value: audio.source.stalls > 0 ? "有中断" : "正常",
      state: audio.source.stalls > 0 ? "error" : "success",
    },
    {
      title: "客户端缓冲",
      subtitle: `峰值 ${audio.client.queue_peak_ms} ms / ${audio.client.queue_peak_items} 块 · 重连 ${audio.client.reconnects} 次`,
      value:
        audio.client.lag_skips > 0
          ? `跳过 ${audio.client.lag_skips} 次`
          : audio.client.queue_drops > 0
            ? `丢弃 ${audio.client.queue_drops} 次`
            : "正常",
      state: audio.client.lag_skips > 0 || audio.client.queue_drops > 0 ? "error" : "success",
    },
    {
      title: "链路丢弃",
      subtitle: `PCM ${audio.drops.tee} · 编码入 ${audio.drops.encoder_in} · 编码出 ${audio.drops.encoder_out}`,
      value:
        audio.drops.tee + audio.drops.encoder_in + audio.drops.encoder_out > 0 ? "有丢弃" : "无丢弃",
      state:
        audio.drops.tee + audio.drops.encoder_in + audio.drops.encoder_out > 0
          ? "error"
          : "success",
    },
  ];
  const events = audio.events.slice(-8).reverse();
  return `
      ${rows
        .map(
          (row) => `
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">${row.title}</span>
          <span class="cell-subtitle">${row.subtitle}</span>
        </div>
        <span class="plain-state ${row.state === "error" ? "error" : "success"}">${row.value}</span>
      </div>`,
        )
        .join("")}
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">最近事件</span>
          <span class="cell-subtitle">${
            events.length === 0
              ? "本次运行尚无异常事件"
              : events
                  .map(
                    (event) =>
                      `${formatClock(event.at)} ${EVENT_LABELS[event.kind] || event.kind}${
                        event.ms ? ` ${Math.round(event.ms)} ms` : ""
                      }`,
                  )
                  .join(" ｜ ")
          }</span>
        </div>
      </div>`;
}

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
      <p>先查看当前连接状态；遇到无声、不同步或格式不兼容时，再运行播放诊断。</p>
    </div>

    <div class="group-header">连接检查</div>
    <div class="group diagnostic-overview" data-connection-checks>
      ${renderConnectionChecks(debug, state)}
    </div>

    <div class="group-header">传输连接</div>
    <div class="group" data-stream-list>
      ${renderStreamRows(debug, state)}
    </div>

    <div class="group-header">音频路径</div>
    <div class="group" data-audio-path>
      ${renderAudioPath(debug)}
    </div>

    <div class="group-header">维护</div>
    <div class="group">
      <div class="cell">
        <div class="cell-content">
          <span class="cell-title">刷新管道</span>
          <span class="cell-subtitle">重建全部播放管道与连接，可清除卡住的会话和异常状态；播放会短暂中断</span>
        </div>
        <button class="button compact secondary" type="button" data-refresh-pipelines ${debug ? "" : "disabled"}>刷新管道</button>
      </div>
    </div>

    <details class="diagnostic-details" open>
      <summary><span><strong>技术计数</strong><small>传输、时钟与编码数据</small></span></summary>
      <div class="technical-metrics">
        <span>服务：${debug?.bridge_status.status || "-"}</span>
        <span>AirPlay 会话：经典 ${sessions.classic} · AirPlay 2 ${sessions.airplay2}</span>
        <span>时钟响应：${sum(raop.map((item) => item.timing_responses))}/${sum(raop.map((item) => item.timing_requests))}</span>
        <span>补包：${sum(raop.map((item) => item.resend_requests))}</span>
        <span>已发送：${((debug?.stream_bytes_sent ?? 0) / 1024).toFixed(1)} KB</span>
        <span>编码：${debug?.audio_config.format.toUpperCase() || "-"} · ${debug?.audio_config.bitrate || ""} · ${debug?.audio_config.sample_rate ? `${debug.audio_config.sample_rate / 1000} kHz` : "-"}</span>
        <span>延迟：${latencyBreakdown(debug)}</span>
      </div>
    </details>

    <div class="group-header">播放诊断</div>
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
        <button class="diagnostic-test" id="btn-debug-codecs" ${selectedTestDeviceIds(state, debug).length ? "" : "disabled"}><strong>测试音频格式</strong><span>依次播放 MP3、FLAC 和 WAV，找出可用格式</span><em>开始检查</em></button>
        <button class="diagnostic-test" id="btn-debug-tts" ${selectedTestDeviceIds(state, debug).length !== 1 ? "disabled" : ""}><strong>测试米家语音</strong><span>让单台音箱朗读测试语句，检查账号与指令响应</span><em>开始检查</em></button>
        <button class="diagnostic-test" id="btn-debug-play-stream" ${selectedStream ? "" : "disabled"}><strong>重新接入当前 AirPlay</strong><span>${selectedStream
          ? selectedSessionActive ? `重新播放“${escapeHtml(selectedStream.name)}”当前收到的内容` : `“${escapeHtml(selectedStream.name)}”当前没有收到音频`
          : "所选音箱没有对应的独立播放入口"}</span><em>尝试接入</em></button>
      </div>
      <label class="debug-volume volume-control test-volume-row">
        <span><strong>音箱音量</strong><small>修改目标音箱的真实音量</small></span>
        <input type="range" min="0" max="100" value="${state.devices.find(d => d.did === selectedTestDeviceIds(state, debug)[0])?.volume ?? 0}" id="debug-volume" ${selectedTestDeviceIds(state, debug).length ? "" : "disabled"} aria-label="测试音箱音量" style="--volume:${state.devices.find(d => d.did === selectedTestDeviceIds(state, debug)[0])?.volume ?? 0}%">
        <output id="debug-volume-output">${state.devices.find(d => d.did === selectedTestDeviceIds(state, debug)[0])?.volume ?? "—"}</output>
      </label>
    </div>

    <details class="diagnostic-details" open>
      <summary><span><strong>运行记录</strong><small>查看最近的连接与播放情况</small></span></summary>
      <section class="runtime-log-panel" aria-label="运行记录">
      <div class="runtime-log-toolbar">
        <div class="live-indicator"><span></span><strong>自动更新</strong></div>
        <select class="log-filter" data-log-filter aria-label="日志范围">
          <option value="micast">AirPlay 与 MiCast</option>
          <option value="all">全部日志</option>
          <option value="warning">仅警告与错误</option>
        </select>
        <button class="button plain log-action" type="button" data-log-pause>暂停</button>
        <button class="button plain log-action" type="button" data-log-copy>复制</button>
        <button class="button plain log-action log-report-action" type="button" data-log-report title="下载脱敏后的设置、状态与近期日志，反馈问题时请附上">导出报告</button>
      </div>
      <div class="runtime-log" role="log" aria-label="最新连接日志" data-runtime-log data-filter="micast">
        ${renderRuntimeLogRows(debug, "micast")}
      </div>
      </section>
    </details>

  `;
}

export function renderStreamRows(debug: DebugState | null, state: State): string {
  const streams = debug?.diagnostics?.streams || {};
  const raop = debug?.diagnostics?.raop || {};
  const ids = Object.keys(streams);
  if (!ids.length) {
    return `<div class="cell"><span class="cell-subtitle">暂无传输连接</span></div>`;
  }
  return ids
    .map((id) => {
      const s = streams[id];
      // Stream ids carry suffixes: stereo channels "-L"/"-R" and per-EQ
      // splits "-q1"… (topology.py: "<receiver>-Lq1"). Strip both, resolve
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
      const subtitle = active
        ? `${s.clients} 台音箱取流中 · 已发 ${mb} MB${s.dropped_chunks ? ` · 丢弃 ${s.dropped_chunks}` : ""}`
        : sessions > 0
          ? `手机已连接，暂无音箱取流 · 已发 ${mb} MB`
          : s.clients > 0
            ? `连接正在清理，当前无音频 · 已发 ${mb} MB`
          : `空闲 · 已发 ${mb} MB`;
      return `
        <div class="cell">
          <div class="cell-icon ${active ? "green" : "gray"}">${active ? "▶" : "—"}</div>
          <div class="cell-content">
            <span class="cell-title">${escapeHtml(name)}</span>
            <span class="cell-subtitle">${subtitle}</span>
          </div>
          <button class="button plain" data-kick-stream="${escapeHtml(id)}" ${active || sessions > 0 ? "" : "disabled"}>断开</button>
        </div>`;
    })
    .join("");
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
  const log = container.querySelector<HTMLElement>("[data-runtime-log]");
  const filter = container.querySelector<HTMLSelectElement>("[data-log-filter]");
  const pause = container.querySelector<HTMLButtonElement>("[data-log-pause]");
  const rerenderTestPanel = () => {
    (document.activeElement as HTMLElement | null)?.blur?.();
    rerender?.();

    const restoreTestAnchor = () => {
      const appRoot = document.getElementById("app");
      const scroller = document.querySelector<HTMLElement>(".app-body");
      const anchor = document.querySelector<HTMLElement>(".diagnostic-workbench");
      if (appRoot?.scrollTop) appRoot.scrollTop = 0;
      if (!scroller || !anchor) return;
      const headerBottom = document.querySelector<HTMLElement>(".app-header")?.getBoundingClientRect().bottom ?? 0;
      const desiredTop = headerBottom + 16;
      scroller.scrollTop += anchor.getBoundingClientRect().top - desiredTop;
    };

    restoreTestAnchor();
    requestAnimationFrame(() => {
      restoreTestAnchor();
      requestAnimationFrame(restoreTestAnchor);
    });
  };
  bindStreamKicks(container, showToast);
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
  filter?.addEventListener("change", () => {
    if (!log) return;
    log.dataset.filter = filter.value;
    updateRuntimeLog(log, store.get().debug, filter.value);
  });
  pause?.addEventListener("click", () => {
    if (!log || !pause) return;
    const paused = log.dataset.paused !== "true";
    log.dataset.paused = String(paused);
    pause.textContent = paused ? "继续" : "暂停";
    container.querySelector(".live-indicator")?.classList.toggle("paused", paused);
  });
  container.querySelector("[data-log-copy]")?.addEventListener("click", async () => {
    const text = log?.innerText.trim() || "";
    if (!text) return showToast("暂无日志可复制");
    try { await navigator.clipboard.writeText(text); showToast("日志已复制"); }
    catch { showToast("复制失败，请手动选择日志"); }
  });
  container.querySelector("[data-log-report]")?.addEventListener("click", async () => {
    try {
      await api.downloadDebugReport();
      showToast("诊断报告已下载（已自动脱敏）");
    } catch (e) {
      showToast(`下载失败: ${e instanceof Error ? e.message : "未知错误"}`);
    }
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

export function renderRuntimeLogRows(debug: DebugState | null, filter: string): string {
  const records = (debug?.logs || []).filter((item) => {
    if (filter === "warning") return ["WARNING", "ERROR", "CRITICAL"].includes(item.level);
    if (filter === "all") return true;
    return item.logger.startsWith("micast") || ["WARNING", "ERROR", "CRITICAL"].includes(item.level);
  }).reverse();
  if (!records.length) return `<div class="empty-log">等待 AirPlay、DLNA 或音箱连接事件…</div>`;
  return records.map((item) => `<article class="runtime-log-row ${item.level.toLowerCase()}">
    <div class="runtime-log-meta"><time>${escapeHtml(item.time)}</time><span>${escapeHtml(item.level)}</span><code>${escapeHtml(shortLogger(item.logger))}</code></div>
    <p>${escapeHtml(item.message)}</p>
  </article>`).join("");
}

function shortLogger(name: string): string {
  return name.replace("micast.raop.server", "AirPlay").replace("micast.", "MiCast · ");
}

/**
 * Re-render the log rows while preserving a deliberate reading position:
 * pinned to the newest entry when the user is already at (or near) the bottom,
 * untouched when they have scrolled up to read history. Call this instead of
 * assigning innerHTML directly — the newest line lives at the end, so an
 * unanchored replace reads as the list endlessly scrolling.
 */
export function updateRuntimeLog(log: HTMLElement, debug: DebugState | null, filter: string): void {
  // Nested desktop logs don't self-scroll (max-height:none) — nothing to pin.
  if (getComputedStyle(log).overflowY === "visible") {
    log.innerHTML = renderRuntimeLogRows(debug, filter);
    return;
  }
  const gap = log.scrollHeight - log.scrollTop - log.clientHeight;
  const pinned = gap <= 48;
  log.innerHTML = renderRuntimeLogRows(debug, filter);
  if (pinned) log.scrollTop = log.scrollHeight;
}
