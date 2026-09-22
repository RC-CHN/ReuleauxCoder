import type {Readable, Writable} from 'node:stream';
import {MessagePeer} from './message-peer.js';
export {RpcError} from './message-peer.js';

/** Node stdio adapter; the message peer itself has no stream or Buffer dependency. */
export class RpcPeer extends MessagePeer {
  static readonly maxFrame = 16 * 1024 * 1024;
  private frameParts: Buffer[] = [];
  private frameBytes = 0;

  constructor(private reader: Readable, writer: Writable) {
    super(message => {
      const line = JSON.stringify(message);
      if (Buffer.byteLength(line) > RpcPeer.maxFrame) throw new Error('RPC frame exceeds 16 MiB');
      writer.write(line + '\n');
    }, () => writer.end());
    reader.on('data', this.receiveChunk);
    reader.on('end', () => this.close(new Error('Backend disconnected')));
    reader.on('error', error => this.close(error));
    writer.on('error', error => this.close(error));
  }

  private receiveChunk = (chunk: Buffer): void => {
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
        this.receive(JSON.parse(frame.toString('utf8')));
        start = end + 1;
      }
    } catch (error) {this.close(error as Error);}
  };

  override close(error?: Error): void {
    this.frameParts = []; this.frameBytes = 0;
    this.reader.off('data', this.receiveChunk);
    super.close(error);
  }
}
