import type {Key} from 'ink';
import stringWidth from 'string-width';

const segmenter = new Intl.Segmenter(undefined, {granularity: 'grapheme'});
// Printable ASCII and BMP Han characters each form one grapheme in this subset.
export const graphemes = (text: string) => /^[\x20-\x7e\n\t\u3400-\u4dbf\u4e00-\u9fff]*$/u.test(text)
  ? text.split('') : Array.from(segmenter.segment(text), item => item.segment);
export interface Editor {text: string; cursor: number; preferredColumn?: number}
export const editor = (text = ''): Editor => ({text, cursor: graphemes(text).length});

export interface EditorLayout {chars: string[]; display: string[]; positions: {x: number; y: number}[]; rows: {text: string; start: number; end: number}[]}
const layouts: {text: string; width: number; layout: EditorLayout; paragraphs: Map<number, {index: number; row: number}>}[] = [];

/** Share the same grapheme/cell geometry between rendering and vertical movement. */
export function layoutEditor(text: string, width: number, secret = false): EditorLayout {
  width = Math.max(1, width);
  const cached = !secret && layouts.find(item => item.text === text && item.width === width);
  if (cached) return cached.layout;
  const previous = !secret && layouts.find(item => item.width === width);
  const paragraphs = new Map<number, {index: number; row: number}>();
  let offset = 0, startIndex = 0, startRow = 0;
  if (previous) {
    let common = 0;
    const limit = Math.min(text.length, previous.text.length);
    while (common < limit && text.charCodeAt(common) === previous.text.charCodeAt(common)) common++;
    for (const [end, point] of previous.paragraphs) {
      if (end > common) break;
      paragraphs.set(end, point);
      offset = end; startIndex = point.index; startRow = point.row;
    }
  }
  const tail = graphemes(text.slice(offset));
  const display = tail.map(char => secret ? '•' : char === '\t' ? '    ' : char.replace(/[\x00-\x09\x0b-\x1f\x7f-\x9f]/g, ''));
  const cellWidths = new Map<string, number>();
  const widths = display.map(char => {
    let cells = cellWidths.get(char);
    if (cells === undefined) {cells = stringWidth(char); cellWidths.set(char, cells);}
    return cells;
  });
  const words = new Map<number, number>();
  for (let start = 0; start < display.length;) {
    if (/\s/u.test(display[start])) {start++; continue;}
    let end = start, cells = 0;
    while (end < display.length && !/\s/u.test(display[end])) cells += widths[end++];
    words.set(start, cells); start = end;
  }
  const rows = previous ? previous.layout.rows.slice(0, startRow) : [];
  rows.push({text: '', start: startIndex, end: startIndex});
  const positions = previous ? previous.layout.positions.slice(0, startIndex) : [];
  let x = 0, y = startRow;
  const next = (index: number, end = index) => {rows[y].end = end; rows.push({text: '', start: index, end: index}); x = 0; y++;};
  for (let i = 0; i < tail.length; i++) {
    const index = startIndex + i;
    offset += tail[i].length;
    const part = display[i], cells = widths[i], word = words.get(i) ?? 0;
    if (x >= width || part !== '\n' && x > 0 && (x + cells > width || word <= width && x + word > width)) next(index);
    positions.push({x, y});
    if (part === '\n') {
      next(index + 1, index);
      paragraphs.set(offset, {index: index + 1, row: y});
    } else {rows[y].text += part; rows[y].end = index + 1; x += cells;}
  }
  const chars = previous && startIndex ? previous.layout.chars.slice(0, startIndex).concat(tail) : tail;
  if (x >= width) next(chars.length);
  positions.push({x, y});
  const layout = {chars, display: previous && startIndex ? previous.layout.display.slice(0, startIndex).concat(display) : display, positions, rows};
  // Masked input must not remain in a module-level cache after its prompt closes.
  if (!secret) {layouts.unshift({text, width, layout, paragraphs}); layouts.length = Math.min(layouts.length, 2);}
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
  const cached = !secret && layouts.find(item => item.text === value.text);
  const chars = cached ? cached.layout.chars.slice() : graphemes(value.text);
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
    const inserted = input.replace(/\r\n?/g, '\n').replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '');
    const lineStart = cursor ? chars.lastIndexOf('\n', cursor - 1) + 1 : 0;
    const line = chars.slice(lineStart, cursor).join('') + inserted;
    return {text: chars.slice(0, lineStart).join('') + line + chars.slice(cursor).join(''), cursor: lineStart + graphemes(line).length};
  }
  return {text: chars.join(''), cursor};
}
