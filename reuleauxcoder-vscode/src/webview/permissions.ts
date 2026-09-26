import type {Panel} from '@reuleauxcoder/client';
import type {CommandSurface, WebRequest} from '../shared.js';
import {t, coreText, errorText, toolLabel} from '../i18n.js';
import {icon} from './icons.js';
import {reveal} from './motion.js';
import {permissionTarget} from '../panel-i18n.js';

const policies = {
  allow: ['Allow automatically', 'Matching calls run without asking'],
  require_approval: ['Ask every time', 'Require a review for every match'],
  deny: ['Block', 'Reject matching calls before execution'],
  warn: ['Warn, then run', 'Show a warning but do not block execution'],
} as const;
const childAt = (panel: Panel, index: number) => {
  const item = panel.items[index]; return item && panel.children.find(([key]) => key === (item.id ?? item.label))?.[1];
};

/** One list of tool policies, backed by the core's existing scoped action tree. */
export class PermissionPolicies {
  private scope = 0;
  private filter = '';
  private busy = false;
  constructor(private request: (action: string, data?: WebRequest['data']) => Promise<any>, private notice: (error: unknown) => void) {}
  syncBusy(busy: boolean): void {this.busy = busy;}
  draw(root: HTMLElement, surface: CommandSurface): void {
    const panel = surface.panel!;
    const tabs = document.createElement('div'); tabs.className = 'policy-scopes'; tabs.setAttribute('role', 'group'); tabs.setAttribute('aria-label', t('Approval scope'));
    for (const [index, title] of [t('This session'), t('This workspace')].entries()) {
      const tab = document.createElement('button'); tab.type = 'button'; tab.dataset.submit = ''; tab.textContent = title; tab.setAttribute('aria-pressed', String(this.scope === index)); tab.disabled = this.busy;
      tab.addEventListener('click', () => {if (this.scope === index) return; this.scope = index; root.replaceChildren(); this.draw(root, surface); reveal(root.querySelector<HTMLElement>('.policy-list')!); root.querySelector<HTMLButtonElement>(`.policy-scopes button:nth-child(${index + 1})`)!.focus();}); tabs.append(tab);
    }
    const help = document.createElement('p'); help.className = 'policy-help';
    help.textContent = this.scope === 0 ? t('Choose how tools may run in this conversation. Changes apply immediately.') : t('Save defaults for this workspace. Existing session rules take priority.');
    const search = document.createElement('input'); search.type = 'search'; search.className = 'menu-search'; search.placeholder = t('Find a tool, path or MCP server'); search.setAttribute('aria-label', search.placeholder); search.value = this.filter; search.dataset.policyFilter = '';
    const list = document.createElement('div'); list.className = 'policy-list';
    const toolbar = document.createElement('div'); toolbar.className = 'policy-toolbar'; toolbar.append(tabs, help, search);
    root.append(toolbar, list);
    for (const [index, item] of panel.items.entries()) {
      const scopes = childAt(panel, index); if (!scopes) continue;
      const actions = childAt(scopes, this.scope); if (!actions || actions.view_type !== 'approval_actions') continue;
      const row = document.createElement('div'); row.className = 'policy-tool'; row.dataset.target = item.id ?? item.label;
      const copy = document.createElement('div'); copy.className = 'policy-tool-copy';
      const command = actions.items.find(action => action.action)?.action?.command;
      const selector = typeof command?.target === 'string' ? command.target : '';
      const fields = Object.fromEntries(selector.split(',').map(part => {const at = part.indexOf('='); return at > 0 ? [part.slice(0, at), part.slice(at + 1)] : ['', ''];}));
      const label = document.createElement('label'); label.htmlFor = `policy-${index}`;
      label.textContent = fields.tool ? (fields.source === 'mcp' ? fields.tool : toolLabel(fields.tool)) : permissionTarget(item.label);
      const scope = document.createElement('small'); scope.className = 'policy-target-scope';
      scope.textContent = [fields.source === 'builtin' ? t('Built-in tools') : fields.source === 'mcp' ? 'MCP' : fields.source ? errorText(fields.source) : t('All sources'), fields.mcp_server,
        fields.effect ? `${t('Effect')}: ${errorText(fields.effect)}` : '', fields.profile ? `${t('Profile')}: ${fields.profile}` : '', command?.pattern].filter(Boolean).join(' · ');
      const source = document.createElement('small'); source.className = 'policy-source';
      // Keep the backend's layer facts: rules with similar names may match different sources.
      source.textContent = coreText(item.description.replace(/^⚠ broad scope · /i, '').replace(/ · no override$/, ''));
      copy.append(label, scope);
      const details = document.createElement('details'); details.className = 'policy-details';
      const summary = document.createElement('summary'); summary.textContent = t('Rule details');
      const raw = document.createElement('code'); raw.textContent = [selector || item.label, command?.pattern].filter(Boolean).join(' · ');
      raw.setAttribute('aria-label', t('Rule selector')); details.append(summary, raw);
      row.dataset.search = [item.label, selector, command?.pattern, scope.textContent].join(' ').toLowerCase();
      const select = document.createElement('select'); select.id = label.htmlFor; select.dataset.submit = ''; select.dataset.row = item.id ?? item.label; select.disabled = this.busy;
      const active = actions.items.findIndex(item => item.current);
      const unset = actions.items.findIndex(item => item.action && !item.action.command.action);
      const inherit = document.createElement('option'); inherit.value = unset < 0 ? '' : String(unset); inherit.textContent = t('Use inherited rule'); inherit.disabled = active >= 0 && unset < 0; select.append(inherit);
      for (const policy of Object.keys(policies)) {
        const optionIndex = actions.items.findIndex(item => item.action?.command.action === policy); if (optionIndex < 0) continue;
        const option = document.createElement('option'); option.value = String(optionIndex); option.textContent = t(policies[policy as keyof typeof policies][0]); select.append(option);
      }
      select.value = active < 0 ? inherit.value : String(active);
      const current = actions.items[active]?.action?.command.action;
      row.dataset.policy = typeof current === 'string' ? current : 'inherit';
      const explanation = document.createElement('div'); explanation.className = 'policy-explanation'; explanation.hidden = true;
      select.title = `${coreText(scopes.items[this.scope].description)} · ${typeof current === 'string' && current in policies ? t(policies[current as keyof typeof policies][1]) : t('No override at this scope; existing defaults still apply.')}`;
      if (/broad scope/i.test(item.description)) {row.classList.add('broad-scope'); const badge = document.createElement('span'); badge.className = 'policy-broad'; badge.append(icon('shield'), document.createTextNode(t('Broad permission'))); copy.append(badge);}
      select.addEventListener('change', () => {
        if (select.value === '') return;
        root.querySelectorAll<HTMLButtonElement | HTMLSelectElement>('button, select').forEach(control => control.disabled = true);
        const previous = active < 0 ? inherit.value : String(active);
        explanation.hidden = false; explanation.textContent = t('Saving rule…');
        void this.request('command.policy', {surfaceId: surface.id, path: [index, this.scope, Number(select.value)]}).catch(error => {select.value = previous; this.notice(error); explanation.textContent = t('Could not confirm the rule. Reopen permissions to check.');}).finally(() => {
          if (root.isConnected) root.querySelectorAll<HTMLButtonElement | HTMLSelectElement>('button, select').forEach(control => control.disabled = false);
        });
      });
      row.append(copy, select, source, details, explanation); list.append(row);
    }
    const empty = document.createElement('p'); empty.className = 'surface-empty'; empty.textContent = t('No matching tools'); list.append(empty);
    const filter = () => {this.filter = search.value; let visible = 0; list.querySelectorAll<HTMLElement>('.policy-tool').forEach(row => {row.hidden = !`${row.dataset.search} ${row.textContent}`.toLowerCase().includes(this.filter.toLowerCase()); if (!row.hidden) visible++;}); empty.hidden = visible > 0;};
    search.addEventListener('input', filter); filter();
  }
}
