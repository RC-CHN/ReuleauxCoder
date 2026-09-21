import sliceAnsi from 'slice-ansi';
import stringWidth from 'string-width';
import {layoutEditor, type Editor} from '../state/editor.js';
import {safe, wrap} from './format.js';
import {fit, paint} from './theme.js';
import type {SelectionViewport} from '../state/scroll.js';
export {TranscriptLayout} from './transcript.js';

export const line = (text: string, width: number) => sliceAnsi(text, 0, Math.max(1, width));
export interface CursorPosition {x: number; y: number}
export interface InputLayout {rows: string[]; cursor?: CursorPosition}

export function inputLayout(value: Editor, width: number, height: number, active = true, secret = false): InputLayout {
  const layout = layoutEditor(value.text, width, secret);
  const position = layout.positions[value.cursor];
  const start = Math.max(0, Math.min(layout.rows.length - height, position.y - height + 1));
  const rows = layout.rows.slice(start, start + height).map((row, index) => {
    if (!active || index + start !== position.y) return row.text;
    const before = layout.display.slice(row.start, value.cursor).join('');
    const cursor = layout.display[value.cursor];
    const marker = !cursor || cursor === '\n' ? ' ' : cursor;
    return before + `\x1b[7m${marker}\x1b[27m` + layout.display.slice(value.cursor + 1, row.end).join('');
  });
  return {rows, cursor: active ? {...position, y: position.y - start} : undefined};
}

export function inputRows(value: Editor, width: number, height: number, active = true, secret = false): string[] {
  return inputLayout(value, width, height, active, secret).rows;
}

export function selectionRows(items: {label: string; description?: string | null; current?: boolean}[], index: number, width: number, height: number, viewport?: SelectionViewport): string[] {
  const perItem = width >= 45 ? 2 : 1;
  const count = Math.max(1, Math.floor(height / perItem));
  let offset = Math.max(0, Math.min(index - count + 1, items.length - count)) * perItem;
  if (viewport) {
    viewport.height = Math.max(1, height); viewport.total = items.length * perItem; viewport.perItem = perItem;
    offset = viewport.offset;
    if (viewport.follow) {
      if (index * perItem < offset) offset = index * perItem;
      else if ((index + 1) * perItem > offset + height) offset = (index + 1) * perItem - height;
    }
    viewport.offset = offset = Math.max(0, Math.min(offset, viewport.total - viewport.height));
  }
  const start = Math.floor(offset / perItem);
  const rows: string[] = [];
  for (let i = start; i < Math.min(items.length, start + Math.ceil(height / perItem) + 1); i++) {
    const item = items[i];
    const title = `${i === index ? '▸' : ' '} ${safe(item.label)}${item.current ? ' ✓' : ''}`;
    rows.push(i === index ? paint.selected(paint.bold(fit(title, width))) : line((item.current ? paint.success : paint.secondary)(title), width));
    if (perItem === 2) {
      const description = '  ' + safe(item.description ?? '');
      rows.push(i === index ? paint.selected(fit(description, width)) : line(paint.muted(description), width));
    }
  }
  if (!items.length) rows.push(paint.muted('No matching items'));
  return rows.slice(offset % perItem, offset % perItem + height);
}

export function statusLine(parts: string[], width: number) {
  let result = '';
  for (const part of parts.filter(Boolean)) {
    const next = result ? `${result} · ${safe(part)}` : safe(part);
    if (stringWidth(next) > width && result) break;
    result = next;
  }
  return line(result, width);
}
