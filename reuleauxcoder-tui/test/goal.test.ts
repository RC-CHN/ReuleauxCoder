import assert from 'node:assert/strict';
import test from 'node:test';
import {record} from '@reuleauxcoder/client';
import {safe} from '../src/ui/format.js';
import {sidebarRows} from '../src/ui/sidebar.js';
import {backend, until} from './helpers.js';

test('goal panel creates through an interaction, tracks continuation and shows real usage', async t => {
  const b = await backend(); t.after(() => b.close());
  const c = b.controller;
  await c.openMenu(c.menus.find(menu => menu.name === '/goal')!);
  await until(() => c.screen?.kind === 'list' && c.screen.panel?.view_type === 'goal');
  if (c.screen?.kind !== 'list') throw new Error('Goal panel missing');
  const creating = c.screen.items.find(item => item.label === 'Create goal')!.select();
  await until(() => c.active?.kind === 'input_text');
  b.client.answer(c.active!.request.request_id, record('InputTextResponse', {value: 'Verify the complete migration', cancelled: false}));
  await creating;
  await until(() => b.client.state.goal?.status === 'complete' && !b.client.state.running);
  await until(() => c.screen?.kind === 'list' && c.screen.items.some(item => item.label === 'Complete'));
  assert.equal(b.client.state.goal!.tokens_used, 60);
  assert.equal(b.client.state.goal!.token_budget, null);
  assert.equal(c.session.cells.filter(cell => cell.kind === 'user').length, 0);
  const sidebar = safe(sidebarRows(c, 40, 35).join('\n'));
  assert.match(sidebar, /GOAL/);
  assert.match(sidebar, /Verify the complete migration/);
  assert.match(sidebar, /60 \/ No limit/);
});
