import {t, errorText, type MessageKey} from '../i18n.js';
import type {RecoverySnapshot} from '../shared.js';
import {icon} from './icons.js';

const diagnosticLabels: Record<string, MessageKey> = {
  invalid_yaml: 'The YAML is invalid or contains duplicate keys.',
  invalid_document: 'Use a UTF-8 YAML configuration object.',
  missing_profile: 'The selected model profile does not exist.',
  unknown_field: 'This configuration field is not supported.',
  unknown_name: 'This configuration name is not supported.',
  invalid_type: 'This field has the wrong value type.',
  invalid_range: 'This value is outside the supported range.',
  invalid_value: 'This value is not one of the supported choices.',
  invalid_config: 'Required configuration is missing or invalid.',
};
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, cls?: string) => {
  const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node;
};

/** Recovery is kept in the conversation; file editing uses VS Code's native editor. */
export class ConfigurationView {
  private signature = '';
  private candidateId?: string;
  private offline = false;
  constructor(private root: HTMLElement, private request: (action: string, data?: Record<string, unknown>) => Promise<unknown>, private fail: (error: unknown) => void) {}
  update(state?: RecoverySnapshot): void {
    this.root.hidden = !state;
    if (!state) {this.signature = ''; this.candidateId = undefined; this.offline = false; return;}
    const signature = JSON.stringify(state); if (signature === this.signature) return; this.signature = signature;
    if (state.candidate?.id !== this.candidateId) {this.candidateId = state.candidate?.id; this.offline = false;}
    this.root.replaceChildren(); this.root.setAttribute('aria-busy', String(state.busy));
    const button = (label: string, action: string, data = {}, primary = false) => {
      const node = el('button', label, primary ? 'primary' : ''); node.disabled = state.busy;
      node.addEventListener('click', () => void this.request(action, data).catch(this.fail)); return node;
    };
    const heading = el('div', undefined, 'recovery-heading');
    heading.append(icon('shield'), el('h3', t('Configuration recovery')),
      button(t('Close'), 'configuration.close'));
    this.root.append(heading, el('p', t('Fix the settings that prevented startup, or restore an earlier version.'), 'recovery-hint'));
    if (state.busy) this.root.append(el('p', t('Working on configuration…'), 'recovery-progress'));
    if (state.error) this.root.append(el('p', state.error, 'recovery-error'));
    if (state.message) this.root.append(el('p', state.message, 'recovery-success'));
    const parseErrors = state.diagnostics.filter(issue => ['invalid_yaml', 'invalid_document'].includes(issue.code));
    const secondary = el('details', undefined, 'recovery-secondary'); secondary.append(el('summary', t('Additional diagnostics')));
    for (const issue of state.diagnostics) {
      const row = el('div', undefined, 'recovery-diagnostic');
      const source = state.sources.find(source => source.scope === issue.source)?.path ?? issue.source;
      row.append(el('strong', diagnosticLabels[issue.code] ? t(diagnosticLabels[issue.code]) : errorText(issue.message)),
        el('code', [source, issue.path, issue.line ? t('Line {0}', issue.line) : ''].filter(Boolean).join(' · ')));
      const details = el('details'); details.append(el('summary', t('Diagnostic details')), el('pre', issue.message)); row.append(details);
      (parseErrors.length && !parseErrors.includes(issue) ? secondary : this.root).append(row);
    }
    if (secondary.childElementCount > 1) this.root.append(secondary);
    const sourceRow = (source: RecoverySnapshot['sources'][number]) => {
      const row = el('div', undefined, 'recovery-source');
      const title = el('div'); title.append(el('strong', t(source.scope === 'user' ? 'User configuration' : source.scope === 'explicit' ? 'Explicit configuration' : 'Workspace configuration')), el('code', source.path));
      row.append(title, button(t('Open in editor'), 'configuration.file', {scope: source.scope})); return row;
    };
    const relevant = state.sources.filter(source => state.diagnostics.some(issue => issue.source === source.path || issue.source === source.scope));
    if (!relevant.length) {
      const primary = [...state.sources].reverse().find(source => source.exists) ?? state.sources.find(source => source.scope === 'workspace');
      if (primary) relevant.push(primary);
    }
    relevant.forEach(source => this.root.append(sourceRow(source)));
    const others = state.sources.filter(source => !relevant.includes(source));
    if (others.length) {const details = el('details'); details.append(el('summary', t('Other configuration files'))); others.forEach(source => details.append(sourceRow(source))); this.root.append(details);}
    this.root.append(button(t('Check again'), 'configuration.check'));
    const candidate = state.candidate;
    if (candidate) {
      const preview = el('section', undefined, 'recovery-preview');
      preview.append(el('h4', t('Review the recovery change')), el('code', candidate.target_path), el('p', t('Only this configuration file will be restored. Changes take effect on the next core start.')));
      for (const change of candidate.diff) {
        const row = el('details'); row.open = candidate.diff.length <= 4;
        row.append(el('summary', change.path || '/'));
        const value = (item: unknown) => typeof item === 'string' ? item : JSON.stringify(item, null, 2);
        row.append(el('div', t('Current value'), 'recovery-label'), el('pre', value(change.before), 'recovery-before'),
          el('div', change.removed ? t('Remove this configuration override') : t('Restored value'), 'recovery-label'), el('pre', change.removed ? '—' : value(change.after), 'recovery-after'));
        preview.append(row);
      }
      for (const issue of candidate.diagnostics) preview.append(el('p', diagnosticLabels[issue.code] ? t(diagnosticLabels[issue.code]) : errorText(issue.message), 'recovery-error'));
      this.root.append(preview);
    }
    const validation = state.validation;
    if (validation) {
      const checks = el('ul', undefined, 'recovery-checks');
      for (const check of validation.checks) {
        const name = t(check.check === 'static' ? 'Configuration format' : check.check === 'startup' ? 'Startup check' : 'Model connection');
        const result = t(check.status === 'passed' ? 'Passed' : check.status === 'failed' ? 'Check failed' : 'Not confirmed');
        checks.append(el('li', `${name}${check.profile ? ` · ${check.profile}` : ''} — ${result}`, `check-${check.status}`));
      }
      this.root.append(checks);
    }
    if (!candidate && state.valid && validation?.checks.some(check => check.check === 'startup' && check.status === 'passed')) {
      this.root.append(el('p', t('Configuration checks passed. Start the core to continue.'), 'recovery-success'), button(t('Start core'), 'start', {}, true));
    } else if (state.message && !candidate) {
      this.root.append(button(t('Start core'), 'start', {}, true));
    }
    if (candidate) {
      const needsModels = candidate.requires_model_probe;
      const verified = !needsModels || (candidate.model_targets?.length && candidate.model_targets.every(target => validation?.checks.some(check => check.check === 'model' && check.profile === target.profile && check.fingerprint === target.fingerprint && check.status === 'passed')));
      const startup = ['static', 'startup'].every(name => validation?.checks.some(check => check.check === name && check.status === 'passed'));
      if (needsModels) {
        this.root.append(el('p', t('Model tests send a short request with the configured output limit and may use tokens. Tools and images are not tested.'), 'recovery-hint'),
          el('code', candidate.model_targets?.map(item => `${item.profile} · ${item.model} · ${item.base_url ?? item.provider} · ${t('Output limit {0}', item.max_tokens)}`).join('\n')),
          button(t('Test next affected models (up to 8)'), 'configuration.test', {id: candidate.id}));
      }
      const apply = el('button', t('Restore this configuration'), 'primary');
      apply.addEventListener('click', () => void this.request('configuration.apply', {id: candidate.id, offline: this.offline}).catch(this.fail));
      const enable = () => {apply.disabled = state.busy || !startup || !(verified || this.offline);}; enable();
      // Keep the override deliberate and scoped to this exact preview.
      if (needsModels && !verified) {
        const label = el('label', undefined, 'recovery-offline'); const checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.checked = this.offline; checkbox.disabled = state.busy;
        label.append(checkbox, document.createTextNode(t('Restore without online model verification')));
        checkbox.addEventListener('change', () => {this.offline = checkbox.checked; enable();});
        this.root.append(label);
      }
      this.root.append(apply);
    }
    const history = el('details', undefined, 'recovery-history'); history.open = !candidate;
    history.append(el('summary', t('Saved configuration changes')));
    if (!state.history.length) history.append(el('p', t('No saved changes yet. Open a configuration file to repair it.')));
    for (const item of state.history) {
      const row = el('div', undefined, 'recovery-history-row');
      row.append(el('time', new Date(item.date).toLocaleString(document.documentElement.lang)), el('code', item.path),
        button(t('Preview the version before this change'), 'configuration.select', {id: item.id})); history.append(row);
    }
    this.root.append(history);
  }
}
