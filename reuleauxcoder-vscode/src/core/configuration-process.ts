import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {ConfigurationClient} from '@reuleauxcoder/client';
import {RpcPeer} from '@reuleauxcoder/client/node';
import type {RuntimeOptions} from './runtime.js';
import {t} from '../i18n.js';

/** A separate management process remains usable when Agent initialization fails. */
export class ConfigurationProcess {
  client?: ConfigurationClient;
  private child?: ChildProcessWithoutNullStreams;
  private exited?: Promise<void>;
  private epoch = 0;
  private starting?: Promise<ConfigurationClient>;

  start(options: RuntimeOptions & {env?: NodeJS.ProcessEnv}): Promise<ConfigurationClient> {
    if (this.starting) return this.starting;
    if (this.client && !this.client.peer.closed) return Promise.resolve(this.client);
    const epoch = ++this.epoch;
    this.starting = this.connect(options, epoch).finally(() => {this.starting = undefined;});
    return this.starting;
  }
  private async connect(options: RuntimeOptions & {env?: NodeJS.ProcessEnv}, epoch: number): Promise<ConfigurationClient> {
    await this.stopChild();
    for (const command of options.commands) {
      if (epoch !== this.epoch) throw new Error(t('Configuration recovery was closed.'));
      const child = spawn(command.command, [...command.args, '--config-management-stdio'], {
        cwd: options.cwd, env: options.env, stdio: 'pipe', windowsHide: true, shell: false,
      });
      this.child = child;
      this.exited = new Promise(resolve => child.once('close', () => resolve()));
      const peer = new RpcPeer(child.stdout, child.stdin);
      const client = new ConfigurationClient(peer);
      this.client = client;
      let spawnError: NodeJS.ErrnoException | undefined;
      child.on('error', error => {spawnError = error; peer.close(error);});
      // Drain stderr but never forward raw configuration/provider diagnostics to the view.
      child.stderr.resume();
      child.once('exit', () => peer.close(new Error(t('Configuration recovery disconnected.'))));
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const info = await Promise.race([
          client.initialize(),
          new Promise<never>((_, reject) => {timer = setTimeout(() => reject(new Error(t('Configuration recovery timed out.'))), options.startupTimeout ?? 15_000);}),
        ]);
        if (epoch !== this.epoch) throw new Error(t('Configuration recovery was closed.'));
        if (!info.editor_documents || !['profile_probes', 'reviewer_protection', 'recovery'].every(name => info.capabilities?.includes(name))) {
          throw new Error(t('Update the core to use configuration recovery.'));
        }
        return client;
      } catch (error) {
        peer.close(); child.kill(); await this.exited;
        if (this.client === client) this.client = undefined;
        if (epoch !== this.epoch) throw error;
        if (spawnError?.code === 'ENOENT') continue;
        throw error;
      } finally {clearTimeout(timer);}
    }
    throw new Error(t('No core installation is available for configuration recovery.'));
  }

  async close(): Promise<void> {
    this.epoch++;
    await this.stopChild();
    await this.starting?.catch(() => {});
  }
  private async stopChild(): Promise<void> {
    this.client?.peer.close(); this.client = undefined;
    const child = this.child, exited = this.exited;
    this.child = undefined; this.exited = undefined;
    if (child && exited) {
      const timer = setTimeout(() => child.kill(), 2_000);
      try {await exited;} finally {clearTimeout(timer);}
    }
  }
}
