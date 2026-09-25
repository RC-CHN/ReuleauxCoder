/** Shared host-neutral configuration API, including the standalone recovery process. */
import type {MessagePeer} from './message-peer.js';
import type {Json} from './wire.js';

export type ConfigScope = 'user' | 'workspace' | 'explicit';
export type ConfigCheck = 'static' | 'startup' | 'model';
export interface ConfigDiagnostic {
  code: string; path: string; message: string; severity: 'error' | 'warning' | 'info';
  source: string | null; line: number | null;
}
export interface ConfigCheckResult {
  check: ConfigCheck; status: 'passed' | 'failed' | 'unknown'; code: string; checked_at?: number;
  profile?: string; fingerprint?: string; coverage?: string; not_checked?: string[];
}
export interface ConfigModelTarget {
  profile: string; roles: string[]; fingerprint: string; model: string; provider: string;
  request_mode: string; base_url: string | null; max_tokens: number;
}
export interface ConfigDescription {
  api_version: 1; core_version: string; schema: {[key: string]: Json}; scopes: ConfigScope[];
  activation: 'next_start'; candidate_ttl_seconds: number; operations: string[];
  checks: Record<ConfigCheck, string>; model_restricted_sections: string[]; sensitive_values: string;
  capabilities?: string[];
}
export interface ConfigInspection {
  api_version: 1; revision: string; valid: boolean; diagnostics: ConfigDiagnostic[];
  sources: {scope: ConfigScope; path: string; exists: boolean; values: {[key: string]: Json}}[];
  next_start: {[key: string]: Json} | null; runtime: {[key: string]: Json} | null;
  model_targets?: ConfigModelTarget[];
}
export type ConfigFieldChange = {path: string; value: Json; remove?: never} | {path: string; remove: true; value?: never};
export type ConfigPreparation = {scope?: ConfigScope; base_revision?: string} & (
  {changes: ConfigFieldChange[]; document?: never} | {document: {[key: string]: Json}; changes?: never}
);
export interface ConfigChange {
  id: string; scope: ConfigScope; target_path: string; base_revision: string; created_at: string; expires_at: number;
  status: 'prepared' | 'committing' | 'applied'; activation: 'next_start';
  diff: {path: string; before: Json; after: Json; removed: boolean}[];
  diagnostics: ConfigDiagnostic[]; checks: ConfigCheckResult[]; requires_model_probe: boolean;
  model_preview: {model: string; provider: string; request_mode: string | null; base_url: string | null} | null;
  revision?: string; applied_at?: string; model_verified?: boolean | null;
  model_targets?: ConfigModelTarget[]; available_model_targets?: ConfigModelTarget[];
}
export interface ConfigValidation {
  change_id: string | null; valid: boolean; checks: ConfigCheckResult[]; diagnostics: ConfigDiagnostic[];
}

export class ConfigurationClient {
  constructor(readonly peer: MessagePeer) {}
  private async request<T>(operation: string, parameters: object = {}, timeout = 30_000): Promise<T> {
    return await this.peer.request(`config.${operation}`, parameters as {[key: string]: Json}, timeout) as T;
  }
  /** Only for a dedicated `rcoder config rpc` process. RuntimeClient owns normal initialization. */
  async initialize(): Promise<ConfigDescription & {mode: 'configuration'; editor_documents?: boolean}> {
    const info = await this.peer.request('initialize', {version: 1}) as unknown as ConfigDescription & {mode: 'configuration'; editor_documents?: boolean};
    if (info.api_version !== 1 || info.mode !== 'configuration') throw new Error('Unsupported configuration management protocol');
    return info;
  }
  describe(section?: string) {return this.request<ConfigDescription>('describe', section === undefined ? {} : {section});}
  inspect() {return this.request<ConfigInspection>('inspect');}
  prepare(parameters: ConfigPreparation) {return this.request<ConfigChange>('prepare', parameters);}
  validate(parameters: {change_id?: string; checks?: ConfigCheck[]; profiles?: string[]} = {}) {return this.request<ConfigValidation>('validate', parameters, 210_000);}
  apply(changeId: string, options: {allow_unverified_model?: boolean} = {}) {
    return this.request<ConfigChange>('apply', {change_id: changeId, ...options}, 45_000);
  }
  history(limit = 20) {return this.request<{changes: ConfigChange[]}>('history', {limit});}
  revert(changeId: string) {return this.request<ConfigChange>('revert', {change_id: changeId});}
  recover(changeId: string, baseRevision: string, side: 'before' | 'after' = 'before') {
    return this.request<ConfigChange>('recover', {change_id: changeId, base_revision: baseRevision, side});
  }
}
