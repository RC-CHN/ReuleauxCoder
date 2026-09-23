import {marked, type Token} from 'marked';
import wrapAnsi from 'wrap-ansi';
import {typeOf} from '@reuleauxcoder/client';
import {humanize} from '../state/menus.js';
import {frameEdge, frameRow, paint} from './theme.js';
import {tableRows} from './table.js';
/** Treat backend/user escape sequences as data. Styling is produced only here. */
export const safe = (text: string) => text.replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, '');

function inline(tokens: Token[]): string {
  return tokens.map((token: any) => {
    switch (token.type) {
      case 'strong': return paint.bold(inline(token.tokens));
      case 'em': return `\x1b[3m${inline(token.tokens)}\x1b[23m`;
      case 'codespan': return paint.accent(token.text);
      case 'link': {
        const label = inline(token.tokens);
        return token.autolink || token.text === token.href ? label : `${label} ${paint.muted(`(${safe(token.href)})`)}`;
      }
      case 'image': return `[${token.text}] ${safe(token.href)}`;
      case 'br': return '\n';
      case 'html': return /^<br\s*\/?\s*>$/i.test(token.text) ? '\n' : token.text;
      default: return token.tokens ? inline(token.tokens) : token.text ?? token.raw;
    }
  }).join('');
}

export function markdown(text: string, width = 80, expanded = false): string {
  function render(tokens: Token[]): string {
    return tokens.map((token: any) => {
      switch (token.type) {
        case 'heading': return paint.secondary(paint.bold(inline(token.tokens))) + '\n';
        case 'paragraph': return inline(token.tokens) + '\n';
        case 'code': return [frameEdge(token.lang || 'code', width, false, paint.info), ...wrap(safe(token.text), width - 4).map(line => frameRow(line, width)), frameEdge('', width, true)].join('\n') + '\n';
        case 'blockquote': return render(token.tokens).split('\n').map(line => '│ ' + line).join('\n');
        case 'list': return token.items.map((item: any, index: number) => `${token.ordered ? `${index + (token.start || 1)}.` : '•'} ${item.task ? (item.checked ? '[✓] ' : '[ ] ') : ''}${render(item.tokens).trimEnd()}`).join('\n') + '\n';
        case 'table': return tableRows(token, Math.max(1, width), expanded, inline).join('\n') + '\n';
        case 'hr': return '────────\n';
        case 'space': return '\n';
        default: return inline([token]);
      }
    }).join('');
  }
  return render(marked.lexer(safe(text))).trimEnd();
}

export function diff(text: string): string {
  return safe(text).split('\n').map(line => line.startsWith('+') ? paint.addition(line) : line.startsWith('-') ? paint.deletion(line) : line.startsWith('@@') ? paint.accent(line) : line).join('\n');
}

/** All public view fields remain reachable, including fields added by the backend. */
export function fields(value: any, depth = 0): string {
  if (value === null || value === undefined) return '—';
  if (typeof value !== 'object') return safe(String(value));
  if (Array.isArray(value)) return value.length ? value.map(item => typeof item === 'object' && item !== null ? fields(item, depth) : '• ' + fields(item, depth)).join('\n\n') : '(none)';
  const isContract = typeOf(value) !== undefined;
  return Object.entries(value).filter(([key]) => !(isContract && key === 'view_type')).map(([key, item]) => {
    const title = humanize(key);
    if (item !== null && typeof item === 'object') return `${title}\n${fields(item, depth + 1).split('\n').map(line => '  ' + line).join('\n')}`;
    return `${title}: ${fields(item, depth + 1)}`;
  }).join('\n');
}

export const wrap = (text: string, width: number) => wrapAnsi(text, Math.max(1, width), {hard: true, trim: false}).split('\n');
