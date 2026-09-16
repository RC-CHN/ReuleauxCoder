import {marked} from 'marked';

export interface MarkdownContext {fence?: string; table?: string}

export function tableHeader(header: string, delimiter: string): string | undefined {
  header = header.replace(/\r$/, ''); delimiter = delimiter.replace(/\r$/, '');
  if (!/^[ |:\t-]+$/.test(delimiter) || !delimiter.includes('-')) return;
  const source = header + '\n' + delimiter + '\n';
  return marked.lexer(source)[0]?.type === 'table' ? source : undefined;
}

/** Carry only syntax needed by the next bounded history block, never old rows. */
export function markdownContext(text: string, before: MarkdownContext): MarkdownContext {
  let {fence, table} = before;
  let previous = '';
  const lines = text.split('\n');
  if (text.endsWith('\n')) lines.pop();
  for (const raw of lines) {
    const line = raw.replace(/\r$/, '');
    const boundary = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (boundary) {
      if (!fence) {fence = boundary[1] + boundary[2]; table = undefined;}
      else if (boundary[1][0] === fence[0] && boundary[1].length >= fence.match(/^(`+|~+)/)![0].length && !boundary[2].trim()) fence = undefined;
    } else if (!fence) {
      if (table && (!line.trim() || marked.Lexer.rules.block.gfm.table.exec(table + line + '\n')?.[0].length !== table.length + line.length + 1)) table = undefined;
      if (!table) table = tableHeader(previous, line);
    }
    previous = line;
  }
  return {fence, table};
}
