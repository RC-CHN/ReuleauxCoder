import React from 'react';
import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdir, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {render} from 'ink-testing-library';
import {App} from '../src/ui/App.js';
import {SessionStore} from '../src/state/session.js';
import {TranscriptLayout} from '../src/ui/transcript.js';
import {decode, record} from '@reuleauxcoder/client';
import {safe} from '../src/ui/format.js';
import {backend, until} from './helpers.js';
import stringWidth from 'string-width';

test('long transcript lays out visible blocks, retains anchors and bounds row storage', () => {
  const session = new SessionStore();
  for (let i = 0; i < 10_000; i++) session.add('user', 'You', `Message ${i}\n` + '中文 content\n'.repeat(10));
  const layout = new TranscriptLayout();
  const draw = (offset: number | null, width = 80) => layout.render(session.cells, width, 20, offset, false, session.takeDirtyIndex());
  let page = draw(null);
  assert(safe(page.rows.join('\n')).includes('Message 9999'));
  assert(layout.measurements < 10, 'tail rendering must not wrap ten thousand messages');
  page = draw(0);
  const original = page.rows;
  for (let i = 0; i < 20; i++) {
    session.runtime({payload: decode(record('AssistantContentDelta', {text: 'appended output\n'}))});
    page = draw(page.start);
    assert.deepEqual(page.rows, original, 'streaming must not move the reader at the start');
  }
  const measured = layout.measurements;
  draw(page.start, 48);
  assert(layout.measurements - measured < 10, 'resize must not eagerly rewrap hidden history');
  for (let i = 0; i < 500; i++) draw(i * 150);
  assert(layout.retainedRows <= 6000);
  assert.equal(session.cells.length, 10_001, 'layout eviction never deletes original content');
});

test('a multi-megabyte streaming message only lays out its visible tail', () => {
  const session = new SessionStore();
  const text = '中文 large output\n'.repeat(150_000);
  session.runtime({payload: decode(record('AssistantContentDelta', {text}))});
  const layout = new TranscriptLayout();
  const draw = () => layout.render(session.cells, 80, 20, null, false, session.takeDirtyIndex());
  draw();
  assert(layout.measurements < 4);
  const before = layout.measurements;
  session.runtime({payload: decode(record('AssistantContentDelta', {text: 'unique latest token'}))});
  assert(safe(draw().rows.join('\n')).includes('unique latest token'));
  assert(layout.measurements - before < 4);
  assert.equal(session.cells[0].body, text + 'unique latest token');
  assert(layout.retainedRows <= 6000);
});

test('large tables retain headers and alignment across lazy blocks and window sizes', () => {
  const session = new SessionStore(), layout = new TranscriptLayout();
  const body = '| Name | Description |\n| :--- | ---: |\n' + Array.from({length: 10_000}, (_, index) => `| row-${index} | 中文 ${index} |\n`).join('');
  session.add('assistant', 'Reuleaux', body);
  const draw = (offset: number | null, width = 80) => layout.render(session.cells, width, 24, offset, true, session.takeDirtyIndex());
  let page = draw(null);
  assert(safe(page.rows.join('\n')).includes('row-9999'));
  assert(safe(page.rows.join('\n')).includes('│'));
  assert(layout.measurements < 4);
  const before = layout.measurements;
  draw(page.start);
  assert.equal(layout.measurements, before, 'continued table blocks reuse the layout cache');
  for (let i = 0; i < 30; i++) {
    page = draw(Math.max(0, page.start - 17));
    assert(page.rows.every(row => stringWidth(row) <= 80));
    assert(!safe(page.rows.join('\n')).includes('| row-'), 'continuations must remain table cells');
  }
  assert(safe(draw(page.start, 16).rows.join('\n')).includes('Description:'));
  assert.equal(session.cells[0].body, body);
  assert(layout.retainedRows <= 6000);
});

test('long cells and a table header at a block edge survive folding and streaming append', () => {
  const session = new SessionStore(), layout = new TranscriptLayout();
  const append = (text: string) => session.runtime({payload: decode(record('ReasoningDelta', {text, display_mode: 'inline'}))});
  const draw = (expanded = true) => layout.render(session.cells, 60, 24, null, expanded, session.takeDirtyIndex());
  append('prefix\n'.repeat(582) + '\n| Key | Value |\n'); draw();
  append('| --- | --- |\n| long | ' + 'word '.repeat(2000) + 'FINAL |\n');
  const full = draw();
  assert(safe(full.rows.join('\n')).includes('FINAL'));
  assert.deepEqual(full.rows, new TranscriptLayout().render(session.cells, 60, 24, null, true).rows);
  const compact = draw(false);
  assert(safe(compact.rows.join('\n')).includes('… +'));
  assert(!safe(compact.rows.join('\n')).includes('FINAL'));
  append('\nAfter table\n\n```text\n| fake | header |\n| --- | --- |\n```\n');
  assert.deepEqual(draw().rows, new TranscriptLayout().render(session.cells, 60, 24, null, true).rows);
});

for (const reasoning of [false, true]) test(`${reasoning ? 'reasoning' : 'assistant'} appends retain stable blocks and Markdown fence state`, () => {
  const session = new SessionStore(), layout = new TranscriptLayout();
  const append = (text: string) => session.runtime({payload: decode(record(reasoning ? 'ReasoningDelta' : 'AssistantContentDelta', {text, display_mode: 'inline'}))});
  const draw = () => layout.render(session.cells, 80, 24, null, true, session.takeDirtyIndex());
  const fresh = () => new TranscriptLayout().render(session.cells, 80, 24, null, true).rows;
  append('```ts\n' + 'const value = "中文";\n'.repeat(100_000)); draw();
  for (const chunk of ['x'.repeat(4090), '👩🏽‍💻', '\n```\n\n[**Docs**](https://example.com)\n', 'More 中文\n'.repeat(600)]) {
    const scanned = layout.scannedChars;
    append(chunk);
    assert.deepEqual(draw().rows, fresh());
    assert(layout.scannedChars - scanned <= chunk.length + 4096, 'only the old tail and appended text are scanned');
  }
  const beginning = layout.render(session.cells, 80, 24, 0, true, Infinity);
  append('last token');
  assert.deepEqual(layout.render(session.cells, 80, 24, beginning.start, true, session.takeDirtyIndex()).rows, beginning.rows);
  session.runtime({payload: decode(record('TurnFinished', {}))});
  assert.deepEqual(draw().rows, fresh(), 'completion can change Markdown rendering without stale rows');
  session.clear(); append('Replacement session');
  assert.deepEqual(draw().rows, fresh());
});

test('Ink history reads and searches the real ledger over RPC without filling the transcript', async t => {
  const b = await backend(); t.after(() => b.close());
  const c = b.controller;
  const directory = join(b.cwd, 'sessions', 'test-session');
  await mkdir(join(directory, 'artifacts'), {recursive: true});
  const events = Array.from({length: 1000}, (_, i) => ({schema_version: 1, seq: i + 1, event_id: `event-${i}`, kind: 'message_committed', created_at: i, session_generation: 0, turn_id: `turn-${i}`, role: 'user', artifact_refs: i === 42 ? ['output.txt'] : [], payload: {message: {role: 'user', content: i === 42 ? 'original needle 中文\n' + 'full detail\n'.repeat(2000) : `Message ${i}`}}}));
  await writeFile(join(directory, 'events.jsonl'), events.map(event => JSON.stringify(event)).join('\n') + '\n');
  await writeFile(join(directory, 'artifacts', 'output.txt'), 'archived 原文');
  const app = render(<App controller={c}/>); t.after(() => app.cleanup());
  const original = [...c.session.cells];
  c.showSession(); await c.key('h');
  const browser = c.screen?.kind === 'history' ? c.screen.browser : undefined;
  assert(browser);
  await until(() => !browser.loading);
  assert.equal(browser.current?.event_id, 'event-999');
  await c.key('n'); await until(() => !browser.loading);
  assert.equal(browser.current?.event_id, 'event-949');
  await c.key('/'); await c.key('needle'); await c.key('', {return: true});
  const search = c.screen?.kind === 'history' ? c.screen.browser : undefined;
  assert(search); await until(() => !search.loading);
  assert.equal(search.current?.event_id, 'event-42');
  await c.key('', {return: true});
  const detail = c.screen?.kind === 'history' ? c.screen.browser : undefined;
  assert(detail); await until(() => !detail.loading);
  assert.equal(detail.current?.content.length, 12000);
  await c.key('n'); await until(() => !detail.loading);
  assert.equal(detail.current?.offset, 12000);
  await c.key('a'); await c.key('', {return: true});
  await until(() => app.lastFrame()?.includes('archived 原文'));
  assert.deepEqual(c.session.cells, original);
  await b.client.submitAction('system.reset');
  await until(() => !b.client.state.running);
  await detail.load();
  assert(detail.error.includes('Session changed'));
});
