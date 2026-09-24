import {Events} from './events.js';
import {cancellation, decode, type Json, type PendingInteraction} from './wire.js';

/** Connection-scoped ownership of modal requests, timers and early cancels. */
export class InteractionInbox extends Events {
  readonly items: PendingInteraction[] = [];
  private cancelled = new Set<string>();
  private closed = false;

  request({kind, request, timeout_seconds}: any): Promise<Json> {
    const cancelled = cancellation(kind);
    const decoded = decode(request);
    if (this.closed || this.cancelled.delete(decoded.request_id)) return Promise.resolve(cancelled);
    return new Promise<Json>(resolve => {
      const item: PendingInteraction = {kind, request: decoded, expiresAt: timeout_seconds == null ? null : performance.now() + timeout_seconds * 1000, resolve};
      if (timeout_seconds != null) item.timer = setTimeout(() => this.answer(item.request.request_id, cancelled), Math.max(0, timeout_seconds * 1000));
      this.items.push(item);
      this.emit('interactions');
    });
  }

  cancel(requestId: string): void {
    const item = this.items.find(item => item.request.request_id === requestId);
    if (item) this.answer(requestId, cancellation(item.kind));
    else {
      this.cancelled.add(requestId);
      if (this.cancelled.size > 1024) this.cancelled.delete(this.cancelled.values().next().value!);
    }
  }

  answer(requestId: string, response: Json): void {
    const index = this.items.findIndex(item => item.request.request_id === requestId);
    if (index < 0) return;
    const [item] = this.items.splice(index, 1);
    clearTimeout(item.timer);
    item.resolve(response);
    this.emit('answered', item, decode(response));
    this.emit('interactions');
  }

  close(): void {
    this.closed = true;
    for (const item of [...this.items]) this.cancel(item.request.request_id);
    this.cancelled.clear();
  }
}
