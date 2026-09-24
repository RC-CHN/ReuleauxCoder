import {t} from '../i18n.js';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {EventEmitter} from 'node:events';
import {RuntimeClient, type UIProfile} from '@reuleauxcoder/client';
import {RpcPeer} from '@reuleauxcoder/client/node';
import {compatibilityProblem} from './compatibility.js';

export interface CoreCommand {command: string; args: string[]}
export interface RuntimeOptions {cwd: string; commands: CoreCommand[]; startupTimeout?: number; beforeReady?: (client: RuntimeClient) => Promise<unknown>}
export type FailureKind = 'missing' | 'incompatible' | 'startup' | 'disconnected';
export class CoreFailure extends Error {
  constructor(readonly kind: FailureKind, message: string, readonly details = '') {super(message);}
}
export const profile: UIProfile = {
  ui_id: 'vscode', display_name: 'ReuleauxCoder VS Code',
  capabilities: ['text_input', 'stream_output', 'palette', 'buttons', 'menus', 'modal', 'diff_review', 'text_select', 'text_edit', 'secure_text_input'],
};

/** One workspace host owns the connection. Webview disposal never reaches this class. */
export class CoreRuntime extends EventEmitter {
  client?: RuntimeClient;
  private child?: ChildProcessWithoutNullStreams;
  private starting?: Promise<RuntimeClient>;
  private epoch = 0;
  private closing = false;
  private stderr = '';
  private exit?: Promise<void>;

  start(options: RuntimeOptions): Promise<RuntimeClient> {
    if (this.starting) return this.starting;
    if (this.client && !this.client.peer.closed) return Promise.resolve(this.client);
    this.closing = false;
    const epoch = ++this.epoch;
    this.starting = this.connect(options, epoch).finally(() => {this.starting = undefined;});
    return this.starting;
  }

  private async connect(options: RuntimeOptions, epoch: number): Promise<RuntimeClient> {
    for (const candidate of options.commands) {
      this.stderr = '';
      this.emit('log', `Starting ${candidate.command} in ${options.cwd}\n`);
      const child = spawn(candidate.command, [...candidate.args, '--rpc-stdio'], {cwd: options.cwd, stdio: 'pipe', windowsHide: true, shell: false});
      this.child = child;
      this.exit = new Promise(resolve => child.once('close', () => resolve()));
      const peer = new RpcPeer(child.stdout, child.stdin);
      const client = new RuntimeClient(peer);
      this.client = client;
      let spawnError: NodeJS.ErrnoException | undefined;
      child.once('error', error => {spawnError = error; peer.close(error);});
      child.stderr.on('data', chunk => {const text = chunk.toString(); this.stderr = (this.stderr + text).slice(-65536); this.emit('log', text);});
      child.once('exit', (code, signal) => {
        const error = new CoreFailure('disconnected', t('Workspace core exited ({0}).', signal ?? code ?? 'unknown'), this.stderr);
        peer.close(error);
        if (!this.closing && epoch === this.epoch) this.emit('exit', error);
      });
      this.emit('client', client);
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([
          client.initialize(profile, {ready: false}),
          new Promise<never>((_, reject) => {timer = setTimeout(() => reject(new CoreFailure('startup', t('Core initialization timed out.'), this.stderr)), options.startupTimeout ?? 30000);}),
        ]);
        if (epoch !== this.epoch) throw new Error(t('Core startup was cancelled.'));
        const problem = compatibilityProblem(client.info ?? {});
        if (problem) throw new CoreFailure('incompatible', problem);
        if (!client.info?.submission_ids || !client.info?.review_documents || !client.info?.attachment_uploads || !client.info?.editor_documents) throw new CoreFailure('incompatible', t('This core lacks the submission, native diff or attachment protocol required by the extension. Install the bundled compatible core.'));
        this.emit('log', `Core ${client.info.core_version}; editor integration ${client.info.editor_api_version}\n`);
        await options.beforeReady?.(client);
        await client.ready();
        if (epoch !== this.epoch) throw new Error(t('Core startup was cancelled.'));
        this.emit('ready', client);
        return client;
      } catch (error) {
        client.close(); child.kill();
        if (this.client === client) this.client = undefined;
        if (epoch !== this.epoch) throw error;
        if (spawnError?.code === 'ENOENT') continue;
        if (error instanceof CoreFailure) throw error;
        throw new CoreFailure('startup', error instanceof Error ? error.message : String(error), this.stderr);
      } finally {clearTimeout(timer);}
    }
    throw new CoreFailure('missing', t('No rcoder core was found on the workspace host. Install it here or select an existing executable.'));
  }

  async shutdown(): Promise<void> {
    this.closing = true;
    if (this.starting) {
      this.epoch++;
      this.client?.close(); this.child?.kill();
      await this.starting.catch(() => {});
    }
    const client = this.client;
    if (client && !client.peer.closed) await client.shutdown();
    this.client = undefined;
    this.epoch++;
    const child = this.child;
    if (child) {
      const timer = setTimeout(() => child.kill(), 2000);
      await this.exit;
      clearTimeout(timer);
    }
    this.child = undefined;
  }

  /** Extension host teardown can be abrupt; EOF triggers the core's own save/cleanup. */
  dispose(): void {
    this.closing = true; this.epoch++;
    this.client?.close(); this.client = undefined;
    this.child?.stdin.end();
    this.removeAllListeners();
  }
}
