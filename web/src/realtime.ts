/** One application-owned business subscription with bounded handshake and HTTP recovery. */
import { appWebSocketUrl } from './paths';
import { SurfaceScope } from './ui/lifecycle';

interface RealtimeOptions {
  message: (message: { type: string; data?: any; revision?: number }) => void;
  refresh: () => Promise<void>;
  fallback: () => Promise<void>;
}

export class RealtimeConnection {
  private scope = new SurfaceScope();
  private socket: WebSocket | null = null;
  private reconnectTimer: number | null = null;
  private fallbackTimer: number | null = null;
  private handshakeTimer: number | null = null;
  private lastMessage = Date.now();
  private busy = false;
  state: 'connecting' | 'open' | 'fallback' | 'closed' = 'closed';

  constructor(private options: RealtimeOptions) {}

  start() {
    const wake = () => { if (!document.hidden) { void this.probe(); this.connect(true); } };
    this.scope.listen(document, 'visibilitychange', wake);
    this.scope.listen(window, 'pageshow', wake);
    this.scope.interval(() => {
      if (!document.hidden && Date.now() - this.lastMessage >= 15000) void this.probe();
    }, 5000);
    this.scope.own(() => this.disconnect());
    this.connect();
  }

  private clearTimer(name: 'reconnectTimer' | 'fallbackTimer' | 'handshakeTimer') {
    if (this[name] !== null) { clearTimeout(this[name]!); clearInterval(this[name]!); this[name] = null; }
  }

  private connect(replace = false) {
    if (!this.scope.active) return;
    if (!replace && this.socket && this.state !== 'fallback') return;
    this.clearTimer('reconnectTimer');
    this.disconnect();
    this.state = 'connecting';
    // HTTP remains available while the handshake is pending.
    this.startFallback();
    let socket: WebSocket;
    try { socket = new WebSocket(appWebSocketUrl('api/ws')); }
    catch { this.scheduleReconnect(); return; }
    this.socket = socket;
    this.handshakeTimer = window.setTimeout(() => {
      if (this.socket === socket && this.state !== 'open') this.scheduleReconnect();
    }, 8000);
    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.clearTimer('handshakeTimer');
      this.state = 'open'; this.lastMessage = Date.now();
      this.clearTimer('fallbackTimer');
    };
    socket.onmessage = event => {
      if (this.socket !== socket) return;
      try {
        const message = JSON.parse(String(event.data));
        this.lastMessage = Date.now();
        window.dispatchEvent(new CustomEvent('micast:connection', { detail: { ok: true } }));
        this.options.message(message);
      } catch { /* Ignore malformed frames. */ }
    };
    socket.onclose = () => { if (this.socket === socket) this.scheduleReconnect(); };
    socket.onerror = () => { if (this.socket === socket) this.scheduleReconnect(); };
  }

  private startFallback() {
    if (this.fallbackTimer !== null) return;
    this.fallbackTimer = window.setInterval(() => { if (!document.hidden) void this.poll(); }, 3000);
  }

  private async poll() {
    if (this.busy || !this.scope.active) return;
    this.busy = true;
    try { await this.options.fallback(); }
    catch { /* API layer reports service health; keep the last snapshot. */ }
    finally { this.busy = false; }
  }

  private async probe() {
    if (this.busy || !this.scope.active) return;
    this.busy = true;
    try { await this.options.refresh(); }
    catch { this.scheduleReconnect(); }
    finally {
      // HTTP confirmation never counts as a socket message.
      this.busy = false;
      if (this.state === 'open' && Date.now() - this.lastMessage > 25000) this.scheduleReconnect();
    }
  }

  private scheduleReconnect() {
    if (!this.scope.active) return;
    this.disconnect(); this.state = 'fallback'; this.startFallback();
    if (this.reconnectTimer === null && this.scope.active) this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null; this.connect();
    }, 30000);
  }

  private disconnect() {
    this.clearTimer('handshakeTimer');
    const socket = this.socket; this.socket = null;
    socket?.close();
  }

  dispose() {
    this.clearTimer('reconnectTimer'); this.clearTimer('fallbackTimer');
    this.scope.dispose(); this.state = 'closed';
  }
}
