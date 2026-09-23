import React from 'react';
import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import {setTimeout as delay} from 'node:timers/promises';
import {render} from 'ink-testing-library';
import CursorContext from '../node_modules/ink/build/components/CursorContext.js';
import {RpcPeer} from '@reuleauxcoder/client/node';
import {RuntimeClient} from '@reuleauxcoder/client';
import {TuiController} from '../src/state/controller.js';
import {editor, edit} from '../src/state/editor.js';
import {App} from '../src/ui/App.js';
import {inputLayout, inputRows, TranscriptLayout, type CursorPosition} from '../src/ui/viewport.js';
import {markdown, safe} from '../src/ui/format.js';
import {panelRows} from '../src/ui/panels.js';
import {TextLayout} from '../src/ui/text-layout.js';
import {sidebarRows} from '../src/ui/sidebar.js';
import {paint} from '../src/ui/theme.js';
import stringWidth from 'string-width';
import {until, backend} from './helpers.js';

test('real Ink input handles F keys, Unicode paste, menus, resize and secret masking', async t => {
  const b = await backend(); t.after(() => b.close());
  const c = b.controller;
  let cursor: CursorPosition | undefined;
  const app = render(<CursorContext.Provider value={{setCursorPosition: value => {cursor = value;}}}><App controller={c}/></CursorContext.Provider>); t.after(() => app.cleanup());
  await until(() => app.lastFrame()?.includes('Ready'));
  await until(() => cursor?.x === 5);
  app.stdin.write('\x1bOQ');
  await until(() => app.lastFrame()?.includes('Session details'));
  assert.equal(cursor, undefined, 'documents do not claim the IME cursor');
  app.stdin.write('\x1b'); await until(() => !c.screen);
  const transcript = [...c.session.cells];
  app.stdin.write('/model');
  await until(() => app.lastFrame()?.includes('Commands'));
  app.stdin.write('\r'); await until(() => c.screen?.kind === 'list' && Boolean(c.screen.panel));
  assert(app.lastFrame()?.includes('Model'));
  assert.deepEqual(c.session.cells, transcript, 'opening a panel must not duplicate it in the transcript');
  const screen = c.screen!;
  assert.equal(screen.kind, 'list');
  if (screen.kind === 'list') await screen.items.find(item => item.label === 'View all details')!.select();
  await until(() => c.screen?.kind === 'document');
  app.stdin.write('\x1b'); await until(() => c.screen?.kind === 'list');
  app.stdin.write('\x1b'); await until(() => !c.screen);
  assert.deepEqual(c.session.cells, transcript, 'closed panels and detail pages leave no history');
  app.stdin.write('\x1b[200~中文\n👩🏽‍💻\x1b[201~');
  await until(() => c.composer.text === '中文\n👩🏽‍💻');
  await until(() => app.lastFrame()?.includes('中文'));
  await until(() => cursor?.x === 7);
  assert.equal(cursor!.y, safe(app.lastFrame()!).split('\n').findIndex(row => row.includes('👩🏽‍💻')));
  c.resize(12, 40);
  await until(() => (app.lastFrame()?.split('\n').length ?? 99) <= 11);
  assert(app.lastFrame()?.includes('中文'));
  c.resize(28, 90);
  await b.client.submit('exercise'); await until(() => c.active?.kind === 'review');
  const review = panelRows(c, 38, 8)!;
  assert(review.hint.includes(paint.action('y/Enter Approve once')));
  assert(review.hint.includes(paint.error('n Reject')));
  assert(review.navigation?.includes('PgUp/PgDn'));
  assert(!safe(review.hint.join(' ')).includes('PgUp'));
  c.resize(18, 40);
  await until(() => safe(app.lastFrame() || '').includes('f feedback'));
  assert(safe(app.lastFrame() || '').includes('Approve once'));
  assert(safe(app.lastFrame() || '').includes('n Reject'));
  assert(safe(app.lastFrame() || '').includes('s scope'));
  c.resize(28, 90);
  app.stdin.write('n'); await until(() => c.active?.kind === 'confirm');
  app.stdin.write('n'); await until(() => c.active?.kind === 'choose_one');
  app.stdin.write('1'); await until(() => c.active?.kind === 'input_text');
  app.stdin.write('\x1b[200~top-secret\x1b[201~');
  await until(() => app.lastFrame()?.includes('••••'));
  assert.equal(cursor?.x, 13, 'masked input anchors after bullets, inside the panel rail');
  assert.equal(cursor!.y, safe(app.lastFrame()!).split('\n').findIndex(row => row.includes('••••')));
  assert(app.frames.every(frame => !frame.includes('top-secret')));
  assert.equal(c.composer.text, '中文\n👩🏽‍💻');
  app.stdin.write('\x1b'); await until(() => !b.client.state.running);
  app.stdin.write('\x1bOS'); await until(() => c.expanded);
  app.stdin.write('\x1b[14~'); await until(() => !c.expanded);
  app.stdin.write('\x1bOP'); await until(() => c.screen?.title === 'Keyboard help');
});

test('input cursor follows rendered wrapping, grapheme width and the visible input window', () => {
  for (const [value, width, height, expected] of [
    [editor('中文👩🏽‍💻'), 12, 4, {x: 6, y: 0}],
    [{text: 'abcd中z', cursor: 4}, 5, 4, {x: 0, y: 1}],
    [{text: 'aa hello', cursor: 4}, 7, 4, {x: 1, y: 1}],
    [editor('abcd'), 4, 4, {x: 0, y: 1}],
    [{text: 'a\nb', cursor: 1}, 8, 4, {x: 1, y: 0}],
    [editor('a\nb\nc\nd\ne'), 8, 2, {x: 1, y: 1}],
  ] as const) assert.deepEqual(inputLayout(value, width, height).cursor, expected, JSON.stringify(value));
  assert.deepEqual(inputLayout(editor('中文👩🏽‍💻'), 12, 2, true, true).cursor, {x: 3, y: 0});
  assert.equal(inputLayout(editor('draft'), 12, 2, false).cursor, undefined);
});

test('queued prompts and commands stay visible until the backend clears them', async t => {
  const b = await backend(); t.after(() => b.close());
  const c = b.controller;
  const app = render(<App controller={c}/>); t.after(() => app.cleanup());
  await b.client.submit('wait');
  await b.client.submit('Check the queue\nKeep all of this text');
  await b.client.submitAction('system.reset');
  await until(() => app.lastFrame()?.includes('QUEUED 2'));
  assert(app.lastFrame()?.includes('Check the queue'));
  assert(app.lastFrame()?.includes('Command 1'));
  assert(app.lastFrame()?.includes('system.reset'));
  for (let index = 0; index < 4; index++) await b.client.submit(`Additional prompt ${index}`);
  await until(() => app.lastFrame()?.includes('+2 more'));
  assert(app.lastFrame()?.includes('Command 1'), 'neither queue hides the other');
  c.showSession();
  assert(c.screen?.kind === 'document');
  if (c.screen?.kind === 'document') {
    assert(c.screen.body.includes('Keep all of this text'));
    assert(c.screen.body.includes('Additional prompt 3'));
  }
  await c.key('', {escape: true});
  c.composer = editor('draft preserved'); c.resize(12, 40);
  await until(() => (app.lastFrame()?.split('\n').length ?? 99) <= 11);
  assert(app.lastFrame()?.includes('draft preserved'));
  assert(app.lastFrame()?.includes('QUEUED'));
  await b.client.interrupt(); await b.client.interrupt();
  await until(() => !b.client.state.running);
  await until(() => !app.lastFrame()?.includes('QUEUED'));
  assert.deepEqual(c.session.state.queued_commands, []);
  assert.deepEqual(c.session.state.queued_steering, []);
  assert.equal(c.composer.text, 'draft preserved');
});

test('stopped work returns to Ready and Ctrl+C resumes idle exit confirmation', async t => {
  const b = await backend(); t.after(() => b.close());
  const c = b.controller;
  const app = render(<App controller={c}/>); t.after(() => app.cleanup());
  await b.client.submit('wait');
  await until(() => c.session.state.running);
  await c.key('c', {ctrl: true});
  await until(() => !c.session.state.running);
  await until(() => app.lastFrame()?.includes('Ready') && !app.lastFrame()?.includes('Stopping'));
  assert(!c.session.state.stopping);
  await c.key('c', {ctrl: true});
  assert(c.exitConfirm, 'Ctrl+C after stopping asks to exit instead of immediately shutting down');
  assert(!c.closing);
});

test('scrolling to the bottom restores shortcuts and follows subsequent output', async t => {
  const c = new TuiController(new RuntimeClient(new RpcPeer(new PassThrough(), new PassThrough())));
  t.after(() => {c.client.peer.close(); c.dispose();});
  c.session.connected = true;
  c.session.add('assistant', 'Reuleaux', Array.from({length: 60}, (_, index) => `line ${index}`).join('\n'));
  const app = render(<App controller={c}/>); t.after(() => app.cleanup());
  await until(() => app.lastFrame()?.includes('line 59'));
  for (const page of [true, false]) {
    c.wheel(-3);
    await until(() => app.lastFrame()?.includes('History '));
    if (page) await c.key('', {pageDown: true}); else c.wheel(3);
    await until(() => c.offset === null && !app.lastFrame()?.includes('History '));
    assert(app.lastFrame()?.includes('F4'));
  }
  c.session.add('assistant', 'Reuleaux', 'Latest response');
  c.changed();
  await until(() => app.lastFrame()?.includes('Latest response'));
  c.wheel(-3);
  await until(() => app.lastFrame()?.includes('History '));
  c.resize(100, 80);
  await delay(40);
  assert.notEqual(c.offset, null, 'resize does not silently resume following');
  await c.key('', {ctrl: true, end: true});
  await until(() => c.offset === null && !app.lastFrame()?.includes('History '));
});

test('panel scrolling reuses wrapping while changed text and width invalidate it', t => {
  const c = new TuiController(new RuntimeClient(new RpcPeer(new PassThrough(), new PassThrough())));
  t.after(() => {c.client.peer.close(); c.dispose();});
  c.document('Details', Array.from({length: 100}, (_, index) => `Field ${index}: 中文 detail`).join('\n'));
  const document = c.screen;
  assert(document?.kind === 'document');
  const layout = new TextLayout();
  for (let offset = 0; offset < 30; offset++) {
    document.offset = offset;
    assert(safe(panelRows(c, 80, 10, layout)!.rows[0]).includes(`Field ${offset}:`));
  }
  assert.equal(layout.measurements, 1);
  document.body = 'Changed: ' + '中文 '.repeat(40);
  assert(safe(panelRows(c, 80, 10, layout)!.rows[0]).includes('Changed:'));
  assert.equal(layout.measurements, 2);
  const narrower = panelRows(c, 30, 10, layout)!;
  assert.equal(layout.measurements, 3);
  assert(narrower.rows.every(row => stringWidth(row) <= 30));
});

test('scroll input applies its full distance immediately and leaves no queued movement', async t => {
  const c = new TuiController(new RuntimeClient(new RpcPeer(new PassThrough(), new PassThrough())));
  t.after(() => {c.client.peer.close(); c.dispose();});
  c.resize(24, 100); // Match Ink's test terminal before measuring row coordinates.
  c.session.connected = true;
  c.session.add('assistant', 'Reuleaux', Array.from({length: 100}, (_, index) => `line ${index}`).join('\n'));
  const app = render(<App controller={c}/>); t.after(() => app.cleanup());
  await until(() => app.lastFrame()?.includes('line 99'));
  const bottom = c.totalRows - c.viewportRows, page = c.viewportRows - 1;
  const positions: number[] = [];
  const unsubscribe = c.subscribe(() => {if (c.offset !== null) positions.push(c.offset);});
  t.after(unsubscribe);
  await c.key('', {pageUp: true});
  assert.equal(positions[0], bottom - page, 'the whole page moves with the key');
  await c.key('', {pageUp: true});
  await until(() => c.offset === bottom - page * 2);
  assert.equal(c.offset, bottom - page * 2);

  await c.key('', {pageDown: true});
  await until(() => c.offset !== null && c.offset > bottom - page * 2);
  const reversed = c.offset!;
  positions.length = 0;
  await c.key('', {pageUp: true});
  await until(() => c.offset === reversed - page);
  assert(positions.every(row => row <= reversed), 'reversal drops outstanding travel in the old direction');
  const stationary = c.offset;
  await delay(150);
  assert.equal(c.offset, stationary, 'no easing or residual animation after input stops');
  await c.key('', {pageUp: true});
  await c.key('', {end: true, ctrl: true});
  await until(() => app.lastFrame()?.includes('line 99'));
  await delay(180);
  assert.equal(c.offset, null, 'an old scroll cannot undo End');

  c.document('Scrollable details', Array.from({length: 80}, (_, index) => `Detail ${index}`).join('\n'));
  await until(() => app.lastFrame()?.includes('Detail 0'));
  const document = c.screen;
  assert(document?.kind === 'document');
  await c.key('', {pageDown: true});
  await until(() => document.offset > 0);
  await c.key('', {escape: true});
  const stopped = document.offset;
  await delay(180);
  assert.equal(document.offset, stopped, 'dismissed panels stop scrolling');
  assert.equal(c.screen, undefined);
});

test('workbench sidebar adapts to terminal size and preserves drafts and full session details', async t => {
  const c = new TuiController(new RuntimeClient(new RpcPeer(new PassThrough(), new PassThrough())));
  t.after(() => {c.client.peer.close(); c.dispose();});
  c.session.connected = true;
  c.session.state = {...c.session.state, model: 'gpt-sol', context_limit: 100000, context_tokens: 24000};
  c.session.plan = {items: Array.from({length: 10}, (_, index) => ({step: `Task ${index}`, status: index < 6 ? 'completed' : index === 6 ? 'in_progress' : 'pending'}))};
  c.session.jobs.set('test', {task: 'Test worker', status: 'running', activity: 'Running focused tests'});
  c.composer = editor('中文草稿\nKeep this draft');
  const app = render(<App controller={c}/>); t.after(() => app.cleanup());
  await until(() => app.lastFrame()?.includes('Ready'));
  c.resize(32, 140);
  await until(() => app.lastFrame()?.includes('WORKBENCH'));
  assert(app.lastFrame()?.includes('Task 6'), 'the current plan step remains visible');
  assert(app.lastFrame()?.includes('Test worker'));
  assert(app.lastFrame()?.includes('24%'));
  assert(app.lastFrame()?.includes('┌'));
  for (const [rows, columns] of [[45, 140], [34, 80], [33, 80], [24, 119], [24, 120], [20, 120], [12, 40], [32, 160]]) {
    const previous = app.lastFrame();
    c.resize(rows, columns);
    await until(() => {
      const frame = app.lastFrame();
      const input = safe(frame || '').split('\n').find(row => row.includes('┌─ YOU'));
      return frame !== previous && frame?.split('\n').length === rows - 1 && input && stringWidth(input.trim()) === columns - 2;
    }, 'the rendered viewport reaches both the requested height and width');
    const frame = app.lastFrame()!;
    assert.equal(frame.includes('WORKBENCH'), columns >= 120 && rows >= 20);
    assert(frame.split('\n').length <= rows - 1);
    assert(frame.split('\n').every(row => stringWidth(row) <= columns));
    assert(frame.includes('中文草稿'));
    assert(frame.includes('Keep this draft'));
    const inputTop = safe(frame).split('\n').findIndex(row => row.includes('┌─ YOU'));
    assert(inputTop >= 0);
    assert.equal(stringWidth(safe(frame).split('\n')[inputTop].trim()), columns - 2, 'the input spans both columns');
  }
  c.showSession();
  assert(c.screen?.kind === 'document');
  if (c.screen?.kind === 'document') {
    assert(c.screen.body.includes('Task 0'));
    assert(c.screen.body.includes('Task 9'));
    assert(c.screen.body.includes('Running focused tests'));
  }
  assert.equal(c.composer.text, '中文草稿\nKeep this draft');
});

test('message numbering survives folding and code frames preserve wrapped Unicode content', () => {
  const c = new TuiController(new RuntimeClient(new RpcPeer(new PassThrough(), new PassThrough())));
  c.session.add('user', 'You', 'Review this');
  c.session.add('reasoning', 'Thinking', 'Retained reasoning');
  c.session.add('assistant', 'Reuleaux', 'Done');
  const layout = new TranscriptLayout();
  for (const expanded of [false, true, false]) {
    const text = safe(layout.render(c.session.cells, 40, 100, 0, expanded).rows.join('\n'));
    assert(text.includes('01 / YOU'));
    assert(text.includes('02 / REULEAUX'));
  }
  c.session.cells.shift();
  assert(safe(layout.render(c.session.cells, 40, 100, 0, false).rows.join('\n')).includes('01 / REULEAUX'), 'cached headings track the visible conversation');
  const code = 'const 内容 =\n"' + '中文👩🏽‍💻'.repeat(12) + '";';
  for (const width of [18, 38, 100]) {
    const rows = safe(markdown('```ts\n' + code + '\n```', width)).split('\n');
    assert(rows.every(row => stringWidth(row) === width));
    assert.equal(rows.slice(1, -1).map(row => row.slice(2, -2).trimEnd()).join(''), code.replaceAll('\n', ''));
  }
  c.client.peer.close(); c.dispose();
});

test('sidebar prioritizes attention and Git summaries before optional file and plan details', () => {
  const c = new TuiController(new RuntimeClient(new RpcPeer(new PassThrough(), new PassThrough())));
  c.session.connected = true;
  c.session.state = {...c.session.state, running: true, context_tokens: 24000, context_limit: 100000, model: 'gpt-sol', mode: 'code', approval_policy: 'require_approval'};
  c.session.plan = {items: [{step: 'Current task', status: 'in_progress'}, {step: 'Later task', status: 'pending'}]};
  c.session.git = {available: true, branch: 'main', head: 'abc123', upstream: 'origin/main', ahead: 2, behind: 0, additions: 12, deletions: 4, truncated: false, reason: null,
    files: [{path: 'conflict.txt', index: 'U', worktree: 'U', conflict: true}, {path: 'secret\x1b[2J.txt', index: '?', worktree: '?', conflict: false}, ...Array.from({length: 7}, (_, index) => ({path: `extra-${index}.txt`, index: '?', worktree: '?', conflict: false}))]};
  const small = sidebarRows(c, 34, 16);
  const text = safe(small.join('\n'));
  assert.equal(small.length, 16);
  assert(text.indexOf('ATTENTION') < text.indexOf('GIT'));
  assert(text.includes('1 Git conflicts'));
  assert(text.includes('Current task'));
  assert(text.includes('9 changed'));
  assert(text.includes('↑ 2 ahead'));
  assert(text.includes('24%'));
  assert(!text.includes('Later task'));
  const tall = sidebarRows(c, 34, 40);
  assert.equal(tall.length, 40);
  assert(tall.every(row => stringWidth(row) <= 34));
  assert(safe(tall.join('\n')).includes('conflict.txt'));
  assert(safe(tall.join('\n')).includes('extra-1.txt'));
  assert(!safe(tall.join('\n')).includes('extra-2.txt'));
  assert(safe(tall.join('\n')).includes('and 5 more'));
  assert(safe(tall.join('\n')).includes('Later task'));
  assert(safe(tall.join('\n')).includes('Approval default Ask'));
  assert(!tall.join('\n').includes('\x1b[2J'));
  c.showSession();
  assert(c.screen?.kind === 'document' && c.screen.body.includes('conflict.txt'));
  assert(c.screen?.kind === 'document' && c.screen.body.includes('extra-6.txt'));
  c.client.peer.close(); c.dispose();
});

test('Unicode editing and viewport folding retain complete output without terminal escapes', () => {
  let value = editor('中文👩🏽‍💻é');
  value = edit(value, '', {backspace: true});
  assert.equal(value.text, '中文👩🏽‍💻');
  value = edit(value, '', {leftArrow: true});
  value = edit(value, '', {delete: true});
  assert.equal(value.text, '中文');
  assert(!inputRows(editor('secret'), 8, 2, true, true).join('').includes('secret'));
  const c = new TuiController(new RuntimeClient(new RpcPeer(new PassThrough(), new PassThrough())));
  const output = Array.from({length: 100}, (_, index) => `line ${index}`).join('\n');
  c.session.add('tool', 'test', output);
  c.session.add('assistant', 'Reuleaux', '**Done**\n\x1b]52;c;clipboard\x07\x1b[2Jtext');
  const layout = new TranscriptLayout();
  const folded = layout.render(c.session.cells, 40, 1000, 0, false);
  assert(!folded.rows.join('\n').includes('line 50'));
  const expanded = layout.render(c.session.cells, 40, 1000, 0, true);
  assert(expanded.rows.join('\n').includes('line 50'));
  assert(!expanded.rows.join('\n').includes('clipboard'));
  assert(!expanded.rows.join('\n').includes('\x1b[2J'));
  assert(safe(expanded.rows.join('\n')).includes('Done'));
  const page = layout.render(c.session.cells, 40, 5, 50, true);
  assert.equal(page.rows.length, 5);
  c.client.peer.close(); c.dispose();
});
