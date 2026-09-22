import {Events} from './events.js';
import type {Json} from './wire.js';

export class RpcError extends Error {
  constructor(public code: number, message: string, public data?: Json) {super(message);}
}
type Handler = (params: any) => Json | Promise<Json>;

/** One reader; request handlers may wait for human input without blocking notifications. */
export class MessagePeer extends Events {
  readonly methods = new Map<string, Handler>();
  closed = false;
  private nextId = 0;
  private pending = new Map<string, {resolve(value: Json): void; reject(error: Error): void; timer?: ReturnType<typeof setTimeout>}>();
  constructor(private writeMessage: (message: Json) => void, private disconnect: () => void = () => {}) {
    super();
  }

  request(method: string, params: Json = {}, timeout: number | null = 30_000): Promise<Json> {
    if (this.closed) return Promise.reject(new Error('Backend disconnected'));
    const id = String(++this.nextId);
    return new Promise((resolve, reject) => {
      const timer = timeout === null ? undefined : setTimeout(() => {this.pending.delete(id); reject(new Error(`Request timed out: ${method}`));}, timeout);
      this.pending.set(id, {resolve, reject, timer});
      try {this.send({jsonrpc: '2.0', id, method, params});}
      catch (error) {clearTimeout(timer); this.pending.delete(id); reject(error);}
    });
  }

  notify(method: string, params: Json = {}): void {this.send({jsonrpc: '2.0', method, params});}

  private send(message: Json): void {
    if (this.closed) throw new Error('Backend disconnected');
    this.writeMessage(message);
  }

  /** Feed one decoded envelope from postMessage, WebSocket, or another host adapter. */
  receive(message: Json): void {
    if (this.closed) return;
    try {this.dispatch(message);}
    catch (error) {this.close(error as Error);}
  }

  private dispatch(message: any): void {
    if (!message || message.jsonrpc !== '2.0') throw new Error('Invalid JSON-RPC envelope');
    if (typeof message.method === 'string') {
      if (!('id' in message)) {this.emit('notification', message.method, message.params ?? {}); return;}
      const handler = this.methods.get(message.method);
      Promise.resolve().then(() => {
        if (!handler) throw new RpcError(-32601, 'Method not found');
        return handler(message.params ?? {});
      }).then(result => {
        if (!this.closed) this.send({jsonrpc: '2.0', id: message.id, result});
      }).catch(error => {
        if (!this.closed) this.send({jsonrpc: '2.0', id: message.id, error: {code: error instanceof RpcError ? error.code : -32603, message: error.message}});
      }).catch(error => this.close(error));
      return;
    }
    const pending = this.pending.get(String(message.id));
    if (!pending) return;
    const error = 'error' in message ? new RpcError(message.error.code, message.error.message, message.error.data) : undefined;
    if (!error && !('result' in message)) throw new Error('RPC reply has neither result nor error');
    clearTimeout(pending.timer);
    this.pending.delete(String(message.id));
    if (error) pending.reject(error);
    else pending.resolve(message.result);
  }

  close(error?: Error): void {
    if (this.closed) return;
    this.closed = true;
    for (const pending of this.pending.values()) {clearTimeout(pending.timer); pending.reject(error ?? new Error('Connection closed'));}
    this.pending.clear();
    this.disconnect();
    this.emit('close', error);
  }
}
