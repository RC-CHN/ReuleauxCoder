import {randomUUID} from 'node:crypto';
import {RpcError, type Json, type RuntimeClient} from '@reuleauxcoder/client';
import type {SessionStore} from './session.js';

interface PendingSubmission {
  id: string;
  text: string;
  value: Json;
  generation: number;
  status: 'sending' | 'accepted' | 'queued' | 'rejected' | 'unconfirmed';
  inFlight: boolean;
}

/** Own sent drafts separately from the editable composer and backend transcript. */
export class SubmissionQueue {
  private entries = new Map<string, PendingSubmission>();
  constructor(private client: RuntimeClient, private session: SessionStore, private report: (message: string) => void) {}

  send(text: string, value: Json): Promise<void> {
    const item: PendingSubmission = {id: randomUUID(), text, value, generation: this.client.state.session_generation, status: 'sending', inFlight: false};
    this.entries.set(item.id, item);
    return this.deliver(item);
  }

  reset(): void {this.entries.clear();}

  async retry(): Promise<void> {
    const item = [...this.entries.values()].reverse().find(item => ['rejected', 'unconfirmed'].includes(item.status));
    if (!item) {this.report('No failed submission to retry.'); return;}
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
      if (!this.current(item)) return;
      item.status = receipt.status === 'rejected' ? 'rejected' : ['queued', 'steering'].includes(receipt.status) ? 'queued' : 'accepted';
      this.session.submission(item.id, item.text, item.status);
      if (item.status === 'rejected') this.report('The backend is stopping. Message retained; /retry resends it.');
    } catch (error) {
      if (!this.current(item)) return;
      item.status = error instanceof RpcError && [-32602, -32002].includes(error.code) ? 'rejected' : 'unconfirmed';
      const detail = error instanceof Error ? error.message : String(error);
      this.session.submission(item.id, item.text, item.status, detail);
      this.report(`${detail}. Message retained; /retry uses the same submission ID.`);
    } finally {item.inFlight = false;}
  }
}
