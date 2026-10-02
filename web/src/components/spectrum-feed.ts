import { api } from "../api";
import { appWebSocketUrl } from "../paths";

/** Live spectrum tap shared by the tuning page and the fullscreen player.
 *
 * WebSocket push preferred; some webviews (WeChat) kill the WS handshake, so
 * after a couple of failed attempts fall back to polling the GET twin. Any
 * close triggers a retry, so a stale page loaded before a backend restart
 * recovers by itself. The server only runs its FFT while somebody is
 * subscribed, an idle overlay costs nothing.
 */
export class SpectrumFeed {
  private socket: WebSocket | null = null;
  private timer: number | null = null; // reconnect delay or poll interval
  private gen = 0; // guards stale callbacks after an attach/close
  private did: string | null = null;
  private attempts = 0;
  private handshake: number | null = null;
  private polling = false;
  private readonly apply: (bands: number[] | null) => void;

  constructor(apply: (bands: number[] | null) => void) {
    this.apply = apply;
  }

  /** Subscribe to a device's spectrum; null closes the feed. Re-attaching the
   *  same did is a no-op so poll-driven re-renders don't bounce the socket. */
  attach(did: string | null): void {
    if (did === this.did) return;
    this.close();
    this.did = did;
    if (did) this.connect(did);
    else this.apply(null);
  }

  get attachedDid(): string | null {
    return this.did;
  }

  destroy(): void {
    this.attach(null);
  }

  private close(): void {
    this.gen += 1;
    this.did = null;
    this.attempts = 0;
    this.polling = false;
    this.socket?.close();
    this.socket = null;
    if (this.handshake !== null) window.clearTimeout(this.handshake);
    this.handshake = null;
    if (this.timer != null) {
      window.clearTimeout(this.timer);
      window.clearInterval(this.timer);
      this.timer = null;
    }
  }

  private connect(did: string): void {
    const gen = this.gen;
    const applyBands = (bands: number[] | null) => {
      if (gen === this.gen && this.did === did) this.apply(bands);
    };

    const startPolling = () => {
      const tick = () => {
        if (document.hidden || this.polling || gen !== this.gen) return;
        this.polling = true;
        api
          .getSpectrum(did)
          .then((r) => applyBands(r.bands ?? null))
          .catch(() => undefined)
          .finally(() => { if (gen === this.gen) this.polling = false; });
      };
      tick();
      this.timer = window.setInterval(tick, 500);
    };

    const connect = () => {
      if (gen !== this.gen || this.did !== did) return;
      const recover = () => {
        if (gen !== this.gen || this.did !== did) return;
        applyBands(null);
        this.attempts += 1;
        if (this.attempts <= 3) this.timer = window.setTimeout(connect, 2000);
        else startPolling();
      };
      let socket: WebSocket;
      try { socket = new WebSocket(appWebSocketUrl(`api/tuning/${encodeURIComponent(did)}/spectrum`)); }
      catch { recover(); return; }
      this.socket = socket;
      const closed = () => {
        if (gen !== this.gen || this.socket !== socket || this.did !== did) return;
        if (this.handshake !== null) window.clearTimeout(this.handshake);
        this.handshake = null;
        this.socket = null;
        socket.close();
        recover();
      };
      this.handshake = window.setTimeout(() => {
        if (this.socket === socket && socket.readyState !== WebSocket.OPEN) closed();
      }, 8000);
      socket.onopen = () => {
        if (this.socket !== socket || gen !== this.gen) return;
        if (this.handshake !== null) window.clearTimeout(this.handshake);
        this.handshake = null;
        this.attempts = 0;
      };
      socket.onerror = closed;
      socket.onmessage = (ev) => {
        try {
          applyBands((JSON.parse(String(ev.data)) as { bands?: number[] | null }).bands ?? null);
        } catch {
          // Malformed frame — ignore.
        }
      };
      socket.onclose = closed;
    };
    connect();
  }
}
