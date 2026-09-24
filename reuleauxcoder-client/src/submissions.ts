import {RpcError} from './message-peer.js';
import type {Json} from './wire.js';
import type {RuntimeClient} from './client.js';

export interface SubmissionSink {
  submission(id: string, text: string, status: string, detail?: string): void;
}
export interface SubmissionMessages {empty: string; stopped: string; uncertain(detail: string): string}
const defaultMessages: SubmissionMessages = {
  empty: 'No failed submission to retry.',
  stopped: 'The backend is stopping. Message retained; /retry resends it.',
  uncertain: detail => `${detail}. Message retained; /retry uses the same submission ID.`,
};

interface PendingSubmission {
  id: string;
  text: string;
  value: Json;
  generation: number;
  status: 'sending' | 'accepted' | 'queued' | 'rejected' | 'unconfirmed' | 'applied';
  inFlight: boolean;
}

/** Own sent drafts separately from the editable composer and backend transcript. */
export class SubmissionQueue {
  private entries = new Map<string, PendingSubmission>();
  constructor(private client: RuntimeClient, private session: SubmissionSink, private report: (message: string) => void, private messages: SubmissionMessages = defaultMessages) {}

  send(text: string, value: Json, id: string = globalThis.crypto.randomUUID()): Promise<void> {
    const previous = this.entries.get(id);
    if (previous) {
      if (JSON.stringify(previous.value) !== JSON.stringify(value)) throw new Error('Submission ID already belongs to different input.');
      return Promise.resolve();
    }
    const item: PendingSubmission = {id, text, value, generation: this.client.state.session_generation, status: 'sending', inFlight: false};
    this.entries.set(item.id, item);
    return this.deliver(item);
  }

  reset(): void {this.entries.clear();}
  has(id: string): boolean {return this.entries.has(id);}

  applied(id: string): void {
    const item = this.entries.get(id);
    if (item && this.current(item)) item.status = 'applied';
  }

  private isApplied(item: PendingSubmission): boolean {return item.status === 'applied';}

  async retry(id?: string): Promise<void> {
    const item = [...this.entries.values()].reverse().find(item => (!id || item.id === id) && ['rejected', 'unconfirmed'].includes(item.status));
    if (!item) {this.report(this.messages.empty); return;}
    await this.deliver(item);
  }

  private current(item: PendingSubmission): boolean {
    return this.entries.get(item.id) === item && item.generation === this.client.state.session_generation;
  }

  private async deliver(item: PendingSubmission): Promise<void> {
    if (item.inFlight || !this.current(item)) return;
    item.inFlight = true;
    item.status = 'sending';
    this.session.submission(item.id, item.text, 'sending');
    try {
      const receipt = await this.client.submit(item.value, item.id, item.generation);
      if (!this.current(item) || this.isApplied(item)) return;
      item.status = receipt.status === 'rejected' ? 'rejected' : ['queued', 'steering'].includes(receipt.status) ? 'queued' : 'accepted';
      this.session.submission(item.id, item.text, item.status);
      if (item.status === 'rejected') this.report(this.messages.stopped);
    } catch (error) {
      if (!this.current(item) || this.isApplied(item)) return;
      item.status = error instanceof RpcError && [-32602, -32002].includes(error.code) ? 'rejected' : 'unconfirmed';
      const detail = error instanceof Error ? error.message : String(error);
      this.session.submission(item.id, item.text, item.status, detail);
      this.report(this.messages.uncertain(detail));
    } finally {item.inFlight = false;}
  }
}
