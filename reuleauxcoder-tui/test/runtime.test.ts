import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {backend, until} from './helpers.js';
import {decode, typeOf} from '../src/protocol/wire.js';
import {editor} from '../src/state/editor.js';
import {panelRows} from '../src/ui/panels.js';
import {TranscriptLayout} from '../src/ui/viewport.js';

test('unchanged RPC refreshes preserve state identity and do not notify the UI', async t => {
  const b = await backend(); t.after(() => b.close());
  const {client} = b;
  const original = client.state;
  let updates = 0;
  client.on('state', () => updates++);
  for (let i = 0; i < 3; i++) await client.refresh();
  assert.equal(client.state, original);
  assert.equal(updates, 0);
  await client.submit('wait');
  assert(client.state.running);
  assert(updates > 0, 'a real state change is still delivered');
  await client.interrupt();
  await until(() => !client.state.running);
});

test('shell picker shows executable paths and dispatches selection through the real backend', async t => {
  const b = await backend(); t.after(() => b.close());
  const {client, controller: c} = b;
  await client.submitAction('shell.list');
  await until(() => c.screen?.kind === 'list' && c.screen.panel?.view_type === 'shells');
  assert(c.screen?.kind === 'list');
  const item = c.screen.panel!.items.find(item => item.action?.action_id === 'shell.select' && item.action.command.selector !== 'auto');
  assert(item, 'fixture host has an executable shell');
  assert(item.description.includes('/') || item.description.includes('\\'));
  await client.submitAction(item.action!.action_id, item.action!.command);
  await client.submitAction('shell.list');
  await until(() => c.screen?.kind === 'list' && c.screen.panel!.items.some(row => row.current && row.label === item.label));
  await client.submitAction('shell.select', {selector: 'auto'});
  await client.submitAction('shell.list');
  await until(() => c.screen?.kind === 'list' && c.screen.panel!.items[0].current);
});

test('Python catalog drives every command menu and typed parameter form', async t => {
  const b = await backend(); t.after(() => b.close());
  const {client, controller: c} = b;
  const fixture = decode(await b.peer.request('test.fixture'));
  assert.equal(new Set(c.menus.flatMap(menu => menu.actions.map(action => action.action_id))).size, client.catalog.length);
  assert(c.menus.every(menu => /^\/\w+$/.test(menu.name)));
  for (const action of client.catalog) assert.deepEqual(action.parameters.map(item => item.name), fixture.parameters[action.action_id]);
  assert.equal(typeOf(fixture.outcome), 'ToolOutcome');
  assert.equal(fixture.outcome.metadata.$type, 'user-data');
  assert.equal(typeOf(fixture.outcome.metadata), undefined);
  assert.equal(fixture.outcome.archive_reference.path, 'archive/full.txt');
  const failures: string[] = []; client.on('operationFailure', message => failures.push(message));
  await client.submitAction('model.show');
  await until(() => c.screen?.kind === 'list' && Boolean(c.screen.panel));
  assert.equal(c.session.cells.length, 0, 'a focused command view opens a panel without leaving a transcript copy');
  await c.key('', {escape: true});
  for (const menu of c.menus.filter(menu => menu.actions.some(action => action.preview))) {
    await c.openMenu(menu);
    await until(() => !client.state.running, menu.name);
    await until(() => c.screen?.kind === 'document' || c.screen?.kind === 'list' && Boolean(c.screen.panel) || menu.name === '/thinking' && c.status.includes('No reasoning'), `view for ${menu.name}`);
  }
  assert.deepEqual(failures, []);
  await client.submitAction('thinking.set_effort', {level: 'high'});
  await until(() => !client.state.running);
  await client.submitAction('thinking.show_effort');
  await until(() => c.session.cells.some(cell => cell.body.includes('high')));
});

test('streaming, all reverse interactions, full tool facts and drafts survive a real Python turn', async t => {
  const b = await backend(); t.after(() => b.close());
  const {client, controller: c} = b;
  await client.submit('exercise');
  await until(() => c.active?.kind === 'review');
  c.composer = editor('draft retained 中文');
  assert(panelRows(c, 80, 40)!.rows.join('\n').includes('+新内容'));
  await client.refresh(); assert(client.state.running, 'human input must not block RPC');
  await c.key('s'); await c.key('', {return: true});
  await until(() => c.active?.kind === 'confirm'); await c.key('y');
  await until(() => c.active?.kind === 'choose_one'); await c.key('', {downArrow: true}); await c.key('', {return: true});
  await until(() => c.active?.kind === 'input_text');
  c.paste('secret-never-render');
  assert(!panelRows(c, 80, 10)!.rows.join('\n').includes('secret-never-render'));
  await c.key('', {return: true});
  await until(() => !client.state.running);
  assert.equal(c.composer.text, 'draft retained 中文');
  assert.equal(c.history.entries.length, 0);
  const text = c.session.cells.map(cell => cell.body + '\n' + cell.details).join('\n');
  for (const value of ['allow_session; file', 'Confirmed: True', 'Choice: b', 'Secret received: True', 'reasoning retained', 'diagnostic retained', 'archive/full.txt', 'stdout retained', 'stderr retained', '中文 👩🏽‍💻']) assert(text.includes(value), value);
  assert(!text.includes('secret-never-render'));
  assert.equal(c.session.cells.filter(cell => cell.kind === 'assistant').length, 1);
  const tool = c.session.cells.find(cell => cell.kind === 'tool')!;
  assert(tool.body.includes('Reviewed diff applied'));
  assert.equal(c.session.jobs.size, 1); assert.equal(c.session.processes.size, 1);
  assert.equal(c.session.plan.items.length, 1);
  const layout = new TranscriptLayout();
  const expanded = layout.render(c.session.cells, 50, 1000, 0, true).rows.join('\n');
  assert(expanded.includes('archive/full.txt')); assert(expanded.includes('reasoning retained'));
});

test('queued command, interrupt, failure, reset and saving preserve backend ownership', async t => {
  const b = await backend(); t.after(() => b.close());
  const {client, controller: c} = b;
  c.composer = editor('wait'); await c.key('', {return: true});
  assert.equal(c.composer.text, '');
  await client.submitAction('system.reset');
  assert(client.state.queued_commands.length);
  await client.interrupt();
  await until(() => !client.state.running);
  assert(!c.session.cells.some(cell => ['user', 'assistant', 'tool'].includes(cell.kind)));
  c.composer = editor('fail'); await c.key('', {return: true});
  await until(() => !client.state.running);
  assert(c.session.cells.some(cell => cell.body.includes('deliberate failure retained')));
  const saved = await client.shutdown(); assert(saved);
  const persisted = await readFile(`${b.cwd}/sessions/${saved}/replay.json`, 'utf8');
  assert(persisted.includes('fail'));
  const history = await readFile(`${b.cwd}/history.jsonl`, 'utf8');
  assert.equal(history, '"wait"\n"fail"\n');
});

test('session resume restores transcript after generation changes, and forms submit typed values', async t => {
  const b = await backend(); t.after(() => b.close());
  const {client, controller: c} = b;
  await client.submit('remember me'); await until(() => !client.state.running);
  await client.submitAction('sessions.save'); await until(() => !client.state.running);
  const saved = client.state.session_id!;
  await client.submitAction('sessions.new'); await until(() => !client.state.running);
  assert.equal(c.session.cells.length, 0);
  await client.submitAction('sessions.resume', {target: saved}); await until(() => !client.state.running);
  assert(c.session.cells.some(cell => cell.kind === 'user' && cell.body.includes('remember me')));
  await c.openMenu(c.menus.find(menu => menu.name === '/thinking')!);
  const screen = c.screen!; assert.equal(screen.kind, 'list');
  if (screen.kind !== 'list') throw new Error('Expected actions');
  const item = screen.items.find(item => /set.*effort/i.test(item.label)); assert(item);
  await item.select(); assert.equal(c.screen?.kind, 'form');
  c.paste('high'); await c.key('', {return: true}); await until(() => !client.state.running);
  await client.submitAction('thinking.show_effort'); await until(() => !client.state.running);
  assert(c.session.cells.some(cell => cell.body.includes('high')));
});
