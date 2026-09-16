import type {Token, Tokens} from 'marked';
import stringWidth from 'string-width';
import wrapAnsi from 'wrap-ansi';
import {fit, paint} from './theme.js';

const PREVIEW_LINES = 3;
const MIN_COLUMN_WIDTH = 8;

/** Widths depend on the header and viewport, so continued table blocks agree. */
export function tableRows(table: Tokens.Table, width: number, expanded: boolean, inline: (tokens: Token[]) => string): string[] {
  const columns = table.header.length;
  const budget = width - (columns - 1) * 3;
  const wrap = (text: string, size: number) => wrapAnsi(text, Math.max(1, size), {hard: true, trim: false}).split('\n');
  const preview = (text: string, size: number) => {
    const rows = wrap(text, size);
    return expanded || rows.length <= PREVIEW_LINES ? rows
      : [...rows.slice(0, PREVIEW_LINES), paint.muted(`… +${rows.length - PREVIEW_LINES}`)];
  };
  const headers = table.header.map(cell => inline(cell.tokens));
  if (budget < columns * MIN_COLUMN_WIDTH) {
    if (!table.rows.length) return headers.flatMap(header => preview(paint.info(paint.bold(header)), width));
    return table.rows.flatMap((row, index) => [
      ...(index ? [paint.border('─'.repeat(width))] : []),
      ...row.flatMap((cell, column) => [
        ...preview(paint.info(paint.bold(headers[column])) + ':', width),
        ...preview(inline(cell.tokens), Math.max(1, width - 2)).map(line => (width > 2 ? '  ' : '') + line),
      ]),
    ]);
  }
  const widths = headers.map((_, column) => Math.floor(budget / columns) + Number(column < budget % columns));
  const rowLines = (row: Tokens.TableCell[], header = false) => {
    const cells = row.map((cell, column) => preview(header ? paint.info(paint.bold(headers[column])) : inline(cell.tokens), widths[column]));
    return Array.from({length: Math.max(...cells.map(rows => rows.length))}, (_, line) => cells.map((rows, column) => {
      const text = rows[line] ?? '';
      const padding = Math.max(0, widths[column] - stringWidth(text));
      const alignment = table.align[column];
      const left = alignment === 'right' ? padding : alignment === 'center' ? Math.floor(padding / 2) : 0;
      return fit(' '.repeat(left) + text, widths[column]);
    }).join(paint.border(' │ ')));
  };
  return [
    ...rowLines(table.header, true),
    widths.map(size => paint.border('─'.repeat(size))).join(paint.border('─┼─')),
    ...table.rows.flatMap(row => rowLines(row)),
  ];
}
