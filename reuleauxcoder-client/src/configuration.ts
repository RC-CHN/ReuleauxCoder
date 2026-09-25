/** Read-only configuration diagnostics; editors own their files and buffers. */
import type {MessagePeer} from './message-peer.js';
import type {Json} from './wire.js';

export type ConfigScope = 'user' | 'workspace' | 'explicit';
export type ConfigCheck = 'static' | 'startup' | 'model';
export interface ConfigDocument {scope: ConfigScope; content: string}
export interface ConfigDiagnostic {code: string; path: string; message: string; severity: 'error' | 'warning' | 'info'; source: string | null; line: number | null}
export interface ConfigCheckResult {check: ConfigCheck; status: 'passed' | 'failed' | 'unknown'; code: string; profile?: string; coverage?: string; not_checked?: string[]}
export interface ConfigModelTarget {profile: string; roles: string[]; model: string; provider: string; request_mode: string; base_url: string | null; max_tokens: number}
export interface ConfigDescription {
  api_version: 2; core_version: string; schema: {[key: string]: Json}; scopes: ConfigScope[];
  activation: 'next_start'; operations: string[]; checks: Record<ConfigCheck, string>; capabilities: string[];
}
export interface ConfigInspection {
  api_version: 2; revision: string; valid: boolean; diagnostics: ConfigDiagnostic[];
  sources: {scope: ConfigScope; path: string; exists: boolean; values: {[key: string]: Json}}[];
  next_start: {[key: string]: Json} | null; runtime: {[key: string]: Json} | null; model_targets: ConfigModelTarget[];
}
export interface ConfigValidation {
  revision: string; valid: boolean; buffer_check: boolean;
  checks: ConfigCheckResult[]; diagnostics: ConfigDiagnostic[]; model_targets: ConfigModelTarget[];
}
export interface ConfigCheckOptions {checks?: ConfigCheck[]; profiles?: string[]; documents?: ConfigDocument[]; base_revision?: string}

export class ConfigurationClient {
  constructor(readonly peer: MessagePeer) {}
  private async request<T>(operation: string, parameters: object = {}, timeout = 30_000): Promise<T> {
    return await this.peer.request('config.' + operation, parameters as {[key: string]: Json}, timeout) as T;
  }
  /** Only for a dedicated management process; RuntimeClient owns normal initialization. */
  async initialize(): Promise<ConfigDescription & {mode: 'configuration'}> {
    const info = await this.peer.request('initialize', {version: 1}) as unknown as ConfigDescription & {mode: 'configuration'};
    if (info.api_version !== 2 || info.mode !== 'configuration') throw new Error('Update the core to use configuration inspection.');
    return info;
  }
  describe(section?: string) {return this.request<ConfigDescription>('describe', section === undefined ? {} : {section});}
  inspect() {return this.request<ConfigInspection>('inspect');}
  check(parameters: ConfigCheckOptions = {}) {return this.request<ConfigValidation>('check', parameters, 210_000);}
}
