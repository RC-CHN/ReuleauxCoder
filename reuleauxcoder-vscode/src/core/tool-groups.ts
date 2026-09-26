import {t, type MessageKey} from '../i18n.js';
import type {ChatCell} from '../shared.js';

/**
 * Folding projection over the flat transcript: consecutive successful tool calls
 * and their reasoning become one serializable group cell. Source cells stay
 * intact in Transcript; failed calls always break out on their own row.
 */

const FAILED = new Set(['failed', 'denied', 'cancelled', 'timeout', 'error']);
const CATEGORIES: Record<string, MessageKey> = {
  read_file: '{0} reads', grep: '{0} searches', glob: '{0} searches',
  list_file: '{0} listings', shell: '{0} commands', shell_session: '{0} commands',
  edit_file: '{0} edits', write_file: '{0} writes',
};

const isFailed = (cell: ChatCell): boolean => FAILED.has(cell.status ?? '');
const isRunning = (cell: ChatCell): boolean => cell.status === 'running';

function pollSession(cell: ChatCell): string | undefined {
  if (cell.role !== 'tool' || cell.title !== 'shell_session' || !cell.detail) return undefined;
  try {
    const args = JSON.parse(cell.detail) as Record<string, unknown>;
    if (args?.action === 'poll' && typeof args.session_id === 'string') return args.session_id;
  } catch { /* detail is display JSON; unparseable arguments never merge */ }
  return undefined;
}

/** Consecutive polls of one process collapse into the latest entry with a check count. */
function mergePolls(run: ChatCell[]): ChatCell[] {
  const merged: ChatCell[] = [];
  for (const cell of run) {
    const session = pollSession(cell);
    const previous = merged.at(-1);
    if (session && previous && pollSession(previous) === session) {
      previous.members ??= [{...previous}];
      previous.members.push({...cell});
      previous.text = cell.text; previous.status = cell.status; previous.detail = cell.detail;
      previous.merged = (previous.merged ?? 1) + 1;
      continue;
    }
    merged.push({...cell});
  }
  return merged;
}

function groupCell(run: ChatCell[], members: ChatCell[]): ChatCell {
  const tools = run.filter(cell => cell.role === 'tool');
  const reasoning = run.length - tools.length;
  const running = tools.filter(isRunning).length;
  const completed = tools.length - running;
  const parts = [t('{0} tool calls', tools.length)];
  if (completed) parts.push(t('{0} completed', completed));
  if (running) parts.push(t('{0} running', running));
  const counts = new Map<MessageKey, number>();
  for (const cell of tools) {
    const key = CATEGORIES[cell.title ?? ''];
    if (key) counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  for (const [key, count] of counts) parts.push(t(key, count));
  if (reasoning) parts.push(t('{0} reasoning', reasoning));
  return {id: `group:${run[0].id}`, role: 'tool-group', text: parts.join(' · '), status: running ? 'running' : 'complete', members};
}

export function foldToolCells(cells: ChatCell[]): ChatCell[] {
  const folded: ChatCell[] = [];
  let run: ChatCell[] = [];
  const flush = (): void => {
    if (run.filter(cell => cell.role === 'tool').length >= 2) folded.push(groupCell(run, mergePolls(run)));
    else folded.push(...run);
    run = [];
  };
  for (const cell of cells) {
    if (cell.role === 'reasoning' || cell.role === 'tool' && !isFailed(cell)) {run.push(cell); continue;}
    flush();
    folded.push(cell);
  }
  flush();
  return folded;
}
