import assert from 'node:assert/strict';
import test from 'node:test';
import {foldToolCells, type ToolGroupCache} from '../src/core/tool-groups.js';
import {captureCells, snapshotUpdate, applySnapshotUpdate} from '../src/transcript-updates.js';
import type {ChatCell, HostSnapshot} from '../src/shared.js';

function snapshot(revision: number, cells: ChatCell[]): HostSnapshot {
  return {hostId: 'host', generation: 1, revision, cells, workspace: '/w', environment: 'Local', phase: 'ready',
    model: 'test', running: true, draftRevision: 0, draftText: '', draftItems: [], reviews: []};
}

test('different view baselines reconstruct streamed text without retransmitting earlier turns', () => {
  const live: ChatCell[] = Array.from({length: 600}, (_, i) => ({id: String(i), role: 'assistant', text: 'x'.repeat(4096)}));
  const first = snapshot(4, captureCells(live, []));
  live[599].text += 'a';
  const middle = snapshot(7, captureCells(live, first.cells));
  live[599].text += 'b';
  const last = snapshot(8, captureCells(live, middle.cells));
  for (const [baseline, append] of [[first, 'ab'], [middle, 'b']] as const) {
    const update = snapshotUpdate(baseline, last);
    assert.deepEqual(update.edits, [{index: 599, append}]);
    assert(Buffer.byteLength(JSON.stringify(update)) < 1024);
    assert.deepEqual(applySnapshotUpdate(baseline, update), last);
  }
  const update = snapshotUpdate(middle, last);
  for (const stale of [undefined, first, {...middle, generation: 2}, {...middle, hostId: 'reconnected'}]) {
    assert.equal(applySnapshotUpdate(stale, update), undefined);
  }
  const unchanged = captureCells(live, last.cells);
  assert.equal(unchanged, last.cells);
  const stateOnly = {...last, revision: 10, draftText: 'unsent', cells: unchanged};
  assert.equal(applySnapshotUpdate(last, snapshotUpdate(last, stateOnly))?.cells, last.cells);
});

test('cell replacement, late tool results, trimming and transcript resets round-trip', () => {
  const live: ChatCell[] = [
    {id: 'user', role: 'user', text: 'test'}, {id: 'shell1', role: 'tool', text: 'ok', status: 'succeeded'},
    {id: 'shell2', role: 'tool', text: 'waiting', status: 'running'}, {id: 'answer', role: 'assistant', text: ''},
  ];
  const groups: ToolGroupCache = new WeakMap();
  let state = snapshot(1, captureCells(live, []));
  const group = foldToolCells(state.cells, groups)[1];
  assert.equal(group.role, 'tool-group');
  function advance() {
    const next = snapshot(state.revision + 1, captureCells(live, state.cells));
    const received = applySnapshotUpdate(state, snapshotUpdate(state, next));
    assert.deepEqual(received, next);
    state = received!;
    return foldToolCells(state.cells, groups);
  }
  live[3].text = 'working';
  assert.equal(advance()[1], group);
  live[2].status = 'timed_out'; live[2].text = 'timeout';
  assert.deepEqual(advance().map(cell => cell.id), ['user', 'shell1', 'shell2', 'answer']);
  live.shift(); live.push({id: 'end', role: 'notice', text: 'done'});
  assert.deepEqual(advance(), live);
  live.length = 0;
  assert.deepEqual(advance(), []);
  live.push({id: 'restored', role: 'user', text: 'resume'});
  assert.deepEqual(advance(), live);
});
