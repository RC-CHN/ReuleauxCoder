import {RpcError, type ConfigDocument, type ConfigInspection, type ConfigValidation} from '@reuleauxcoder/client';
import {ConfigurationProcess} from './configuration-process.js';
import type {RuntimeOptions} from './runtime.js';
import type {ConfigurationSnapshot} from '../shared.js';
import {t, errorText} from '../i18n.js';

/** Read-only diagnostics. Native TextDocuments own editing, saving and undo. */
export class ConfigurationEditor {
  readonly process = new ConfigurationProcess();
  state?: ConfigurationSnapshot;
  private inspection?: ConfigInspection;
  private documents: ConfigDocument[] = [];
  private revision = 0;
  private epoch = 0;
  constructor(private changed: () => void) {}

  async open(options: RuntimeOptions & {env?: NodeJS.ProcessEnv}): Promise<void> {
    if (this.state?.busy) throw new Error(t('A configuration operation is already running.'));
    this.state = {busy: false, diagnostics: [], sources: [], dirty: false, modelTargets: []};
    await this.operation(async () => {await this.process.start(options); await this.refresh();});
  }
  setDocuments(documents: ConfigDocument[], invalidate = false): void {
    if (!this.state) return;
    if (!invalidate && JSON.stringify(documents) === JSON.stringify(this.documents)) return;
    this.documents = documents; this.revision++;
    this.state.dirty = documents.length > 0;
    this.state.validation = undefined; this.state.valid = undefined; this.state.error = undefined;
    this.state.diagnostics = []; this.state.modelTargets = [];
    for (const source of this.state.sources) source.dirty = documents.some(doc => doc.scope === source.scope);
    this.changed();
  }
  private client() {
    const client = this.process.client;
    if (!client || client.peer.closed) throw new Error(t('Open configuration files first.'));
    return client;
  }
  private async operation(run: () => Promise<void>): Promise<void> {
    if (!this.state || this.state.busy) throw new Error(t('A configuration operation is already running.'));
    const state = this.state, epoch = this.epoch;
    state.busy = true; state.error = undefined; this.changed();
    try {await run();}
    catch (error) {
      const conflict = error instanceof RpcError && (error.data as {code?: string} | undefined)?.code === 'conflict';
      const message = conflict ? t('Configuration changed on disk. Check again.') : errorText(error);
      if (epoch === this.epoch) state.error = message;
      throw new Error(message);
    }
    finally {if (epoch === this.epoch) {state.busy = false; this.changed();}}
  }
  private async refresh(): Promise<void> {
    const inspection = await this.client().inspect();
    this.inspection = inspection;
    if (!this.state) return;
    this.state.diagnostics = inspection.diagnostics;
    this.state.valid = inspection.valid;
    this.state.modelTargets = inspection.model_targets;
    this.state.sources = inspection.sources.map(({scope, path, exists}) => ({scope, path, exists, dirty: this.documents.some(doc => doc.scope === scope)}));
  }
  async check(profile?: string): Promise<ConfigValidation | undefined> {
    let result: ConfigValidation | undefined;
    await this.operation(async () => {
      this.state!.validation = undefined;
      const revision = this.revision, epoch = this.epoch;
      await this.refresh();
      const value = await this.client().check({
        checks: profile === undefined ? ['static', 'startup'] : ['static', 'startup', 'model'],
        ...(profile === undefined ? {} : {profiles: [profile]}),
        ...(this.documents.length ? {documents: this.documents} : {}),
        base_revision: this.inspection!.revision,
      });
      if (revision !== this.revision || epoch !== this.epoch) throw new Error(t('Configuration changed during the check. Check again.'));
      if (!this.state) return;
      result = value;
      this.state.validation = value; this.state.valid = value.valid;
      this.state.diagnostics = value.diagnostics; this.state.modelTargets = value.model_targets;
    });
    return result;
  }
  source(scope: string): string {
    const source = this.inspection?.sources.find(item => item.scope === scope);
    if (!source) throw new Error(t('Check the configuration sources first.'));
    return source.path;
  }
  async close(): Promise<void> {
    this.epoch++; this.state = undefined; this.inspection = undefined; this.documents = []; this.revision++;
    this.changed(); await this.process.close();
  }
}
