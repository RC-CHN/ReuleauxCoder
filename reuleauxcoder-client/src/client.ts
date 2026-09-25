import {Events} from './events.js';
import {InteractionInbox} from './interactions.js';
import {ConfigurationClient} from './configuration.js';
import type {MessagePeer} from './message-peer.js';
import type {ArtifactPage, HistoryOperation, HistoryPage} from './history.js';
import type {AttachmentReference, GitWorkspace, ImageReference} from './wire.js';
import {actionRequest, decode, emptyState, enumValue, record, tuple, type Action, type Json, type RuntimeState, type UIEvent} from './wire.js';

export class RuntimeClient extends Events {
  readonly configuration: ConfigurationClient;
  state: RuntimeState = emptyState;
  catalog: Action[] = [];
  info: any;
  private inbox = new InteractionInbox();
  get interactions() {return this.inbox.items;}
  private closing = false;

  constructor(readonly peer: MessagePeer) {
    super();
    this.configuration = new ConfigurationClient(peer);
    this.inbox.on('interactions', () => this.emit('interactions'));
    this.inbox.on('answered', (item, response) => this.emit('answered', item, response));
    peer.on('notification', (method, params) => this.notification(method, params));
    peer.on('close', (error) => {
      this.inbox.close();
      if (!this.closing) this.emit('failure', error ?? new Error('Backend disconnected'));
    });
    peer.methods.set('interaction.request', params => this.inbox.request(params));
  }

  async initialize(profile: UIProfile, options: {ready?: boolean} = {}): Promise<void> {
    const capabilities = profile.capabilities;
    this.info = decode(await this.peer.request('initialize', {version: 1, profile: record('UIProfile', {ui_id: profile.ui_id, display_name: profile.display_name, capabilities: tuple(capabilities.map(item => enumValue('UICapability', item)), 'frozenset')})}));
    this.catalog = this.info.catalog.actions;
    if (this.info.version !== 1 || this.catalog.some(item => !Array.isArray(item.parameters))) throw new Error('This client requires a backend with command form metadata. Update the Python package.');
    this.update(this.info.state);
    this.emit('initialized', this.info);
    if (options.ready !== false) await this.ready();
  }

  /** Hosts can synchronize editor state before allowing restored goals to continue. */
  async ready(): Promise<void> {
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
        this.inbox.cancel(params.request_id);
        break;
      }
    }
  }

  answer(requestId: string, response: Json): void {
    this.inbox.answer(requestId, response);
  }

  async submit(value: Json, submissionId?: string, generation = this.state.session_generation): Promise<{status: string; state: RuntimeState; submission_id?: string}> {
    const params: Json = submissionId ? {value, submission_id: submissionId, session_generation: generation} : {value};
    const result = decode(await this.peer.request('runtime.submit', params));
    this.update(result.state);
    return result;
  }
  submitAction(id: string, command: {[key: string]: Json} = {}) {return this.submit(actionRequest(id, command));}
  async uploadImage(source: ImageSource): Promise<ImageReference> {
    if (!this.info?.image_uploads) throw new Error('Backend does not support image attachments.');
    return this.upload(source, 'images');
  }
  async uploadAttachment(source: AttachmentSource): Promise<AttachmentReference> {
    if (!this.info?.attachment_uploads) throw new Error('Backend does not support file attachments.');
    if (!Number.isSafeInteger(source.size) || source.size < 0 || source.size > 64 * 1024 * 1024) throw new Error('Attachment size must be between 0 and 64 MiB.');
    return this.upload(source, 'attachments');
  }
  private async upload(source: AttachmentSource, namespace: 'images' | 'attachments') {
    const state = this.state;
    const upload = await this.peer.request(`${namespace}.begin`, {session_id: state.session_id, session_generation: state.session_generation, name: source.name, size_bytes: source.size}) as any;
    try {
      if (!Number.isSafeInteger(upload.chunk_bytes) || upload.chunk_bytes <= 0 || upload.chunk_bytes > 256 * 1024) throw new Error('Invalid upload chunk limit');
      let offset = 0;
      while (offset < source.size) {
        const length = Math.min(upload.chunk_bytes, source.size - offset);
        const chunk = await source.read(offset, length);
        if (!chunk.length || chunk.length > length) throw new Error('Invalid upload source chunk');
        let binary = '';
        for (const byte of chunk) binary += String.fromCharCode(byte);
        const next = await this.peer.request(`${namespace}.append`, {upload_id: upload.upload_id, offset, data: btoa(binary)});
        if (next !== offset + chunk.length) throw new Error('Invalid upload acknowledgement');
        offset = next as number;
      }
      const reference = decode(await this.peer.request(`${namespace}.complete`, {upload_id: upload.upload_id}));
      if (state.session_id !== this.state.session_id || state.session_generation !== this.state.session_generation) throw new Error('Session changed during upload; attach it again.');
      return reference;
    } finally {
      await this.peer.request(`${namespace}.cancel`, {upload_id: upload.upload_id});
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
  async reviewDocument(requestId: string, documentId: string, side: 'before' | 'after'): Promise<string> {
    if (!this.info?.review_documents) throw new Error('Update the core to support native review documents.');
    let result = '', offset = 0;
    for (;;) {
      const page = await this.peer.request('review.document', {request_id: requestId, document_id: documentId, side, offset, limit: 65536}) as {text: string; next_offset: number; complete: boolean};
      if (typeof page.text !== 'string' || typeof page.complete !== 'boolean' || !Number.isSafeInteger(page.next_offset) || page.next_offset < offset || (!page.complete && page.next_offset === offset)) throw new Error('Invalid review document page');
      result += page.text;
      if (result.length > 4 * 1024 * 1024) throw new Error('Native review document exceeds the 4 Mi-character display limit. Review the text preview.');
      if (page.complete) return result;
      offset = page.next_offset;
    }
  }
  resize(rows: number, columns: number) {this.peer.notify('runtime.resize', {rows, columns});}
  recordPerformance(name: string, elapsedMs: number) {this.peer.notify('runtime.record_performance', {category: 'ui_render', name, elapsed_ms: elapsedMs});}
  /** Disconnect the transport; process ownership belongs to the host. */
  close(): void {
    this.closing = true;
    this.inbox.close();
    this.peer.close();
  }
  async shutdown(): Promise<string | null> {
    this.closing = true;
    this.inbox.close();
    if (this.peer.closed) return null;
    // Keep the transport alive until the durable save completes. Disconnects,
    // backend errors and an explicit host close still reject this request.
    try {return await this.peer.request('runtime.shutdown', {}, null) as string | null;}
    finally {this.close();}
  }
}

/** Browser File/Blob sources can use slice(offset, offset + length).arrayBuffer(). */
export interface AttachmentSource {
  name: string;
  size: number;
  read(offset: number, length: number): Promise<Uint8Array>;
}

export type ImageSource = AttachmentSource;

/** Each frontend advertises only the interactions it implements. */
export interface UIProfile {
  ui_id: string;
  display_name: string;
  capabilities: string[];
}
