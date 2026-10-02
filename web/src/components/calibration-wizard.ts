/**
 * Calibration wizard (inside the tuning page): play a sweep on the speaker,
 * record it through the browser microphone, derive and apply a compensation
 * curve. Optionally continues into group level matching.
 *
 * Requires a secure context (localhost / HTTPS) for getUserMedia; the intro
 * step says so when the microphone is unavailable.
 */

import { api } from "../api";
import { store } from "../state";
import { EqCurveCanvas, type CurvePoint } from "./eq-curve-canvas";

type WizardStep = "intro" | "measuring" | "result" | "level-intro" | "level-measuring" | "level-result" | "error";

interface WizardResult {
  points: CurvePoint[];
  measured: { freqs: number[]; gains: number[] };
  levelDbfs: number;
}

export class CalibrationWizard {
  private container: HTMLElement;
  private did: string;
  private onApplied: () => void;
  private step: WizardStep = "intro";
  private target = "";
  private token: string | null = null;
  private stream: MediaStream | null = null;
  private result: WizardResult | null = null;
  private error = "";
  private levels: Record<string, number> = {};
  private levelQueue: string[] = [];
  private chart: EqCurveCanvas | null = null;
  private destroyed = false;

  constructor(container: HTMLElement, did: string, onApplied: () => void) {
    this.container = container;
    this.did = did;
    this.onApplied = onApplied;
    this.render();
  }

  destroy() {
    this.destroyed = true;
    this.chart?.destroy();
    this.stream?.getTracks().forEach((t) => t.stop());
    if (this.token) {
      api.calibrationStop(this.token).catch(() => undefined);
      this.token = null;
    }
  }

  private get micAvailable(): boolean {
    return Boolean(window.isSecureContext && navigator.mediaDevices?.getUserMedia);
  }

  private groupOf(did: string) {
    return store.get().fullConfig?.groups.find((g) => g.speaker_ids.includes(did) && g.speaker_ids.length >= 2);
  }

  private speakerName(did: string): string {
    const d = store.get().devices.find((item) => item.did === did);
    return d ? d.alias || d.name : did;
  }

  private render() {
    if (this.destroyed) return;
    this.chart?.destroy();
    this.chart = null;
    const body: Record<WizardStep, () => string> = {
      intro: () => `
        <h3>自动校准</h3>
        <p>音箱将播放一段 ${"约 6 秒的扫频信号"}，请把这台设备的麦克风对准音箱、保持环境安静。测得房间的频率响应后，自动生成补偿曲线。</p>
        <p class="wizard-note">实验性功能：结果依赖房间声学与麦克风摆放，测量可能有偏差，建议应用后结合试听微调。</p>
        ${this.micAvailable ? "" : `<p class="wizard-warning">当前页面不是 localhost 或 HTTPS，浏览器不允许使用麦克风。请改用本机访问地址后再试。</p>`}
        <label class="wizard-field">
          <span class="caption">校准目标</span>
          <select data-wiz-target>
            <option value="" ${this.target === "" ? "selected" : ""}>平直</option>
            <option value="harman" ${this.target === "harman" ? "selected" : ""}>Harman 目标</option>
            <option value="diffuse_field" ${this.target === "diffuse_field" ? "selected" : ""}>扩散场</option>
          </select>
        </label>
        <div class="wizard-actions">
          <button class="button" data-wiz-start ${this.micAvailable ? "" : "disabled"}>开始测量</button>
          <button class="button secondary" data-wiz-close>取消</button>
        </div>`,
      measuring: () => `
        <h3>正在测量…</h3>
        <p>扫频信号播放中，请勿移动设备。<span data-wiz-countdown></span></p>
        <div class="wizard-progress"><div class="wizard-progress-bar" data-wiz-progress></div></div>`,
      result: () => `
        <h3>测量完成</h3>
        <p>下图虚线是测得的房间响应，实线是建议的补偿曲线（${this.result?.points.length ?? 0} 个控制点）。</p>
        <div class="wizard-chart"><canvas data-wiz-chart></canvas></div>
        <div class="wizard-actions">
          <button class="button" data-wiz-apply>应用补偿曲线</button>
          <button class="button secondary" data-wiz-retry>重新测量</button>
          <button class="button secondary" data-wiz-close>关闭</button>
        </div>`,
      "level-intro": () => `
        <h3>声压配平</h3>
        <p>检测到这台音箱属于组合「${escapeHtml(this.groupOf(this.did)?.name ?? "")}」。逐台测量各音箱的响度，自动配平组合音量。</p>
        <div class="wizard-actions">
          <button class="button" data-wiz-level-start>开始配平（共 ${this.levelQueue.length} 台）</button>
          <button class="button secondary" data-wiz-close>跳过</button>
        </div>`,
      "level-measuring": () => `
        <h3>声压配平中…</h3>
        <p>正在测量「${escapeHtml(this.speakerName(this.levelQueue[0] ?? ""))}」（还剩 ${this.levelQueue.length} 台）。</p>
        <div class="wizard-progress"><div class="wizard-progress-bar" data-wiz-progress></div></div>`,
      "level-result": () => `
        <h3>配平完成</h3>
        <p>${Object.entries(this.levels).map(([did, v]) => `${escapeHtml(this.speakerName(did))}: ${v.toFixed(1)} dB`).join(" · ")}</p>
        <div class="wizard-actions">
          <button class="button" data-wiz-close>完成</button>
        </div>`,
      error: () => `
        <h3>测量失败</h3>
        <p class="wizard-warning">${escapeHtml(this.error)}</p>
        <div class="wizard-actions">
          <button class="button" data-wiz-retry>重试</button>
          <button class="button secondary" data-wiz-close>关闭</button>
        </div>`,
    };
    this.container.innerHTML = `<div class="wizard">${body[this.step]()}</div>`;
    this.bind();
  }

  private bind() {
    const q = <T extends HTMLElement>(sel: string) => this.container.querySelector<T>(sel);
    q("[data-wiz-close]")?.addEventListener("click", () => this.close());
    q("[data-wiz-retry]")?.addEventListener("click", () => {
      this.step = "intro";
      this.render();
    });
    q<HTMLSelectElement>("[data-wiz-target]")?.addEventListener("change", (e) => {
      this.target = (e.target as HTMLSelectElement).value;
    });
    q("[data-wiz-start]")?.addEventListener("click", () => void this.runMeasure());
    q("[data-wiz-apply]")?.addEventListener("click", () => void this.apply());
    q("[data-wiz-level-start]")?.addEventListener("click", () => void this.measureLevelNext());

    if (this.step === "result" && this.result) {
      const canvas = q<HTMLCanvasElement>("[data-wiz-chart]");
      if (canvas) {
        const measured = this.result.measured.freqs.map((f, i) => ({
          freq: f,
          gain: this.result!.measured.gains[i] ?? 0,
        }));
        this.chart = new EqCurveCanvas(canvas, {
          points: this.result.points,
          target: measured,
          readOnly: true,
        });
      }
    }
  }

  private close() {
    this.destroy();
    this.container.innerHTML = "";
    this.container.hidden = true;
    this.onApplied();
  }

  private fail(message: string) {
    this.error = message;
    this.step = "error";
    this.render();
  }

  private setProgress(fraction: number, label: string) {
    const bar = this.container.querySelector<HTMLElement>("[data-wiz-progress]");
    if (bar) bar.style.width = `${Math.round(fraction * 100)}%`;
    const cd = this.container.querySelector<HTMLElement>("[data-wiz-countdown]");
    if (cd) cd.textContent = label;
  }

  private async runMeasure(): Promise<void> {
    this.step = "measuring";
    this.render();
    const result = await this.measure(this.did, true);
    // measure() already switched to the error step on failure.
    if (result) {
      this.step = "result";
      this.render();
    }
  }

  private async measure(did: string, keepResult: boolean): Promise<WizardResult | null> {
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
      if (this.destroyed) {
        this.stream.getTracks().forEach(t => t.stop());
        this.stream = null;
        return null;
      }
    } catch {
      this.fail("无法访问麦克风，请检查浏览器授权");
      return null;
    }
    let session: { token: string; duration_seconds: number };
    try {
      session = await api.calibrationStart(did);
      if (this.destroyed) {
        this.stream?.getTracks().forEach(t => t.stop());
        this.stream = null;
        await api.calibrationStop(session.token).catch(() => undefined);
        return null;
      }
    } catch (e) {
      this.stream?.getTracks().forEach((t) => t.stop());
      this.stream = null;
      this.fail(e instanceof Error ? e.message : "音箱未能开始播放");
      return null;
    }
    this.token = session.token;
    const durationMs = (session.duration_seconds + 2) * 1000;

    const recorder = new MediaRecorder(this.stream);
    const chunks: Blob[] = [];
    recorder.ondataavailable = (e) => chunks.push(e.data);
    recorder.start();

    const started = performance.now();
    await new Promise<void>((resolve) => {
      const tick = () => {
        if (this.destroyed) return resolve();
        const elapsed = performance.now() - started;
        this.setProgress(Math.min(1, elapsed / durationMs), `剩余 ${Math.max(0, Math.ceil((durationMs - elapsed) / 1000))} 秒`);
        if (elapsed >= durationMs) resolve();
        else window.setTimeout(tick, 200);
      };
      tick();
    });
    await new Promise<void>(resolve => {
      recorder.onstop = () => resolve();
      if (recorder.state === 'inactive') resolve(); else recorder.stop();
    });
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    await api.calibrationStop(session.token).catch(() => undefined);
    this.token = null;
    if (this.destroyed) return null;

    // MediaRecorder yields opus/webm etc.; decode and re-encode as PCM WAV
    // so the server needs no codec support.
    let wav: Blob;
    try {
      const raw = await new Blob(chunks).arrayBuffer();
      const ctx = new AudioContext();
      const decoded = await ctx.decodeAudioData(raw);
      await ctx.close();
      wav = encodeWav(decoded);
    } catch {
      this.fail("录音解码失败，请重试");
      return null;
    }

    try {
      const analysis = await api.calibrationAnalyze(wav, did, this.target);
      if (this.destroyed) return null;
      const result: WizardResult = {
        points: analysis.points.map(([freq, gain]) => ({ freq, gain })),
        measured: analysis.measured,
        levelDbfs: analysis.level_dbfs,
      };
      if (keepResult) this.result = result;
      return result;
    } catch (e) {
      this.fail(e instanceof Error ? e.message : "录音分析失败");
      return null;
    }
  }

  private async apply() {
    if (!this.result) return;
    try {
      await api.calibrationApply(
        this.did,
        this.result.points.map((p) => [p.freq, p.gain]),
        this.target,
        store.get().devices.find((device) => device.did === this.did)?.eq?.revision
      );
      store.showToast("补偿曲线已应用");
    } catch (e) {
      this.fail(e instanceof Error ? e.message : "应用失败");
      return;
    }
    const group = this.groupOf(this.did);
    if (group && this.micAvailable) {
      this.levels = {};
      this.levelQueue = group.speaker_ids.filter((d) => d !== this.did);
      this.levels[this.did] = this.result.levelDbfs;
      if (this.levelQueue.length > 0) {
        this.step = "level-intro";
        this.render();
        return;
      }
    }
    this.close();
  }

  private async measureLevelNext() {
    const did = this.levelQueue[0];
    if (!did) {
      const group = this.groupOf(this.did);
      if (group) {
        try {
          await api.levelMatch(group.id, this.levels);
          store.showToast("组合声压已配平");
        } catch (e) {
          store.showToast(`配平失败: ${e instanceof Error ? e.message : "未知错误"}`);
        }
      }
      this.step = "level-result";
      this.render();
      return;
    }
    this.step = "level-measuring";
    this.render();
    const result = await this.measure(did, false);
    if (this.destroyed) return;
    if (result) {
      this.levels[did] = result.levelDbfs;
      this.levelQueue.shift();
      await this.measureLevelNext();
    }
    // measure() already switched to the error step on failure.
  }
}

function encodeWav(buffer: AudioBuffer): Blob {
  // Downmix to mono 16-bit PCM at the buffer's native rate.
  const rate = buffer.sampleRate;
  const n = buffer.length;
  const mono = new Float32Array(n);
  for (let ch = 0; ch < buffer.numberOfChannels; ch++) {
    const data = buffer.getChannelData(ch);
    for (let i = 0; i < n; i++) mono[i] += data[i] / buffer.numberOfChannels;
  }
  const out = new DataView(new ArrayBuffer(44 + n * 2));
  const writeStr = (offset: number, s: string) => {
    for (let i = 0; i < s.length; i++) out.setUint8(offset + i, s.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  out.setUint32(4, 36 + n * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  out.setUint32(16, 16, true);
  out.setUint16(20, 1, true);
  out.setUint16(22, 1, true);
  out.setUint32(24, rate, true);
  out.setUint32(28, rate * 2, true);
  out.setUint16(32, 2, true);
  out.setUint16(34, 16, true);
  writeStr(36, "data");
  out.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    out.setInt16(44 + i * 2, Math.max(-32768, Math.min(32767, Math.round(mono[i] * 32767))), true);
  }
  return new Blob([out.buffer], { type: "audio/wav" });
}

function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
