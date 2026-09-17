import {EventEmitter} from 'node:events';
import type {Readable, Writable} from 'node:stream';
import type {Json} from './wire.js';

export class RpcError extends Error {
  constructor(public code: number, message: string, public data?: Json) {super(message);}
}
type Handler = (params: any) => Json | Promise<Json>;

/** One reader; request handlers may wait for human input without blocking notifications. */
export class RpcPeer extends EventEmitter {
  readonly methods = new Map<string, Handler>();
  closed = false;
  private nextId = 0;
  private frameParts: Buffer[] = [];
  private frameBytes = 0;
  private pending = new Map<string, {resolve(value: Json): void; reject(error: Error): void; timer: NodeJS.Timeout}>();
  static readonly maxFrame = 16 * 1024 * 1024;

  constructor(private reader: Readable, private writer: Writable) {
    super();
    reader.on('data', this.receive);
    reader.on('end', () => this.close(new Error('Backend disconnected')));
    reader.on('error', error => this.close(error));
    writer.on('error', error => this.close(error));
  }

  request(method: string, params: Json = {}, timeout = 30_000): Promise<Json> {
    if (this.closed) return Promise.reject(new Error('Backend disconnected'));
    const id = String(++this.nextId);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {this.pending.delete(id); reject(new Error(`Request timed out: ${method}`));}, timeout);
      this.pending.set(id, {resolve, reject, timer});
      try {this.send({jsonrpc: '2.0', id, method, params});}
      catch (error) {clearTimeout(timer); this.pending.delete(id); reject(error);}
    });
  }

  notify(method: string, params: Json = {}): void {this.send({jsonrpc: '2.0', method, params});}

  private send(message: Json): void {
    if (this.closed) throw new Error('Backend disconnected');
    const line = JSON.stringify(message);
    if (Buffer.byteLength(line) > RpcPeer.maxFrame) throw new Error('RPC frame exceeds 16 MiB');
    this.writer.write(line + '\n');
  }

  private receive = (chunk: Buffer): void => {
    try {
      // Search only new bytes and join fragmented frames once, after the newline.
      let start = 0;
      while (start < chunk.length && !this.closed) {
        const end = chunk.indexOf(10, start);
        const part = chunk.subarray(start, end < 0 ? chunk.length : end);
        this.frameBytes += part.length;
        if (this.frameBytes > RpcPeer.maxFrame) throw new Error('RPC frame exceeds 16 MiB');
        if (end < 0) {this.frameParts.push(part); break;}
        const frame = this.frameParts.length ? Buffer.concat([...this.frameParts, part], this.frameBytes) : part;
        this.frameParts = []; this.frameBytes = 0;
        this.dispatch(JSON.parse(frame.toString('utf8')));
        start = end + 1;
      }
    } catch (error) {this.close(error as Error);}
  };

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
    this.frameParts = []; this.frameBytes = 0;
    this.reader.off('data', this.receive);
    for (const pending of this.pending.values()) {clearTimeout(pending.timer); pending.reject(error ?? new Error('Connection closed'));}
    this.pending.clear();
    this.writer.end();
    this.emit('close', error);
  }
}
