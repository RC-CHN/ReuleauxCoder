import type {RuntimeClient} from '@reuleauxcoder/client';
import type {RecordData} from '@reuleauxcoder/client';
import type {HistoryPage, ArtifactPage, HistoryOperation} from '@reuleauxcoder/client';
import {editor, type Editor} from './editor.js';

/** Retain one payload page; navigation keeps only cursors, never old bodies. */
export class HistoryBrowser {
  page?: HistoryPage;
  artifact?: ArtifactPage;
  loading = false;
  error = '';
  index = 0;
  offset = 0;
  end = 0;
  search: Editor | null = null;
  cursors: (string | null)[] = [null];
  private request = 0;
  private readonly generation: number;
  constructor(readonly client: RuntimeClient, readonly changed: () => void,
    readonly operation: HistoryOperation = 'read', readonly parameters: RecordData = {reverse: true}) {
    this.generation = client.state.session_generation;
  }
  get detailed() {return Boolean(this.parameters.event_id) || this.operation === 'artifact';}
  get nextCursor() {return (this.artifact ?? this.page)?.next_cursor;}
  get current() {return this.page?.records[this.index];}
  dispose() {this.request++; this.loading = false;}
  async load() {
    if (this.client.state.session_generation !== this.generation) {
      this.page = undefined; this.artifact = undefined;
      this.error = 'Session changed. Reopen history for the current session.'; this.changed(); return;
    }
    const request = ++this.request;
    this.loading = true; this.error = ''; this.changed();
    try {
      while (true) {
        const result = await this.client.history(this.operation, {...this.parameters, cursor: this.cursors.at(-1) ?? null});
        if (request !== this.request) return;
        if (result.session_generation !== this.generation || this.client.state.session_generation !== this.generation) {
          this.page = undefined; this.artifact = undefined;
          this.error = 'Session changed. Reopen history for the current session.'; break;
        }
        if (this.operation === 'artifact') {this.artifact = result.page as ArtifactPage; break;}
        this.page = result.page as HistoryPage;
        this.changed();
        // Newest-first reads wait for the index. An incomplete writer tail is
        // reported explicitly; it must not trigger a polling loop.
        if (!this.parameters.reverse || this.page.awaiting_tail || this.page.indexed_bytes >= this.page.source_bytes) break;
      }
      this.index = 0; this.offset = 0;
    } catch (error) {if (request === this.request) this.error = (error as Error).message;}
    finally {if (request === this.request) {this.loading = false; this.changed();}}
  }
  async next() {if (!this.loading && this.nextCursor) {this.cursors.push(this.nextCursor); await this.load();}}
  async previous() {if (!this.loading && this.cursors.length > 1) {this.cursors.pop(); await this.load();}}
  beginSearch() {this.search = editor(); this.changed();}
}
