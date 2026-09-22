import {Events} from './events.js';
import type {MessagePeer} from './message-peer.js';
import type {ArtifactPage, HistoryOperation, HistoryPage} from './history.js';
import type {GitWorkspace, ImageReference} from './wire.js';
import {actionRequest, cancellation, decode, emptyState, enumValue, record, tuple, type Action, type Json, type PendingInteraction, type RuntimeState, type UIEvent} from './wire.js';

export class RuntimeClient extends Events {
  state: RuntimeState = emptyState;
  catalog: Action[] = [];
  info: any;
  interactions: PendingInteraction[] = [];
  private closing = false;

  constructor(readonly peer: MessagePeer) {
    super();
    peer.on('notification', (method, params) => this.notification(method, params));
    peer.on('close', (error) => {
      for (const item of [...this.interactions]) this.answer(item.request.request_id, cancellation(item.kind));
      if (!this.closing) this.emit('failure', error ?? new Error('Backend disconnected'));
    });
    peer.methods.set('interaction.request', ({kind, request, timeout_seconds}) => new Promise<Json>(resolve => {
      cancellation(kind); // Reject unsupported interaction kinds at the boundary.
      const item: PendingInteraction = {kind, request: decode(request), expiresAt: timeout_seconds == null ? null : performance.now() + timeout_seconds * 1000, resolve};
      if (timeout_seconds != null) item.timer = setTimeout(() => this.answer(item.request.request_id, cancellation(kind)), Math.max(0, timeout_seconds * 1000));
      this.interactions.push(item);
      this.emit('interactions');
    }));
  }

  async initialize(profile: {ui_id: string; display_name: string; capabilities: string[]} = tuiProfile): Promise<void> {
    const capabilities = profile.capabilities;
    this.info = decode(await this.peer.request('initialize', {version: 1, profile: record('UIProfile', {ui_id: profile.ui_id, display_name: profile.display_name, capabilities: tuple(capabilities.map(item => enumValue('UICapability', item)), 'frozenset')})}));
    this.catalog = this.info.catalog.actions;
    if (this.info.version !== 1 || this.catalog.some(item => !Array.isArray(item.parameters))) throw new Error('This client requires a backend with command form metadata. Update the Python package.');
    this.update(this.info.state);
    this.emit('initialized', this.info);
    if (this.info.goals && !this.info.host_mode) this.update(decode(await this.peer.request('runtime.ready')));
  }

  private update(state: RuntimeState): void {
    if (state.revision > this.state.revision) {this.state = state; this.emit('state', state);}
  }

  private notification(method: string, params: any): void {
    switch (method) {
      case 'runtime.state': this.update(decode(params.state)); break;
      case 'runtime.event': this.emit('event', decode(params.event) as UIEvent, params.event, params.session_generation); break;
      case 'runtime.completed': this.emit('completed', decode(params.result)); break;
      case 'runtime.command': this.emit('command', params.text); break;
      case 'runtime.failed': this.emit('operationFailure', `${params.error_type}: ${params.message}`); break;
      case 'runtime.shutdown_progress': this.emit('shutdownProgress', params.message); break;
      case 'interaction.cancel': {
        const item = this.interactions.find(item => item.request.request_id === params.request_id);
        if (item) this.answer(item.request.request_id, cancellation(item.kind));
        break;
      }
    }
  }

  answer(requestId: string, response: Json): void {
    const index = this.interactions.findIndex(item => item.request.request_id === requestId);
    if (index < 0) return;
    const [item] = this.interactions.splice(index, 1);
    clearTimeout(item.timer);
    this.emit('answered', item, decode(response));
    item.resolve(response);
    this.emit('interactions');
  }

  async submit(value: Json): Promise<{status: string; state: RuntimeState}> {
    const result = decode(await this.peer.request('runtime.submit', {value}));
    this.update(result.state);
    return result;
  }
  submitAction(id: string, command: {[key: string]: Json} = {}) {return this.submit(actionRequest(id, command));}
  async uploadImage(source: ImageSource): Promise<ImageReference> {
    if (!this.info?.image_uploads) throw new Error('Backend does not support image attachments.');
    const state = this.state;
    const upload = await this.peer.request('images.begin', {session_id: state.session_id, session_generation: state.session_generation, name: source.name, size_bytes: source.size}) as any;
    try {
      let offset = 0;
      while (offset < source.size) {
        const chunk = await source.read(offset, Math.min(upload.chunk_bytes, source.size - offset));
        if (!chunk.length || chunk.length > upload.chunk_bytes) throw new Error('Invalid image source chunk');
        let binary = '';
        for (const byte of chunk) binary += String.fromCharCode(byte);
        offset = await this.peer.request('images.append', {upload_id: upload.upload_id, offset, data: btoa(binary)}) as number;
      }
      const image = decode(await this.peer.request('images.complete', {upload_id: upload.upload_id}));
      if (state.session_id !== this.state.session_id || state.session_generation !== this.state.session_generation) throw new Error('Session changed during image upload; attach it again.');
      return image;
    } finally {
      await this.peer.request('images.cancel', {upload_id: upload.upload_id});
    }
  }
  async panel(payload: Json) {return decode(await this.peer.request('view.panel', {payload}));}
  async refresh() {
    const state = await this.peer.request('runtime.snapshot', this.info?.conditional_snapshots ? {known_revision: this.state.revision} : {}, 5000);
    if (state !== null) this.update(decode(state));
  }
  async git(): Promise<GitWorkspace | null> {return decode(await this.peer.request('runtime.git', {}, 5000));}
  async history(operation: HistoryOperation, parameters: {[key: string]: Json}): Promise<{session_generation: number; page: HistoryPage | ArtifactPage}> {
    return decode(await this.peer.request(`history.${operation}`, parameters));
  }
  async interrupt(): Promise<{outcome: string; discarded_count: number}> {return decode(await this.peer.request('runtime.interrupt'));}
  resize(rows: number, columns: number) {this.peer.notify('runtime.resize', {rows, columns});}
  recordPerformance(elapsedMs: number) {this.peer.notify('runtime.record_performance', {category: 'ui_render', name: 'ink_render', elapsed_ms: elapsedMs});}
  /** Disconnect the transport; process ownership belongs to the host. */
  close(): void {
    this.closing = true;
    for (const item of [...this.interactions]) this.answer(item.request.request_id, cancellation(item.kind));
    this.peer.close();
  }
  async shutdown(): Promise<string | null> {
    this.closing = true;
    for (const item of [...this.interactions]) this.answer(item.request.request_id, cancellation(item.kind));
    if (this.peer.closed) return null;
    // Keep the transport alive until the durable save completes. Disconnects,
    // backend errors and an explicit host close still reject this request.
    try {return await this.peer.request('runtime.shutdown', {}, null) as string | null;}
    finally {this.close();}
  }
}

/** Browser File/Blob sources can use slice(offset, offset + length).arrayBuffer(). */
export interface ImageSource {
  name: string;
  size: number;
  read(offset: number, length: number): Promise<Uint8Array>;
}

export const tuiProfile = {
  ui_id: 'tui', display_name: 'ReuleauxCoder React TUI',
  capabilities: ['text_input', 'stream_output', 'palette', 'buttons', 'menus', 'modal', 'diff_review', 'text_select', 'text_edit', 'secure_text_input'],
};
