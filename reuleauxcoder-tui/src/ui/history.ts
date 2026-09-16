import type {HistoryBrowser} from '../state/history-browser.js';
import type {PanelRows} from './panels.js';
import {safe} from './format.js';
import {inputLayout, selectionRows} from './viewport.js';
import {keyHint, paint} from './theme.js';
import {TextLayout} from './text-layout.js';

export function historyRows(browser: HistoryBrowser, width: number, height: number, layout = new TextLayout()): PanelRows {
  const page = browser.page;
  const progress = page && page.indexed_bytes < page.source_bytes
    ? `Index ${page.indexed_bytes}/${page.source_bytes} bytes${page.awaiting_tail ? ' · awaiting complete record' : ''}` : '';
  const title = browser.artifact?.artifact_ref ?? (browser.parameters.event_id ? `Event ${browser.parameters.event_id}` : browser.operation === 'search' ? `Search: ${browser.parameters.pattern}` : browser.parameters.messages_only === false ? 'Session events' : 'Session history');
  const hint = browser.search ? [keyHint('Enter', 'search'), keyHint('Esc', 'cancel')]
    : [keyHint('n/p', 'next / previous page'), keyHint('/', 'search'), ...(browser.detailed ? [] : [keyHint('↑↓ Enter', 'read record'), keyHint('m', 'messages / events')]), ...(browser.current?.artifact_refs.length ? [keyHint('a', 'artifacts')] : []), keyHint('r', 'refresh'), keyHint('Esc', 'back')];
  const header = [browser.error && paint.error(safe(browser.error)), progress && paint.info(progress), page?.skipped_records && paint.warning(`${page.skipped_records} malformed ledger records skipped`), browser.loading && paint.muted('Loading history…')].filter(Boolean) as string[];
  const search = browser.search ? inputLayout(browser.search, width - 2, 1) : undefined;
  const cursor = search?.cursor && {...search.cursor, x: search.cursor.x + 2, y: search.cursor.y + header.length};
  if (search) header.push(...search.rows.map(row => '⌕ ' + row));
  let rows: string[];
  if (browser.detailed) {
    const record = browser.current, artifact = browser.artifact;
    const body = artifact?.content ?? record?.content ?? '';
    const offset = artifact?.offset ?? record?.offset ?? 0;
    const total = artifact?.total_chars ?? record?.total_chars;
    const metadata = [record && `${record.role ?? record.kind} · seq ${record.seq}${record.turn_id ? ' · ' + record.turn_id : ''}`, `Chars ${offset}–${offset + body.length}${total == null ? '' : '/' + total}`].filter(Boolean).join('\n');
    rows = layout.rows(artifact ?? record, width, () => paint.info(safe(metadata)) + '\n\n' + safe(body));
    browser.offset = Math.min(browser.offset, Math.max(0, rows.length - Math.max(1, height - header.length)));
    rows = rows.slice(browser.offset, browser.offset + height - header.length);
  } else {
    const records = page?.records ?? [];
    rows = selectionRows(records.map(record => ({label: `${record.seq} · ${record.role ?? record.kind}${record.turn_id ? ' · ' + record.turn_id : ''}`, description: record.content.slice(0, 300).replace(/\s+/g, ' ')})), browser.index, width, Math.max(1, height - header.length));
    if (!records.length && !browser.loading) rows = [paint.muted(browser.nextCursor ? 'No matches in this scan. Next page continues searching.' : progress ? 'Refresh when the writer completes the record.' : 'No records in this page.')];
  }
  return {title, rows: [...header, ...rows].slice(0, height), cursor: cursor && cursor.y < height ? cursor : undefined, hint, navigation: `Page ${browser.cursors.length}${browser.nextCursor ? ' · more' : ''}`};
}
