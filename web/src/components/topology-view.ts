/**
 * Live link-topology view: a dark "flight map" canvas where audio paths are
 * drawn as glowing arcs with particles flowing along them. Data arrives over
 * the application's shared business connection and HTTP fallback; layout is a small
 * hand-rolled force simulation keyed by node id so frames stay stable.
 */

import { api, type Topology, type TopologyEdge, type TopologyNode } from "../api";
import { appUrl } from "../paths";
import { store } from '../state';

const KIND_COLORS: Record<string, string> = {
  source: "#a78bfa",
  engine: "#22d3ee",
  pipeline: "#2dd4bf",
  stream: "#38bdf8",
  cloud: "#60a5fa",
  speaker: "#34d399",
};

const STATUS_COLORS: Record<string, string> = {
  running: "#34d399",
  playing: "#34d399",
  idle: "#64748b",
  paused: "#fbbf24",
  error: "#f87171",
};

// Soft anchors per node kind (fractions of the canvas) — the force sim is
// free to drift away from them, which gives the "loose scatter" look.
const KIND_ANCHORS: Record<string, [number, number]> = {
  source: [0.1, 0.5],
  engine: [0.32, 0.45],
  pipeline: [0.46, 0.55],
  stream: [0.6, 0.45],
  cloud: [0.78, 0.16],
  speaker: [0.88, 0.5],
};

interface SimNode {
  data: TopologyNode;
  x: number;
  y: number;
  vx: number;
  vy: number;
  born: number;
}

interface RenderEdge {
  data: TopologyEdge;
  particles: number[];
}

export function renderTopologyView(): string {
  return `
    <div class="topology-stage" data-topology-stage>
      <canvas class="topology-canvas" data-topology-canvas></canvas>
      <details class="topology-node-list"><summary>链路设备与状态</summary><div data-topology-nodes></div></details>
      <div class="topology-hud topology-hud-left">
        <span class="topology-pill" data-topology-status>连接中…</span>
      </div>
      <div class="topology-hud topology-hud-right">
        <span data-topology-ts></span>
        <span class="topology-note">延迟为服务端估算</span>
      </div>
      <div class="topology-legend" data-topology-legend>
        <span><i style="background:${KIND_COLORS.source}"></i>音源</span>
        <span><i style="background:${KIND_COLORS.engine}"></i>MiCast</span>
        <span><i style="background:${KIND_COLORS.cloud}"></i>云服务</span>
        <span><i style="background:${KIND_COLORS.speaker}"></i>音箱</span>
        <button class="topology-toggle" data-topology-toggle type="button" aria-pressed="false">完整拓扑</button>
      </div>
      <div class="topology-empty" data-topology-empty hidden>
        <div class="topology-empty-title">等待播放</div>
      </div>
      <div class="topology-detail" data-topology-detail hidden></div>
    </div>
  `;
}

export function bindTopologyView(container: HTMLElement): () => void {
  const canvas = container.querySelector<HTMLCanvasElement>("[data-topology-canvas]")!;
  const statusPill = container.querySelector<HTMLElement>("[data-topology-status]")!;
  const tsEl = container.querySelector<HTMLElement>("[data-topology-ts]")!;
  const emptyEl = container.querySelector<HTMLElement>("[data-topology-empty]")!;
  const detailEl = container.querySelector<HTMLElement>("[data-topology-detail]")!;
  const ctx = canvas.getContext("2d")!;

  const nodes = new Map<string, SimNode>();
  let edges: RenderEdge[] = [];
  let snapshot: Topology | null = null;
  let hover: { kind: "node" | "edge"; id: string } | null = null;
  let selected: { kind: "node" | "edge"; id: string } | null = null;
  let mouse = { x: -1, y: -1 };
  let destroyed = false;
  let raf = 0;
  // Default view: only flows that are actually moving. The HUD toggle reveals
  // the full configured topology for troubleshooting.
  let showAll = false;
  const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');

  function isEdgeVisible(edge: RenderEdge): boolean {
    // Stalled edges (connected but no data) stay visible as a warning.
    if (showAll || edge.data.stalled) return true;
    if (!edge.data.active) return false;
    // A receiver can publish fallback/EQ/channel variants in parallel. In
    // the activity view, an encoded stream is only a real playback path when
    // a downstream target is pulling it; unused variants belong to the full
    // diagnostic topology, not the user's current route.
    if (edge.data.to.startsWith("stream:")) {
      return edges.some(
        (candidate) =>
          candidate.data.from === edge.data.to &&
          (Boolean(candidate.data.active) || Boolean(candidate.data.stalled))
      );
    }
    // An unused local pipeline is not part of the live route. Without this
    // check, engine→transcoder remains visible after its stream consumer is
    // filtered out, leaving a misleading dangling branch.
    if (edge.data.to.startsWith("pipe:")) {
      return edges.some(
        (candidate) =>
          candidate.data.from === edge.data.to &&
          candidate.data.to.startsWith("stream:") &&
          candidate.data.active &&
          edges.some(
            (consumer) =>
              consumer.data.from === candidate.data.to &&
              (Boolean(consumer.data.active) || Boolean(consumer.data.stalled))
          )
      );
    }
    return true;
  }

  function isNodeVisible(node: SimNode): boolean {
    if (showAll) return true;
    return edges.some(
      (e) => isEdgeVisible(e) && (e.data.from === node.data.id || e.data.to === node.data.id)
    );
  }

  function visibleNodes(): SimNode[] {
    return [...nodes.values()].filter(isNodeVisible);
  }

  function visibleEdges(): RenderEdge[] {
    return edges.filter(isEdgeVisible);
  }

  // ---------- data ----------

  function applySnapshot(next: Topology) {
    const epoch = store.runtimeGeneration;
    if (!store.acceptRuntime(next.runtime, 'topology')) return;
    if (epoch !== store.runtimeGeneration) document.dispatchEvent(new Event('micast:render-playback'));
    snapshot = next;
    const now = performance.now();
    const seen = new Set<string>();
    for (const node of next.nodes) {
      seen.add(node.id);
      let sim = nodes.get(node.id);
      if (!sim) {
        const [ax, ay] = KIND_ANCHORS[node.kind] ?? [0.5, 0.5];
        const seed = hashCode(node.id);
        sim = {
          data: node,
          x: ax * canvas.clientWidth + (seeded(seed) - 0.5) * 120,
          y: ay * canvas.clientHeight + (seeded(seed + 1) - 0.5) * 120,
          vx: 0,
          vy: 0,
          born: now,
        };
        nodes.set(node.id, sim);
      } else {
        sim.data = node;
      }
    }
    for (const id of [...nodes.keys()]) {
      if (!seen.has(id)) nodes.delete(id);
    }
    const prevParticles = new Map(edges.map((e) => [edgeKey(e.data), e.particles]));
    edges = next.edges.map((data) => ({
      data,
      particles:
        prevParticles.get(edgeKey(data)) ??
        Array.from({ length: 3 }, (_, i) => i / 3),
    }));
    updateHud();
    const list = container.querySelector<HTMLElement>('[data-topology-nodes]');
    if (list) {
      const markup = next.nodes.map(node => `<button type="button" data-topology-node="${escapeHtml(node.id)}"><span>${escapeHtml(node.label)}</span><span>${escapeHtml(node.status === 'running' || node.status === 'playing' ? '运行中' : node.status === 'error' ? '出错' : node.status === 'paused' ? '暂停' : '空闲')}</span></button>`).join('');
      if (list.innerHTML !== markup) list.innerHTML = markup;
    }
  }

  function updateHud() {
    if (!snapshot) return;
    const status = snapshot.status || "idle";
    const label =
      status === "running" ? "运行中" : status === "error" ? "出错" : status === "idle" ? "空闲" : status === "degraded" ? "部分功能不可用" : status === "starting" ? "启动中" : "状态未知";
    statusPill.textContent = label;
    statusPill.dataset.state = status;
    tsEl.textContent = new Date(snapshot.ts * 1000).toLocaleTimeString();
    const anyActive = snapshot.edges.some((e) => e.active);
    emptyEl.hidden = snapshot.nodes.length > 0 && anyActive;
  }

  const onTopology = (event: Event) => {
    try { applySnapshot((event as CustomEvent<Topology>).detail); } catch { /* Invalid snapshot. */ }
  };
  window.addEventListener('micast:topology', onTopology);
  let disposed = false;
  void api.getTopology().then(next => { if (!disposed) applySnapshot(next); }).catch(() => {
    if (!disposed) { statusPill.textContent = "连接中断，重试中…"; statusPill.dataset.state = "error"; }
  });

  // ---------- layout ----------

  /** Dynamic anchor (fractions of canvas): a sparse, idle graph centers
   * itself; once a live chain exists, nodes fall into left-to-right lanes
   * with siblings spread vertically. */
  function anchorFor(node: SimNode, visible: SimNode[]): [number, number] {
    const kind = node.data.kind;
    const compact = (canvas.clientWidth || 1) < 640;
    const chainVisible = visible.some((n) =>
      ["source", "engine", "pipeline", "stream"].includes(n.data.kind)
    );
    const byId = (a: SimNode, b: SimNode) => (a.data.id < b.data.id ? -1 : 1);
    const spreadX = (list: SimNode[], from: number, to: number): number => {
      const i = list.indexOf(node);
      const n = list.length;
      if (i < 0 || n === 1) return (from + to) / 2;
      return from + ((to - from) * i) / (n - 1);
    };

    if (!chainVisible) {
      if (kind === "cloud") {
        // One cloud per provider; future providers fan out across the top.
        const clouds = visible.filter((n) => n.data.kind === "cloud").sort(byId);
        return [spreadX(clouds, 0.35, 0.65), 0.22];
      }
      const speakers = visible.filter((n) => n.data.kind === "speaker").sort(byId);
      const n = speakers.length;
      if (!speakers.includes(node)) return [0.5, 0.55];
      if (n === 1) return [0.5, 0.55];
      return [0.2 + (0.6 * speakers.indexOf(node)) / (n - 1), 0.55];
    }

    if (kind === "cloud") {
      const clouds = visible.filter((n) => n.data.kind === "cloud").sort(byId);
      return [spreadX(clouds, 0.62, 0.82), 0.14];
    }
    const lanes: Record<string, number> = {
      source: 0.12,
      engine: compact ? 0.25 : 0.3,
      pipeline: compact ? 0.5 : 0.44,
      stream: compact ? 0.72 : 0.62,
      speaker: 0.85,
    };
    const mates = visible.filter((n) => n.data.kind === kind).sort(byId);
    const i = mates.indexOf(node);
    const n = mates.length;
    const compactY: Record<string, number> = { source: .38, engine: .55, pipeline: .39, stream: .56, speaker: .4 };
    const y = n <= 1 ? (compact ? compactY[kind] ?? .5 : .5) : .22 + (.5 * i) / (n - 1);
    return [lanes[kind] ?? 0.5, y];
  }

  function tickLayout() {
    const w = canvas.clientWidth || 1;
    const h = canvas.clientHeight || 1;
    const compact = w < 640;
    const all = visibleNodes();
    for (const a of all) {
      // spring toward the dynamic anchor (weak, keeps lanes loosely ordered)
      const [ax, ay] = anchorFor(a, all);
      const anchorStrength = compact ? 0.009 : 0.006;
      a.vx += (ax * w - a.x) * anchorStrength;
      a.vy += (ay * h - a.y) * anchorStrength;
      // pairwise repulsion
      for (const b of all) {
        if (a === b) continue;
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy + 0.01;
        if (d2 > (compact ? 120 : 160) ** 2) continue;
        const force = 900 / d2;
        a.vx += dx * force * 0.016;
        a.vy += dy * force * 0.016;
      }
    }
    // edge springs
    for (const edge of visibleEdges()) {
      const a = nodes.get(edge.data.from);
      const b = nodes.get(edge.data.to);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.hypot(dx, dy) || 1;
      const rest = edge.data.direction === "control"
        ? (compact ? 135 : 260)
        : (compact ? 92 : 170);
      const force = (dist - rest) * 0.004;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }
    for (const n of all) {
      n.vx *= compact ? 0.76 : 0.82;
      n.vy *= compact ? 0.76 : 0.82;
      n.x = clamp(n.x + n.vx, 40, w - 40);
      n.y = clamp(n.y + n.vy, 44, h - 60);
    }
  }

  // ---------- render ----------
  // Performance notes: this view often runs inside an RDP session, where
  // canvas shadowBlur and per-frame backdrop redraws are brutally expensive.
  // Glow is a pre-rendered sprite, the grid backdrop is cached offscreen, and
  // the loop is capped at ~30fps.

  let lastLayout = 0;
  let lastFrame = 0;
  let windTime = 0;
  const displayNodes = new Map<string, SimNode>();
  let backdropCache: { w: number; h: number; dpr: number; canvas: HTMLCanvasElement } | null = null;

  function draw(now: number) {
    if (destroyed) return;
    raf = requestAnimationFrame(draw);
    if (document.hidden || now - lastFrame < 33) return;
    // Advance only while visible: returning from the background must not jump.
    const elapsed = lastFrame ? Math.min(now - lastFrame, 100) : 0;
    lastFrame = now;
    if (!motionQuery.matches) windTime += elapsed;
    // Simulation is fixed-rate; visual flow follows the display refresh rate.
    if (now - lastLayout >= 33) {
      tickLayout();
      lastLayout = now;
    }

    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    // Ambient movement is independent of the settling layout. Edges, labels
    // and hit targets share these positions, including after status updates.
    displayNodes.clear();
    const compact = w < 640;
    const amplitude = motionQuery.matches ? 0 : 1 - Math.exp(-windTime / 1800);
    for (const node of nodes.values()) {
      const phase = seeded(hashCode(node.data.id)) * Math.PI * 2;
      const breezeX = Math.sin(windTime / 9000) * .55 + Math.sin(windTime / 6200 + phase) * .45;
      const breezeY = Math.sin(windTime / 11000) * .55 + Math.sin(windTime / 8100 + phase + 1) * .45;
      displayNodes.set(node.data.id, {
        ...node,
        x: clamp(node.x + breezeX * amplitude * (compact ? 8 : 16), 40, w - 40),
        y: clamp(node.y + breezeY * amplitude * (compact ? 12 : 22), 44, h - 60),
      });
    }
    // Bound raster cost independently of desktop resolution and display scaling.
    const dpr = Math.min(window.devicePixelRatio || 1, Math.sqrt(2_000_000 / Math.max(1, w * h)));
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    drawBackdrop(w, h, dpr);

    const highlight = computeHighlight();

    for (const edge of edges) {
      if (isEdgeVisible(edge)) drawEdge(edge, now, highlight);
    }
    for (const node of displayNodes.values()) {
      if (isNodeVisible(node)) drawNode(node, now, highlight);
    }
  }

  function drawBackdrop(w: number, h: number, dpr: number) {
    if (!backdropCache || backdropCache.w !== w || backdropCache.h !== h || backdropCache.dpr !== dpr) {
      const off = document.createElement("canvas");
      off.width = w * dpr;
      off.height = h * dpr;
      const octx = off.getContext("2d")!;
      octx.setTransform(dpr, 0, 0, dpr, 0, 0);
      octx.strokeStyle = "rgba(56, 189, 248, 0.028)";
      octx.lineWidth = 1;
      const step = 56;
      octx.beginPath();
      for (let x = step; x < w; x += step) {
        octx.moveTo(x, 0);
        octx.lineTo(x, h);
      }
      for (let y = step; y < h; y += step) {
        octx.moveTo(0, y);
        octx.lineTo(w, y);
      }
      octx.stroke();
      octx.fillStyle = "rgba(56, 189, 248, 0.045)";
      for (let x = step; x < w; x += step) {
        for (let y = step; y < h; y += step) {
          octx.fillRect(x - 0.5, y - 0.5, 1.5, 1.5);
        }
      }
      backdropCache = { w, h, dpr, canvas: off };
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.drawImage(backdropCache.canvas, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function curve(a: SimNode, b: SimNode, key: string) {
    // Curvature is stable per edge and separates parallel arcs.
    const bend = ((hashCode(key) % 100) / 100 - 0.5) * 0.5;
    const mx = (a.x + b.x) / 2;
    const my = (a.y + b.y) / 2;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const dist = Math.hypot(dx, dy) || 1;
    return {
      cx: mx + (-dy / dist) * dist * bend,
      cy: my + (dx / dist) * dist * bend,
    };
  }

  function quadPoint(a: SimNode, c: { cx: number; cy: number }, b: SimNode, t: number) {
    const u = 1 - t;
    return {
      x: u * u * a.x + 2 * u * t * c.cx + t * t * b.x,
      y: u * u * a.y + 2 * u * t * c.cy + t * t * b.y,
    };
  }

  function drawEdge(edge: RenderEdge, now: number, highlight: Set<string> | null) {
    const a = displayNodes.get(edge.data.from);
    const b = displayNodes.get(edge.data.to);
    if (!a || !b) return;
    const key = edgeKey(edge.data);
    const c = curve(a, b, key);
    const active = Boolean(edge.data.active);
    const stalled = Boolean(edge.data.stalled);
    const control = edge.data.direction === "control";
    const dimmed = highlight !== null && !highlight.has(key);
    const color = stalled
      ? "#fbbf24"
      : active
        ? KIND_COLORS[b.data.kind] ?? "#38bdf8"
        : "#475569";

    ctx.save();
    ctx.globalAlpha = dimmed ? 0.08 : control ? 0.35 : active || stalled ? 0.8 : 0.3;
    ctx.strokeStyle = color;
    ctx.lineWidth = control ? 1 : active ? 2 : 1.2;
    if (control || stalled) ctx.setLineDash([4, 6]);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.quadraticCurveTo(c.cx, c.cy, b.x, b.y);
    ctx.stroke();
    ctx.setLineDash([]);

    if (active && !dimmed) {
      // Particles flow in the data direction: "pull" edges stream toward the
      // speaker, which is also the edge's `to` node in our model.
      const speed = (control ? 0.00012 : 0.00045) * (motionQuery.matches ? .4 : 1);
      const size = control ? 4 : 6;
      for (const p of edge.particles) {
        const t = (p + now * speed) % 1;
        const pos = quadPoint(a, c, b, t);
        ctx.globalAlpha = (control ? 0.35 : 0.75) * (0.4 + 0.6 * Math.sin(t * Math.PI));
        if (control) {
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(pos.x, pos.y, 1.4, 0, Math.PI * 2);
          ctx.fill();
        } else {
          ctx.drawImage(glowSprite(color), pos.x - size, pos.y - size, size * 2, size * 2);
        }
      }
    }

    if (stalled && !dimmed) {
      // A socket held open with zero traffic is a stuck speaker, not a flow.
      const mid = quadPoint(a, c, b, 0.5);
      ctx.globalAlpha = 0.9;
      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.fillStyle = "#fbbf24";
      ctx.fillText("连接滞留 · 无数据", mid.x, mid.y - 6);
      ctx.restore();
      return;
    }

    const latency = edge.data.latency_ms;
    // Push edges carry their protocol in the label: "PCM" into the
    // transcoder, "MP3 · ≈24ms" out of it, "PCM 直出" when bypassed.
    const protocol = edge.data.direction === "push" && edge.data.protocol !== "RAOP"
      ? edge.data.protocol
      : null;
    if ((latency != null || protocol) && !control && !dimmed) {
      const mid = quadPoint(a, c, b, 0.5);
      ctx.globalAlpha = dimmed ? 0.1 : 0.95;
      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.fillStyle = latencyColor(latency ?? 0);
      const text = [protocol, latency != null ? `≈${latency}ms` : null]
        .filter(Boolean)
        .join(" · ");
      ctx.fillText(text, mid.x, mid.y - 6);
    }
    ctx.restore();
  }

  function drawNode(node: SimNode, now: number, highlight: Set<string> | null) {
    const { data } = node;
    const dimmed = highlight !== null && !highlight.has(`node:${data.id}`);
    const base =
      data.kind === "speaker" || data.kind === "engine"
        ? STATUS_COLORS[String(data.status)] ?? KIND_COLORS[data.kind]
        : KIND_COLORS[data.kind] ?? "#38bdf8";
    const active =
      data.kind === "source"
        ? Boolean(data.active)
        : data.status
          ? ["running", "playing"].includes(String(data.status))
          : true;
    const color = active || data.kind === "cloud" ? base : "#475569";
    const radius = data.kind === "cloud" ? 9 : data.kind === "speaker" ? 8 : 7;

    ctx.save();
    ctx.globalAlpha = dimmed ? 0.15 : 1;
    // Idle speakers/cloud are just the backdrop in the default view.
    if (!active && (data.kind === "speaker" || data.kind === "cloud")) {
      ctx.globalAlpha *= 0.55;
    }

    if (active) {
      // breathing halo, drawn from the cached glow sprite (shadowBlur is far
      // too slow inside an RDP session)
      const pulse = 1 + 0.12 * Math.sin(now / 2400 + hashCode(data.id));
      const haloSize = radius * 2.2 * pulse;
      ctx.globalAlpha = (dimmed ? 0.1 : 0.35) * (pulse - 0.55);
      ctx.drawImage(glowSprite(color), node.x - haloSize, node.y - haloSize, haloSize * 2, haloSize * 2);
      ctx.globalAlpha = dimmed ? 0.15 : 1;
    }

    // classic color dot: filled glow sprite + solid core + dark center
    ctx.globalAlpha = dimmed ? 0.15 : 0.9;
    const glowSize = radius * 1.8;
    ctx.drawImage(glowSprite(color), node.x - glowSize, node.y - glowSize, glowSize * 2, glowSize * 2);
    ctx.globalAlpha = dimmed ? 0.15 : 1;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(node.x, node.y, radius * 0.62, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(6, 13, 20, 0.75)";
    ctx.beginPath();
    ctx.arc(node.x, node.y, radius * 0.28, 0, Math.PI * 2);
    ctx.fill();

    ctx.font = "13px system-ui, sans-serif";
    ctx.textAlign = "center";
    const backdropIdle = !active && (data.kind === "speaker" || data.kind === "cloud");
    ctx.fillStyle = dimmed
      ? "rgba(148, 163, 184, 0.3)"
      : backdropIdle
        ? "rgba(148, 163, 184, 0.55)"
        : "rgba(226, 232, 240, 0.92)";
    // Keep names readable without running beyond the canvas or adjacent lanes.
    const labelWidth = Math.min(canvas.clientWidth < 640 ? 100 : 180, canvas.clientWidth - 24);
    let label = nodeLabel(data);
    if (ctx.measureText(label).width > labelWidth) {
      while (label.length > 1 && ctx.measureText(label + "…").width > labelWidth) label = label.slice(0, -1);
      label += "…";
    }
    const labelX = clamp(node.x, labelWidth / 2 + 12, canvas.clientWidth - labelWidth / 2 - 12);
    ctx.fillText(label, labelX, node.y + radius + 17);
    if (data.kind === "speaker" && data.delay_ms) {
      ctx.font = "11px ui-monospace, monospace";
      ctx.fillStyle = "rgba(148, 163, 184, 0.8)";
      ctx.fillText(`+${data.delay_ms}ms 补偿`, node.x, node.y + radius + 28);
    }
    ctx.restore();
  }

  // ---------- interaction ----------

  function computeHighlight(): Set<string> | null {
    const focus = hover ?? selected;
    if (!focus) return null;
    const set = new Set<string>();
    const startId = focus.kind === "node" ? focus.id : null;
    const focusEdge = focus.kind === "edge" ? focus.id : null;

    // Walk both directions from the focus so the whole path lights up.
    const adjacency = new Map<string, string[]>();
    for (const edge of edges) {
      push(adjacency, edge.data.from, edge.data.to);
      push(adjacency, edge.data.to, edge.data.from);
    }
    const starts = startId
      ? [startId]
      : (() => {
          const e = edges.find((item) => edgeKey(item.data) === focusEdge);
          return e ? [e.data.from, e.data.to] : [];
        })();
    const queue = [...starts];
    const visited = new Set(queue);
    while (queue.length) {
      const id = queue.shift()!;
      set.add(`node:${id}`);
      for (const edge of edges) {
        if (edge.data.from === id || edge.data.to === id) set.add(edgeKey(edge.data));
      }
      for (const next of adjacency.get(id) ?? []) {
        if (!visited.has(next)) {
          visited.add(next);
          queue.push(next);
        }
      }
    }
    return set;
  }

  function hitTest(x: number, y: number): { kind: "node" | "edge"; id: string } | null {
    for (const node of displayNodes.values()) {
      if (!isNodeVisible(node)) continue;
      if (Math.hypot(node.x - x, node.y - y) < 24) return { kind: "node", id: node.data.id };
    }
    let best: { key: string; dist: number } | null = null;
    for (const edge of edges) {
      if (!isEdgeVisible(edge)) continue;
      const a = displayNodes.get(edge.data.from);
      const b = displayNodes.get(edge.data.to);
      if (!a || !b) continue;
      const c = curve(a, b, edgeKey(edge.data));
      for (let i = 0; i <= 16; i++) {
        const p = quadPoint(a, c, b, i / 16);
        const d = Math.hypot(p.x - x, p.y - y);
        if (d < 8 && (!best || d < best.dist)) best = { key: edgeKey(edge.data), dist: d };
      }
    }
    return best ? { kind: "edge", id: best.key } : null;
  }

  function renderDetail() {
    if (!selected) {
      detailEl.hidden = true;
      return;
    }
    let rows: Array<[string, string]> = [];
    let title = "";
    if (selected.kind === "node") {
      const node = nodes.get(selected.id)?.data;
      if (!node) {
        detailEl.hidden = true;
        return;
      }
      title = node.label;
      rows = objectRows(node, ["id", "label"]);
    } else {
      const selectedId = selected.id;
      const edge = edges.find((item) => edgeKey(item.data) === selectedId)?.data;
      if (!edge) {
        detailEl.hidden = true;
        return;
      }
      title = `${shortId(edge.from)} → ${shortId(edge.to)}`;
      rows = objectRows(edge, ["from", "to", "segments"]);
      if (edge.segments) {
        for (const [k, v] of Object.entries(edge.segments)) {
          rows.push([segmentLabel(k), `${v} ms`]);
        }
      }
    }
    detailEl.innerHTML = `
      <div class="topology-detail-head">
        <span>${escapeHtml(title)}</span>
        <button class="icon-button" data-topology-close aria-label="关闭">✕</button>
      </div>
      <dl>${rows
        .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`)
        .join("")}</dl>
    `;
    detailEl.hidden = false;
    detailEl.querySelector("[data-topology-close]")?.addEventListener("click", () => {
      selected = null;
      renderDetail();
    });
  }

  canvas.addEventListener("pointermove", (event) => {
    const rect = canvas.getBoundingClientRect();
    mouse = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    hover = hitTest(mouse.x, mouse.y);
    canvas.style.cursor = hover ? "pointer" : "default";
  });
  canvas.addEventListener("mouseleave", () => {
    hover = null;
  });
  canvas.addEventListener("click", (event) => {
    const rect = canvas.getBoundingClientRect();
    selected = hitTest(event.clientX - rect.left, event.clientY - rect.top);
    renderDetail();
  });
  container.querySelector('[data-topology-nodes]')?.addEventListener('click', event => {
    const button = (event.target as HTMLElement).closest<HTMLElement>('[data-topology-node]');
    if (!button?.dataset.topologyNode) return;
    selected = { kind: 'node', id: button.dataset.topologyNode };
    renderDetail();
  });

  const toggle = container.querySelector<HTMLButtonElement>("[data-topology-toggle]")!;
  toggle.addEventListener("click", () => {
    showAll = !showAll;
    toggle.setAttribute("aria-pressed", String(showAll));
    toggle.textContent = showAll ? "只看活动" : "完整拓扑";
    if (selected && selected.kind === "edge" && !edges.some((e) => isEdgeVisible(e) && edgeKey(e.data) === selected?.id)) {
      selected = null;
    }
    renderDetail();
  });

  raf = requestAnimationFrame(draw);

  return () => {
    destroyed = true;
    cancelAnimationFrame(raf);
    disposed = true;
    window.removeEventListener('micast:topology', onTopology);

  };
}

// ---------- helpers ----------

function nodeLabel(node: TopologyNode): string {
  // Naming rules: the entry (receiver) name lives only on the engine node.
  // Sources show the sender device, streams show just their channel.
  if (node.kind === "source") {
    if (node.label.startsWith("DLNA")) return node.label;
    const device = typeof node.device === "string" ? node.device : "";
    return device ? deviceLabel(device) : "音源";
  }
  if (node.kind === "stream") {
    const base =
      node.channel === "left" ? "左声道" : node.channel === "right" ? "右声道" : "音频流";
    return node.eq ? `${base} · EQ` : base;
  }
  // The engine node IS the AirPlay receiver entry — the name phones tap in
  // the AirPlay picker — so prefix it with the protocol.
  if (node.kind === "engine") return `AirPlay · ${node.label}`;
  return node.label;
}

function deviceLabel(device: string): string {
  if (/itunes|windows|macintosh/i.test(device)) return "电脑";
  if (/^airplay/i.test(device)) return "手机";
  // mDNS-resolved hostnames ("Dys-iPhone", "MacBook-Pro") are shown as-is.
  return device;
}

// Pre-rendered radial-gradient glow per color — a drawImage call is orders of
// magnitude cheaper than shadowBlur, which matters over RDP.
const spriteCache = new Map<string, HTMLCanvasElement>();

function glowSprite(color: string): HTMLCanvasElement {
  const cached = spriteCache.get(color);
  if (cached) return cached;
  const size = 64;
  const off = document.createElement("canvas");
  off.width = size;
  off.height = size;
  const octx = off.getContext("2d")!;
  const grad = octx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, color);
  grad.addColorStop(0.35, color + "aa");
  grad.addColorStop(1, color + "00");
  octx.fillStyle = grad;
  octx.fillRect(0, 0, size, size);
  spriteCache.set(color, off);
  return off;
}

function edgeKey(edge: TopologyEdge): string {
  return `${edge.from}->${edge.to}:${edge.protocol ?? ""}`;
}

function hashCode(text: string): number {
  let hash = 0;
  for (let i = 0; i < text.length; i++) {
    hash = (hash * 31 + text.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

function seeded(seed: number): number {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

function push(map: Map<string, string[]>, key: string, value: string) {
  const list = map.get(key);
  if (list) list.push(value);
  else map.set(key, [value]);
}

function latencyColor(ms: number): string {
  if (ms < 100) return "#34d399";
  if (ms <= 500) return "#fbbf24";
  return "#f87171";
}

function segmentLabel(key: string): string {
  const labels: Record<string, string> = {
    input_buffer_ms: "输入缓冲",
    encoding_ms: "编码",
    stream_buffer_ms: "流缓冲",
    send_queue_ms: "发送队列",
  };
  return labels[key] ?? key;
}

function shortId(id: string): string {
  return id.includes(":") ? id.split(":", 2)[1] : id;
}

const FIELD_LABELS: Record<string, string> = {
  kind: "类型",
  protocol: "协议",
  status: "状态",
  active: "活跃",
  sessions: "会话数",
  provider: "服务商",
  format: "编码格式",
  input: "输入",
  output: "输出",
  clients: "连接数",
  bytes_sent: "已发送",
  dropped_packets: "丢包",
  dropped_chunks: "丢弃块",
  decode_errors: "解码错误",
  resend_requests: "重传请求",
  direction: "方向",
  latency_ms: "延迟",
  estimated: "服务端估算",
  stalled: "连接滞留",
  compensation_ms: "启动补偿",
  audio_delay_ms: "延迟补偿",
  channel: "声道",
  receiver: "播放入口",
  enabled: "已启用",
  detail: "详情",
  device: "来源设备",
};

function fieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key;
}

function objectRows(obj: Record<string, unknown>, skip: string[]): Array<[string, string]> {
  return Object.entries(obj)
    .filter(([key, value]) => !skip.includes(key) && value != null && typeof value !== "object")
    .map(([key, value]) => {
      const text =
        typeof value === "boolean" ? (value ? "是" : "否") : String(value);
      return [fieldLabel(key), key === "latency_ms" ? `${text} ms` : text];
    });
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
