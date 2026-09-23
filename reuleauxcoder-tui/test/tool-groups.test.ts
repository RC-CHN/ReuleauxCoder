import assert from 'node:assert/strict';
import test from 'node:test';
import stringWidth from 'string-width';
import {decode, record, type Json} from '@reuleauxcoder/client';
import {SessionStore} from '../src/state/session.js';
import {safe} from '../src/ui/format.js';
import {TranscriptLayout} from '../src/ui/viewport.js';

function transcript() {
  const session = new SessionStore();
  const layout = new TranscriptLayout();
  const event = (name: string, fields: {[key: string]: Json}) => session.runtime({payload: decode(record(name, fields))});
  return {
    session, layout, event,
    start: (id: string, name: string, args: {[key: string]: Json}) => event('ToolCallStarted', {tool_call_id: id, tool_name: name, arguments: args}),
    finish: (id: string, name: string, outcome: {[key: string]: Json}) => event('ToolCallFinished', {tool_call_id: id, tool_name: name, outcome}),
    text: (expanded = false) => safe(layout.render(session.cells, 120, 1000, 0, expanded).rows.join('\n')),
  };
}

test('consecutive process polls share one waiting surface while F4 keeps all results', () => {
  const t = transcript();
  for (let i = 0; i < 3; i++) {
    t.start(`poll-${i}`, 'shell_session', {session_id: 'proc_test', action: 'poll'});
    assert(!t.text().includes('Waited for'), 'the active poll lives in the activity line');
    t.finish(`poll-${i}`, 'shell_session', {status: 'succeeded', summary: 'Process running', stdout: `batch-${i}`});
  }
  assert.equal((t.text().match(/Waited for process/g) ?? []).length, 1);
  assert(t.text().includes('3 checks'));
  for (let i = 0; i < 3; i++) assert(t.text(true).includes(`batch-${i}`));
  t.start('write', 'shell_session', {session_id: 'proc_test', action: 'write', chars: 'hello'});
  t.finish('write', 'shell_session', {status: 'failed', summary: 'stdin is closed'});
  assert(t.text().includes('stdin is closed'));
  t.start('later', 'shell_session', {session_id: 'proc_test', action: 'poll'});
  t.finish('later', 'shell_session', {status: 'succeeded', summary: 'Process running'});
  assert.equal((t.text().match(/Waited for process/g) ?? []).length, 2, 'writes split poll streaks');
});

test('tool groups keep summaries and failures while F4 restores every original record', () => {
  const t = transcript();
  t.start('read-1', 'read_file', {file_path: 'old.txt'});
  t.finish('read-1', 'read_file', {status: 'succeeded', summary: 'Read 20 lines (400 chars) from old.txt', content: 'older file content'});
  t.event('ReasoningDelta', {text: 'retained reasoning', display_mode: 'hidden'});
  t.start('read-2', 'read_file', {file_path: 'package.json'});
  t.finish('read-2', 'read_file', {status: 'succeeded', summary: 'Read 34 lines (808 chars) from package.json', content: 'latest file content'});
  let compact = t.text();
  assert(compact.includes('2 tools · 2 completed · 2 reads'));
  assert(compact.includes('34 lines (808 chars) from package.json'));
  assert(!compact.includes('old.txt'));
  assert(!compact.includes('file content'));
  assert(!compact.includes('reasoning'));

  t.start('test', 'shell', {command: 'npm test'});
  t.event('ToolOutputDelta', {tool_call_id: 'test', text: 'line 1\nline 2\nline 3\nline 4\nline 5\n'});
  compact = t.text();
  assert(compact.includes('1 running'));
  assert(!compact.includes('line 2'));
  for (const n of [3, 4, 5]) assert(compact.includes(`line ${n}`));
  assert(t.text(true).includes('line 1'), 'expanded running tools retain output as well as arguments');
  t.finish('test', 'shell', {status: 'failed', summary: 'Tests failed', stderr: 'full failure detail', exit_code: 1});
  t.start('read-3', 'read_file', {file_path: 'fix.ts'});
  t.finish('read-3', 'read_file', {status: 'succeeded', summary: 'Read 10 lines (100 chars) from fix.ts', content: 'fix file content'});
  compact = t.text();
  assert(compact.includes('Tests failed'), 'a newer successful tool does not hide failure');
  assert(compact.includes('fix.ts'));
  assert(!compact.includes('line 5'), 'finished shell output folds back to a summary');
  const before = structuredClone(t.session.cells);
  const full = t.text(true);
  for (const content of ['older file content', 'latest file content', 'retained reasoning', 'full failure detail', 'npm test', 'fix file content']) assert(full.includes(content), content);
  assert(full.indexOf('older file content') < full.indexOf('retained reasoning'));
  assert(full.indexOf('retained reasoning') < full.indexOf('latest file content'));
  assert.equal(t.text(), compact, 'F4 returns to the same compact projection');
  assert.deepEqual(structuredClone(t.session.cells), before);
  const narrow = t.layout.render(t.session.cells, 30, 5, null, false);
  assert(narrow.rows.length <= 5);
  assert(narrow.rows.every(row => stringWidth(row) <= 30));
});

test('parallel tools stay visible until completion and visible messages separate groups', () => {
  const t = transcript();
  t.start('slow', 'shell', {command: 'slow command'});
  t.start('fast', 'read_file', {file_path: 'fast.txt'});
  t.finish('fast', 'read_file', {status: 'succeeded', summary: 'Fast read done'});
  assert(t.text().includes('slow command'));
  assert(t.text().includes('1 running'));
  t.finish('slow', 'shell', {status: 'succeeded', summary: 'Slow command done'});
  assert(t.text().includes('2 completed'));
  assert(!t.text().includes('running'));
  t.event('AssistantContentDelta', {text: 'Now checking another file.'});
  t.start('next', 'read_file', {file_path: 'next.txt'});
  t.finish('next', 'read_file', {status: 'succeeded', summary: 'Next file read'});
  const compact = t.text();
  assert(!compact.includes('3 tools'));
  assert(compact.indexOf('2 tools') < compact.indexOf('Now checking'));
  assert(compact.indexOf('Now checking') < compact.indexOf('Next file read'));
  t.session.clear();
  assert.equal(t.text(), '');
});
