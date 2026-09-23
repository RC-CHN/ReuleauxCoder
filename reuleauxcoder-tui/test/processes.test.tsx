import React from 'react';
import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import {render} from 'ink-testing-library';
import stringWidth from 'string-width';
import {RuntimeClient} from '@reuleauxcoder/client';
import {RpcPeer} from '@reuleauxcoder/client/node';
import {decode, record} from '@reuleauxcoder/client';
import {TuiController} from '../src/state/controller.js';
import {updateProcess, processElapsed} from '../src/state/processes.js';
import {App} from '../src/ui/App.js';
import {sidebarRows} from '../src/ui/sidebar.js';
import {safe} from '../src/ui/format.js';
import {backend, until} from './helpers.js';
import type {ListScreen} from '../src/state/controller.js';

const processEvent = (overrides = {}) => ({process_session_id: 'proc_test', command: 'npm run dev', state: 'running', change: 'published', elapsed_seconds: 0, stdout: '', stderr: '', ...overrides});

test('process panels keep identity, output and scroll position through polling, confirmation and exit', async t => {
  const b = await backend(); t.after(() => b.close());
  const ids = await b.peer.request('test.processes') as string[];
  const c = b.controller;
  const app = render(<App controller={c}/>); t.after(() => app.cleanup());
  const screen = () => c.screen as ListScreen;
  await c.openMenu(c.menus.find(menu => menu.name === '/ps')!);
  await until(() => screen()?.panel?.view_type === 'process_sessions');
  assert.equal(screen().items[0].label, screen().items[1].label, 'same commands remain separate processes');
  await c.key('', {downArrow: true});
  const beforeRefresh = screen();
  await screen().items.find(item => item.id === 'refresh')!.select();
  await until(() => screen() !== beforeRefresh);
  assert.equal(c.listItems(screen())[screen().index].id, ids[1]);
  await screen().items.find(item => item.id === ids[1])!.select();
  await until(() => screen()?.panel?.view_type === `process_session:${ids[1]}`);
  const outputDeadline = Date.now() + 5000;
  while (!screen().output?.includes('output 199')) {
    assert(Date.now() < outputDeadline, 'fixture process output was not observed');
    const beforePoll = screen();
    await screen().items.find(item => item.label === 'Refresh output')!.select();
    await until(() => screen() !== beforePoll);
  }
  assert.equal(screen().panel!.view_type, `process_session:${ids[1]}`);
  await until(() => (screen().contentOffset ?? 0) > 0);
  const end = screen().contentOffset!;
  await c.key('', {pageUp: true});
  await until(() => screen().contentOffset! < end);
  const offset = screen().contentOffset;
  const transcript = [...c.session.cells];
  const previous = screen();
  await c.key('', {return: true});
  await until(() => screen() !== previous);
  assert.equal(screen().contentOffset, offset);
  assert(screen().output!.includes('output 199'));
  assert.deepEqual(c.session.cells, transcript, 'poll output stays in the panel');
  await c.key('', {home: true});
  await until(() => app.lastFrame()?.includes(ids[1]));
  const stop = () => screen().items.find(item => item.label === 'Terminate process tree…')!.select();
  await stop();
  await until(() => c.active?.kind === 'confirm');
  assert(c.active!.request.message.includes(ids[1]));
  await c.key('n');
  await until(() => !c.active);
  assert.equal(screen().panel!.view_type, `process_session:${ids[1]}`);
  await stop();
  await until(() => c.active?.kind === 'confirm');
  const beforeStop = screen();
  await c.key('y');
  await until(() => !c.active && screen() !== beforeStop);
  const deadline = Date.now() + 5000;
  while (!screen().panel?.body?.includes('exited')) {
    assert(Date.now() < deadline, 'termination was not observed');
    const beforePoll = screen();
    await screen().items.find(item => item.label === 'Refresh output')!.select();
    await until(() => screen() !== beforePoll);
  }
  assert.deepEqual(screen().items.map(item => item.label), ['Refresh output']);
  assert(screen().output!.includes('output 199'));
  await c.key('', {escape: true});
  assert.equal(screen().panel!.view_type, 'process_sessions');
  assert(screen().items.some(item => item.id === ids[0]));
  await screen().items.find(item => item.id === 'ended')!.select();
  await screen().items.find(item => item.id === ids[1])!.select();
  await until(() => screen().output?.includes('output 199'));
  assert.equal(screen().panel!.view_type, `process_session:${ids[1]}`);
});

test('process output survives chunk boundaries and completion, which is announced once', () => {
  const c = new TuiController(new RuntimeClient(new RpcPeer(new PassThrough(), new PassThrough())));
  try {
    const event = (name: string, fields: any) => c.session.runtime({payload: decode(record(name, fields))});
    event('ProcessSessionChanged', processEvent());
    event('ToolCallFinished', {tool_call_id: 'shell', tool_name: 'shell', outcome: {status: 'succeeded', metadata: {process_snapshot: {session_id: 'proc_test', state: 'running'}}}});
    event('ProcessSessionChanged', processEvent({change: 'output', stdout: 'old\n'.repeat(2000) + 'Ready at http://local'}));
    event('ProcessSessionChanged', processEvent({change: 'output', stdout: 'host:3000\n', stderr: 'warning\n'}));
    const finished = processEvent({change: 'completed', state: 'exited', elapsed_seconds: 12, exit_code: 0, termination_reason: 'exit'});
    event('ProcessSessionChanged', finished);
    event('ProcessSessionChanged', finished);
    const process = c.session.processes.get('proc_test')!;
    assert(process.outputTail.stdout.includes('Ready at http://localhost:3000'));
    assert(process.outputTail.stdout.length < 2048);
    assert.equal(process.outputTail.stderr, 'warning\n');
    assert.equal(processElapsed(process, Date.now() + 60000), 12);
    assert.equal(c.session.cells.filter(cell => cell.title === 'Background process completed').length, 1);
    assert(!safe(sidebarRows(c, 36, 40).join('\n')).includes('PROCESSES'));
    c.session.clear();
    assert.equal(c.session.processes.size, 0);
  } finally {c.client.peer.close(); c.dispose();}
});

test('sidebar bounds process details and derives elapsed time without output events', async t => {
  const c = new TuiController(new RuntimeClient(new RpcPeer(new PassThrough(), new PassThrough())));
  t.after(() => {c.client.peer.close(); c.dispose();});
  c.session.connected = true;
  const now = Date.now();
  for (let i = 0; i < 5; i++) {
    const process = processEvent({process_session_id: `proc_${i}`, command: `server-${i}`, elapsed_seconds: 65, stdout: i === 0 ? 'Listening on :3000\n' : ''});
    c.session.processes.set(process.process_session_id, updateProcess(undefined, process, now));
  }
  const rows = sidebarRows(c, 36, 45, now);
  const text = safe(rows.join('\n'));
  for (const expected of ['PROCESSES', '5 active', '1m 5s', 'Listening on :3000', 'and 2 more']) assert(text.includes(expected), expected);
  assert(!text.includes('server-3'));
  assert(rows.every(row => stringWidth(row) <= 36));
  assert.equal(rows.length, 45);
  const short = safe(sidebarRows(c, 36, 12, now).join('\n'));
  assert(short.includes('server-0'));
  const app = render(<App controller={c}/>); t.after(() => app.cleanup());
  c.resize(45, 140);
  await until(() => app.lastFrame()?.includes('server-0'));
  const elapsedRow = () => safe(app.lastFrame()!).split('\n').find(row => row.includes('running ·'));
  const before = elapsedRow();
  const revision = c.snapshot();
  await until(() => elapsedRow() !== before, 'process elapsed time advances', 3000);
  assert.equal(c.snapshot(), revision, 'elapsed ticks do not mutate runtime state');
  c.resize(24, 80);
  await until(() => app.lastFrame()?.includes('/ps') && !app.lastFrame()?.includes('WORKBENCH'));
  assert(app.lastFrame()?.includes('5 processes'));
});
