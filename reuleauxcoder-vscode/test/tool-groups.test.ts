import assert from 'node:assert/strict';
import test from 'node:test';
import {foldToolCells} from '../src/core/tool-groups.js';
import type {ChatCell} from '../src/shared.js';

const tool = (id: string, title = 'read_file', status = 'complete', detail = '{}'): ChatCell => ({id, role: 'tool', text: `out-${id}`, title, status, detail});
const reasoning = (id: string): ChatCell => ({id, role: 'reasoning', text: 'thinking'});
const assistant = (id: string): ChatCell => ({id, role: 'assistant', text: 'answer'});
const notice = (id: string): ChatCell => ({id, role: 'notice', text: 'note'});
const poll = (id: string, session: string, status = 'complete'): ChatCell => tool(id, 'shell_session', status, JSON.stringify({action: 'poll', session_id: session}));

test('consecutive tools and reasoning fold into one group with a stable id and counts', () => {
  const folded = foldToolCells([assistant('a1'), tool('t1'), reasoning('r1'), tool('t2', 'grep'), tool('t3'), assistant('a2')]);
  assert.deepEqual(folded.map(cell => cell.id), ['a1', 'group:t1', 'a2']);
  const group = folded[1];
  assert.equal(group.role, 'tool-group');
  assert.equal(group.status, 'complete');
  assert.equal(group.text, '3 tool calls · 3 completed · 2 reads · 1 searches · 1 reasoning');
  assert.deepEqual(group.members?.map(member => member.id), ['t1', 'r1', 't2', 't3']);
});

test('single tools, standalone reasoning and non-tool cells stay flat', () => {
  const cells = [tool('t1'), reasoning('r1'), reasoning('r2'), notice('n1')];
  assert.deepEqual(foldToolCells(cells).map(cell => cell.id), ['t1', 'r1', 'r2', 'n1']);
});

test('failed calls break out and split the run, keeping order', () => {
  const folded = foldToolCells([tool('t1'), tool('t2'), tool('t3', 'read_file', 'failed'), tool('t4'), tool('t5')]);
  assert.deepEqual(folded.map(cell => cell.id), ['group:t1', 't3', 'group:t4']);
  assert.equal(folded[1].role, 'tool');
  assert.equal(folded[1].status, 'failed');
});

test('denied and cancelled calls also break out', () => {
  const folded = foldToolCells([tool('t1'), tool('t2', 'edit_file', 'denied'), tool('t3', 'edit_file', 'cancelled')]);
  assert.deepEqual(folded.map(cell => cell.id), ['t1', 't2', 't3']);
});

test('a running member marks the group running and reports progress', () => {
  const folded = foldToolCells([tool('t1'), tool('t2', 'shell', 'running')]);
  const group = folded[0];
  assert.equal(group.status, 'running');
  assert.equal(group.text, '2 tool calls · 1 completed · 1 running · 1 reads · 1 commands');
});

test('consecutive polls of one process merge into the latest entry with a count', () => {
  const folded = foldToolCells([poll('p1', 'proc-a'), poll('p2', 'proc-a'), poll('p3', 'proc-a', 'running')]);
  const group = folded[0];
  assert.equal(group.role, 'tool-group');
  assert.equal(group.members?.length, 1);
  const merged = group.members![0];
  assert.equal(merged.id, 'p1');
  assert.equal(merged.merged, 3);
  assert.equal(merged.text, 'out-p3');
  assert.equal(merged.status, 'running');
  assert.deepEqual(merged.members?.map(member => member.text), ['out-p1', 'out-p2', 'out-p3']);
  assert.equal(group.text, '3 tool calls · 2 completed · 1 running · 3 commands');
});

test('polls of different processes and non-poll calls do not merge', () => {
  const folded = foldToolCells([poll('p1', 'proc-a'), poll('p2', 'proc-b'), tool('t3', 'shell_session', 'complete', JSON.stringify({action: 'start', command: 'sleep 1'}))]);
  const group = folded[0];
  assert.deepEqual(group.members?.map(member => member.id), ['p1', 'p2', 't3']);
  assert.equal(group.members?.every(member => !member.merged), true);
});

test('the projection never mutates source cells', () => {
  const cells = [poll('p1', 'proc-a'), poll('p2', 'proc-a')];
  const before = JSON.stringify(cells);
  foldToolCells(cells);
  assert.equal(JSON.stringify(cells), before);
  assert.equal(cells[0].merged, undefined);
});
