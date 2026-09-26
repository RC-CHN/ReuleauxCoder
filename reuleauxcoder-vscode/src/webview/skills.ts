import type {PanelItem} from '@reuleauxcoder/client';
import type {CommandSurface, WebRequest} from '../shared.js';
import {t} from '../i18n.js';
import {skillText, skillSource, skillCategory} from '../skill-text.js';
import {icon, skillIcon} from './icons.js';
import {reveal} from './motion.js';

type Request = (action: string, data?: WebRequest['data']) => Promise<any>;
/** Skill names and presentation come entirely from the owning core's panel. */
export class SkillBrowser {
  private query = '';
  private source = '';
  private expanded?: string;
  private pending = false;
  private epoch = 0;
  private refresh?: () => void;
  private focusAfter?: string;
  private body?: HTMLElement;
  constructor(private request: Request, private notice: (error: unknown) => void) {}
  reset(): void {this.epoch++; this.pending = false; this.query = ''; this.source = ''; this.expanded = undefined; this.focusAfter = undefined; this.refresh = undefined;}

  draw(body: HTMLElement, surface: CommandSurface): void {
    this.body = body;
    const items = surface.panel!.items.map((item, index) => ({item, index})).filter(({item}) => item.details && item.id);
    const toolbar = document.createElement('div'); toolbar.className = 'skill-toolbar';
    const summary = document.createElement('span'); summary.className = 'skill-count';
    summary.textContent = t('{0} enabled · {1} available', items.filter(({item}) => item.current).length, items.length);
    const reload = this.action(t('Reload skills'), 'skill.reload', {surfaceId: surface.id});
    reload.prepend(icon('history')); reload.dataset.row = 'skill.reload'; toolbar.append(summary, reload);
    const search = document.createElement('input'); search.type = 'search'; search.className = 'skill-search';
    search.placeholder = t('Search skills'); search.setAttribute('aria-label', search.placeholder); search.value = this.query; search.dataset.row = 'skill.search';
    const scopes = document.createElement('div'); scopes.className = 'skill-scopes'; scopes.setAttribute('role', 'group'); scopes.setAttribute('aria-label', t('Skill sources'));
    const list = document.createElement('div'); list.className = 'skill-list';
    const empty = document.createElement('p'); empty.className = 'surface-empty'; empty.textContent = t('No matching skills. Try another name or source.');
    const hint = document.createElement('p'); hint.className = 'skill-hint'; hint.textContent = t('Enabled skills are available to the assistant. Enabling does not run them.');
    body.append(toolbar, search, scopes, list, empty, hint);

    const render = () => {
      const focus = (document.activeElement as HTMLElement)?.dataset.row;
      scopes.replaceChildren(); list.replaceChildren();
      for (const source of ['', 'builtin', 'user', 'project']) {
        const count = items.filter(({item}) => !source || item.details!.source === source).length;
        const button = document.createElement('button'); button.type = 'button'; button.textContent = `${source ? skillSource(source) : t('All')} ${count}`;
        button.dataset.row = `skill.source:${source}`; button.setAttribute('aria-pressed', String(this.source === source));
        button.addEventListener('click', () => {this.source = source; render(); reveal(list);}); scopes.append(button);
      }
      const query = this.query.trim().toLocaleLowerCase();
      const matches = items.filter(({item}) => (!this.source || item.details!.source === this.source) &&
        [item.id, item.label, item.details!.description, ...item.details!.titles.map(([, text]) => text), ...item.details!.summaries.map(([, text]) => text), skillCategory(item.details!.category)].join(' ').toLocaleLowerCase().includes(query));
      matches.sort((a, b) => skillCategory(a.item.details!.category).localeCompare(skillCategory(b.item.details!.category)) || skillText(a.item).label.localeCompare(skillText(b.item).label));
      let category: string | undefined;
      for (const {item, index} of matches) {
        if (category !== skillCategory(item.details!.category)) {
          category = skillCategory(item.details!.category);
          const heading = document.createElement('div'); heading.className = 'skill-category'; heading.textContent = category; list.append(heading);
        }
        list.append(this.row(item, index, surface, render));
      }
      empty.hidden = matches.length > 0;
      if (focus && focus !== 'skill.search') body.querySelector<HTMLElement>(`[data-row="${CSS.escape(focus)}"]`)?.focus();
      this.lock(surface.busy);
    };
    search.addEventListener('input', () => {this.query = search.value; render();});
    this.refresh = render;
    render();
  }

  private row(item: PanelItem, index: number, surface: CommandSurface, render: () => void): HTMLElement {
    const details = item.details!; const text = skillText(item); const id = item.id!;
    const row = document.createElement('article'); row.className = 'skill-row'; row.dataset.skill = id;
    const heading = document.createElement('div'); heading.className = 'skill-row-heading';
    const name = document.createElement('button'); name.type = 'button'; name.className = 'skill-name'; name.dataset.row = `skill.details:${id}`;
    name.setAttribute('aria-expanded', String(this.expanded === id)); name.setAttribute('aria-label', t('Details for {0}', text.label));
    const copy = document.createElement('span'); copy.className = 'skill-copy';
    const title = document.createElement('span'); title.className = 'skill-title'; title.textContent = text.label;
    const badge = document.createElement('span'); badge.className = 'skill-source'; badge.dataset.source = details.source; badge.textContent = skillSource(details.source); title.append(badge);
    const summary = document.createElement('small'); summary.textContent = text.description; copy.append(title, summary);
    name.append(icon(skillIcon(details.icon)), copy, icon('chevron'));
    name.addEventListener('click', () => {this.expanded = this.expanded === id ? undefined : id; render(); const content = this.body?.querySelector<HTMLElement>('.skill-details'); if (content) reveal(content);});
    const toggle = this.action(t(item.current ? 'Disable {0}' : 'Enable {0}', text.label), 'command.select', {surfaceId: surface.id, index});
    toggle.textContent = ''; toggle.className = 'skill-toggle'; toggle.dataset.row = `skill.toggle:${id}`;
    toggle.setAttribute('role', 'switch'); toggle.setAttribute('aria-checked', String(item.current));
    toggle.dataset.readonly = String(!item.action);
    const track = document.createElement('span'); track.className = `switch${item.current ? ' on' : ''}`;
    const status = document.createElement('small'); status.textContent = t(item.current ? 'Enabled' : 'Disabled'); toggle.append(track, status);
    heading.append(name, toggle); row.append(heading);
    if (this.expanded === id) {
      const content = document.createElement('div'); content.className = 'skill-details';
      const identifier = document.createElement('code'); identifier.textContent = id;
      const description = document.createElement('p'); description.textContent = details.description;
      const path = document.createElement('code'); path.className = 'skill-path'; path.textContent = details.location;
      const file = document.createElement('details'); file.className = 'skill-location';
      const fileLabel = document.createElement('summary'); fileLabel.textContent = t('Skill file'); file.append(fileLabel, path);
      const note = document.createElement('p'); note.className = 'skill-hint';
      note.textContent = t(details.source === 'builtin' ? 'Bundled instructions open read-only. Copy the complete skill to this workspace to customize it.' : details.source === 'user' ? 'Global instructions apply across workspaces. Copy to this workspace for a local variant. Save changes, then reload skills.' : 'Save your changes, then reload skills. Workspace skills override global and bundled skills with the same name.');
      const actions = document.createElement('div'); actions.className = 'skill-actions';
      const open = this.action(t(details.source === 'builtin' ? 'View instructions' : 'Edit instructions'), 'skill.open', {surfaceId: surface.id, name: id}); open.prepend(icon('document')); open.dataset.row = `skill.open:${id}`; actions.append(open);
      if (details.source !== 'project') {
        const customize = this.action(t('Copy to workspace'), 'skill.copy', {surfaceId: surface.id, name: id}); customize.prepend(icon('copy')); customize.dataset.row = `skill.copy:${id}`; actions.append(customize);
      }
      content.append(identifier, description, actions, note, file); row.append(content);
    }
    return row;
  }

  private action(label: string, action: string, data: WebRequest['data']): HTMLButtonElement {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = label; button.title = label; button.setAttribute('aria-label', label); button.dataset.submit = ''; button.dataset.skillAction = '';
    button.addEventListener('click', async () => {
      if (this.pending) return;
      const epoch = this.epoch;
      this.focusAfter = action === 'skill.open' || action === 'skill.copy' ? undefined : button.dataset.row;
      this.pending = true; this.lock(false);
      try {
        await this.request(action, data);
        if (epoch === this.epoch && action === 'skill.copy' && this.source) {this.source = 'project'; this.refresh?.();}
      } catch (error) {if (epoch === this.epoch) this.notice(error);}
      finally {if (epoch === this.epoch) {this.pending = false; this.syncBusy(this.body?.closest('[aria-busy]')?.getAttribute('aria-busy') === 'true');}}
    });
    return button;
  }
  private lock(busy: boolean): void {
    this.body?.querySelectorAll<HTMLButtonElement>('[data-skill-action]').forEach(button => {button.dataset.pending = String(this.pending); button.disabled = busy || this.pending || button.dataset.readonly === 'true';});
  }
  syncBusy(busy: boolean): void {
    this.lock(busy);
    if (!busy && !this.pending && this.focusAfter) {
      const row = this.focusAfter; this.focusAfter = undefined;
      if (document.activeElement === document.body && this.body?.isConnected) this.body.querySelector<HTMLElement>(`[data-row="${CSS.escape(row)}"]`)?.focus();
    }
  }
}
