/**
 * Drawable EQ curve editor on <canvas>.
 *
 * Log-frequency axis (20 Hz – 20 kHz), dB axis, PCHIP-smoothed curve through
 * sparse control points. Interaction model (see slider-commit-on-release):
 * drags only repaint the canvas locally; `onCommit` fires on pointer release
 * and the host must NOT trigger a full re-render in response.
 */

export interface CurvePoint {
  freq: number;
  gain: number;
}

export interface CurveCanvasOptions {
  points: CurvePoint[];
  /** Reference/target curve drawn dashed underneath (no interaction). */
  target?: CurvePoint[];
  freqRange?: [number, number];
  gainRange?: [number, number];
  readOnly?: boolean;
  /** Display-only mode for ambient layers: no grid, no axes, no labels —
   * just the curve, its soft fill, and the live spectrum. */
  minimal?: boolean;
  onCommit?: (points: CurvePoint[]) => void;
  /** Tap on an existing point toggles selection (the mobile path to deletion,
   *  since preventDefault on touchstart suppresses the synthetic dblclick).
   *  Fires with the selected index, or null when selection clears. */
  onSelect?: (index: number | null) => void;
}

const GRID_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
const GRID_FREQ_LABELS = ["31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"];
const GRID_FREQS_NARROW = [31, 125, 500, 2000, 16000];
const GRID_GAINS = [-12, -6, 0, 6, 12];
const HIT_RADIUS = 22; // Keep the visible dot small, the interaction area generous.
const PAD = { top: 24, right: 16, bottom: 30, left: 44 };
const PAD_NARROW = { top: 22, right: 10, bottom: 26, left: 36 };
const NARROW_WIDTH = 520; // px plot-area breakpoint for reduced labels

// Frequency-to-color identity: warm amber bass → brand blue mids → violet
// treble. Our own gradient language (not Apple's green/cyan), theme-independent.
const CURVE_STOPS: [number, [number, number, number]][] = [
  [0, [255, 159, 10]],
  [0.5, [10, 132, 255]],
  [1, [191, 90, 242]],
];

function curveColorAt(t: number, alpha = 1): string {
  const x = Math.min(1, Math.max(0, t));
  let i = 0;
  while (i < CURVE_STOPS.length - 2 && x > CURVE_STOPS[i + 1][0]) i++;
  const [t0, c0] = CURVE_STOPS[i];
  const [t1, c1] = CURVE_STOPS[i + 1];
  const k = t1 === t0 ? 0 : (x - t0) / (t1 - t0);
  const c = c0.map((v, j) => Math.round(v + (c1[j] - v) * k));
  return alpha >= 1 ? `rgb(${c[0]}, ${c[1]}, ${c[2]})` : `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${alpha})`;
}

// Bass / mid / treble region captions along the top of the plot.
const BAND_REGIONS: [number, number, string, string][] = [
  [20, 250, "低频", "低"],
  [250, 4000, "中频", "中"],
  [4000, 20000, "高频", "高"],
];

// --- PCHIP (Fritsch–Carlson) on the log-frequency axis, mirrors curve_fit.py

function pchipSlopes(xs: number[], ys: number[]): number[] {
  const n = xs.length;
  if (n === 1) return [0];
  const h: number[] = [];
  const d: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    h.push(xs[i + 1] - xs[i]);
    d.push((ys[i + 1] - ys[i]) / h[i]);
  }
  const m = new Array<number>(n).fill(0);
  for (let i = 1; i < n - 1; i++) {
    if (d[i - 1] * d[i] <= 0) {
      m[i] = 0;
    } else {
      const w1 = 2 * h[i] + h[i - 1];
      const w2 = h[i] + 2 * h[i - 1];
      m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i]);
    }
  }
  m[0] = d[0];
  m[n - 1] = d[n - 1];
  if (m[0] * d[0] < 0) m[0] = 0;
  if (m[n - 1] * d[n - 1] < 0) m[n - 1] = 0;
  return m;
}

export function evalCurve(points: CurvePoint[], freq: number): number {
  if (points.length === 0) return 0;
  if (points.length === 1) return points[0].gain;
  const xs = points.map((p) => Math.log10(p.freq));
  const ys = points.map((p) => p.gain);
  const x = Math.log10(freq);
  if (x <= xs[0]) return ys[0];
  if (x >= xs[xs.length - 1]) return ys[ys.length - 1];
  let i = 0;
  while (x > xs[i + 1] && i < xs.length - 2) i++;
  const m = pchipSlopes(xs, ys);
  const h = xs[i + 1] - xs[i];
  const t = (x - xs[i]) / h;
  const t2 = t * t;
  const t3 = t2 * t;
  return (
    (2 * t3 - 3 * t2 + 1) * ys[i] +
    (t3 - 2 * t2 + t) * h * m[i] +
    (-2 * t3 + 3 * t2) * ys[i + 1] +
    (t3 - t2) * h * m[i + 1]
  );
}

function normalize(points: CurvePoint[], gainRange: [number, number]): CurvePoint[] {
  const byFreq = new Map<number, number>();
  for (const p of points) byFreq.set(p.freq, Math.max(gainRange[0], Math.min(gainRange[1], p.gain)));
  return [...byFreq.entries()]
    .map(([freq, gain]) => ({ freq, gain }))
    .sort((a, b) => a.freq - b.freq)
    .slice(0, 24);
}

export class EqCurveCanvas {
  private canvas: HTMLCanvasElement;
  private opts: CurveCanvasOptions;
  private points: CurvePoint[];
  private gridLayer: HTMLCanvasElement | null = null;
  private dragIndex = -1;
  private hoverIndex = -1;
  private moved = false;
  private dragWasExisting = false;
  private downX = 0;
  private downY = 0;
  private selectedIndex = -1;
  private resizeObserver: ResizeObserver | null = null;
  private themeObserver: MutationObserver | null = null;
  private colorSchemeQuery: MediaQueryList | null = null;
  private destroyed = false;
  // Live spectrum: server-pushed targets eased toward in a rAF loop
  // (fast attack, slow release) so the bars feel analog rather than steppy.
  private spectrumTarget: number[] | null = null;
  private spectrumDisplay: number[] = [];
  private spectrumRaf = 0;

  constructor(canvas: HTMLCanvasElement, opts: CurveCanvasOptions) {
    this.canvas = canvas;
    this.opts = opts;
    this.points = normalize(opts.points, this.gainRange);
    this.attach();
  }

  get gainRange(): [number, number] {
    return this.opts.gainRange ?? [-12, 12];
  }

  get freqRange(): [number, number] {
    return this.opts.freqRange ?? [20, 20000];
  }

  setReadOnly(readOnly: boolean) {
    if (this.opts.readOnly === readOnly) return;
    this.opts.readOnly = readOnly;
    this.dragIndex = -1;
    this.hoverIndex = -1;
    this.clearSelection();
    this.canvas.setAttribute('aria-readonly', String(readOnly));
    this.repaint();
  }

  /** Replace the curve (e.g. preset applied); repaints without committing. */
  setPoints(points: CurvePoint[]) {
    this.points = normalize(points, this.gainRange);
    this.clearSelection();
    this.repaint();
  }

  /** Canvas-relative position of a control point (for host overlays). */
  pointPosition(index: number): { x: number; y: number } | null {
    const p = this.points[index];
    if (!p) return null;
    return { x: this.xOf(p.freq), y: this.yOf(p.gain) };
  }

  /** Remove a control point and commit — the host's delete action. */
  deletePoint(index: number) {
    if (this.opts.readOnly || index < 0 || index >= this.points.length) return;
    this.points.splice(index, 1);
    this.clearSelection();
    this.repaint();
    this.opts.onCommit?.(this.points.map((p) => ({ ...p })));
  }

  clearPointSelection() {
    this.clearSelection();
    this.repaint();
  }

  private clearSelection(notify = true) {
    if (this.selectedIndex < 0) return;
    this.selectedIndex = -1;
    if (notify) this.opts.onSelect?.(null);
  }

  setTarget(points: CurvePoint[] | undefined) {
    this.opts.target = points;
    this.repaint();
  }

  /** Push the latest spectrum bands (0..1, log-spaced over the freq axis);
   *  null hides the bars and stops the animation loop. */
  setSpectrum(bands: number[] | null) {
    this.spectrumTarget = bands;
    if (bands && this.spectrumDisplay.length !== bands.length) {
      this.spectrumDisplay = new Array<number>(bands.length).fill(0);
    }
    if (bands && !this.spectrumRaf) {
      this.spectrumRaf = requestAnimationFrame(this.animateSpectrum);
    }
  }

  private animateSpectrum = () => {
    this.spectrumRaf = 0;
    if (this.destroyed) return;
    const target = this.spectrumTarget;
    if (!target) {
      // Fade out whatever is still on screen, then stop.
      const alive = this.spectrumDisplay.some((v) => v > 0.01);
      this.spectrumDisplay = this.spectrumDisplay.map((v) => v * 0.8);
      this.repaint();
      if (alive) this.spectrumRaf = requestAnimationFrame(this.animateSpectrum);
      return;
    }
    for (let i = 0; i < target.length; i++) {
      const cur = this.spectrumDisplay[i] ?? 0;
      const t = target[i] ?? 0;
      const next = t > cur ? cur + (t - cur) * 0.45 : cur + (t - cur) * 0.12;
      this.spectrumDisplay[i] = Math.abs(next - t) < 0.004 ? t : next;
    }
    this.repaint();
    // Keep looping even when settled: the next push needs the loop alive.
    this.spectrumRaf = requestAnimationFrame(this.animateSpectrum);
  };

  destroy() {
    this.destroyed = true;
    if (this.spectrumRaf) cancelAnimationFrame(this.spectrumRaf);
    this.spectrumRaf = 0;
    this.resizeObserver?.disconnect();
    this.themeObserver?.disconnect();
    this.colorSchemeQuery?.removeEventListener("change", this.onThemeChange);
    const c = this.canvas;
    c.removeEventListener("touchstart", this.onTouchStart);
    c.removeEventListener("pointerdown", this.onPointerDown);
    c.removeEventListener("pointermove", this.onPointerMove);
    c.removeEventListener("pointerup", this.onPointerUp);
    c.removeEventListener("pointercancel", this.onPointerUp);
    c.removeEventListener("dblclick", this.onDoubleClick);
  }

  // ---- coordinates ----

  private plotRect(): { x: number; y: number; w: number; h: number } {
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    const pad = w < NARROW_WIDTH ? PAD_NARROW : PAD;
    return { x: pad.left, y: pad.top, w: Math.max(10, w - pad.left - pad.right), h: Math.max(10, h - pad.top - pad.bottom) };
  }

  /** 0..1 position of a frequency on the log axis (for gradient sampling). */
  private freqT(freq: number): number {
    const [fmin, fmax] = this.freqRange;
    return (Math.log10(freq) - Math.log10(fmin)) / (Math.log10(fmax) - Math.log10(fmin));
  }

  private xOf(freq: number): number {
    const [fmin, fmax] = this.freqRange;
    const { x, w } = this.plotRect();
    const t = (Math.log10(freq) - Math.log10(fmin)) / (Math.log10(fmax) - Math.log10(fmin));
    return x + t * w;
  }

  private yOf(gain: number): number {
    const [gmin, gmax] = this.gainRange;
    const { y, h } = this.plotRect();
    return y + ((gmax - gain) / (gmax - gmin)) * h;
  }

  private freqAt(px: number): number {
    const [fmin, fmax] = this.freqRange;
    const { x, w } = this.plotRect();
    const t = Math.min(1, Math.max(0, (px - x) / w));
    return 10 ** (Math.log10(fmin) + t * (Math.log10(fmax) - Math.log10(fmin)));
  }

  private gainAt(py: number): number {
    const [gmin, gmax] = this.gainRange;
    const { y, h } = this.plotRect();
    const t = Math.min(1, Math.max(0, (py - y) / h));
    return gmax - t * (gmax - gmin);
  }

  // ---- rendering ----

  private attach() {
    const c = this.canvas;
    // pan-y lets a vertical swipe scroll the page instead of dragging the
    // curve; grabbing a control point prevents the scroll at touchstart.
    c.style.touchAction = "pan-y";
    c.addEventListener("touchstart", this.onTouchStart, { passive: false });
    c.addEventListener("pointerdown", this.onPointerDown);
    c.addEventListener("pointermove", this.onPointerMove);
    c.addEventListener("pointerup", this.onPointerUp);
    c.addEventListener("pointercancel", this.onPointerUp);
    c.addEventListener("dblclick", this.onDoubleClick);
    this.resizeObserver = new ResizeObserver(() => this.repaint());
    this.resizeObserver.observe(c);
    // Theme tokens change without a resize — drop the cached grid layer.
    this.themeObserver = new MutationObserver(this.onThemeChange);
    this.themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    this.colorSchemeQuery = window.matchMedia("(prefers-color-scheme: dark)");
    this.colorSchemeQuery.addEventListener("change", this.onThemeChange);
    this.repaint();
  }

  private onThemeChange = () => {
    this.gridLayer = null;
    this.repaint();
  };

  private cssVar(name: string, fallback: string): string {
    const v = getComputedStyle(this.canvas).getPropertyValue(name).trim();
    return v || fallback;
  }

  private renderGrid() {
    const rect = this.plotRect();
    const grid = document.createElement("canvas");
    const dpr = window.devicePixelRatio || 1;
    grid.width = this.canvas.clientWidth * dpr;
    grid.height = this.canvas.clientHeight * dpr;
    const ctx = grid.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    // Theme-aware contrast: separator-strong at partial alpha reads as a
    // subtle hairline on both light and dark wraps; labels use secondary text.
    const gridLine = this.cssVar("--separator-strong", "rgba(128,128,128,0.4)");
    const label = this.cssVar("--text-secondary", "rgba(128,128,128,1)");
    const faint = this.cssVar("--text-tertiary", "rgba(128,128,128,0.7)");
    const narrow = rect.w + 60 < NARROW_WIDTH;
    ctx.lineWidth = 1;
    ctx.font = `${narrow ? 10 : 11}px system-ui, sans-serif`;
    // Vertical octave lines; labels thin out on narrow (phone) widths.
    GRID_FREQS.forEach((f, i) => {
      const px = Math.round(this.xOf(f)) + 0.5;
      ctx.globalAlpha = 0.45;
      ctx.strokeStyle = gridLine;
      ctx.beginPath();
      ctx.moveTo(px, rect.y);
      ctx.lineTo(px, rect.y + rect.h);
      ctx.stroke();
      ctx.globalAlpha = 1;
      if (narrow && !GRID_FREQS_NARROW.includes(f)) return;
      ctx.fillStyle = label;
      ctx.textAlign = "center";
      ctx.fillText(GRID_FREQ_LABELS[i], px, rect.y + rect.h + 17);
    });
    // Band region captions along the top edge (低频/中频/高频).
    ctx.fillStyle = faint;
    ctx.globalAlpha = 0.8;
    ctx.textAlign = "center";
    for (const [f0, f1, full, short] of BAND_REGIONS) {
      ctx.fillText(narrow ? short : full, this.xOf(Math.sqrt(f0 * f1)), rect.y - 8);
    }
    ctx.globalAlpha = 1;
    // Horizontal dB lines; 0 dB emphasized.
    for (const g of GRID_GAINS) {
      const py = Math.round(this.yOf(g)) + 0.5;
      ctx.globalAlpha = g === 0 ? 0.95 : 0.45;
      ctx.strokeStyle = gridLine;
      ctx.lineWidth = g === 0 ? 1.5 : 1;
      ctx.beginPath();
      ctx.moveTo(rect.x, py);
      ctx.lineTo(rect.x + rect.w, py);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.lineWidth = 1;
      ctx.fillStyle = label;
      ctx.textAlign = "right";
      ctx.fillText(`${g > 0 ? "+" : ""}${g}`, rect.x - 7, py + 4);
    }
    // Axis titles with units: y on the top-left, x on the top-right (the
    // bottom-right corner belongs to the 16k tick label).
    ctx.fillStyle = faint;
    ctx.textAlign = "left";
    ctx.fillText(narrow ? "dB" : "增益 (dB)", 2, 13);
    if (!narrow) {
      ctx.textAlign = "right";
      ctx.fillText("频率 (Hz)", rect.x + rect.w, 13);
    }
    this.gridLayer = grid;
  }

  private freqGradient(ctx: CanvasRenderingContext2D, alpha: number): CanvasGradient {
    const rect = this.plotRect();
    const grad = ctx.createLinearGradient(rect.x, 0, rect.x + rect.w, 0);
    for (const [t, _c] of CURVE_STOPS) grad.addColorStop(t, curveColorAt(t, alpha));
    return grad;
  }

  private curvePaths = new Map<string, Path2D>();
  private curvePath(points: CurvePoint[]): Path2D {
    const key = JSON.stringify([this.canvas.clientWidth, this.canvas.clientHeight, this.freqRange, this.gainRange, points]);
    const cached = this.curvePaths.get(key);
    if (cached) return cached;
    const path = new Path2D();
    const rect = this.plotRect();
    const steps = Math.max(64, Math.floor(rect.w / 2));
    for (let step = 0; step <= steps; step++) {
      const px = rect.x + step / steps * rect.w;
      const py = this.yOf(evalCurve(points, this.freqAt(px)));
      if (!step) path.moveTo(px, py); else path.lineTo(px, py);
    }
    if (this.curvePaths.size >= 8) this.curvePaths.clear();
    this.curvePaths.set(key, path);
    return path;
  }

  private drawCurve(ctx: CanvasRenderingContext2D, points: CurvePoint[], color: string | CanvasGradient, width: number, dashed = false) {
    if (points.length === 0) return;
    const rect = this.plotRect();
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    if (dashed) ctx.setLineDash([5, 4]);
    ctx.stroke(this.curvePath(points));
    ctx.restore();
  }

  /** Spectrum bars mirroring around the 0 dB axis (Apple-style center-out),
   *  hue matched to the curve gradient. */
  private drawSpectrum(ctx: CanvasRenderingContext2D) {
    const n = this.spectrumDisplay.length;
    if (!n || !this.spectrumDisplay.some((v) => v > 0.004)) return;
    const rect = this.plotRect();
    const [fmin, fmax] = this.freqRange;
    const ratio = fmax / fmin;
    const centerY = this.yOf(0);
    const halfSpan = Math.min(centerY - rect.y, rect.y + rect.h - centerY);
    ctx.save();
    for (let i = 0; i < n; i++) {
      const v = this.spectrumDisplay[i];
      if (v <= 0.004) continue;
      const e0 = fmin * ratio ** (i / n);
      const e1 = fmin * ratio ** ((i + 1) / n);
      // Thinnest readable line in each slot — iPhone-style hairlines.
      const slot0 = this.xOf(e0);
      const slot1 = this.xOf(e1);
      const bw = Math.max(2, Math.min((slot1 - slot0) * 0.35, 4));
      const x = (slot0 + slot1 - bw) / 2;
      const half = Math.max(1, v * halfSpan);
      ctx.fillStyle = curveColorAt(this.freqT(Math.sqrt(e0 * e1)), 0.45);
      ctx.beginPath();
      ctx.roundRect(x, centerY - half, bw, half * 2, bw / 2);
      ctx.fill();
    }
    ctx.restore();
  }

  /** Soft color wash between the curve and the 0 dB line, following the
   *  frequency gradient so the fill matches the stroke's hue. */
  private drawCurveFill(ctx: CanvasRenderingContext2D) {
    if (this.points.length === 0) return;
    const rect = this.plotRect();
    const zeroY = this.yOf(0);
    ctx.save();
    ctx.fillStyle = this.freqGradient(ctx, 0.13);
    const fill = new Path2D(this.curvePath(this.points));
    fill.lineTo(rect.x + rect.w, zeroY);
    fill.lineTo(rect.x, zeroY);
    fill.closePath();
    ctx.fill(fill);
    ctx.restore();
  }

  private formatFreq(freq: number): string {
    return freq >= 1000 ? `${(freq / 1000).toFixed(freq >= 10000 ? 0 : 1)} kHz` : `${Math.round(freq)} Hz`;
  }

  /** Floating "250 Hz · +3.0 dB" chip next to the hovered/dragged point. */
  private drawReadout(ctx: CanvasRenderingContext2D, index: number) {
    const p = this.points[index];
    if (!p) return;
    const text = `${this.formatFreq(p.freq)} · ${p.gain >= 0 ? "+" : ""}${p.gain.toFixed(1)} dB`;
    ctx.save();
    ctx.font = "11px system-ui, sans-serif";
    const padX = 9;
    const w = ctx.measureText(text).width + padX * 2;
    const h = 22;
    const rect = this.plotRect();
    let x = this.xOf(p.freq) + 12;
    let y = this.yOf(p.gain) - h - 12;
    if (x + w > rect.x + rect.w) x = this.xOf(p.freq) - w - 12;
    if (y < rect.y) y = this.yOf(p.gain) + 14;
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, 6);
    ctx.fillStyle = this.cssVar("--bg", "#fff");
    ctx.globalAlpha = 0.92;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = this.cssVar("--separator-strong", "rgba(128,128,128,0.4)");
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = this.cssVar("--text", "#111");
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(text, x + padX, y + h / 2 + 0.5);
    ctx.restore();
  }

  private repaint() {
    if (this.destroyed) return;
    const c = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth;
    const h = c.clientHeight;
    if (w === 0 || h === 0) return;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
      this.gridLayer = null;
    }
    if (!this.opts.minimal && !this.gridLayer) this.renderGrid();
    const ctx = c.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (this.gridLayer) ctx.drawImage(this.gridLayer, 0, 0, w, h);
    this.drawSpectrum(ctx);
    if (this.opts.target && this.opts.target.length > 1) {
      this.drawCurve(ctx, normalize(this.opts.target, this.gainRange), this.cssVar("--text-tertiary", "#888"), 1.5, true);
    }
    this.drawCurveFill(ctx);
    this.drawCurve(ctx, this.points, this.freqGradient(ctx, 1), 2.5);
    if (this.opts.readOnly) return; // display mode: no control dots, no readout
    // Control points: surface-colored dot with a ring in the curve's local hue.
    const dotFill = this.cssVar("--bg-secondary", this.cssVar("--bg", "#fff"));
    this.points.forEach((p, i) => {
      const px = this.xOf(p.freq);
      const py = this.yOf(p.gain);
      const active = i === this.hoverIndex || i === this.dragIndex || i === this.selectedIndex;
      const ring = curveColorAt(this.freqT(p.freq));
      ctx.save();
      if (active) {
        ctx.shadowColor = curveColorAt(this.freqT(p.freq), 0.6);
        ctx.shadowBlur = 12;
      }
      ctx.beginPath();
      ctx.arc(px, py, active ? 7 : 5.5, 0, Math.PI * 2);
      ctx.fillStyle = dotFill;
      ctx.fill();
      ctx.lineWidth = active ? 3 : 2.5;
      ctx.strokeStyle = ring;
      ctx.stroke();
      ctx.restore();
    });
    const readoutIndex = this.dragIndex >= 0 ? this.dragIndex : this.hoverIndex >= 0 ? this.hoverIndex : this.selectedIndex;
    if (readoutIndex >= 0) this.drawReadout(ctx, readoutIndex);
  }

  // ---- interaction ----

  private eventPos(e: PointerEvent | MouseEvent): { px: number; py: number } {
    const rect = this.canvas.getBoundingClientRect();
    return { px: e.clientX - rect.left, py: e.clientY - rect.top };
  }

  private hitPoint(px: number, py: number): number {
    let best = -1;
    let bestDist = HIT_RADIUS;
    this.points.forEach((p, i) => {
      const d = Math.hypot(this.xOf(p.freq) - px, this.yOf(p.gain) - py);
      if (d < bestDist) {
        bestDist = d;
        best = i;
      }
    });
    return best;
  }

  private onTouchStart = (e: TouchEvent) => {
    if (this.opts.readOnly) return;
    const touch = e.touches[0];
    if (!touch) return;
    const rect = this.canvas.getBoundingClientRect();
    // Claim the gesture only when it starts on a control point — elsewhere
    // the page keeps its vertical scroll so the canvas never eats swipes.
    if (this.hitPoint(touch.clientX - rect.left, touch.clientY - rect.top) >= 0) {
      e.preventDefault();
    }
  };

  private onPointerDown = (e: PointerEvent) => {
    if (this.opts.readOnly) return;
    const { px, py } = this.eventPos(e);
    const hit = this.hitPoint(px, py);
    this.dragWasExisting = hit >= 0;
    if (hit >= 0) {
      this.dragIndex = hit;
    } else {
      // Click on empty canvas inserts a control point and starts dragging it.
      this.clearSelection();
      const point = { freq: this.freqAt(px), gain: this.gainAt(py) };
      this.points.push(point);
      this.points = normalize(this.points, this.gainRange);
      this.dragIndex = this.points.findIndex(
        (p) => Math.abs(p.freq - point.freq) < point.freq * 0.02
      );
    }
    if (this.dragIndex >= 0) {
      this.moved = false;
      this.downX = px;
      this.downY = py;
      this.canvas.setPointerCapture(e.pointerId);
      e.preventDefault();
    }
  };

  private onPointerMove = (e: PointerEvent) => {
    const { px, py } = this.eventPos(e);
    if (this.dragIndex < 0) {
      const hover = this.hitPoint(px, py);
      if (hover !== this.hoverIndex) {
        this.hoverIndex = hover;
        this.canvas.style.cursor = hover >= 0 ? "grab" : "crosshair";
        this.repaint();
      }
      return;
    }
    // Touch fingers jitter: a sub-3px wobble is a tap, not a drag. Without
    // this threshold every tap on mobile reads as a (curve-rewriting) drag.
    if (!this.moved && Math.hypot(px - this.downX, py - this.downY) < 3) return;
    const point = this.points[this.dragIndex];
    if (!point) return;
    point.freq = this.freqAt(px);
    point.gain = this.gainAt(py);
    const [gmin, gmax] = this.gainRange;
    point.gain = Math.max(gmin, Math.min(gmax, point.gain));
    // Keep the array sorted so the renderer walks it in frequency order.
    this.points.sort((a, b) => a.freq - b.freq);
    this.dragIndex = this.points.indexOf(point);
    this.moved = true;
    this.repaint(); // local repaint only — no server round-trip mid-drag
  };

  private onPointerUp = (e: PointerEvent) => {
    if (this.dragIndex < 0) return;
    const releasedIndex = this.dragIndex;
    const wasExisting = this.dragWasExisting;
    const didMove = this.moved;
    this.dragIndex = -1;
    this.dragWasExisting = false;
    this.canvas.releasePointerCapture?.(e.pointerId);
    if (wasExisting && !didMove) {
      // A clean tap on an existing point toggles selection (mobile delete
      // path) instead of committing an unchanged curve to the server.
      this.selectedIndex = this.selectedIndex === releasedIndex ? -1 : releasedIndex;
      this.repaint();
      this.opts.onSelect?.(this.selectedIndex >= 0 ? this.selectedIndex : null);
      return;
    }
    // Commit the model before notifying selection: the host indexes the new model.
    this.selectedIndex = releasedIndex;
    this.repaint();
    this.opts.onCommit?.(this.points.map((p) => ({ ...p })));
    this.opts.onSelect?.(releasedIndex);
  };

  private onDoubleClick = (e: MouseEvent) => {
    if (this.opts.readOnly) return;
    const { px, py } = this.eventPos(e);
    const hit = this.hitPoint(px, py);
    if (hit >= 0 && this.points.length > 0) {
      this.points.splice(hit, 1);
      this.repaint();
      this.opts.onCommit?.(this.points.map((p) => ({ ...p })));
    }
  };
}
