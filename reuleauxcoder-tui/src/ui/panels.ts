import type {TuiController} from '../state/controller.js';
import stringWidth from 'string-width';
import {humanize} from '../state/menus.js';
import {diff, fields, safe, wrap} from './format.js';
import {keyHint, paint} from './theme.js';
import {inputRows, line, selectionRows} from './viewport.js';
import {historyRows} from './history.js';
import {TextLayout} from './text-layout.js';

export interface PanelRows {title: string; rows: string[]; hint: string[]; navigation?: string}

export function hintRows(hints: string[], width: number): string[] {
  const rows: string[] = [];
  for (const hint of hints) {
    const previous = rows.at(-1);
    if (previous && stringWidth(previous) + stringWidth(hint) + 2 <= width) rows[rows.length - 1] += '  ' + hint;
    else rows.push(...wrap(hint, width));
  }
  return rows;
}
export function panelRows(c: TuiController, width: number, height: number, layout = new TextLayout()): PanelRows | null {
  if (c.active) {
    const {kind, request, expiresAt} = c.active;
    const waiting = Math.max(c.client.interactions.length - 1, c.session.state.approval_waiting);
    const title = `${request.title}${waiting ? ` · ${waiting} waiting` : ''}${expiresAt === null ? '' : ` · ${Math.max(0, Math.ceil((expiresAt - performance.now()) / 1000))}s`}`;
    if (kind === 'input_text' || c.interactionMode === 'feedback') {
      const prompt = c.interactionMode === 'feedback' ? 'Explain what should change before this runs:' : request.prompt;
      const intro = wrap(safe(prompt), width);
      const introHeight = Math.max(0, Math.min(intro.length, height - 2));
      const prefix = introHeight ? [...intro.slice(0, introHeight), ''] : [];
      return {title, rows: [...prefix, ...inputRows(c.interactionInput, width, Math.max(1, height - prefix.length), true, request.secret)], hint: [request.secret ? paint.warning('Masked') : '', keyHint('Enter', 'submit'), keyHint('Alt+Enter', 'newline'), keyHint('Esc', 'cancel')].filter(Boolean)};
    }
    if (kind === 'choose_one' || c.interactionMode === 'scope') {
      const items = kind === 'choose_one' ? request.items : request.grant_options.map((item: any) => ({...item, label: item.label + (item.broad ? ' (broad scope)' : '')}));
      const intro = kind === 'choose_one' ? request.message : 'Allow matching requests for this session:';
      return {title, rows: [...(intro ? [line(paint.secondary(safe(intro)), width)] : []), ...selectionRows(items, c.interactionIndex, width, height - (intro ? 1 : 0))], hint: [keyHint('↑↓', 'select'), keyHint('Enter', 'confirm'), keyHint('Esc', 'back')]};
    }
    const rows = layout.rows(request, width, () => {
      const context = request.context;
      const contextText = context ? [
        [context.tool_name, context.tool_source, context.operation].filter(Boolean).join(' · '),
        context.subjects.join('\n'), context.reason,
        context.is_subagent ? `Subagent${context.subagent_mode ? ' · ' + context.subagent_mode : ''}${context.subagent_task ? '\n' + context.subagent_task : ''}` : '',
      ].filter(Boolean).map(safe).join('\n') : '';
      return kind === 'confirm' ? safe(request.message) : [paint.secondary(safe(request.summary)), paint.info(contextText), ...request.sections.map((section: any) => `${paint.accent(paint.bold(safe(section.title ?? section.kind)))}\n${section.kind === 'diff' ? diff(section.content) : fields(section.content)}`)].filter(Boolean).join('\n\n');
    });
    c.interactionOffset = Math.min(c.interactionOffset, Math.max(0, rows.length - height));
    const hint = kind === 'confirm'
      ? [paint.action('y/Enter yes'), paint.error('n no'), keyHint('Esc', 'cancel')]
      : [paint.action(`y/Enter ${safe(request.approve_label)} once`), paint.error(`n ${safe(request.reject_label)}`), ...(request.grant_options.length ? [keyHint('s', 'scope')] : []), keyHint('f', 'feedback')];
    return {title, rows: rows.slice(c.interactionOffset, c.interactionOffset + height), hint, navigation: `PgUp/PgDn ${c.interactionOffset + 1}/${rows.length}`};
  }
  const screen = c.screen;
  if (screen?.kind === 'history') return historyRows(screen.browser, width, height, layout);
  if (screen?.kind === 'list') {
    const items = c.listItems(screen);
    screen.index = Math.min(screen.index, Math.max(0, items.length - 1));
    const filter = height > 1 && screen.panel?.filterable !== false ? inputRows(screen.filter, width - 2, 1).map(row => '⌕ ' + row + (screen.filter.text ? '' : paint.muted('Filter options…'))) : [];
    return {title: screen.title, rows: [...filter, ...selectionRows(items, screen.index, width, height - filter.length)], hint: [keyHint('↑↓', 'select'), keyHint('Enter', 'open'), keyHint('Esc', 'back')], navigation: `${items.length ? screen.index + 1 : 0}/${items.length}`};
  }
  if (screen?.kind === 'document') {
    const rows = layout.rows(screen.body, width, () => safe(screen.body).split('\n').map(row => {
      const separator = row.indexOf(': ');
      return separator >= 0 ? paint.info(row.slice(0, separator + 1)) + row.slice(separator + 1) : row;
    }).join('\n'));
    screen.offset = Math.min(screen.offset, Math.max(0, rows.length - height));
    return {title: screen.title, rows: rows.slice(screen.offset, screen.offset + height), hint: [...(screen.title === 'Session details' && c.client.info.history_query ? [keyHint('h', 'browse / search history')] : []), keyHint('↑↓ / PgUp/PgDn', 'scroll', paint.info), keyHint('Esc', 'back'), ...(screen.menu ? [keyHint('Tab', 'actions')] : [])], navigation: `${screen.offset + 1}/${rows.length}`};
  }
  if (screen?.kind === 'form') {
    const parameter = screen.action.parameters[screen.index];
    return {title: screen.title, rows: [line(paint.accent(`${humanize(parameter.name)} · ${screen.index + 1}/${screen.action.parameters.length}${parameter.required ? ' · required' : ''}`), width), ...wrap(paint.muted(parameter.kind === 'boolean' ? 'Tab changes value: true / false' + (parameter.nullable ? ' / auto' : '') : parameter.kind === 'integer' ? 'Enter a whole number.' : 'Enter text.'), width), '', ...inputRows(screen.input, width, Math.max(1, height - 4)), ...(screen.error ? [line(paint.error(screen.error), width)] : [])].slice(0, height), hint: [keyHint('Enter', 'next / submit'), keyHint('↑', 'previous field'), keyHint('Esc', 'back')]};
  }
  if (c.palette.length) return {title: 'Commands', rows: selectionRows(c.palette.map(menu => ({label: menu.name, description: menu.title})), c.paletteIndex % c.palette.length, width, height), hint: [keyHint('↑↓', 'select'), keyHint('Tab', 'complete'), keyHint('Enter', 'open'), keyHint('Esc', 'dismiss')]};
  return null;
}
