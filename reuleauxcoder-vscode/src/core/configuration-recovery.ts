import {RpcError, type ConfigChange, type ConfigInspection} from '@reuleauxcoder/client';
import {ConfigurationProcess} from './configuration-process.js';
import type {RuntimeOptions} from './runtime.js';
import type {RecoverySnapshot} from '../shared.js';
import {t, errorText, type MessageKey} from '../i18n.js';

const errors: Record<string, MessageKey> = {
  conflict: 'Configuration changed on disk. Check again and review a new preview.',
  expired: 'This preview expired. Select the saved change again.',
  unsaved_document: 'Save or discard unsaved changes in the configuration editor before restoring.',
  validation_required: 'Run configuration checks and verify affected models before restoring.',
  invalid_backup: 'This saved configuration cannot be recovered. Open the file to repair it.',
};
function recoveryError(error: unknown): Error {
  const code = error instanceof RpcError && error.data && typeof error.data === 'object' && 'code' in error.data ? String(error.data.code) : '';
  return new Error(errors[code] ? t(errors[code]) : errorText(error));
}

/** Host-owned recovery state. Views choose records; they cannot supply paths or documents. */
export class ConfigurationRecovery {
  readonly process = new ConfigurationProcess();
  state?: RecoverySnapshot;
  private inspection?: ConfigInspection;
  private records: ConfigChange[] = [];
  private editorRevision = 0;
  private epoch = 0;
  constructor(private changed: () => void) {}

  async open(options: RuntimeOptions & {env?: NodeJS.ProcessEnv}): Promise<void> {
    if (this.state?.busy) return;
    this.state = {busy: true, diagnostics: [], sources: [], history: []}; this.changed();
    await this.operation(async () => {await this.process.start(options); await this.refresh();}, true);
  }
  private client() {
    const client = this.process.client;
    if (!client || client.peer.closed) throw new Error(t('Open configuration recovery first.'));
    return client;
  }
  private async operation(run: () => Promise<void>, opening = false): Promise<void> {
    if (!this.state || this.state.busy && !opening) throw new Error(t('A configuration operation is already running.'));
    const state = this.state, epoch = this.epoch;
    state.busy = true; state.error = undefined; state.message = undefined; this.changed();
    try {await run();}
    catch (error) {
      const translated = recoveryError(error);
      if (epoch === this.epoch) {
        state.error = translated.message;
        if (error instanceof RpcError && (error.data as {code?: string} | undefined)?.code === 'validation_required' && state.validation) {
          state.validation.checks = state.validation.checks.filter(check => check.check !== 'model');
        }
      }
      throw translated;
    }
    finally {if (epoch === this.epoch) {state.busy = false; this.changed();}}
  }
  private async refresh(): Promise<void> {
    const client = this.client();
    const [inspection, history] = await Promise.all([client.inspect(), client.history()]);
    this.inspection = inspection; this.records = history.changes.filter(item => ['applied', 'committing'].includes(item.status));
    if (!this.state) return;
    this.state.valid = inspection.valid;
    this.state.diagnostics = inspection.diagnostics;
    this.state.sources = inspection.sources.map(({scope, path, exists}) => ({scope, path, exists}));
    this.state.history = this.records.map(({id, applied_at, created_at, target_path, scope}) => ({id, date: applied_at ?? created_at, path: target_path, scope}));
  }
  async check(): Promise<void> {
    await this.operation(async () => {
      this.state!.candidate = undefined; this.state!.validation = undefined;
      await this.refresh();
      this.state!.validation = await this.client().validate({checks: ['static', 'startup']});
    });
  }
  async select(id: string): Promise<void> {
    await this.operation(async () => {
      if (!this.records.some(item => item.id === id) || !this.inspection) throw new Error(t('Configuration history changed. Check it again.'));
      this.state!.candidate = undefined; this.state!.validation = undefined;
      const candidate = await this.client().recover(id, this.inspection.revision, 'before');
      this.state!.candidate = candidate;
      this.state!.validation = await this.client().validate({change_id: candidate.id, checks: ['static', 'startup']});
    });
  }
  private candidate(id: string): ConfigChange {
    const candidate = this.state?.candidate;
    if (!candidate || candidate.id !== id) throw new Error(t('The recovery preview changed. Review it again.'));
    return candidate;
  }
  async testModels(id: string): Promise<void> {
    await this.operation(async () => {
      const candidate = this.candidate(id);
      const previous = this.state!.validation?.checks ?? [];
      // Bound each click to eight probes; repeat to verify remaining profiles.
      const names = (candidate.model_targets ?? []).filter(target => !previous.some(check => check.check === 'model' && check.profile === target.profile && check.fingerprint === target.fingerprint && check.status === 'passed' && (check.checked_at ?? 0) > Date.now() / 1000 - 300)).slice(0, 8).map(target => target.profile);
      if (!names.length) return;
      const result = await this.client().validate({change_id: id, checks: ['model'], profiles: names});
      const checks = previous.filter(check => check.check !== 'model' || !names.includes(check.profile ?? ''));
      this.state!.validation = {...result, checks: [...checks, ...result.checks.filter(check => check.check === 'model')]};
    });
  }
  async syncEditors(paths: string[]): Promise<void> {
    if (this.process.client && !this.process.client.peer.closed) {
      await this.process.client.peer.request('config.editor_documents', {revision: ++this.editorRevision, paths});
    }
  }
  async apply(id: string, offline: boolean, dirtyPaths: string[]): Promise<void> {
    await this.operation(async () => {
      this.candidate(id);
      await this.syncEditors(dirtyPaths);
      await this.client().apply(id, {allow_unverified_model: offline});
      this.state!.candidate = undefined; this.state!.validation = undefined;
      await this.refresh();
      this.state!.message = t('Configuration restored. Start the core when you are ready.');
    });
  }
  source(scope: string): string {
    const source = this.inspection?.sources.find(item => item.scope === scope);
    if (!source) throw new Error(t('Check the configuration sources first.'));
    return source.path;
  }
  async close(): Promise<void> {
    this.epoch++; this.state = undefined; this.inspection = undefined; this.records = [];
    this.changed(); await this.process.close();
  }
}
