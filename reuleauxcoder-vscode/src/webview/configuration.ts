import {t, errorText, type MessageKey} from '../i18n.js';
import type {ConfigurationSnapshot} from '../shared.js';
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

export class ConfigurationView {
  private signature = '';
  private profile = '';
  constructor(private root: HTMLElement, private request: (action: string, data?: Record<string, unknown>) => Promise<unknown>, private fail: (error: unknown) => void) {}
  update(state?: ConfigurationSnapshot, running = false, connected = false): void {
    this.root.hidden = !state;
    if (!state) {this.signature = ''; return;}
    const signature = JSON.stringify([state, running, connected]); if (signature === this.signature) return; this.signature = signature;
    const focused = this.root.contains(document.activeElement) ? (document.activeElement as HTMLElement).dataset.control : undefined;
    const expanded = this.root.querySelector('details.model-diagnostics')?.hasAttribute('open') ?? false;
    const globalOpen = this.root.querySelector('details.global-configuration')?.hasAttribute('open') ?? false;
    this.root.replaceChildren(); this.root.setAttribute('aria-busy', String(state.busy));
    const button = (label: string, action: string, data: Record<string, unknown> = {}, primary = false) => {
      const node = el('button', label, primary ? 'primary' : ''); node.disabled = state.busy;
      node.dataset.control = action + String(data.scope ?? '');
      node.addEventListener('click', () => void this.request(action, data).catch(this.fail)); return node;
    };
    const heading = el('div', undefined, 'configuration-heading');
    heading.append(icon('settings'), el('h3', t('Configuration files')), button(t('Close'), 'configuration.close'));
    this.root.append(heading, el('p', t('Edit in the native editor. Check, save, then restart to use the changes.'), 'configuration-hint'));
    const status = el('div', undefined, 'configuration-status'); status.setAttribute('aria-live', 'polite');
    if (state.busy) status.append(el('p', t('Checking configuration…'), 'configuration-progress'));
    else if (state.error) status.append(el('p', state.error, 'configuration-error'));
    else status.append(el('p', t(state.dirty ? 'Unsaved editor changes. Checks include this content.' : 'Saved files are used on the next core start.'), 'configuration-hint'));
    this.root.append(status);
    this.root.append(el('p', t('Workspace settings override global defaults. Unset fields inherit the global value.'), 'configuration-hint'));
    if (state.sources.some(source => source.scope === 'explicit')) this.root.append(el('p', t('This launch also uses an explicit file. Its settings have the highest priority.'), 'configuration-hint'));
    const globals = el('details', undefined, 'global-configuration');
    globals.open = globalOpen || state.sources.some(source => source.scope === 'user' && source.dirty) || state.diagnostics.some(issue => state.sources.some(source => source.scope === 'user' && [source.scope, source.path].includes(issue.source ?? '')));
    globals.append(el('summary', t('Global defaults · all projects')));
    for (const source of [...state.sources].sort((a, b) => (a.scope === 'workspace' ? -1 : 1) - (b.scope === 'workspace' ? -1 : 1))) {
      const row = el('div', undefined, 'configuration-source');
      const title = el('div'); title.append(el('strong', t(source.scope === 'user' ? 'All projects on this host' : source.scope === 'explicit' ? 'This launch · highest priority' : 'Current workspace only')), el('code', source.path));
      if (source.dirty) title.append(el('small', t('Unsaved changes'), 'configuration-dirty'));
      row.append(title, button(t(source.exists ? 'Open in editor' : 'Create in editor'), 'configuration.file', {scope: source.scope}));
      (source.scope === 'user' ? globals : this.root).append(row);
    }
    if (globals.childElementCount > 1) this.root.append(globals);
    for (const issue of state.diagnostics) {
      const row = el('div', undefined, 'configuration-diagnostic');
      const source = state.sources.find(source => source.scope === issue.source || source.path === issue.source);
      row.append(el('strong', diagnosticLabels[issue.code] ? t(diagnosticLabels[issue.code]) : errorText(issue.message)),
        el('code', [source?.path ?? issue.source, issue.path, issue.line ? t('Line {0}', issue.line) : ''].filter(Boolean).join(' · ')));
      if (source) row.append(button(t('Open in editor'), 'configuration.file', {scope: source.scope}));
      const details = el('details'); details.append(el('summary', t('Diagnostic details')), el('pre', issue.message)); row.append(details);
      this.root.append(row);
    }
    const validation = state.validation;
    if (validation) {
      const checks = el('ul', undefined, 'configuration-checks');
      for (const check of validation.checks) {
        const name = t(check.check === 'static' ? 'Configuration format' : check.check === 'startup' ? 'Startup check' : 'Model connection');
        const result = t(check.status === 'passed' ? 'Passed' : check.status === 'failed' ? 'Check failed' : 'Not confirmed');
        checks.append(el('li', `${name}${check.profile ? ' · ' + check.profile : ''} — ${result}`, `check-${check.status}`));
      }
      this.root.append(checks);
    }
    const actions = el('div', undefined, 'configuration-actions');
    actions.append(button(t('Check configuration'), 'configuration.check'));
    const passed = !!validation?.valid && ['static', 'startup'].every(name => validation.checks.some(check => check.check === name && check.status === 'passed'));
    const save = button(t('Save configuration files'), 'configuration.save', {}, state.dirty);
    save.disabled ||= !state.dirty || !validation?.valid;
    const restart = button(t(connected ? 'Restart to apply' : 'Start core'), 'configuration.restart', {}, !state.dirty);
    restart.disabled ||= state.dirty || !passed || running;
    actions.append(save, restart); this.root.append(actions);
    if (running) this.root.append(el('p', t('The current turn continues. Restart when it finishes.'), 'configuration-hint'));
    const models = el('details', undefined, 'model-diagnostics'); models.open = expanded;
    models.append(el('summary', t('Test model connection (optional)')), el('p', t('Model tests send a short request with the configured output limit and may use tokens. Tools and images are not tested.'), 'configuration-hint'));
    const select = el('select'); select.setAttribute('aria-label', t('Model profile')); select.disabled = state.busy || !state.modelTargets.length;
    if (!state.modelTargets.some(target => target.profile === this.profile)) this.profile = state.modelTargets[0]?.profile ?? '';
    for (const target of state.modelTargets) {const option = el('option', `${target.profile} · ${target.model}`); option.value = target.profile; option.selected = target.profile === this.profile; select.append(option);}
    select.dataset.control = 'model-profile'; select.addEventListener('change', () => {this.profile = select.value;});
    // Read the selected value at click time, without stale model selections.
    const test = el('button', t('Test connection')); test.disabled = state.busy || !state.modelTargets.length; test.dataset.control = 'configuration.test';
    test.addEventListener('click', () => void this.request('configuration.test', {profile: this.profile}).catch(this.fail));
    models.append(select, test); this.root.append(models);
    if (focused) this.root.querySelector<HTMLElement>(`[data-control="${CSS.escape(focused)}"]`)?.focus({preventScroll: true});
  }
}
