import type {ChatCell} from '../shared.js';
import {t, errorText, toolLabel} from '../i18n.js';
import {icon} from './icons.js';
import {renderMarkdown} from './markdown.js';
import {reveal} from './motion.js';

const failed = new Set(['failed', 'denied', 'cancelled', 'timeout', 'error']);
interface Entry {
  cell: ChatCell; details: HTMLDetailsElement; title: HTMLElement; target: HTMLElement; status: HTMLElement;
  content: HTMLElement; body?: HTMLElement; arguments?: HTMLElement; text?: string; detail?: string; header?: Partial<ChatCell>;
}
function sameCell(a: ChatCell | undefined, b: ChatCell): boolean {
  return !!a && a.id === b.id && a.role === b.role && a.text === b.text && a.title === b.title && a.detail === b.detail && a.status === b.status && a.merged === b.merged && (a.members?.length ?? 0) === (b.members?.length ?? 0) && (b.members ?? []).every((member, index) => sameCell(a.members?.[index], member));
}

/** Keep disclosure state and visible DOM across streaming and flat-to-group transitions. */
export class ToolCells {
  private entries = new Map<string, Entry>();
  private expandNew = false;
  constructor(private openLink: (url: string) => void, private openFile: (path: string) => void, private changed: () => void) {}

  render(root: HTMLElement, cell: ChatCell): boolean {
    const previous = this.entries.get(cell.id)?.cell;
    const changed = !sameCell(previous, cell);
    const entry = this.entry(cell);
    if (entry.details.parentElement !== root) root.replaceChildren(entry.details);
    return changed;
  }

  private entry(cell: ChatCell): Entry {
    let entry = this.entries.get(cell.id);
    if (!entry) {
      const details = document.createElement('details'); details.className = 'tool-disclosure'; details.dataset.toolId = cell.id;
      const summary = document.createElement('summary');
      const copy = document.createElement('span'); copy.className = 'tool-copy';
      const title = document.createElement('span'); title.className = 'tool-name';
      const target = document.createElement('span'); target.className = 'tool-target';
      const status = document.createElement('span'); status.className = 'tool-state';
      copy.append(title, target); summary.append(icon(cell.role === 'reasoning' ? 'skills' : 'tools'), copy, status);
      const content = document.createElement('div'); content.className = cell.role === 'tool-group' ? 'tool-members' : 'tool-content';
      details.append(summary, content);
      const inheritedOpen = cell.members?.some(member => this.entries.get(member.id)?.details.open);
      details.open = this.expandNew || !!inheritedOpen || failed.has(cell.status ?? '');
      entry = {cell, details, title, target, status, content}; this.entries.set(cell.id, entry);
      const current = entry;
      details.addEventListener('toggle', () => {
        if (details.open) {this.draw(current); reveal(content);}
        this.changed();
      });
    }
    if (failed.has(cell.status ?? '') && !failed.has(entry.cell.status ?? '')) entry.details.open = true;
    entry.cell = cell;
    const header = {role: cell.role, title: cell.title, status: cell.status, detail: cell.detail, merged: cell.merged, text: cell.role === 'tool-group' ? cell.text : ''};
    if (!entry.header || Object.entries(header).some(([key, value]) => entry!.header![key as keyof ChatCell] !== value)) {
      entry.header = header;
      const args = this.arguments(cell.detail);
      const group = cell.role === 'tool-group' ? cell.text.split(' · ') : [];
      entry.title.textContent = group[0] ?? (cell.role === 'reasoning' ? t('Reasoning') : toolLabel(cell.title ?? t('Tool')));
      const target = group.length ? group.slice(1).join(' · ') : cell.merged ? t('{0} checks', cell.merged) : args.command ?? args.file_path ?? args.path ?? args.pattern ?? args.url ?? args.task ?? '';
      entry.target.textContent = String(target); entry.target.title = String(target); entry.target.hidden = !target;
      entry.details.dataset.status = cell.status ?? '';
      entry.status.textContent = cell.role === 'tool-group' || cell.role === 'reasoning' ? '' : errorText(cell.status ?? '');
      entry.status.hidden = !entry.status.textContent;
      entry.details.title = cell.role === 'tool' ? cell.title ?? '' : '';
    }
    if (entry.details.open) this.draw(entry);
    return entry;
  }

  private draw(entry: Entry): void {
    const cell = entry.cell;
    if (cell.role === 'tool-group' || cell.merged && cell.members?.length) {
      const members = cell.merged ? cell.members!.map(member => ({...member, id: `${cell.id}:check:${member.id}`})) : cell.members ?? [];
      const visible = new Set(members.map(member => member.id));
      for (const node of [...entry.content.children]) if (!visible.has((node as HTMLElement).dataset.toolId!)) node.remove();
      let previous: Node | null = null;
      for (const member of members) {
        const node = this.entry(member).details;
        const expected: ChildNode | null = previous ? previous.nextSibling : entry.content.firstChild;
        if (node !== expected) entry.content.insertBefore(node, expected);
        previous = node;
      }
      return;
    }
    if (!entry.body) {
      entry.arguments = document.createElement('section'); entry.arguments.className = 'tool-arguments'; entry.arguments.hidden = true;
      entry.body = document.createElement(cell.role === 'reasoning' ? 'div' : 'pre'); entry.body.className = cell.role === 'reasoning' ? 'body' : 'tool-output';
      entry.content.append(entry.arguments, entry.body);
    }
    if (entry.detail !== cell.detail) {
      entry.detail = cell.detail; entry.arguments!.replaceChildren(); entry.arguments!.hidden = !cell.detail;
      if (cell.detail) {
        const heading = document.createElement('h4'); heading.textContent = t('Arguments');
        const list = document.createElement('dl'), args = this.arguments(cell.detail);
        if (Object.keys(args).length) for (const [key, value] of Object.entries(args)) {
          const label = document.createElement('dt'); label.textContent = key;
          const content = document.createElement('dd'); content.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
          list.append(label, content);
        } else {const value = document.createElement('pre'); value.textContent = cell.detail; list.append(value);}
        entry.arguments!.append(heading, list);
      }
    }
    if (entry.text !== cell.text) {
      entry.text = cell.text;
      if (cell.role === 'reasoning') renderMarkdown(entry.body, cell.text, this.openLink, this.openFile);
      else {
        let text = cell.text;
        if (/^\s*[\[{]/.test(text)) {try {text = JSON.stringify(JSON.parse(text), null, 2);} catch { /* Tool output may be ordinary text. */ }}
        entry.body.textContent = text || (cell.status === 'running' ? t('Waiting for output…') : t('No output'));
      }
    }
  }

  private arguments(text?: string): Record<string, unknown> {
    if (!text) return {};
    try {const value = JSON.parse(text); return value && typeof value === 'object' && !Array.isArray(value) ? value : {};} catch {return {};}
  }
  allExpanded(): boolean {return this.entries.size > 0 && [...this.entries.values()].every(entry => entry.details.open);}
  get size(): number {return this.entries.size;}
  setExpanded(open: boolean): void {
    this.expandNew = open;
    for (const entry of this.entries.values()) {entry.details.open = open; if (open) this.draw(entry);}
    this.changed();
  }
  retain(cells: ChatCell[]): void {
    const ids = new Set<string>();
    const visit = (cell: ChatCell): void => {ids.add(cell.id); for (const member of cell.members ?? []) visit(cell.merged ? {...member, id: `${cell.id}:check:${member.id}`} : member);};
    for (const cell of cells) visit(cell);
    for (const id of this.entries.keys()) if (!ids.has(id)) this.entries.delete(id);
    this.changed();
  }
  reset(): void {this.entries.clear(); this.expandNew = false;}
}
