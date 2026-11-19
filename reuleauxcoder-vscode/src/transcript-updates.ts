import type {ChatCell, HostSnapshot} from './shared.js';

type Edit = {index: number; cell: ChatCell} | {index: number; append: string};
export interface SnapshotUpdate {
  baseRevision: number;
  state: Omit<HostSnapshot, 'cells'>;
  drop: number;
  length: number;
  edits: Edit[];
}

function sameMetadata(a: ChatCell, b: ChatCell): boolean {
  return a.id === b.id && a.role === b.role && a.status === b.status && a.title === b.title
    && a.detail === b.detail && a.images === b.images && a.members === b.members && a.merged === b.merged;
}

// Transcript mutates its flat cells in place. Keep each transmitted revision
// stable, sharing unchanged cells across broadcasts and across attached views.
export function captureCells(live: ChatCell[], previous: ChatCell[]): ChatCell[] {
  const drop = firstRetained(previous, live);
  let captured: ChatCell[] | undefined;
  if (drop || live.length !== previous.length) captured = previous.slice(drop, drop + live.length);
  for (let i = 0; i < live.length; i++) {
    const cell = live[i], old = previous[drop + i];
    if (old && sameMetadata(old, cell) && old.text === cell.text) continue;
    captured ??= previous.slice();
    captured[i] = {...cell};
  }
  return captured ?? previous;
}

function firstRetained(base: ChatCell[], cells: ChatCell[]): number {
  if (!base.length || !cells.length || base[0].id === cells[0].id) return 0;
  const first = base.findIndex(cell => cell.id === cells[0].id);
  return first < 0 ? base.length : first;
}

export function snapshotUpdate(base: HostSnapshot, next: HostSnapshot): SnapshotUpdate {
  const {cells, ...state} = next;
  const drop = firstRetained(base.cells, cells);
  const edits: Edit[] = [];
  if (base.cells !== cells) {
    for (let index = 0; index < cells.length; index++) {
      const cell = cells[index], old = base.cells[index + drop];
      if (cell === old) continue;
      if (old && sameMetadata(old, cell) && cell.text.startsWith(old.text)) {
        edits.push({index, append: cell.text.slice(old.text.length)});
      } else {
        edits.push({index, cell});
      }
    }
  }
  return {state, baseRevision: base.revision, drop, length: cells.length, edits};
}

export function applySnapshotUpdate(base: HostSnapshot | undefined, update: SnapshotUpdate): HostSnapshot | undefined {
  if (!base || base.revision !== update.baseRevision || base.hostId !== update.state.hostId
      || base.generation !== update.state.generation) return undefined;
  let cells = base.cells;
  if (update.edits.length || update.drop || cells.length !== update.length) {
    cells = cells.slice(update.drop, update.drop + update.length);
    for (const edit of update.edits) {
      if ('cell' in edit) cells[edit.index] = edit.cell;
      else cells[edit.index] = {...cells[edit.index], text: cells[edit.index].text + edit.append};
    }
  }
  return {...update.state, cells};
}
