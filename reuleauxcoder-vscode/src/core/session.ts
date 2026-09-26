import {t, errorText} from '../i18n.js';
import {EventEmitter} from 'node:events';
import {randomUUID} from 'node:crypto';
import {record, tuple, SubmissionQueue, type Json, type RuntimeClient} from '@reuleauxcoder/client';
import {CoreFailure, CoreRuntime, type RuntimeOptions} from './runtime.js';
import {Transcript} from './transcript.js';
import {foldToolCells} from './tool-groups.js';
import {Uploads} from './uploads.js';
import {ConversationCommands} from './commands.js';
import {inlineInteractions} from './interactions.js';
import {WorkOverviewStore} from './overview.js';
import {ConfigurationEditor} from './configuration-editor.js';
import type {DraftItem, HostSnapshot, ReviewSummary} from '../shared.js';

export class WorkspaceSession extends EventEmitter {
  readonly runtime = new CoreRuntime();
  readonly transcript = new Transcript();
  readonly commands = new ConversationCommands(() => this.changed(), error => this.report(error));
  readonly overview = new WorkOverviewStore(() => this.changed());
  readonly configuration = new ConfigurationEditor(() => this.changed());
  phase: HostSnapshot['phase'] = 'idle';
  error?: HostSnapshot['error'];
  notice = '';
  draftText = '';
  draftItems: DraftItem[] = [];
  submissions?: SubmissionQueue;
  uploads?: Uploads;
  private generation = 0;
  private hostId: string = randomUUID();
  private revision = 0;
  private draftRevision = 0;
  private clientListeners: (() => void)[] = [];
  private lifecycle = 0;
  constructor(readonly workspace: string, readonly environment: string) {
    super();
    this.runtime.on('client', client => this.bind(client));
    this.runtime.on('log', text => this.emit('log', text));
    this.runtime.on('exit', error => {if (this.phase === 'ready') this.fail(error);});
    this.transcript.on('change', () => this.changed());
    this.transcript.on('applied', id => this.submissions?.applied(id));
  }
  get client(): RuntimeClient | undefined {return this.runtime.client;}
  requireClient(): RuntimeClient {
    if (this.phase !== 'ready' || !this.client || this.client.peer.closed) throw new Error(t('Start the workspace core first.'));
    return this.client;
  }
  private bind(client: RuntimeClient): void {
    this.hostId = randomUUID();
    for (const off of this.clientListeners.splice(0)) off();
    this.transcript.bind(client);
    this.commands.bind(client);
    this.overview.bind(client);
    this.submissions = new SubmissionQueue(client, this.transcript, text => {this.notice = text; this.changed();}, {
      empty: t('No failed submission to retry.'), stopped: t('The core is stopping. Your message is retained; use Retry to send it again.'),
      uncertain: detail => t('{0}. Your message is retained; Retry uses the same submission ID.', errorText(detail)),
    });
    this.uploads = new Uploads(client);
    this.draftItems = []; this.generation = 0;
    const listen = (name: string, handler: (...args: any[]) => void) => {client.on(name, handler); this.clientListeners.push(() => client.off(name, handler));};
    listen('state', state => {
      if (state.session_generation !== this.generation) {
        this.generation = state.session_generation; this.submissions?.reset(); this.draftItems = [];
        void this.uploads?.cancel().catch(error => this.report(error));
      }
      this.changed();
    });
    listen('failure', error => this.fail(error));
    listen('interactions', () => this.changed());
    listen('shutdownProgress', text => {this.notice = text; this.changed();});
    listen('completed', result => {if (result.control === 'exit') void this.shutdown().catch(error => this.fail(error));});
    this.emit('client', client);
  }
  async start(options: RuntimeOptions): Promise<void> {
    if (this.phase === 'starting' || this.phase === 'ready') return;
    if (this.configuration.state?.busy) throw new Error(t('Wait for configuration check to finish.'));
    const lifecycle = ++this.lifecycle;
    this.phase = 'starting'; this.error = undefined; this.changed();
    try {
      await this.configuration.close();
      if (lifecycle !== this.lifecycle) throw new Error(t('Core startup was cancelled.'));
      await this.runtime.start(options);
      if (lifecycle !== this.lifecycle) throw new Error(t('Core startup was cancelled.'));
      this.phase = 'ready'; this.changed();
    }
    catch (error) {if (lifecycle === this.lifecycle) this.fail(error); throw error;}
  }
  async shutdown(): Promise<void> {
    this.lifecycle++;
    this.phase = 'stopping'; this.changed();
    try {await this.uploads?.cancel(); await this.runtime.shutdown(); this.phase = 'idle'; this.changed();}
    catch (error) {this.fail(error); throw error;}
  }
  submit(id: string, text: string, itemIds: string[], generation: number): void {
    const client = this.requireClient();
    if (generation !== client.state.session_generation) throw new Error(t('Session changed. Review your draft and send it again.'));
    if (typeof id !== 'string' || !/^[\w-]{1,128}$/.test(id) || typeof text !== 'string' || text.length > 1024 * 1024 || !Array.isArray(itemIds) || itemIds.length > 30) throw new Error(t('Invalid chat submission.'));
    if (this.submissions!.has(id)) return;
    const items = itemIds.map(id => {const item = this.draftItems.find(item => item.id === id); if (!item) throw new Error(t('An attachment has expired. Attach it again.')); return item;});
    const images = items.filter(item => item.kind === 'image');
    if (images.length && !client.state.support_modal?.includes('image')) throw new Error(t('The selected model does not support images.'));
    const context = items.filter(item => item.kind !== 'image').map(item => item.kind === 'file' ? `Attached file: ${item.name}\nWorkspace path: ${item.reference.path}` : item.text).join('\n\n');
    const input = [text.trim(), context].filter(Boolean).join('\n\n');
    if (!input && !images.length) return;
    const value: Json = images.length ? record('ChatInput', {
      text: input, images: tuple(images.map(item => record('ImageReference', item.reference))),
      session_id: client.state.session_id, session_generation: generation,
    }) : input;
    const display = [text, ...items.filter(item => item.kind !== 'image').map(item => `[${t(item.kind)}: ${item.name}]`)].filter(Boolean).join('\n');
    const sending = this.submissions!.send(display, value, id);
    this.transcript.images(id, images.map(item => item.reference));
    this.draftItems = this.draftItems.filter(item => !itemIds.includes(item.id));
    // Only clear the sent text; a later view message may already own a new draft.
    if (this.draftText === text) this.draftText = '';
    this.changed();
    void sending.catch(error => this.report(error));
  }
  snapshot(reviews: ReviewSummary[] = []): HostSnapshot {
    const state = this.client?.state;
    return {hostId: this.hostId, revision: this.revision, draftRevision: this.draftRevision, phase: this.phase, environment: this.environment, workspace: this.workspace, generation: state?.session_generation ?? 0, model: state?.model ?? '', running: state?.running ?? false, cells: foldToolCells(this.transcript.cells), reviews, draftItems: this.draftItems, draftText: this.draftText, error: this.error, notice: this.notice, catalog: this.client?.catalog ?? [], commandSurface: this.commands.surface, interactions: inlineInteractions(this.client), mode: state?.mode ?? undefined, overview: this.overview.snapshot(), configuration: this.configuration.state};
  }
  async imagePreview(attachmentId: unknown, variantId: unknown): Promise<string> {
    const client = this.requireClient();
    const reference = [...this.draftItems.filter(item => item.kind === 'image').map(item => item.reference), ...this.transcript.cells.flatMap(cell => cell.images ?? [])]
      .find(image => image.attachment_id === attachmentId && image.variant_id === variantId);
    if (!reference) throw new Error(t('This image is no longer available.'));
    const generation = client.state.session_generation;
    const result = await client.peer.request('images.preview', {session_id: client.state.session_id, session_generation: generation, image: record('ImageReference', reference)});
    if (this.client !== client || client.state.session_generation !== generation) throw new Error(t('Session changed. Review your draft and send it again.'));
    if (typeof result !== 'string' || result.length > 3 * 1024 * 1024 || !/^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(result)) throw new Error(t('Image preview is unavailable.'));
    return result;
  }
  add(item: DraftItem): void {if (this.draftItems.length >= 30) throw new Error(t('The draft already has 30 attachments or context items.')); this.draftItems.push(item); this.changed();}
  remove(id: string): void {this.draftItems = this.draftItems.filter(item => item.id !== id); this.changed();}
  insertDraft(text: string): void {this.draftText = [this.draftText, text].filter(Boolean).join('\n'); this.draftRevision++; this.changed();}
  changed(): void {this.revision++; this.emit('change');}
  report(error: unknown): void {this.notice = error instanceof Error ? error.message : String(error); this.transcript.notice(this.notice);}
  fail(error: unknown): void {this.error = {kind: error instanceof CoreFailure ? error.kind : 'startup', message: error instanceof Error ? error.message : String(error)}; this.phase = 'failed'; this.changed();}
  dispose(): void {this.lifecycle++; void this.uploads?.cancel().catch(() => {}); void this.configuration.close().catch(() => {}); for (const off of this.clientListeners.splice(0)) off(); this.commands.dispose(); this.overview.dispose(); this.transcript.dispose(); this.runtime.dispose(); this.removeAllListeners();}
}
