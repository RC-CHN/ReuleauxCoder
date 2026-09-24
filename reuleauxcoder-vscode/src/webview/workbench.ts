import type {Action} from '@reuleauxcoder/client';
import type {CommandSurface, HostSnapshot, WebRequest} from '../shared.js';
import {t, errorText} from '../i18n.js';
import {actionLabel, actionScope, commandItems, featureInfo, panelText} from './catalog.js';
import {icon} from './icons.js';
import {PermissionPolicies} from './permissions.js';

type Request = (action: string, data?: WebRequest['data']) => Promise<any>;
export class ComposerWorkbench {
  private state?: HostSnapshot;
  private menu = false;
  private slash = false;
  private index = 0;
  private signature = '';
  private surfaceId?: number;
  private rows: HTMLButtonElement[] = [];
  private entries: ReturnType<typeof commandItems> = [];
  private busy = false;
  private focusReturn: HTMLElement | null = null;
  private search = '';
  private permissions: PermissionPolicies;
  constructor(private root: HTMLElement, private composer: HTMLTextAreaElement, private request: Request, private notice: (error: unknown) => void, private save: () => void) {
    this.permissions = new PermissionPolicies(request, notice);
    document.addEventListener('pointerdown', event => {if (this.menu && !root.contains(event.target as Node) && event.target !== composer && !(event.target as Element).closest('#commands')) this.dismiss();});
    root.addEventListener('keydown', event => {if (event.key === 'Escape' && !event.isComposing) {event.preventDefault(); this.dismiss();} });
  }
  show(query = '', slash = false): void {
    this.focusReturn = document.activeElement as HTMLElement;
    this.menu = true; this.slash = slash; this.index = 0; this.search = query; this.signature = ''; this.drawMenu();
    if (!slash) this.root.querySelector<HTMLInputElement>('input')?.focus();
  }
  input(): void {
    if (/^\/[^\n]*$/.test(this.composer.value) && this.composer.selectionStart === this.composer.value.length) this.show(this.composer.value, true);
    else if (this.menu && this.slash) this.hideMenu();
  }
  key(event: KeyboardEvent): boolean {
    if (!this.menu || !this.slash || event.isComposing || event.key === 'Enter' && event.shiftKey) return false;
    if (['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'].includes(event.key)) {
      event.preventDefault();
      if (event.key === 'Escape') this.dismiss();
      else if (event.key === 'Enter' || event.key === 'Tab') this.chooseSelected();
      else this.highlight(this.index + (event.key === 'ArrowDown' ? 1 : -1));
      return true;
    }
    return false;
  }
  consumeSend(): boolean {if (!this.menu) return false; this.chooseSelected(); return true;}
  private chooseSelected(): void {const entry = this.entries[this.index]; if (entry) void this.choose(entry.action);}
  private hideMenu(): void {this.menu = false; this.composer.removeAttribute('aria-activedescendant'); this.composer.setAttribute('aria-expanded', 'false'); this.signature = ''; this.update(this.state!);}
  dismiss(): void {
    if (this.menu) this.hideMenu();
    else {this.root.hidden = true; void this.request('command.close', {surfaceId: this.state?.commandSurface?.id}).catch(this.notice);}
    (this.focusReturn?.isConnected ? this.focusReturn : this.composer)?.focus();
  }
  update(state: HostSnapshot): void {
    if (!state) return;
    if (this.state && (state.hostId !== this.state.hostId || state.generation !== this.state.generation)) {this.menu = false; this.signature = '';}
    this.state = state;
    if (this.menu) {this.drawMenu(); return;}
    if (state.interactions?.length) {this.root.hidden = true; this.signature = ''; return;}
    const surface = state.commandSurface;
    if (!surface || !surface.panel && !surface.action && !surface.busy) {this.root.hidden = true; this.signature = ''; return;}
    this.root.hidden = false;
    // Streaming snapshots must not replace a focused form or erase entered values.
    const signature = JSON.stringify({...surface, busy: false});
    if (signature !== this.signature) {
      const filter = this.root.querySelector<HTMLInputElement>('[data-panel-filter]')?.value ?? '';
      const focus = (document.activeElement as HTMLElement)?.dataset.row;
      const scroll = this.root.querySelector('.workbench-body')?.scrollTop ?? 0;
      const sameSurface = this.surfaceId === surface.id;
      this.signature = signature; this.surfaceId = surface.id; this.drawSurface(surface, filter);
      if (focus) this.root.querySelector<HTMLElement>(`[data-row="${CSS.escape(focus)}"]`)?.focus();
      else if (!sameSurface && surface.action) this.root.querySelector<HTMLInputElement>('input, textarea, select')?.focus();
      this.root.querySelector('.workbench-body')!.scrollTop = scroll;
    }
    this.root.setAttribute('aria-busy', String(surface.busy));
    this.root.querySelectorAll<HTMLButtonElement | HTMLSelectElement>('[data-submit]').forEach(button => button.disabled = surface.busy || button.dataset.readonly === 'true');
  }
  private header(title: string, back?: () => void): HTMLElement {
    const head = document.createElement('div'); head.className = 'workbench-heading';
    if (back) head.append(this.button('', back, 'back', t('Back')));
    const text = document.createElement('strong'); text.textContent = title;
    head.append(text, this.button('', () => this.dismiss(), 'close', t('Close'))); return head;
  }
  private drawMenu(): void {
    const signature = `menu:${this.slash}:${this.search}:${JSON.stringify(this.state?.catalog)}`;
    if (signature === this.signature) return;
    this.signature = signature; this.root.hidden = false; this.root.dataset.feature = ''; this.root.replaceChildren(this.header(t('What would you like to do?')));
    if (!this.slash) {
      const search = document.createElement('input'); search.type = 'search'; search.className = 'menu-search'; search.placeholder = t('Search commands and skills'); search.setAttribute('aria-label', search.placeholder); search.value = this.search;
      search.addEventListener('input', () => {this.search = search.value; this.index = 0; this.drawEntries();});
      search.addEventListener('keydown', event => {if (event.isComposing) return; if (event.key === 'ArrowDown') {event.preventDefault(); this.rows[0]?.focus();} else if (event.key === 'Enter') {event.preventDefault(); this.chooseSelected();}});
      this.root.append(search);
    }
    const body = document.createElement('div'); body.className = 'workbench-body command-list'; body.id = 'command-list'; body.setAttribute('role', 'listbox'); body.setAttribute('aria-label', t('Commands')); this.root.append(body);
    this.drawEntries();
  }
  private drawEntries(): void {
    const body = this.root.querySelector('.command-list')!; body.replaceChildren(); this.rows = [];
    this.entries = commandItems(this.state?.catalog ?? [], this.search);
    let group = '';
    for (const [index, entry] of this.entries.entries()) {
      if (entry.group !== group) {group = entry.group; const heading = document.createElement('div'); heading.className = 'menu-group'; heading.textContent = group; body.append(heading);}
      const row = this.row(entry.label, entry.description, () => void this.choose(entry.action));
      row.prepend(icon(entry.icon)); row.id = `command-option-${index}`; row.dataset.action = entry.action.action_id; row.setAttribute('role', 'option');
      row.addEventListener('pointermove', () => this.highlight(index, false));
      row.addEventListener('keydown', event => {if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {event.preventDefault(); this.highlight(index + (event.key === 'ArrowDown' ? 1 : -1)); this.rows[this.index]?.focus();}});
      body.append(row); this.rows.push(row);
    }
    if (!this.entries.length) {const empty = document.createElement('p'); empty.className = 'surface-empty'; empty.textContent = this.state?.phase === 'ready' ? t('No matching commands') : t('Start the workspace core first.'); body.append(empty);}
    this.composer.setAttribute('aria-expanded', String(this.slash)); this.highlight(0, false);
  }
  private highlight(index: number, scroll = true): void {
    this.index = Math.max(0, Math.min(this.rows.length - 1, index));
    this.rows.forEach((row, index) => {row.classList.toggle('selected', index === this.index); row.setAttribute('aria-selected', String(index === this.index));});
    const selected = this.rows[this.index]; if (selected && this.slash) this.composer.setAttribute('aria-activedescendant', selected.id);
    if (scroll) selected?.scrollIntoView({block: 'nearest'});
  }
  private async choose(action: Action): Promise<void> {
    if (this.busy) return; this.busy = true;
    const draft = this.slash ? this.composer.value : undefined;
    if (draft !== undefined) {this.composer.value = ''; this.save();}
    this.menu = false; this.slash = false; this.composer.setAttribute('aria-expanded', 'false'); this.composer.removeAttribute('aria-activedescendant'); this.signature = ''; this.root.hidden = true;
    try {await this.request('command.open', {actionId: action.action_id});}
    catch (error) {if (draft !== undefined && !this.composer.value) {this.composer.value = draft; this.save();} this.notice(error);}
    finally {this.busy = false;}
  }
  private drawSurface(surface: CommandSurface, filter: string): void {
    const title = surface.action ? actionLabel(surface.action) : panelText(surface.panel?.title ?? featureInfo(surface.feature).label);
    this.root.dataset.feature = surface.feature;
    this.root.replaceChildren(this.header(title, surface.canBack ? () => void this.request('command.back', {surfaceId: surface.id}).catch(this.notice) : undefined));
    const body = document.createElement('div'); body.className = 'workbench-body'; this.root.append(body);
    if (surface.action) {this.form(body, surface); return;}
    const panel = surface.panel;
    if (!panel) {const loading = document.createElement('p'); loading.className = 'surface-empty'; loading.textContent = surface.busy ? t('Loading…') : t('Command sent. Results appear in the conversation.'); body.append(loading); return;}
    if (surface.feature === 'approval' && panel.view_type === 'approval_rules') {this.permissions.draw(body, surface); return;}
    if (surface.feature === 'approval') {
      const steps = document.createElement('div'); steps.className = 'permission-steps';
      const active = panel.view_type === 'approval_actions' ? 2 : panel.view_type === 'approval_lifetime' ? 1 : 0;
      for (const [index, title] of [t('Target'), t('Lifetime'), t('Rule')].entries()) {const step = document.createElement('span'); step.textContent = `${index + 1}  ${title}`; step.className = index === active ? 'active' : ''; steps.append(step);}
      body.append(steps);
      const hint = document.createElement('p'); hint.className = 'permission-hint'; hint.textContent = active === 0 ? t('Choose which tools this permission applies to.') : active === 1 ? t('Choose whether the rule belongs to this conversation or the workspace.') : t('Select how matching tool calls should be handled.'); body.append(hint);
    }
    if (panel.body || panel.output) {const output = document.createElement('div'); output.className = 'panel-output'; output.textContent = [panel.body, panel.output].filter(Boolean).join('\n\n'); body.append(output);}
    const rows = document.createElement('div'); rows.className = 'panel-rows';
    for (const [index, item] of panel.items.entries()) {
      const child = panel.children.some(([id]) => id === (item.id ?? item.label));
      const row = this.row(panelText(item.label), panelText(item.description), () => void this.request('command.select', {surfaceId: surface.id, index}).catch(this.notice));
      row.dataset.row = item.id ?? item.label; row.dataset.submit = ''; row.disabled = !item.action && !child; row.dataset.readonly = String(row.disabled);
      if (child) row.append(icon('chevron'));
      else if (item.action?.action_id.startsWith('skills.') || item.action?.action_id.startsWith('mcp.')) {
        const toggle = document.createElement('span'); toggle.className = `switch${item.current ? ' on' : ''}`; row.append(toggle); row.setAttribute('role', 'switch'); row.setAttribute('aria-checked', String(item.current));
      } else if (item.current) {row.classList.add('current'); row.append(icon('check')); row.setAttribute('aria-current', 'true');}
      if (surface.feature === 'approval') {row.classList.add('permission-row'); if (item.action?.command.action) row.dataset.policy = String(item.action.command.action); if (/broad scope/i.test(item.description)) row.classList.add('broad-scope');}
      rows.append(row);
    }
    if (panel.filterable && panel.items.length > 5) {
      const search = document.createElement('input'); search.type = 'search'; search.className = 'menu-search'; search.dataset.panelFilter = ''; search.placeholder = t('Filter this list'); search.setAttribute('aria-label', search.placeholder); search.value = filter;
      const apply = () => {for (const row of rows.children) (row as HTMLElement).hidden = !row.textContent!.toLowerCase().includes(search.value.toLowerCase());};
      search.addEventListener('input', apply); apply(); body.append(search);
    }
    body.append(rows);
    if (panel.show_auxiliary_actions !== false) {
      const more = document.createElement('details'); more.className = 'more-actions'; const summary = document.createElement('summary'); summary.textContent = t('More actions'); more.append(summary);
      for (const action of this.state?.catalog?.filter(action => action.feature_id === surface.feature) ?? []) more.append(this.row(actionLabel(action), actionScope(action), () => void this.choose(action)));
      body.append(more);
    }
  }
  private form(body: HTMLElement, surface: CommandSurface): void {
    const action = surface.action!; const form = document.createElement('form'); form.className = 'command-form';
    const scope = actionScope(action); if (scope) {const hint = document.createElement('div'); hint.className = 'scope-chip'; hint.textContent = scope; form.append(hint);}
    for (const parameter of action.parameters) {
      const label = document.createElement('label'); const caption = document.createElement('span'); caption.textContent = `${errorText(parameter.name)}${parameter.required ? ' *' : ''}`;
      const input = parameter.kind === 'boolean' ? document.createElement('select') : parameter.name === 'objective' || parameter.name === 'message' ? document.createElement('textarea') : document.createElement('input');
      input.name = parameter.name; input.required = parameter.required && !parameter.nullable;
      if (input instanceof HTMLSelectElement) {for (const [value, text] of [...(!input.required ? [['', t('Default')]] : []), ['true', t('Enabled')], ['false', t('Disabled')]]) {const option = document.createElement('option'); option.value = value; option.textContent = text; input.append(option);}}
      if (input instanceof HTMLInputElement) {input.type = parameter.kind === 'integer' ? 'number' : 'text'; if (parameter.kind === 'integer') input.step = '1';}
      input.value = parameter.default === null ? '' : String(parameter.default); label.append(caption, input); form.append(label);
    }
    const error = document.createElement('p'); error.className = 'form-error'; error.setAttribute('role', 'alert'); error.hidden = true;
    const submit = this.button(t('Apply'), () => {}); submit.type = 'submit'; submit.className = 'primary'; submit.dataset.submit = '';
    form.append(error, submit);
    form.addEventListener('submit', event => {
      event.preventDefault(); if (submit.disabled) return;
      const data = new FormData(form); const values = Object.fromEntries(action.parameters.map(parameter => {const value = String(data.get(parameter.name) ?? ''); return [parameter.name, value === '' ? null : parameter.kind === 'integer' ? Number(value) : parameter.kind === 'boolean' ? value === 'true' : value];}));
      submit.disabled = true;
      void this.request('command.submit', {surfaceId: surface.id, values}).catch(reason => {error.textContent = errorText(reason); error.hidden = false;}).finally(() => {submit.disabled = false;});
    }); body.append(form);
  }
  private button(text: string, click: () => void, name?: Parameters<typeof icon>[0], title = text): HTMLButtonElement {
    const button = document.createElement('button'); button.type = 'button'; button.title = title; button.setAttribute('aria-label', title); if (name) button.append(icon(name)); if (text) button.append(document.createTextNode(text)); button.addEventListener('click', click); return button;
  }
  private row(label: string, description: string, click: () => void): HTMLButtonElement {
    const row = this.button('', click, undefined, label); row.className = 'menu-row';
    const text = document.createElement('span'); text.className = 'menu-copy'; const title = document.createElement('span'); title.className = 'menu-title'; title.textContent = label; const detail = document.createElement('small'); detail.textContent = description; text.append(title, detail); row.append(text); return row;
  }
}
