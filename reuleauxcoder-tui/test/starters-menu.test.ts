import assert from 'node:assert/strict';
import test from 'node:test';
import {backend} from './helpers.js';
import {editor} from '../src/state/editor.js';
import {STARTER_PROMPTS} from '../src/state/starters.js';


test('Alt+number fills only an empty starter draft without sending', async t => {
  const b = await backend(); t.after(() => b.close());
  const c = b.controller;
  assert(c.starterVisible);
  await c.key('2', {meta: true});
  assert.equal(c.composer.text, STARTER_PROMPTS[1]);
  assert(!c.session.cells.some(cell => cell.kind === 'user'));
  await c.key('1', {meta: true});
  assert.equal(c.composer.text, STARTER_PROMPTS[1], 'an existing draft is preserved');
});


test('command palette searches descriptions while direct slash names stay intact', async t => {
  const b = await backend(); t.after(() => b.close());
  const c = b.controller;
  c.composer = editor('/permissions');
  assert.deepEqual(c.palette.map(menu => menu.name), ['/approval']);
  assert(c.palette[0].description.includes('approval rules'));
  c.composer = editor('/choose models');
  assert.deepEqual(c.palette.map(menu => menu.name), ['/model']);
  c.composer = editor('/model');
  assert.deepEqual(c.palette.map(menu => menu.name), ['/model']);
  c.composer = editor('/mode');
  assert.equal(c.palette[0].name, '/mode');
  c.composer = editor('/mod');
  assert.deepEqual(new Set(c.palette.map(menu => menu.name)), new Set(['/model', '/mode']));
  c.composer = editor('/model use-main');
  assert(!c.palette[0].actions.some(action => action.action_id === 'model.switch'));
  await c.key('', {return: true});
  assert.equal(c.screen?.kind, 'form');
  if (c.screen?.kind === 'form') assert.equal(c.screen.action.action_id, 'model.use_main');
});
