import type {Key} from 'ink';
import stringWidth from 'string-width';

const segmenter = new Intl.Segmenter(undefined, {granularity: 'grapheme'});
export const graphemes = (text: string) => Array.from(segmenter.segment(text), item => item.segment);
export interface Editor {text: string; cursor: number; preferredColumn?: number}
export const editor = (text = ''): Editor => ({text, cursor: graphemes(text).length});

export interface EditorLayout {chars: string[]; display: string[]; positions: {x: number; y: number}[]; rows: {text: string; start: number; end: number}[]}
const layouts: {text: string; width: number; secret: boolean; layout: EditorLayout}[] = [];

/** Share the same grapheme/cell geometry between rendering and vertical movement. */
export function layoutEditor(text: string, width: number, secret = false): EditorLayout {
  width = Math.max(1, width);
  const cached = layouts.find(item => item.text === text && item.width === width && item.secret === secret);
  if (cached) return cached.layout;
  const chars = graphemes(text);
  const display = chars.map(char => secret ? '•' : char === '\t' ? '    ' : char.replace(/[\x00-\x09\x0b-\x1f\x7f-\x9f]/g, ''));
  const widths = display.map(char => stringWidth(char));
  const words = new Map<number, number>();
  for (let start = 0; start < display.length;) {
    if (/\s/u.test(display[start])) {start++; continue;}
    let end = start, cells = 0;
    while (end < display.length && !/\s/u.test(display[end])) cells += widths[end++];
    words.set(start, cells); start = end;
  }
  const rows = [{text: '', start: 0, end: 0}];
  const positions: {x: number; y: number}[] = [];
  let x = 0, y = 0;
  const next = (index: number, end = index) => {rows[y].end = end; rows.push({text: '', start: index, end: index}); x = 0; y++;};
  for (let i = 0; i < chars.length; i++) {
    const part = display[i], cells = widths[i], word = words.get(i) ?? 0;
    if (x >= width || part !== '\n' && x > 0 && (x + cells > width || word <= width && x + word > width)) next(i);
    positions.push({x, y});
    if (part === '\n') next(i + 1, i);
    else {rows[y].text += part; rows[y].end = i + 1; x += cells;}
  }
  if (x >= width) next(chars.length);
  positions.push({x, y});
  const layout = {chars, display, positions, rows};
  // Masked input must not remain in a module-level cache after its prompt closes.
  if (!secret) {layouts.unshift({text, width, secret, layout}); layouts.length = Math.min(layouts.length, 2);}
  return layout;
}

export function edit(value: Editor, input: string, key: Partial<Key>, width = 80, secret = false): Editor {
  if (key.upArrow || key.downArrow) {
    const layout = layoutEditor(value.text, width, secret);
    const position = layout.positions[value.cursor];
    const column = value.preferredColumn ?? position.x;
    const row = position.y + (key.upArrow ? -1 : 1);
    if (row < 0 || row >= layout.rows.length) return value;
    let cursor = layout.rows[row].start, distance = Infinity;
    for (let i = cursor; i < layout.positions.length && layout.positions[i].y <= row; i++) {
      const point = layout.positions[i], delta = Math.abs(point.x - column);
      if (point.y === row && delta < distance) {cursor = i; distance = delta;}
    }
    return {...value, cursor, preferredColumn: column};
  }
  const chars = graphemes(value.text);
  let cursor = value.cursor;
  if (key.ctrl && input === 'a') cursor = 0;
  else if (key.ctrl && input === 'e') cursor = chars.length;
  else if (key.home) cursor = cursor ? chars.lastIndexOf('\n', cursor - 1) + 1 : 0;
  else if (key.end) {const end = chars.indexOf('\n', cursor); cursor = end < 0 ? chars.length : end;}
  else if (key.ctrl && input === 'u') {chars.splice(0, cursor); cursor = 0;}
  else if (key.ctrl && input === 'k') chars.splice(cursor);
  else if (key.ctrl && input === 'w') {
    let start = cursor;
    while (start > 0 && /\s/u.test(chars[start - 1])) start--;
    while (start > 0 && !/\s/u.test(chars[start - 1])) start--;
    chars.splice(start, cursor - start); cursor = start;
  } else if (key.leftArrow) cursor = Math.max(0, cursor - 1);
  else if (key.rightArrow) cursor = Math.min(chars.length, cursor + 1);
  else if (key.backspace) {if (cursor) chars.splice(--cursor, 1);}
  else if (key.delete) chars.splice(cursor, 1);
  else if (!key.ctrl && !key.meta && !key.escape && !key.tab && input) {
    const inserted = graphemes(input.replace(/\r\n?/g, '\n').replace(/[\x00-\x08\x0b-\x1f\x7f]/g, ''));
    chars.splice(cursor, 0, ...inserted); cursor += inserted.length;
  }
  return {text: chars.join(''), cursor};
}
